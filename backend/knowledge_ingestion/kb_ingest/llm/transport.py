"""Bounded synchronous transport and project dispatch accounting."""

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Unpack
from urllib.parse import SplitResult, urlsplit

import httpx

from ..core import clock
from ..core.endpoints import normalize_base_url
from ..storage.model_result_types import ModelResultBinding
from . import http
from .errors import BudgetExceeded, ModelCallError
from .identity import Json
from .ledger import append_attempt
from .model_limits import model_output_reservation
from .provider_protocol import decode_response, encode_request
from .settings import ClientOptions, Settings

if TYPE_CHECKING:
    from .model_requests import ModelRequestContext

logger = logging.getLogger(__name__)

_SERVICE_LIMITERS: dict[tuple[str, str | None, int], threading.BoundedSemaphore] = {}
_SERVICE_LIMITERS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class _SentClaim:
    request_sha256: str
    sent_at: float


def _sent_claim(receipt: Json) -> _SentClaim:
    request_sha256, sent_at = receipt.get("request_sha256"), receipt.get("sent_at")
    if (
        not isinstance(request_sha256, str)
        or not isinstance(sent_at, (int, float))
        or type(sent_at) not in (int, float)
    ):
        raise ModelCallError("model_outcome_unknown", outcome_unknown=True)
    return _SentClaim(request_sha256, float(sent_at))


def _service_limiter(parsed: SplitResult) -> threading.BoundedSemaphore:
    # All task clients using one service in this Python service process share two
    # slots, independent of API key, model or /v1 path. Multi-process services may
    # inject one cross-process limiter through ModelClient.shared_limiter.
    identity = (parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    with _SERVICE_LIMITERS_LOCK:
        return _SERVICE_LIMITERS.setdefault(identity, threading.BoundedSemaphore(2))


class ModelTransport:
    def __init__(self, *, api_key: str, model: str, **options: Unpack[ClientOptions]) -> None:
        cfg = Settings(**options)
        cfg.validate(api_key, model)
        self.budget = cfg.scoped_budget()
        scope = self.budget.scope
        self.workspace_id = scope.workspace_id
        self.protocol = cfg.protocol
        self.cache_namespace = cfg.cache_namespace
        self.output_reservation_tokens = model_output_reservation(model, cfg.max_output_tokens)
        self.api_key, self.model, self.base_url = api_key, model, normalize_base_url(cfg.base_url).rstrip("/")
        self.ledger_path = scope.path(cfg.ledger_path)
        if cfg.ledger_path is not None and self.ledger_path is not None:
            self.budget.bind_ledger(cfg.ledger_path)
            if self.ledger_path.exists():
                self.budget.reconcile_ledger(cfg.ledger_path)
        self.max_output_tokens, self.max_attempts, self.timeout = cfg.max_output_tokens, cfg.max_attempts, cfg.timeout
        if cfg.retry_transient and type(self)._post_json is not ModelTransport._post_json:
            message = "retries need the base transport (an overriding _post_json takes no idempotency key)"
            raise ValueError(message)
        self.retry_transient = cfg.retry_transient
        self.require_full_timeout = cfg.require_full_timeout
        self.reasoning_effort, self.retry_delay = cfg.reasoning_effort, cfg.retry_delay
        self.count_input_tokens = cfg.count_input_tokens
        self.cost_ledger, self.batch_stage, self.run_id = None, cfg.batch_stage, cfg.run_id
        self.cache_dir = scope.path(cfg.cache_dir, directory=True)
        self.incomplete_artifact_dir = scope.path(cfg.incomplete_artifact_dir, directory=True)
        self.transport, self.project_budget_context = cfg.transport, cfg.project_budget_context
        self.cancel_check = cfg.cancel_check
        self._semaphore = threading.BoundedSemaphore(cfg.concurrency)
        self._shared_limiter = (
            cfg.shared_limiter if cfg.shared_limiter is not None else _service_limiter(urlsplit(cfg.base_url))
        )
        self._cross_process_limiter = nullcontext()
        self._ledger_lock = threading.Lock()

    def _check_cancelled(self) -> None:
        if self.cancel_check is None:
            return
        try:
            cancelled = self.cancel_check()
        except Exception:
            raise ModelCallError("cancellation_check_failed") from None
        if cancelled:
            raise ModelCallError("cancelled")

    def _record(self, row: Json) -> None:
        if self.ledger_path:
            with self._ledger_lock:
                append_attempt(self.ledger_path, row, self.budget.scope)

    def _record_incomplete_artifact(self, payload: Json, attempt_id: str) -> None:
        """Opt-in diagnostic response prefix; never written to the attempt ledger."""
        if self.incomplete_artifact_dir is None:
            return
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if self.api_key:
            raw = raw.replace(self.api_key, "[REDACTED]")
        encoded = raw.encode("utf-8")
        # The wrapper re-escapes JSON quotes and backslashes. Keep its on-disk
        # size below 64 KiB even for an adversarial response of only escapes.
        prefix = encoded[:30_000].decode("utf-8", errors="ignore")
        artifact = {
            "attempt_id": attempt_id,
            "workspace_id": self.workspace_id,
            "status": "incomplete",
            "truncated": len(encoded) > 30_000,
            "raw_response_prefix": prefix,
        }
        directory = self.incomplete_artifact_dir
        self.budget.scope.guard(directory, self.budget.workspace_root)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / f"{attempt_id}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            json.dump(artifact, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())

    async def _send(self, endpoint: str, wire: Json, key: str, seconds: float) -> httpx.Response:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        pending = asyncio.create_task(
            http.post(
                self.base_url + endpoint,
                wire,
                api_key=self.api_key,
                seconds=seconds,
                transport=self.transport,
                idempotency_key=self.budget.scope.provider_key(key),
            )
        )
        try:
            while True:
                self._check_cancelled()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    message = "model time budget exhausted; request cancelled"
                    raise BudgetExceeded(message)
                done, _ = await asyncio.wait({pending}, timeout=min(0.05, remaining))
                if done:
                    self._check_cancelled()
                    if loop.time() >= deadline:
                        message = "model time budget exhausted; late response rejected"
                        raise BudgetExceeded(message)
                    return await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    def _post_json(self, endpoint: str, request: Json, *, attempt_id: str, timeout: float) -> httpx.Response:
        return self._post_with_key(endpoint, request, attempt_id, timeout, attempt_id)

    def _post_with_key(
        self, endpoint: str, request: Json, attempt_id: str, timeout: float, idempotency_key: str
    ) -> httpx.Response:
        wire_endpoint, wire = (
            encode_request(request, self.protocol) if endpoint == "/responses" else (endpoint, request)
        )
        if endpoint != "/responses" and self.protocol != "responses":
            message = "unsupported_input_token_count"
            raise ModelCallError(message)
        context = self.project_budget_context
        from .model_requests import ModelRequestError

        deadline = time.monotonic() + timeout
        parent_deadline = clock.IO_DEADLINE.get()
        if parent_deadline is not None:
            deadline = min(deadline, parent_deadline)
        claim = None
        if context is not None:
            try:
                self._check_cancelled()
                prepared = context.before_send(self.base_url, endpoint, request, attempt_id)
            except (ModelRequestError, ValueError, ModelCallError):
                message = "model_not_dispatched"
                raise ModelCallError(message) from None
            claim = _sent_claim(prepared)
        network_timeout = self._network_timeout(context, endpoint, attempt_id, timeout, deadline, claim)
        try:
            response = asyncio.run(self._send(wire_endpoint, wire, idempotency_key, network_timeout))
        except BaseException:
            if context is not None:
                context.unknown(attempt_id)
            raise
        if 200 <= response.status_code < 300 and endpoint == "/responses":
            response = self._decode(response, attempt_id)
        if context is not None:
            self._finish_response(
                context, endpoint, response, attempt_id, claim.request_sha256 if claim else None, deadline
            )
        return response

    def _network_timeout(
        self,
        context: "ModelRequestContext | None",
        endpoint: str,
        attempt_id: str,
        timeout: float,
        deadline: float,
        claim: _SentClaim | None,
    ) -> float:
        if context is None or endpoint != "/responses":
            return timeout
        try:
            return clock.seconds_until(deadline)
        except TimeoutError as error:
            from .model_requests import ModelRequestError

            if claim is None:
                raise ModelCallError("model_outcome_unknown", outcome_unknown=True) from error
            binding = ModelResultBinding(
                context.service.workspace_id,
                context.project_id,
                attempt_id,
                context.policy.provider,
                context.policy.service_identity,
                context.policy.model,
                claim.request_sha256,
            )
            try:
                context.service.release_proven_unsent(binding, claim.sent_at)
            except ModelRequestError as state_error:
                logger.warning("Unsent request state could not be confirmed: %s", state_error.code)
                raise ModelCallError("model_outcome_unknown", outcome_unknown=True) from state_error
            raise ModelCallError("model_not_dispatched") from error

    def _finish_response(
        self,
        context: "ModelRequestContext",
        endpoint: str,
        response: httpx.Response,
        attempt_id: str,
        request_sha256: str | None,
        deadline: float,
    ) -> None:
        from .model_requests import ModelRequestError

        try:
            if endpoint == "/responses" and 200 <= response.status_code < 300:
                if request_sha256 is None:
                    raise ModelCallError("request_identity_conflict")
                context.finish_received(attempt_id, response, request_sha256=request_sha256, deadline=deadline)
            else:
                context.finish(attempt_id, response)
        except ModelRequestError as error:
            raise ModelCallError(error.code) from None

    def _decode(self, response: httpx.Response, attempt_id: str) -> httpx.Response:
        context = self.project_budget_context
        try:
            if len(response.content) > 4 * 1024 * 1024:
                message = "response_too_large"
                raise ModelCallError(message)
            payload = decode_response(response.json(), self.protocol, requested_model=self.model)
            # HTTPX decoded the provider body; the normalized JSON has new body metadata.
            headers = response.headers.copy()
            for name in ("content-encoding", "content-length", "transfer-encoding"):
                headers.pop(name, None)
            response = httpx.Response(response.status_code, json=payload, headers=headers)
        except ValueError as error:
            if context is not None:
                context.unknown(attempt_id)
            raise ModelCallError(getattr(error, "code", "invalid_json")) from None
        return response
