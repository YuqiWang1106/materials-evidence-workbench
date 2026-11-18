"""One logical call has at most three bounded, independently recorded dispatches."""

import json
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from ..core.files import atomic_bytes
from .errors import NOT_PROCESSED, BudgetExceeded, ModelCallError
from .identity import Json, result_hash
from .logical_request import RequestKind, request_metadata
from .request import Request
from .status import _NOT_REACHED, transport_status
from .transport import ModelTransport

# A server error does not prove the provider did not execute the request.
_UNCERTAIN_HTTP = {500, 502, 503, 504}
_RATE_LIMIT_CODES = {"rate_limit_exceeded", "rate_limit_error"}
RETRY_BACKOFF_SECONDS = 2.0  # per attempt number, before a proven-unsent retry


@dataclass
class Dispatch:
    client: ModelTransport
    req: Request
    call_id: str
    limit: int
    maybe_sent: bool = False
    reached: bool = False
    failure: ModelCallError | None = None


@dataclass
class Attempt:
    number: int
    attempt_id: str
    started: float
    timeout: float
    reserved: int
    retry_wait: float
    status: str = "transport_unknown"
    retry: bool = False
    details: Json = field(default_factory=dict)
    usage: Json | None = None


def _require_time(client: ModelTransport, timeout: float) -> None:
    if client.require_full_timeout and client.budget.active_time and timeout < client.timeout:
        message = NOT_PROCESSED + ": remaining time allowance is below one full request"
        raise BudgetExceeded(message)


def _reserve(call: Dispatch, number: int) -> Attempt:
    client = call.client
    client._check_cancelled()
    attempt_id = call.call_id if number == 1 else f"{call.call_id}.r{number}"
    started = time.monotonic()
    timeout = min(client.timeout, client.budget.remaining_seconds()) if client.budget.active_time else client.timeout
    if number > 1:
        client.budget.register_attempt(call.req.request_hash, attempt_id)
    try:
        _require_time(client, timeout)
        reserved = client.budget.reserve(
            client.output_reservation_tokens,
            call.req.input_bound,
            reservation_id=attempt_id,
            seconds=timeout if client.budget.active_time else None,
        )
    except Exception as exc:
        if number == 1:
            client.budget.update_request(call.req.request_hash, state="failed", dispatched=False)
            raise
        if call.failure is not None:
            raise call.failure from exc
        raise
    return Attempt(number, attempt_id, started, timeout, reserved, min(client.retry_delay * number, 5))


def _base(call: Dispatch, a: Attempt) -> Json:
    client, req = call.client, call.req
    return {
        **request_metadata(req.options.logical_binding, RequestKind.GENERATION),
        "attempt_id": a.attempt_id,
        "attempt": a.number,
        "phase": req.options.phase,
        "possible_duplicate_charge": a.number > 1 and call.maybe_sent,
        "model": client.model,
        "request_hash": req.request_hash,
        "context": req.options.context or {},
        "image_count": req.image_count,
        "file_count": req.file_count,
        "requested_max_output_tokens": client.max_output_tokens,
        "output_limit_policy": "provider_default" if client.max_output_tokens is None else "explicit",
        "reserved_output_tokens": client.output_reservation_tokens,
        "reserved_total_tokens": a.reserved,
        "input_reservation_source": req.input_source,
        "reserved_seconds": a.timeout,
        "requested_service_tier": req.payload.get("service_tier"),
        "requested_model": client.model,
        "started_at_epoch": time.time(),
        **({"stage_input_id": req.stage_input_id} if req.stage_input_id else {}),
    }


def _error_identity(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        error = response.json().get("error", {})
        code, kind = error.get("code"), error.get("type")
    except (ValueError, AttributeError):
        return None, None

    def safe(value: object) -> str | None:
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) else None

    return safe(code), safe(kind)


def _retry_after(a: Attempt, value: str) -> None:
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            a.retry = False
            return
    a.retry_wait = max(a.retry_wait, seconds)
    if a.retry_wait > 60:
        a.retry = False


def _raise_http(response: httpx.Response, a: Attempt) -> None:
    a.status = f"http_{response.status_code}"
    code, kind = _error_identity(response)
    a.details.update(error_code=code, error_type=kind)
    if response.status_code == 429:
        a.retry = code in _RATE_LIMIT_CODES
    if a.retry and response.headers.get("retry-after"):
        _retry_after(a, response.headers["retry-after"])
    message = a.status + (":" + code if code else "")
    raise ModelCallError(message)


def _validate(call: Dispatch, a: Attempt, payload: Json) -> None:
    client = call.client
    returned = payload.get("model")
    a.details.update(response_id=payload.get("id"), returned_model=returned)
    a.usage = payload.get("usage")
    if (
        returned is not None
        and returned != client.model
        and not (client.model == "gpt-5.4-mini" and returned == "gpt-5.4-mini-2026-03-17")
    ):
        a.status = "returned_model_mismatch"
        message = "returned model differs from requested model"
        raise ModelCallError(message)
    tier = payload.get("service_tier")
    a.details["returned_service_tier"] = (
        tier
        if isinstance(tier, str) and tier
        else "invalid"
        if tier is not None and not isinstance(tier, str)
        else "not_reported"
    )
    if client.budget.active_time and time.monotonic() - a.started >= a.timeout:
        a.status = "time_budget_exhausted"
        message = "model time budget exhausted; late response rejected"
        raise BudgetExceeded(message)
    if payload.get("status") != "completed":
        a.status = str(payload.get("status") or "unknown_status")
        reason = None
        if a.status == "incomplete":
            client._record_incomplete_artifact(payload, a.attempt_id)
            details = payload.get("incomplete_details")
            raw = details.get("reason") if isinstance(details, dict) else None
            reason = raw if type(raw) is str and raw in {"max_output_tokens", "content_filter"} else None
            a.details["incomplete_reason"] = reason
        raise ModelCallError(a.status, reason=reason)


def _output(a: Attempt, payload: Json) -> Json:
    pieces = [
        c["text"]
        for item in payload.get("output", [])
        if item.get("type") == "message"
        for c in item.get("content", [])
        if c.get("type") == "output_text"
    ]
    if not pieces:
        a.status = "refusal_or_empty"
        raise ModelCallError(a.status)
    result = json.loads("".join(pieces))
    if not isinstance(result, dict):
        a.status = "invalid_json_object"
        raise ModelCallError(a.status)
    return result


def _perform(call: Dispatch, a: Attempt) -> Json:
    client, req = call.client, call.req
    client.budget.update_request(req.request_hash, state="sent", dispatched=True)
    if a.number == 1:
        response = client._post_json("/responses", req.payload, attempt_id=a.attempt_id, timeout=a.timeout)
    else:
        # Construction refuses retries for overridden first-send transports.
        response = client._post_with_key("/responses", req.payload, a.attempt_id, a.timeout, call.call_id)
    if response.status_code >= 400:
        _raise_http(response, a)
    payload = response.json()
    _validate(call, a, payload)
    result = _output(a, payload)
    a.status = "completed"
    client.budget.update_request(
        req.request_hash,
        state="completed",
        result=result,
        result_sha256=result_hash(result),
        receipt_id=a.details.get("response_id"),
    )
    if req.cache and not req.options.bypass_cache:
        client.budget.scope.guard(req.cache, client.budget.workspace_root)
        req.cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        req.cache.parent.chmod(0o700)
        data = {
            "workspace_id": client.workspace_id,
            "request_hash": req.request_hash,
            "source_context": req.options.context or {},
            "result": result,
        }
        atomic_bytes(req.cache, json.dumps(data, ensure_ascii=False).encode())
    return result


def _finish_state(call: Dispatch, a: Attempt) -> None:
    uncertain = call.maybe_sent or a.status in ("transport_unknown", "transport_write_failed", "time_budget_exhausted")
    server_unknown = a.status in {f"http_{status}" for status in _UNCERTAIN_HTTP}
    exhausted = {}
    if call.failure is not None and a.number == call.limit and call.limit > 1 and uncertain:
        call.failure.unknown_retries_exhausted = True
        exhausted["unknown_retries_exhausted"] = True
        a.details.update(exhausted)
    if a.status == "completed":
        return
    update = call.client.budget.update_request
    if not call.maybe_sent and a.status in _NOT_REACHED:
        update(call.req.request_hash, state="failed", dispatched=call.reached, error_code=a.status)
    elif not call.maybe_sent and not server_unknown and (a.status.startswith("http_") or a.status == "incomplete"):
        update(call.req.request_hash, state="failed", error_code=a.status)
    else:
        if call.failure is not None:
            call.failure.outcome_unknown = True
        update(
            call.req.request_hash,
            state="unknown",
            error_code=a.status if server_unknown else "model_outcome_unknown",
            **exhausted,
        )


def _once(call: Dispatch, a: Attempt) -> Json | None:
    client = call.client
    base = _base(call, a)
    client._record({**base, "status": "started", "usage": None})
    call.failure = None
    a.details = dict.fromkeys(
        ("error_code", "error_type", "transport_error", "incomplete_reason", "response_id", "returned_model")
    )
    a.details["returned_service_tier"] = "not_reported"
    try:
        return _perform(call, a)
    except ValueError:
        a.status = "invalid_json"
        call.failure = ModelCallError(a.status)
    except httpx.TransportError as exc:
        a.status = transport_status(exc)
        a.details["transport_error"] = type(exc).__name__
        # Only a proven-unsent transport failure may be sent again.
        a.retry = a.status == "transport_not_sent"
        a.retry_wait = max(a.retry_wait, RETRY_BACKOFF_SECONDS * a.number)
        call.failure = ModelCallError(a.status)
    except BudgetExceeded as exc:
        a.status = "time_budget_exhausted"
        call.failure = exc
        raise
    except ModelCallError as exc:
        a.status = {
            "cancelled": "cancelled_after_dispatch",
            "project_budget_not_dispatched": "project_budget_not_dispatched",
            "model_not_dispatched": "model_not_dispatched",
        }.get(str(exc), a.status)
        a.retry = a.retry and a.status.startswith("http_")
        call.failure = exc
    finally:
        _settle(call, a, base)
    return None


def _settle(call: Dispatch, a: Attempt, base: Json) -> None:
    client = call.client
    _finish_state(call, a)
    client.budget.finish(
        client.output_reservation_tokens,
        a.reserved,
        a.usage,
        reservation_id=a.attempt_id,
        elapsed_seconds=time.monotonic() - a.started,
    )
    client._record(
        {
            **base,
            **a.details,
            "status": a.status,
            "usage": a.usage,
            "will_retry": bool(a.retry and a.number < call.limit and a.status != "completed"),
            "wall_seconds": time.monotonic() - a.started,
        }
    )


def _wait(client: ModelTransport, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        client._check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))


def send_attempts(client: ModelTransport, req: Request, call_id: str) -> Json:
    call = Dispatch(client, req, call_id, client.max_attempts if client.retry_transient else 1)
    for number in range(1, call.limit + 1):
        a = _reserve(call, number)
        result = _once(call, a)
        if result is not None:
            return result
        if not a.retry or number == call.limit:
            break
        call.maybe_sent |= a.status in ("transport_unknown", "transport_write_failed")
        call.reached |= a.status.startswith("http_")
        _wait(client, a.retry_wait)
    raise call.failure or ModelCallError("attempts exhausted")
