"""Trusted model request policy and dispatch ownership, independent of fees."""

import re
from dataclasses import dataclass
from typing import Any, TypedDict, Unpack

import httpx
from sqlalchemy import Engine

from ..core.workspace import validate_workspace_id
from ..storage.budget_repository import legacy_reservations
from ..storage.model_result_types import (
    ModelResultBinding,
    ModelResultProtocol,
    NormalizedResult,
    SavedModelResult,
    StoredModelResult,
    normalized_receipt,
)
from ..storage.request_repository import Change, RequestRepository
from ..storage.request_repository import ModelRequestError as ModelRequestError
from .identity import result_hash as _result_hash
from .model_results import ModelResultService


def identity(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > 256:
        raise ModelRequestError("invalid_identity", 400)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or not re.fullmatch("[a-f0-9]{64}", value):
        raise ModelRequestError("invalid_request_hash", 400)
    return value


class RequestIdentity(TypedDict):
    project_id: str
    call_id: str
    provider: str
    model: str
    service_identity: str
    request_sha256: str


class ModelRequestService:
    def __init__(self, engine: Engine, *, workspace_id: str) -> None:
        self.engine = engine
        self._workspace_id = validate_workspace_id(workspace_id)
        self._repository = RequestRepository(engine)
        self._results = ModelResultService(self._repository)

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    result_hash = staticmethod(_result_hash)

    def read_call(self, project_id: str, call_id: str) -> dict[str, Any] | None:
        """Return the project receipt with epoch-second timestamps and a nullable send time."""
        return self._repository.read(self.workspace_id, identity(project_id), identity(call_id))

    def prepare_call(self, **binding: Unpack[RequestIdentity]) -> dict[str, Any]:
        if binding.keys() != RequestIdentity.__required_keys__:
            message = "Missing or unexpected request identity fields"
            raise TypeError(message)
        values = {key: identity(value) for key, value in binding.items() if key != "request_sha256"}
        values["request_sha256"] = _digest(binding["request_sha256"])
        return self._repository.prepare(self.workspace_id, values)

    def _change(self, project_id: str, call_id: str, change: Change) -> dict[str, Any]:
        if change.reason is not None and (
            type(change.reason) is not str or not re.fullmatch("[A-Za-z0-9_-]{1,128}", change.reason)
        ):
            raise ModelRequestError("invalid_reason", 400)
        return self._repository.change(self.workspace_id, identity(project_id), identity(call_id), change)

    def mark_sent(self, project_id: str, call_id: str) -> dict[str, Any]:
        return self._change(project_id, call_id, Change("sent"))

    def complete_call(
        self, project_id: str, call_id: str, *, result_sha256: str, receipt_id: str | None = None
    ) -> dict[str, Any]:
        receipt_id = normalized_receipt(receipt_id)
        return self._change(
            project_id, call_id, Change("completed", result_sha256=_digest(result_sha256), receipt_id=receipt_id)
        )

    def _result_binding(self, binding: ModelResultBinding) -> ModelResultBinding:
        if binding.workspace_id != self.workspace_id:
            raise ModelRequestError("call_not_found", 404)
        for value in (binding.project_id, binding.call_id, binding.provider, binding.service_identity, binding.model):
            identity(value)
        _digest(binding.request_sha256)
        return binding

    def save_received_result(
        self,
        binding: ModelResultBinding,
        *,
        response: NormalizedResult,
        protocol: ModelResultProtocol,
        deadline: float,
    ) -> StoredModelResult:
        return self._results.save(
            self._result_binding(binding), response=response, protocol=protocol, deadline=deadline
        )

    def read_saved_result(self, binding: ModelResultBinding) -> SavedModelResult | None:
        return self._results.read(self._result_binding(binding))

    def reconcile_saved_result(self, binding: ModelResultBinding, *, deadline: float) -> SavedModelResult:
        return self._results.reconcile(self._result_binding(binding), deadline=deadline)

    def release_proven_unsent(self, binding: ModelResultBinding, sent_at: float) -> None:
        binding = self._result_binding(binding)
        try:
            self._repository.release_proven_unsent(binding, sent_at)
        except ModelRequestError as error:
            if error.code not in {"request_state_unavailable", "request_state_timeout"}:
                raise
            if not self._repository.unsent_release_confirmed(binding):
                raise

    def mark_call_unknown(self, project_id: str, call_id: str, reason: str = "transport_failure") -> dict[str, Any]:
        return self._change(project_id, call_id, Change("unknown", reason=reason))

    def fail_before_send(self, project_id: str, call_id: str, reason: str = "not_sent") -> dict[str, Any]:
        return self._change(project_id, call_id, Change("failed", reason=reason, unsent_only=True))

    def fail_after_send(self, project_id: str, call_id: str, reason: str = "provider_failure") -> dict[str, Any]:
        return self._change(project_id, call_id, Change("failed", reason=reason))

    def import_legacy(self, engine: Engine, project_id: str | None = None) -> int:
        rows = legacy_reservations(engine, self.workspace_id, project_id)
        return self._repository.import_reservations(self.workspace_id, rows)


@dataclass(frozen=True)
class ModelRequestPolicy:
    provider: str
    model: str
    base_url: str
    endpoint: str = "/responses"
    protocol: str = "responses"
    max_input_tokens: int = 1_000_000
    max_output_tokens: int = 32768
    service_tier: str = "default"
    controlled_input_pages: int = 0
    provider_default_output_bound: bool = False

    def __post_init__(self) -> None:
        identity(self.provider)
        identity(self.model)
        from ..core.endpoints import normalize_base_url
        from ..core.errors import ProviderProtocolError

        try:
            normalized = normalize_base_url(self.base_url)
        except ProviderProtocolError:
            raise ModelRequestError("invalid_service_url", 400) from None
        if self.protocol not in ("responses", "chat_completions"):
            raise ModelRequestError("unsupported_protocol", 400)
        for value in (self.max_input_tokens, self.max_output_tokens):
            if type(value) is not int or value <= 0:
                raise ModelRequestError("invalid_output_bound", 400)
        object.__setattr__(self, "base_url", normalized)

    @property
    def service_identity(self) -> str:
        return _result_hash([self.provider, self.base_url, self.protocol, self.endpoint])

    def request_hash(self, base_url: str, endpoint: str, request: dict[str, Any]) -> str:
        if base_url.rstrip("/") != self.base_url or endpoint not in (
            self.endpoint,
            "/responses/input_tokens",
            "/chat/completions",
        ):
            raise ModelRequestError("service_identity_mismatch", 400)
        if request.get("model") != self.model:
            raise ModelRequestError("model_identity_mismatch", 400)
        if endpoint != "/responses/input_tokens":
            output = request.get(
                "max_output_tokens", self.max_output_tokens if self.provider_default_output_bound else None
            )
            if type(output) is not int or not 0 < output <= self.max_output_tokens:
                raise ModelRequestError("invalid_output_bound", 400)
            tools = request.get("tools", [])
            if type(tools) is not list or any(
                type(tool) is not dict or tool.get("type") != "function" for tool in tools
            ):
                raise ModelRequestError("unsupported_model_capability", 400)
        if self.controlled_input_pages:
            from .model_inputs import ModelInputError, validate_controlled_input

            try:
                validate_controlled_input(request, max_pages=self.controlled_input_pages)
            except ModelInputError:
                raise ModelRequestError("unsupported_controlled_input", 400) from None
        return _result_hash(request)


def _response_error(payload: object, model: str) -> str | None:
    if type(payload) is not dict:
        return "provider_protocol"
    if payload.get("model") not in (None, model):
        return "provider_model_mismatch"
    if payload.get("object") == "response.input_tokens":
        return None if type(payload.get("input_tokens")) is int and payload["input_tokens"] > 0 else "provider_protocol"
    if payload.get("status") != "completed" or payload.get("error") is not None:
        return "provider_incomplete"
    return None


@dataclass(frozen=True)
class ModelRequestContext:
    service: ModelRequestService
    project_id: str
    policy: ModelRequestPolicy

    def request_hash(self, base_url: str, endpoint: str, request: dict[str, Any]) -> str:
        return self.policy.request_hash(base_url, endpoint, request)

    def prepare(self, base_url: str, endpoint: str, request: dict[str, Any], call_id: str) -> dict[str, Any]:
        return self.service.prepare_call(
            project_id=self.project_id,
            call_id=call_id,
            provider=self.policy.provider,
            model=self.policy.model,
            service_identity=self.policy.service_identity,
            request_sha256=self.request_hash(base_url, endpoint, request),
        )

    def before_send(self, base_url: str, endpoint: str, request: dict[str, Any], call_id: str) -> dict[str, Any]:
        self.prepare(base_url, endpoint, request, call_id)
        return self.service.mark_sent(self.project_id, call_id)

    def finish(self, call_id: str, response: httpx.Response) -> dict[str, Any]:
        if response.status_code in (500, 502, 503, 504):
            return self.service.mark_call_unknown(
                self.project_id, call_id, "provider_http_" + str(response.status_code)
            )
        if not 200 <= response.status_code < 300:
            return self.service.fail_after_send(self.project_id, call_id, "provider_http_" + str(response.status_code))
        payload = response.json()
        code = _response_error(payload, self.policy.model)
        if code:
            self.service.mark_call_unknown(self.project_id, call_id, code)
            raise ModelRequestError(code, 502)
        return self.service.complete_call(
            self.project_id, call_id, result_sha256=_result_hash(payload), receipt_id=payload.get("id")
        )

    def finish_received(self, call_id: str, response: httpx.Response, *, request_sha256: str, deadline: float) -> None:
        payload = response.json()
        code = _response_error(payload, self.policy.model)
        if code:
            self.service.mark_call_unknown(self.project_id, call_id, code)
            raise ModelRequestError(code, 502)
        binding = ModelResultBinding(
            self.service.workspace_id,
            self.project_id,
            call_id,
            self.policy.provider,
            self.policy.service_identity,
            self.policy.model,
            request_sha256,
        )
        try:
            normalized = NormalizedResult.model_validate(payload)
        except ValueError:
            raise ModelRequestError("model_result_invalid") from None
        self.service.save_received_result(
            binding,
            response=normalized,
            protocol=ModelResultProtocol(self.policy.protocol),
            deadline=deadline,
        )
        try:
            saved = self.service.reconcile_saved_result(binding, deadline=deadline)
        except ModelRequestError as error:
            if error.code in {"request_state_timeout", "request_state_unavailable"}:
                raise ModelRequestError("model_completion_pending") from None
            raise
        if saved.request_state != "completed":
            raise ModelRequestError("model_completion_pending")

    def unknown(self, call_id: str) -> dict[str, Any]:
        return self.service.mark_call_unknown(self.project_id, call_id, "transport_failure")
