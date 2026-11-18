"""Trusted logical ownership, independent of model payload and cache identity."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ModelCallError, ModelOutcomeUnknownError, ModelRequestBindingError
from .identity import Json, result_hash


class LogicalRequestFamily(StrEnum):
    DOCUMENT_V2 = "document_v2"
    WINDOW_EXTRACTION = "window_extraction"
    TABLE_MAPPING = "table_mapping"
    VISUAL_PAGE = "visual_page"
    VISUAL_REGIONS = "visual_regions"
    HANDWRITING_CHECK = "handwriting_check"
    SYNONYMS = "synonyms"
    IDENTITY = "identity"
    RESOLVE = "resolve"
    CLAIMS = "claims"


class RequestKind(StrEnum):
    GENERATION = "generation"
    COUNT = "input_token_count"


class _BindingEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal["entity-request-guard-v1"] = "entity-request-guard-v1"
    workspace_id: str = Field(min_length=1, max_length=200)
    source_revision_id: str = Field(min_length=1, max_length=1000)
    family: str
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    unit_id: str = Field(min_length=1, max_length=1000)


@dataclass(frozen=True, slots=True)
class LogicalRequestBinding:
    workspace_id: str
    source_revision_id: str
    family: LogicalRequestFamily
    plan_sha256: str
    unit_id: str

    def encoded(self) -> Json:
        try:
            value = _BindingEnvelope(
                workspace_id=self.workspace_id,
                source_revision_id=self.source_revision_id,
                family=LogicalRequestFamily(self.family).value,
                plan_sha256=self.plan_sha256,
                unit_id=self.unit_id,
            )
        except (ValidationError, ValueError):
            raise ModelRequestBindingError() from None
        return value.model_dump(mode="json")

    @classmethod
    def read(cls, value: object) -> "LogicalRequestBinding":
        try:
            row = _BindingEnvelope.model_validate(value)
            return cls(
                row.workspace_id, row.source_revision_id, LogicalRequestFamily(row.family), row.plan_sha256, row.unit_id
            )
        except (ValidationError, ValueError):
            raise ModelRequestBindingError() from None

    def lease_key(self) -> str:
        return result_hash(self.encoded())


class BudgetOpenMode(StrEnum):
    INIT_NEW = "init_new"
    OPEN_EXISTING = "open_existing"
    RECOVER_LEDGER = "recover_ledger"


@dataclass(frozen=True, slots=True)
class BudgetOpenEvidence:
    mode: BudgetOpenMode
    owner_path: Path
    workspace_id: str
    ledger_paths: tuple[Path, ...] = ()
    source_revision_id: str | None = None


class BudgetCreationPermit:
    """A host-owned first-use capability consumed under the budget file lock."""

    def __init__(self, evidence: BudgetOpenEvidence, verify_owner: Callable[[], bool]) -> None:
        self._evidence = evidence
        self._verify_owner = verify_owner
        self._consumed = False
        self._lock = threading.Lock()

    def consume(self, evidence: BudgetOpenEvidence) -> None:
        with self._lock:
            if self._consumed or evidence != self._evidence:
                raise ModelRequestBindingError()
            self._consumed = True
            if evidence.mode != BudgetOpenMode.INIT_NEW or not self._verify_owner():
                raise ModelRequestBindingError()


class LogicalOwnership:
    """Live lexical ownership; never persisted or accepted from model input."""

    def __init__(self, budget: object, binding: LogicalRequestBinding | None, request_hash: str) -> None:
        self._budget = budget
        self._thread = threading.get_ident()
        self.binding = binding
        self.request_hash = request_hash
        self._active = True
        self._parent_claimed = False

    def verify(self, budget: object, binding: LogicalRequestBinding | None) -> None:
        if (
            not self._active
            or self._budget is not budget
            or self._thread != threading.get_ident()
            or binding != self.binding
        ):
            raise ModelRequestBindingError()

    def claim_parent(self) -> None:
        self._parent_claimed = True

    def close(self) -> None:
        self._active = False

    def allows_parent(self, parent_hash: str | None, child_hash: str) -> bool:
        return (
            self._active
            and self._thread == threading.get_ident()
            and self._parent_claimed
            and parent_hash == self.request_hash
            and child_hash == result_hash({"kind": "input_token_count", "request_hash": parent_hash})
        )


def _row_binding(row: Json) -> LogicalRequestBinding | None:
    if "logical_binding" not in row:
        return None
    binding = LogicalRequestBinding.read(row["logical_binding"])
    try:
        kind = RequestKind(row["request_kind"])
    except (KeyError, ValueError):
        raise ModelRequestBindingError() from None
    if kind == RequestKind.COUNT:
        parent = row.get("parent_request_hash")
        if not isinstance(parent, str) or row.get("request_hash") != result_hash(
            {"kind": "input_token_count", "request_hash": parent}
        ):
            raise ModelRequestBindingError()
    elif row.get("parent_request_hash") is not None:
        raise ModelRequestBindingError()
    return binding


def validate_request_metadata(state: Json) -> None:
    """Reject corrupt persisted guard fields before any budget synchronization or save."""
    requests = state.get("requests", {})
    if not isinstance(requests, dict) or any(not isinstance(row, dict) for row in requests.values()):
        raise ModelRequestBindingError()
    for request_hash, row in requests.items():
        binding = _row_binding(row)
        if binding is not None and (
            binding.workspace_id != state.get("workspace_id") or row.get("request_hash") != request_hash
        ):
            raise ModelRequestBindingError()


def _same_family(left: LogicalRequestBinding, right: LogicalRequestBinding) -> bool:
    return (left.workspace_id, left.source_revision_id, left.family) == (
        right.workspace_id,
        right.source_revision_id,
        right.family,
    )


def _owned_pending(row: Json, request_hash: str, ownership: LogicalOwnership | None) -> bool:
    return bool(
        ownership is not None
        and ownership.request_hash == request_hash
        and row.get("request_hash") == request_hash
        and row.get("dispatched") is False
        and row.get("state") == "pending"
    )


@dataclass(frozen=True, slots=True)
class _Guard:
    request_hash: str
    binding: LogicalRequestBinding
    ownership: LogicalOwnership | None
    kind: RequestKind
    parent_hash: str | None


def _check_parent(requests: Json, guard: _Guard) -> None:
    if guard.kind != RequestKind.COUNT:
        if guard.ownership is not None and guard.ownership.request_hash != guard.request_hash:
            raise ModelRequestBindingError()
        return
    if guard.parent_hash is None:
        raise ModelRequestBindingError()
    parent = requests.get(guard.parent_hash)
    if (
        guard.ownership is None
        or not guard.ownership.allows_parent(guard.parent_hash, guard.request_hash)
        or parent is None
        or parent.get("state") != "pending"
        or parent.get("dispatched") is not False
        or _row_binding(parent) != guard.binding
    ):
        raise ModelRequestBindingError()


def _check_open_row(key: str, row: Json, bound: LogicalRequestBinding | None, guard: _Guard) -> bool:
    if bound is not None and key == guard.request_hash and bound != guard.binding:
        raise ModelRequestBindingError()
    if row.get("state") not in {"pending", "sent", "unknown"}:
        return False
    if bound is None:
        raise ModelOutcomeUnknownError()
    if not _same_family(bound, guard.binding):
        return False
    if bound.plan_sha256 != guard.binding.plan_sha256:
        raise ModelOutcomeUnknownError()
    if bound.unit_id != guard.binding.unit_id:
        return False
    is_unsent = row.get("state") == "pending" and row.get("dispatched") is False
    if _owned_pending(row, guard.request_hash, guard.ownership):
        return True
    if guard.kind == RequestKind.COUNT and is_unsent and key in (guard.request_hash, guard.parent_hash):
        return key == guard.request_hash
    raise ModelOutcomeUnknownError()


def _reject_orphan_reservations(state: Json, requests: Json, binding: LogicalRequestBinding | None) -> None:
    known_ids = {row.get("call_id") for row in requests.values()}
    known_ids.update(attempt for row in requests.values() for attempt in row.get("attempt_ids") or [])
    if any(key not in known_ids for key in state.get("pending", {})):
        if binding is None:
            raise ModelCallError("legacy_model_outcome_unknown", outcome_unknown=True)
        raise ModelOutcomeUnknownError()


def check_logical_request(
    state: Json,
    request_hash: str,
    binding: LogicalRequestBinding | None,
    *,
    ownership: LogicalOwnership | None = None,
    request_kind: RequestKind = RequestKind.GENERATION,
    parent_request_hash: str | None = None,
) -> bool:
    """Validate before every cache/claim; return whether an unsent pending must be reclaimed."""
    requests = state.get("requests", {})
    if not isinstance(requests, dict) or any(not isinstance(row, dict) for row in requests.values()):
        raise ModelRequestBindingError()
    _reject_orphan_reservations(state, requests, binding)
    bound_rows = [(key, row, _row_binding(row)) for key, row in requests.items()]
    if binding is None:
        if any(bound is not None for _, _, bound in bound_rows):
            raise ModelRequestBindingError()
        return False
    binding.encoded()
    if binding.workspace_id != state.get("workspace_id"):
        raise ModelRequestBindingError()
    guard = _Guard(request_hash, binding, ownership, request_kind, parent_request_hash)
    _check_parent(requests, guard)
    reclaim_pending = False
    for key, row, bound in bound_rows:
        reclaim_pending |= _check_open_row(key, row, bound, guard)
    return reclaim_pending


def request_metadata(binding: LogicalRequestBinding | None, kind: RequestKind, parent: str | None = None) -> Json:
    if binding is None:
        return {}
    result: Json = {"logical_binding": binding.encoded(), "request_kind": kind.value}
    if parent is not None:
        result["parent_request_hash"] = parent
    return result
