"""Task allowances, durable request ownership and restart reconciliation."""

import copy
import math
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Unpack

from . import budget_state
from .errors import BudgetExceeded, ModelCallError, ModelRequestBindingError
from .identity import Json
from .ledger import FileScope, read_scoped_attempts
from .logical_request import (
    BudgetCreationPermit,
    BudgetOpenEvidence,
    BudgetOpenMode,
    LogicalOwnership,
    LogicalRequestBinding,
    check_logical_request,
    validate_request_metadata,
)
from .reconcile import recover
from .request_state import Claim, ClaimOptions, claim_request


@dataclass
class ModelBudget:
    """Task-wide budget, optionally durable across processes and restarts.

    Reservations are persisted before HTTP dispatch. A crashed/unknown attempt
    keeps its full reservation; recreating a client never refunds it. Reopening
    may tighten limits, but cannot silently raise the original task limits.
    """

    max_calls: int = 40
    max_output_tokens: int = 240000
    max_total_tokens: int = 1200000
    max_wall_seconds: float = 3600
    active_time: bool = False
    budget_state_path: Path | str | None = None
    calls: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    _output_reserved: int = 0
    _total_reserved: int = 0
    _started_at_epoch: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _memory_state: Json = field(default_factory=dict, repr=False)
    _request_locks_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _request_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)

    open_evidence: BudgetOpenEvidence | None = field(default=None, kw_only=True, repr=False)
    creation_permit: BudgetCreationPermit | None = field(default=None, kw_only=True, repr=False)
    _has_opened: bool = field(default=False, init=False, repr=False)
    _guarded: bool = field(default=False, init=False, repr=False)
    _logical_owners: set[LogicalOwnership] = field(default_factory=set, init=False, repr=False)

    workspace_id: str = field(default="local", kw_only=True)
    workspace_root: Path | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if any(v < 1 for v in (self.max_calls, self.max_output_tokens, self.max_total_tokens, self.max_wall_seconds)):
            message = "budget limits must be positive"
            raise ValueError(message)
        self.scope = FileScope(self.workspace_id, self.workspace_root)
        self.workspace_root = self.scope.workspace_root
        self.budget_state_path = self.scope.path(self.budget_state_path)
        with self._transaction() as state:
            # This also persists first-use limits before the first request.
            self._sync(state)

    def _initial_state(self) -> Json:
        return {
            "version": 1,
            "workspace_id": self.workspace_id,
            "started_at_epoch": self._started_at_epoch,
            "limits": {
                k: getattr(self, k) for k in ("max_calls", "max_output_tokens", "max_total_tokens", "max_wall_seconds")
            },
            "calls": self.calls,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "pending": {},
            "settled": {},
            "active_time": self.active_time,
            "active_seconds": 0.0,
        }

    def _sync(self, state: Json) -> None:
        state["workspace_id"] = self.workspace_id
        if state.get("active_time", False) != self.active_time:
            message = "task time accounting mode cannot change on resume"
            raise ModelCallError(message)
        for key in ("max_calls", "max_output_tokens", "max_total_tokens", "max_wall_seconds"):
            state["limits"][key] = min(getattr(self, key), state["limits"][key])
            setattr(self, key, state["limits"][key])
        self.calls, self.output_tokens, self.total_tokens = (
            state[k] for k in ("calls", "output_tokens", "total_tokens")
        )
        self._output_reserved = sum(r["output"] for r in state["pending"].values())
        self._total_reserved = sum(r["total"] for r in state["pending"].values())
        self._started_at_epoch = state["started_at_epoch"]

    @contextmanager
    def _transaction(self) -> Iterator[Json]:
        with self._lock, self._snapshot_lock() as path:
            state = self._load_state(path)
            self._sync(state)
            yield state
            validate_request_metadata(state)
            self._sync(state)
            if path:
                budget_state.save(path, state)
            else:
                self._memory_state = state

    def _load_state(self, path: Path | None) -> Json:
        evidence = self.open_evidence
        if evidence is not None and (
            path is None or path != self.scope.path(evidence.owner_path) or evidence.workspace_id != self.workspace_id
        ):
            raise ModelRequestBindingError()
        if path and path.exists():
            try:
                state = budget_state.load(path, self.scope)
            except ModelCallError:
                if evidence is not None or self._guarded:
                    raise ModelRequestBindingError() from None
                raise
        elif not path and self._memory_state:
            state = copy.deepcopy(self._memory_state)
        else:
            if self._has_opened and (evidence is not None or self._guarded):
                raise ModelRequestBindingError()
            state = self._initial_state()
            if evidence is not None:
                self._authorize_initial(state, evidence)
        validate_request_metadata(state)
        self._has_opened = True
        self._guarded |= evidence is not None or any(
            "logical_binding" in row for row in state.get("requests", {}).values()
        )
        return state

    def _authorize_initial(self, state: Json, evidence: BudgetOpenEvidence) -> None:
        if evidence.mode == BudgetOpenMode.INIT_NEW:
            if self.creation_permit is None:
                raise ModelRequestBindingError()
            self.creation_permit.consume(evidence)
            return
        if evidence.mode != BudgetOpenMode.RECOVER_LEDGER or not evidence.ledger_paths:
            raise ModelRequestBindingError()
        # The trusted host supplies the complete original ledger set; never infer it from the current run.
        for path in evidence.ledger_paths:
            self._recover_initial_ledger(state, path, evidence)

    def _recover_initial_ledger(self, state: Json, path: Path, evidence: BudgetOpenEvidence) -> None:
        target = self.scope.path(path)
        if target is None or not target.is_file():
            raise ModelRequestBindingError()
        rows = list(read_scoped_attempts(target, self.scope))
        if not rows:
            raise ModelRequestBindingError()
        for row in rows:
            if "logical_binding" in row:
                binding = LogicalRequestBinding.read(row["logical_binding"])
                if binding.source_revision_id != evidence.source_revision_id:
                    raise ModelRequestBindingError()
            attempt_id = row.get("attempt_id")
            if not isinstance(attempt_id, str):
                raise ModelRequestBindingError()
            recover(state, attempt_id, row)

    @contextmanager
    def _request_snapshot(self) -> Iterator[Json]:
        # A cache check must not rewrite limits, counters or execution records.
        with self._lock, self._snapshot_lock() as path:
            if path and path.exists():
                try:
                    state = budget_state.load(path, self.scope)
                except ModelCallError:
                    raise ModelRequestBindingError() from None
            elif path or not self._memory_state:
                raise ModelRequestBindingError()
            else:
                state = copy.deepcopy(self._memory_state)
            yield state

    @contextmanager
    def logical_lease(
        self, request_hash: str, binding: LogicalRequestBinding | None, legacy_key: str
    ) -> Iterator[LogicalOwnership]:
        with self.request_lease(binding.lease_key() if binding else legacy_key):
            ownership = LogicalOwnership(self, binding, request_hash)
            self._logical_owners.add(ownership)
            try:
                yield ownership
            finally:
                ownership.close()
                self._logical_owners.remove(ownership)

    def verify_ownership(self, ownership: LogicalOwnership, binding: LogicalRequestBinding | None) -> None:
        ownership.verify(self, binding)
        if ownership not in self._logical_owners:
            raise ModelRequestBindingError()

    def check_request_snapshot(
        self, state: Json, request_hash: str, binding: LogicalRequestBinding | None, ownership: LogicalOwnership
    ) -> bool:
        self.verify_ownership(ownership, binding)
        if ownership.request_hash != request_hash:
            raise ModelRequestBindingError()
        return check_logical_request(state, request_hash, binding, ownership=ownership)

    @contextmanager
    def _snapshot_lock(self) -> Iterator[Path | None]:
        path = Path(self.budget_state_path) if self.budget_state_path is not None else None
        with budget_state.locked(path, self.scope) if path else nullcontext():
            yield path

    def bind_ledger(self, ledger: Path) -> None:
        """An attempts-only client needs a cross-process lease and durable result bindings too."""
        with self._request_locks_guard, self._lock:
            if self.budget_state_path is not None:
                return
            if self._request_locks:
                message = "cannot change task budget persistence during an active request"
                raise ModelCallError(message)
            path = self.scope.path(ledger.with_suffix(ledger.suffix + ".budget.json"))
            if path is None:
                message = "attempt ledger path is required"
                raise ValueError(message)
            with budget_state.locked(path, self.scope):
                if path.exists():
                    state = budget_state.load(path, self.scope)
                    if self._memory_state.get("calls") and self._memory_state != state:
                        message = "existing budget conflicts with in-memory task usage"
                        raise ModelCallError(message)
                else:
                    state = self._memory_state or self._initial_state()
                self._sync(state)
                budget_state.save(path, state)
                self.budget_state_path = path

    def execution(self, request_hash: str) -> Json | None:
        with self._transaction() as state:
            execution: Json | None = state.get("requests", {}).get(request_hash)
            return execution

    def _remaining(self, state: Json) -> float:
        elapsed = (
            state.get("active_seconds", 0) + sum(row.get("seconds", 0) for row in state["pending"].values())
            if self.active_time
            else max(0, time.time() - self._started_at_epoch)
        )
        return max(0.0, float(self.max_wall_seconds - elapsed))

    def remaining_seconds(self) -> float:
        with self._transaction() as state:
            return self._remaining(state)

    def reserve(
        self, output: int, input_upper_bound: int, *, reservation_id: str | None = None, seconds: float | None = None
    ) -> int:
        if output < 1 or input_upper_bound < 0:
            message = "invalid token reservation"
            raise ValueError(message)
        total = output + input_upper_bound
        reservation_id = reservation_id or str(uuid.uuid4())
        with self._transaction() as state:
            if reservation_id in state["pending"] or reservation_id in state["settled"]:
                message = "attempt identity already reserved; automatic redispatch refused"
                raise BudgetExceeded(message)
            remaining = self._remaining(state)
            if (
                self.calls >= self.max_calls
                or remaining <= 0
                or self.output_tokens + self._output_reserved + output > self.max_output_tokens
                or self.total_tokens + self._total_reserved + total > self.max_total_tokens
            ):
                message = "model task budget exhausted; remaining scope not processed"
                raise BudgetExceeded(message)
            state["calls"] += 1
            reserved_seconds = remaining if seconds is None else seconds
            if not math.isfinite(reserved_seconds) or reserved_seconds <= 0 or reserved_seconds > remaining:
                message = "model time reservation exceeds remaining allowance"
                raise BudgetExceeded(message)
            state["pending"][reservation_id] = {
                "output": output,
                "total": total,
                "started_at_epoch": time.time(),
                "seconds": reserved_seconds,
            }
        return total

    def reserve_count_request(self, *, reservation_id: str, seconds: float | None = None) -> None:
        """Charge one request and its time without inventing token usage."""
        with self._transaction() as state:
            if reservation_id in state["pending"] or reservation_id in state["settled"]:
                message = "attempt identity already reserved; automatic redispatch refused"
                raise BudgetExceeded(message)
            remaining = self._remaining(state)
            if self.calls >= self.max_calls or remaining <= 0:
                message = "model task budget exhausted; remaining scope not processed"
                raise BudgetExceeded(message)
            reserved_seconds = remaining if seconds is None else seconds
            if not math.isfinite(reserved_seconds) or reserved_seconds <= 0 or reserved_seconds > remaining:
                message = "model time reservation exceeds remaining allowance"
                raise BudgetExceeded(message)
            state["calls"] += 1
            state["pending"][reservation_id] = {
                "output": 0,
                "total": 0,
                "started_at_epoch": time.time(),
                "seconds": reserved_seconds,
                "request_kind": "input_token_count",
            }

    def finish(
        self,
        output_reserved: int,
        total_reserved: int,
        usage: Json | None,
        *,
        reservation_id: str | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        with self._transaction() as state:
            if reservation_id in state["settled"]:
                return
            if reservation_id is None:
                reservation_id = next(
                    (
                        key
                        for key, row in state["pending"].items()
                        if row["output"] == output_reserved and row["total"] == total_reserved
                    ),
                    None,
                )
            if reservation_id not in state["pending"]:
                message = "unknown task budget reservation; usage not reset"
                raise ModelCallError(message)
            row = state["pending"].pop(reservation_id)
            elapsed = elapsed_seconds if elapsed_seconds is not None else max(0, time.time() - row["started_at_epoch"])
            if not math.isfinite(elapsed) or elapsed < 0:
                message = "invalid active model duration"
                raise ValueError(message)
            if self.active_time:
                state["active_seconds"] += elapsed
            usage = usage if isinstance(usage, dict) else {}
            output, total = usage.get("output_tokens"), usage.get("total_tokens")
            output = output if type(output) is int and output >= 0 else row["output"]
            total = total if type(total) is int and total >= output else row["total"]
            state["output_tokens"] += output
            state["total_tokens"] += total
            state["settled"][reservation_id] = {
                "output": output,
                "total": total,
                "seconds": elapsed,
                "usage_status": "count_usage_unavailable"
                if row.get("request_kind") == "input_token_count"
                else "reported"
                if usage
                else "unknown_reserved_ceiling",
            }

    def snapshot(self) -> Json:
        with self._transaction() as state:
            return {
                "calls": self.calls,
                "output_tokens_or_reserved": self.output_tokens + self._output_reserved,
                "total_tokens_or_reserved": self.total_tokens + self._total_reserved,
                "pending_reservations": len(state["pending"]),
                "active_seconds_or_reserved": state.get("active_seconds", 0)
                + sum(r.get("seconds", 0) for r in state["pending"].values()),
                "remaining_seconds": self._remaining(state),
                "active_time": self.active_time,
                "started_at_epoch": self._started_at_epoch,
            }

    def reconcile_ledger(self, path: Path) -> None:
        """Import pre-existing attempts once; missing usage is never a refund.

        Legacy rows without a reservation ceiling conservatively consume a full
        configured task ceiling. An operator must establish their actual usage
        separately instead of obtaining a new allowance by recreating a file.
        """
        try:
            target = self.scope.path(path)
            if target is None:
                return
            rows = read_scoped_attempts(target, self.scope)
            attempts: dict[str, Json] = {}
            for row in rows:
                if row.get("attempt_id"):
                    attempts.setdefault(row["attempt_id"], {}).update(row)
        except (ValueError, AttributeError):
            message = "attempt ledger invalid; refusing to reset accumulated usage"
            raise ModelCallError(message) from None
        with self._transaction() as state:
            for attempt_id, row in attempts.items():
                recover(state, attempt_id, row)

    @contextmanager
    def request_lease(self, request_hash: str) -> Iterator[None]:
        """A dead process releases its lease, proving a pending row has no sender."""
        stream = None
        request_lock = None
        with self._request_locks_guard:
            state_path = self.budget_state_path
            if state_path is None:
                request_lock = self._request_locks.setdefault(request_hash, threading.Lock())
                if not request_lock.acquire(blocking=False):
                    raise ModelCallError("model_request_in_progress", outcome_unknown=True)
        if state_path:
            import fcntl

            path = Path(state_path).with_name(Path(state_path).name + "." + request_hash + ".request.lock")
            self.scope.guard(path, self.scope.workspace_root)
            stream = path.open("a+b")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                raise ModelCallError("model_request_in_progress", outcome_unknown=True) from None
        try:
            yield
        finally:
            if stream is not None:
                stream.close()
            elif request_lock is not None:
                with self._request_locks_guard:
                    request_lock.release()
                    del self._request_locks[request_hash]

    def claim_request(
        self, request_hash: str, *, model: str, service_identity: str, **options: Unpack[ClaimOptions]
    ) -> tuple[str, Json | None]:
        binding, ownership = options.get("logical_binding"), options.get("ownership")
        if binding is not None:
            if ownership is None:
                raise ModelRequestBindingError()
            self.verify_ownership(ownership, binding)
        with self._transaction() as state:
            return claim_request(state, request_hash, Claim(model, service_identity, **options))

    def register_attempt(self, request_hash: str, attempt_id: str) -> None:
        """Record a retry's reservation id on its request, so it counts as a known attempt."""
        with self._transaction() as state:
            row = state["requests"][request_hash]
            ids = row.setdefault("attempt_ids", [])
            if attempt_id not in ids:
                ids.append(attempt_id)

    def update_request(self, request_hash: str, **values: Any) -> None:
        with self._transaction() as state:
            row = state["requests"][request_hash]
            if row["state"] == "completed" and values.get("state") != "completed":
                return
            row.update(values)
