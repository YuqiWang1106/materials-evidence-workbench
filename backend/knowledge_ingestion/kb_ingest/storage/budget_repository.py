"""Offline access to historical project accounting. Model dispatch does not charge this ledger."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Engine, Select, func, select
from sqlalchemy.orm import Session

from ..core.errors import raise_state_conflict
from .models import ProjectBudget, ProjectCallReservation
from .sqlite import begin_immediate


def legacy_reservations(engine: Engine, workspace_id: str, project_id: str | None) -> list[Mapping[str, Any]]:
    """Read execution evidence from the scoped accounting ledger of an admitted workspace."""
    row = ProjectCallReservation
    query = select(
        row.call_id, row.project_id, row.provider, row.model, row.request_sha256, row.state, row.receipt_id
    ).where(row.workspace_id == workspace_id)
    if project_id is not None:
        query = query.where(row.project_id == project_id)
    with engine.connect() as conn:
        return [dict(value) for value in conn.execute(query).mappings()]


@dataclass(frozen=True)
class Reservation:
    call_id: str
    provider: str
    model: str
    price_version: str
    request_sha256: str
    reserved_micro_usd: int
    parent_call_id: str | None = None


@dataclass(frozen=True)
class Balance:
    cap_micro_usd: int
    settled_micro_usd: int
    reserved_micro_usd: int
    breach_reason: str | None


def _balance_query(workspace_id: str, project_id: str) -> Select[int, int, int, str | None]:
    budget, reservation = ProjectBudget, ProjectCallReservation
    open_amount = select(func.coalesce(func.sum(reservation.reserved_micro_usd), 0)).where(
        reservation.workspace_id == workspace_id,
        reservation.project_id == project_id,
        reservation.state.not_in(("settled", "canceled")),
    )
    return select(
        budget.cap_micro_usd, budget.settled_micro_usd, open_amount.scalar_subquery(), budget.breach_reason
    ).where(budget.workspace_id == workspace_id, budget.project_id == project_id)


def _balance(session: Session, workspace_id: str, budget: ProjectBudget) -> Balance:
    return Balance(*session.execute(_balance_query(workspace_id, budget.project_id)).one())


def _amount(value: int) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        message = "Invalid amount in micro-USD"
        raise ValueError(message)
    return value


class BudgetRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def _write(self) -> Iterator[Session]:
        with Session(self.engine) as session:
            begin_immediate(session)
            yield session
            session.commit()

    def open(self, workspace_id: str, project_id: str, cap_micro_usd: int) -> Balance:
        cap = _amount(cap_micro_usd)
        with self._write() as session:
            budget = session.get(ProjectBudget, (workspace_id, project_id))
            if budget is None:
                budget = ProjectBudget(
                    workspace_id=workspace_id, project_id=project_id, cap_micro_usd=cap, settled_micro_usd=0
                )
                session.add(budget)
            elif budget.cap_micro_usd != cap:
                raise_state_conflict("Budget cap differs from the existing project")
            return _balance(session, workspace_id, budget)

    def read(self, workspace_id: str, project_id: str) -> Balance | None:
        with Session(self.engine) as session:
            row = session.execute(_balance_query(workspace_id, project_id)).one_or_none()
            return Balance(*row) if row is not None else None

    def reserve(self, workspace_id: str, project_id: str, request: Reservation) -> str:
        amount = _amount(request.reserved_micro_usd)
        with self._write() as session:
            budget = session.get(ProjectBudget, (workspace_id, project_id))
            if budget is None:
                raise_state_conflict("Project budget not found")
            values = asdict(request) | {"workspace_id": workspace_id, "project_id": project_id}
            prior = session.get(ProjectCallReservation, (workspace_id, request.call_id))
            if prior is not None:
                if any(getattr(prior, k) != v for k, v in values.items()):
                    raise_state_conflict("Reservation identity conflict")
                return prior.state
            if request.parent_call_id is not None:
                parent = session.get(ProjectCallReservation, (workspace_id, request.parent_call_id))
                if parent is None or parent.project_id != project_id:
                    raise_state_conflict("Parent reservation not found")
            balance = _balance(session, workspace_id, budget)
            if (
                budget.breach_reason
                or balance.settled_micro_usd + balance.reserved_micro_usd + amount > balance.cap_micro_usd
            ):
                raise_state_conflict("Project budget exhausted")
            session.add(ProjectCallReservation(**values, state="reserved"))
            return "reserved"

    def settle(
        self, workspace_id: str, project_id: str, call_id: str, *, actual_micro_usd: int, receipt_id: str
    ) -> Balance:
        actual = _amount(actual_micro_usd)
        if not receipt_id:
            message = "Settlement requires a receipt"
            raise ValueError(message)
        with self._write() as session:
            row = session.get(ProjectCallReservation, (workspace_id, call_id))
            budget = session.get(ProjectBudget, (workspace_id, project_id))
            if row is None or row.project_id != project_id or budget is None:
                raise_state_conflict("Project reservation not found")
            if row.state == "settled":
                if (row.actual_micro_usd, row.receipt_id) != (actual, receipt_id):
                    raise_state_conflict("Settlement receipt conflict")
            elif row.state in ("reserved", "sent", "unknown"):
                budget.settled_micro_usd = _amount(budget.settled_micro_usd + actual)
                if budget.settled_micro_usd > budget.cap_micro_usd:
                    budget.breach_reason = budget.breach_reason or "settled_above_cap"
                row.state, row.actual_micro_usd, row.receipt_id = "settled", actual, receipt_id
                row.unknown_reason = None
            else:
                raise_state_conflict("Reservation is closed")
            return _balance(session, workspace_id, budget)

    def reconcile(self, workspace_id: str, project_id: str) -> int:
        """A stopped dispatcher cannot prove delivery. Retain each unresolved reservation."""
        with self._write() as session:
            pending = session.scalars(
                select(ProjectCallReservation).where(
                    ProjectCallReservation.workspace_id == workspace_id,
                    ProjectCallReservation.project_id == project_id,
                    ProjectCallReservation.state == "sent",
                )
            ).all()
            for row in pending:
                row.state, row.unknown_reason = "unknown", "process_recovery"
            return len(pending)
