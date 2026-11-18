"""Bounded local result preservation and reconciliation, with no provider transport."""

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256

from ..core import clock
from ..core.serialization import canonical
from ..storage.model_result_types import (
    MAX_RESULT_BYTES,
    MODEL_RESULT_VERSION,
    ModelRequestState,
    ModelResultBinding,
    ModelResultProtocol,
    NormalizedResult,
    SavedModelResult,
    StoredModelResult,
    StoredResultRecord,
    normalized_receipt,
)
from ..storage.request_repository import ModelRequestError, RequestRepository

_STORAGE_ERRORS = frozenset({"request_state_unavailable", "request_state_timeout"})


@contextmanager
def _deadline(deadline: float) -> Iterator[None]:
    if not math.isfinite(deadline):
        raise ModelRequestError("request_state_timeout", 503)
    parent = clock.IO_DEADLINE.get()
    token = clock.IO_DEADLINE.set(min(deadline, parent) if parent is not None else deadline)
    try:
        yield
    finally:
        clock.IO_DEADLINE.reset(token)


def _available(deadline: float) -> bool:
    parent = clock.IO_DEADLINE.get()
    return time.monotonic() < (min(deadline, parent) if parent is not None else deadline)


def _record(
    binding: ModelResultBinding, response: NormalizedResult, protocol: ModelResultProtocol
) -> StoredModelResult:
    try:
        encoded = canonical(response.root)
        size = len(encoded.encode())
        if size > MAX_RESULT_BYTES:
            raise ModelRequestError("model_result_too_large", 503)
        return StoredResultRecord(
            workspace_id=binding.workspace_id,
            project_id=binding.project_id,
            call_id=binding.call_id,
            provider=binding.provider,
            service_identity=binding.service_identity,
            model=binding.model,
            request_sha256=binding.request_sha256,
            format_version=MODEL_RESULT_VERSION,
            protocol=protocol,
            response=response,
            result_sha256=sha256(encoded.encode()).hexdigest(),
            result_bytes=size,
            receipt_id=normalized_receipt(response.root.get("id")),
            received_at=clock.now(),
        ).stored()
    except (ValueError, TypeError, OverflowError):
        raise ModelRequestError("model_result_invalid") from None


class ModelResultService:
    def __init__(self, repository: RequestRepository) -> None:
        self.repository = repository

    def read(self, binding: ModelResultBinding) -> SavedModelResult | None:
        return self.repository.read_result(binding)

    def save(
        self,
        binding: ModelResultBinding,
        *,
        response: NormalizedResult,
        protocol: ModelResultProtocol,
        deadline: float,
    ) -> StoredModelResult:
        value = _record(binding, response, protocol)
        with _deadline(deadline):
            for attempt in range(2):
                try:
                    return self.repository.save_result(value)
                except ModelRequestError as error:
                    if error.code not in _STORAGE_ERRORS:
                        raise
                    existing = self._save_evidence(binding)
                    if existing is not None:
                        if replace(value, received_at=existing.stored.received_at) != existing.stored:
                            raise ModelRequestError("result_identity_conflict") from None
                        return existing.stored
                    if attempt or not _available(deadline):
                        raise ModelRequestError(error.code, 503) from None
        raise ModelRequestError("request_state_unavailable", 503)

    def _save_evidence(self, binding: ModelResultBinding) -> SavedModelResult | None:
        try:
            return self.read(binding)
        except ModelRequestError as error:
            if error.code in _STORAGE_ERRORS:
                raise ModelRequestError(error.code, 503) from None
            raise

    def reconcile(self, binding: ModelResultBinding, *, deadline: float) -> SavedModelResult:
        with _deadline(deadline):
            # Certify the body before reporting completion_pending, even when the first write fails.
            preserved = self._save_evidence(binding)
            if preserved is None:
                raise ModelRequestError("model_result_missing")
            if preserved.request_state == ModelRequestState.COMPLETED:
                return preserved
            for attempt in range(2):
                try:
                    return self.repository.reconcile_result(binding)
                except ModelRequestError as error:
                    if error.code not in _STORAGE_ERRORS:
                        raise
                    reconciled = self._completion_evidence(binding)
                    if reconciled is not None and reconciled.request_state == ModelRequestState.COMPLETED:
                        return reconciled
                    if attempt or not _available(deadline):
                        raise ModelRequestError("model_completion_pending") from None
        raise ModelRequestError("model_completion_pending")

    def _completion_evidence(self, binding: ModelResultBinding) -> SavedModelResult | None:
        try:
            result = self.read(binding)
        except ModelRequestError as error:
            if error.code in _STORAGE_ERRORS:
                raise ModelRequestError("model_completion_pending") from None
            raise
        if result is None:
            raise ModelRequestError("model_result_missing")
        return result
