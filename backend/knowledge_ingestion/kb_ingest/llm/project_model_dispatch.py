"""Host-bound buffered model dispatch with durable execution ownership."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Never, NotRequired, TypedDict

import httpx

from ..core import clock
from ..core.errors import ProviderProtocolError
from ..storage.model_result_types import SavedModelResult
from . import http
from .dispatch_failure_log import safe_error_id, safe_exc_info, safe_stack
from .dispatch_results import DispatchResults, dispatch_deadline
from .dispatch_validation import DispatchValidation
from .dispatch_validation import ModelDispatchError as ModelDispatchError
from .model_requests import ModelRequestContext, ModelRequestError
from .provider_protocol import decode_response, encode_request, provider_error_code

if TYPE_CHECKING:
    from ..core.config import ModelCredentials
    from .model_requests import ModelRequestPolicy
    from .provider_protocol import Payload


logger = logging.getLogger(__name__)


def _strict_provider_json(raw: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                message = "duplicate provider field"
                raise ValueError(message)
            value[key] = item
        return value

    def finite(_: str) -> Never:
        message = "nonfinite provider value"
        raise ValueError(message)

    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite)


class ProjectModelDispatcher(DispatchValidation):
    def __init__(
        self,
        context: ModelRequestContext,
        *,
        api_key: str,
        timeout_seconds: float = 60,
        max_body_bytes: int = 262144,
        max_tools: int = 32,
        transport: httpx.AsyncBaseTransport | None = None,
        controlled_inputs: bool = False,
        reasoning_effort: str | None = None,
    ) -> None:
        if not isinstance(context, ModelRequestContext):
            raise TypeError("trusted_request_context_required")
        if type(api_key) is not str or not api_key or "\n" in api_key or "\r" in api_key:
            raise ValueError("provider_credential_required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 300
        ):
            raise ValueError("invalid_timeout")
        if (
            type(max_body_bytes) is not int
            or not 1 <= max_body_bytes <= (24 * 1024 * 1024 if controlled_inputs else 1048576)
            or type(max_tools) is not int
            or not 0 <= max_tools <= 128
        ):
            raise ValueError("invalid_limits")
        self.context = context
        self._results = DispatchResults(context)
        self._api_key = api_key
        self.timeout_seconds, self.max_body_bytes, self.max_tools = timeout_seconds, max_body_bytes, max_tools
        if type(controlled_inputs) is not bool or controlled_inputs != (context.policy.controlled_input_pages == 4):
            raise ValueError("invalid_controlled_input_policy")
        from kb_ingest.core.config import REASONING_EFFORTS

        if reasoning_effort is not None and (
            type(reasoning_effort) is not str or reasoning_effort not in REASONING_EFFORTS
        ):
            raise ValueError("invalid_reasoning_effort")
        self.reasoning_effort = reasoning_effort
        self.controlled_inputs = controlled_inputs
        self._transport = transport

    def _decode_response(
        self, context: ModelRequestContext, status: int, body: bytes, call_id: str
    ) -> dict[str, object]:
        if not 200 <= status < 300:
            if status not in (500, 502, 503, 504):
                context.service.fail_after_send(context.project_id, call_id, "provider_http_" + str(status))
            raise ModelDispatchError(provider_error_code(status), 502)
        raw = _strict_provider_json(body)
        result = decode_response(raw, context.policy.protocol, requested_model=context.policy.model)
        if result.get("model") not in (None, context.policy.model):
            raise ModelDispatchError("provider_model_mismatch", 502)
        if result.get("status") != "completed" or result.get("error") is not None:
            raise ModelDispatchError("provider_incomplete", 502)
        return result

    def _dispatch_error(self, error: Exception) -> ModelDispatchError:
        if isinstance(error, (TimeoutError, httpx.TimeoutException)):
            return ModelDispatchError("provider_timeout", 502)
        if isinstance(error, ProviderProtocolError):
            return ModelDispatchError(error.code, 502)
        if isinstance(error, ModelDispatchError):
            return error
        return ModelDispatchError("provider_failure", 502)

    def _prepare_dispatch(
        self, request: dict[str, object], call_id: str | None
    ) -> tuple[ModelRequestContext, str, dict[str, object], str, dict[str, object]]:
        payload = self.validate(request)
        # The host override is part of the effective payload before wire encoding
        # and durable request hashing; it never changes in response to an HTTP error.
        if self.reasoning_effort == "omit":
            payload.pop("reasoning", None)
            payload.pop("include", None)
        elif self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        context = self.context
        try:
            endpoint, wire = encode_request(payload, context.policy.protocol)
        except ProviderProtocolError as error:
            raise ModelDispatchError(error.code, 400) from None
        call_id = call_id or uuid.uuid4().hex
        try:
            prepared = context.prepare(context.policy.base_url, "/responses", payload, call_id)
        except ModelRequestError as error:
            raise ModelDispatchError(error.code, error.status) from None
        # Only the first pending dispatch can claim sending. Duplicates with an
        # unknown outcome cannot acquire a fresh identity from this code path.
        if prepared["state"] != "pending":
            raise ModelDispatchError(
                "request_outcome_unknown" if prepared["state"] in ("sent", "unknown") else "dispatch_already_claimed",
                409,
            )
        return context, endpoint, wire, call_id, prepared

    def _mark_sent(self, context: ModelRequestContext, call_id: str) -> None:
        try:
            context.service.mark_sent(context.project_id, call_id)
        except ModelRequestError as error:
            raise ModelDispatchError(error.code, error.status) from None

    def _unknown(self, context: ModelRequestContext, call_id: str, reason: str = "transport_failure") -> None:
        try:
            context.service.mark_call_unknown(context.project_id, call_id, reason)
        except ModelRequestError as error:
            if error.code != "invalid_transition":
                logger.warning("Unknown outcome recording failed call_id=%s code=%s", call_id, error.code)

    def _notify_dispatch(
        self,
        context: ModelRequestContext,
        call_id: str,
        prepared: dict[str, object],
        on_dispatch: Callable[[dict[str, object]], None],
    ) -> None:
        callback_error: ModelDispatchError | None = None
        try:
            try:
                on_dispatch(
                    {
                        "call_id": call_id,
                        "request_sha256": prepared["request_sha256"],
                        "model": context.policy.model,
                    }
                )
            except Exception as caught_error:
                try:
                    context.service.fail_before_send(context.project_id, call_id, "durability_failure")
                except ModelRequestError as error:
                    logger.warning("Unsent outcome recording failed call_id=%s code=%s", call_id, error.code)
                callback_error = ModelDispatchError("dispatch_record_failed", 500)
                error_id = safe_error_id(context.service.workspace_id, context.project_id, call_id)
                logger.exception(
                    "Model dispatch before-send failed error_id=%s\n%s",
                    error_id,
                    safe_stack(caught_error.__traceback__),
                    exc_info=safe_exc_info(),
                    extra={"error_id": error_id},
                )
        finally:
            if callback_error is not None:
                raise callback_error from None

    async def dispatch(
        self,
        request: dict[str, object],
        *,
        on_dispatch: Callable[[dict[str, object]], None] | None = None,
        call_id: str | None = None,
    ) -> Payload:
        with dispatch_deadline(self.timeout_seconds) as deadline:
            context, endpoint, wire, call_id, prepared = self._prepare_dispatch(request, call_id)
            if on_dispatch is not None:
                self._notify_dispatch(context, call_id, prepared, on_dispatch)
            self._mark_sent(context, call_id)

            status = None
            dispatch_error: ModelDispatchError | None = None
            try:
                try:
                    async with asyncio.timeout(clock.seconds_until(deadline)):
                        try:
                            status, _, body = await http.post_bounded(
                                context.policy.base_url + endpoint,
                                wire,
                                api_key=self._api_key,
                                seconds=clock.seconds_until(deadline),
                                max_bytes=self.max_body_bytes,
                                transport=self._transport,
                            )
                        except http.ResponseTooLargeError:
                            raise ModelDispatchError("provider_response_too_large", 502) from None
                    result = self._decode_response(context, status, body, call_id)
                except asyncio.CancelledError:
                    self._unknown(context, call_id)
                    raise
                except Exception as error:
                    reason = "provider_http_" + str(status) if status in (500, 502, 503, 504) else "transport_failure"
                    self._unknown(context, call_id, reason)
                    dispatch_error = self._dispatch_error(error)
                    error_id = safe_error_id(context.service.workspace_id, context.project_id, call_id)
                    logger.exception(
                        "Model dispatch failed error_id=%s\n%s",
                        error_id,
                        safe_stack(error.__traceback__),
                        exc_info=safe_exc_info(),
                        extra={"error_id": error_id},
                    )
            finally:
                if dispatch_error is not None:
                    raise dispatch_error from None

            # Local persistence failures are not provider failures and never re-enter transport.
            request_sha256 = prepared["request_sha256"]
            if not isinstance(request_sha256, str):
                raise ModelDispatchError("request_identity_conflict", 409)
            try:
                await self._results.preserve(result, call_id, request_sha256, deadline)
            except ModelRequestError as error:
                raise ModelDispatchError(error.code, error.status) from None
            return result

    async def read_saved_result(self, *, call_id: str, request_sha256: str) -> SavedModelResult | None:
        try:
            return await self._results.read(call_id, request_sha256)
        except ModelRequestError as error:
            raise ModelDispatchError(error.code, error.status) from None

    async def reconcile_saved_result(
        self,
        *,
        call_id: str,
        request_sha256: str,
        deadline: float,
    ) -> SavedModelResult:
        try:
            return await self._results.reconcile(call_id, request_sha256, deadline)
        except ModelRequestError as error:
            raise ModelDispatchError(error.code, error.status) from None


class _PolicyChanges(TypedDict):
    model: str
    controlled_input_pages: int
    base_url: NotRequired[str]
    protocol: NotRequired[str]


def _configured_policy(
    dispatcher: ProjectModelDispatcher, model: str, pages: int, credentials: ModelCredentials | None = None
) -> ModelRequestPolicy:
    from dataclasses import replace

    changes: _PolicyChanges = {"model": model, "controlled_input_pages": pages}
    if credentials is not None:
        changes.update(
            {"model": credentials.model or model, "base_url": credentials.base_url, "protocol": credentials.protocol}
        )
    return replace(dispatcher.context.policy, **changes)


def make_study_wiki_dispatchers(
    dispatcher: ProjectModelDispatcher, *, credentials: ModelCredentials | None = None
) -> dict[str, ProjectModelDispatcher]:
    """Host-selected generation models share website request execution storage."""
    if not isinstance(dispatcher, ProjectModelDispatcher):
        raise TypeError("trusted_dispatcher_required")
    models = (credentials.model,) if credentials is not None and credentials.model else ("gpt-6-luna", "gpt-6-sol")
    return {
        model: ProjectModelDispatcher(
            ModelRequestContext(
                dispatcher.context.service,
                dispatcher.context.project_id,
                _configured_policy(dispatcher, model, 4, credentials),
            ),
            api_key=credentials.api_key if credentials is not None else dispatcher._api_key,
            timeout_seconds=180,
            max_body_bytes=24 * 1024 * 1024,
            max_tools=0,
            transport=dispatcher._transport,
            controlled_inputs=True,
        )
        for model in models
    }


def make_document_extraction_context(
    dispatcher: ProjectModelDispatcher, *, credentials: ModelCredentials | None = None
) -> ModelRequestContext:
    """Existing bounded extraction inputs with website request recovery."""
    from dataclasses import replace

    if not isinstance(dispatcher, ProjectModelDispatcher):
        raise TypeError("trusted_dispatcher_required")
    policy = replace(_configured_policy(dispatcher, "gpt-6-sol", 30, credentials), provider_default_output_bound=True)
    return ModelRequestContext(dispatcher.context.service, dispatcher.context.project_id, policy)
