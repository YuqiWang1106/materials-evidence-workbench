"""Best-effort input-token count with its own durable request identity."""

import time
from dataclasses import dataclass

import httpx

from .errors import ModelCallError
from .identity import Json, result_hash
from .logical_request import LogicalOwnership, LogicalRequestBinding, RequestKind, request_metadata
from .status import _NOT_REACHED, transport_status
from .transport import ModelTransport

_UNCERTAIN_HTTP_STATUS = frozenset({500, 502, 503, 504})
_UNCERTAIN_HTTP_NAMES = frozenset(f"http_{code}" for code in _UNCERTAIN_HTTP_STATUS)


@dataclass
class CountResult:
    status: str = "transport_unknown"
    count: int | None = None


class TokenCounting(ModelTransport):
    def _count_input_tokens(
        self,
        request: Json,
        request_hash: str,
        phase: str,
        stage_input_id: str | None = None,
        *,
        logical_binding: LogicalRequestBinding | None = None,
        ownership: LogicalOwnership | None = None,
    ) -> tuple[int | None, bool]:
        """One best-effort count on the configured service, charged to task time/calls."""
        count_hash = result_hash({"kind": "input_token_count", "request_hash": request_hash})
        count_input_id = result_hash(["input_token_count", stage_input_id]) if stage_input_id else None
        if ownership is not None:
            self.budget.verify_ownership(ownership, logical_binding)
            return self._count_owned(
                request,
                count_hash,
                phase,
                count_input_id,
                logical_binding=logical_binding,
                ownership=ownership,
                parent_request_hash=request_hash,
            )
        with self.budget.request_lease(count_input_id or count_hash):
            return self._count_owned(request, count_hash, phase, count_input_id)

    def _count_owned(
        self,
        request: Json,
        request_hash: str,
        phase: str,
        stage_input_id: str | None,
        *,
        logical_binding: LogicalRequestBinding | None = None,
        ownership: LogicalOwnership | None = None,
        parent_request_hash: str | None = None,
    ) -> tuple[int | None, bool]:
        identity = result_hash(
            {"base_url": self.base_url, "endpoint": "/responses/input_tokens", "protocol": self.protocol}
        )
        attempt_id, saved = self.budget.claim_request(
            request_hash,
            model=self.model,
            service_identity=identity,
            recover_pending=True,
            stage_input_id=stage_input_id,
            logical_binding=logical_binding,
            ownership=ownership,
            request_kind=RequestKind.COUNT,
            parent_request_hash=parent_request_hash,
        )
        if saved is not None:
            return saved["input_tokens"], False
        started = time.monotonic()
        timeout = (
            min(self.timeout, 30, self.budget.remaining_seconds()) if self.budget.active_time else min(self.timeout, 30)
        )
        self.budget.reserve_count_request(
            reservation_id=attempt_id, seconds=timeout if self.budget.active_time else None
        )
        base: Json = {
            "attempt_id": attempt_id,
            "request_kind": "input_token_count",
            "phase": phase,
            "model": self.model,
            "request_hash": request_hash,
            "usage": None,
            "reserved_output_tokens": 0,
            "reserved_total_tokens": 0,
            "reserved_seconds": timeout,
            "started_at_epoch": time.time(),
            "requested_model": self.model,
        }
        base.update(request_metadata(logical_binding, RequestKind.COUNT, parent_request_hash))
        if stage_input_id:
            base["stage_input_id"] = stage_input_id
        self._record({**base, "status": "started", "input_tokens": None})
        return self._send_count(request, base, started)

    def _send_count(self, request: Json, base: Json, started: float) -> tuple[int | None, bool]:
        attempt_id, request_hash, timeout = base["attempt_id"], base["request_hash"], base["reserved_seconds"]
        outcome = CountResult()
        try:
            # These are the request fields supported by the official count API.
            # They are copied unchanged from the corresponding Responses request.
            counted_request = {
                key: request[key] for key in ("model", "instructions", "input", "reasoning", "text") if key in request
            }
            self.budget.update_request(request_hash, state="sent", dispatched=True)
            response = self._post_json(
                "/responses/input_tokens", counted_request, attempt_id=attempt_id, timeout=timeout
            )
            if response.status_code != 200:
                outcome.status = f"http_{response.status_code}"
                # A server error does not prove the provider skipped the count request.
                return None, response.status_code in _UNCERTAIN_HTTP_STATUS
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("object") != "response.input_tokens"
                or type(payload.get("input_tokens")) is not int
                or payload["input_tokens"] <= 0
            ):
                outcome.status = "invalid_count"
                return None, False
            if self.budget.active_time and time.monotonic() - started >= timeout:
                outcome.status = "time_budget_exhausted"
                return None, True
            outcome.count = payload["input_tokens"]
            outcome.status = "completed"
        except ModelCallError as exc:
            if str(exc) in _NOT_REACHED:
                outcome.status = str(exc)
            exc.outcome_unknown = outcome.status not in _NOT_REACHED
            raise
        except httpx.TransportError as exc:
            outcome.status = transport_status(exc)
            return None, outcome.status not in _NOT_REACHED
        except ValueError:
            outcome.status = "invalid_json"
            return None, True
        else:
            return outcome.count, False
        finally:
            self._finish_count(base, outcome, time.monotonic() - started)

    def _finish_count(self, base: Json, outcome: CountResult, elapsed: float) -> None:
        request_hash, attempt_id = base["request_hash"], base["attempt_id"]
        status, count = outcome.status, outcome.count
        if status == "completed":
            result = {"input_tokens": count}
            self.budget.update_request(
                request_hash, state="completed", result=result, result_sha256=result_hash(result)
            )
        elif status in _NOT_REACHED:
            self.budget.update_request(request_hash, state="failed", dispatched=False, error_code=status)
        elif (status.startswith("http_") and status not in _UNCERTAIN_HTTP_NAMES) or status == "invalid_count":
            self.budget.update_request(request_hash, state="failed", error_code=status)
        else:
            self.budget.update_request(request_hash, state="unknown", error_code="model_outcome_unknown")
        self.budget.finish(0, 0, None, reservation_id=attempt_id, elapsed_seconds=elapsed)
        self._record({**base, "status": status, "input_tokens": count, "wall_seconds": elapsed})
