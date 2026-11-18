"""Read original UTC instants for verified snapshots of the execution ledger."""

from collections.abc import Iterable
from itertools import batched

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from .models import ModelRequest

type Snapshot = dict[str, object]

_LATER_STATES = {
    "pending": {"sent", "unknown", "completed", "failed"},
    "sent": {"unknown", "completed", "failed"},
    "unknown": {"completed"},
    "completed": set(),
    "failed": set(),
}


def ambiguous_epoch(value: object) -> bool:
    # At 2**33 seconds adjacent microseconds first share a binary64 representation.
    return isinstance(value, (float, int)) and abs(value) >= 2**33


def _matches(snapshot: Snapshot, row: ModelRequest) -> bool:
    if not all(snapshot.get(key) == getattr(row, key) for key in ("project_id", "request_sha256", "model")):
        return False
    previous = snapshot.get("ledger_state")
    if not isinstance(previous, str) or previous not in _LATER_STATES:
        return False
    same = previous == row.state
    if not same and row.state not in _LATER_STATES[previous]:
        return False
    return all(
        key in snapshot and (snapshot[key] == getattr(row, key) or (not same and snapshot[key] is None))
        for key in ("receipt_id", "result_sha256")
    )


def _restore(snapshot: Snapshot, row: ModelRequest, fields: tuple[str, ...]) -> None:
    if not _matches(snapshot, row):
        return
    for key in fields:
        instant = getattr(row, key)
        if key == "updated_at":
            # Pending and sent have immutable transition clocks. A repeated unknown has none.
            state = snapshot.get("state")
            if state != snapshot.get("ledger_state") or state == "unknown":
                continue
            if state == "pending":
                instant = row.created_at
            elif state == "sent":
                instant = row.sent_at
        if instant is not None and ambiguous_epoch(snapshot.get(key)) and snapshot[key] == instant.timestamp():
            snapshot[key] = instant


def restore_receipt_times(
    engine: Engine, workspace_id: str, snapshots: Iterable[Snapshot], fields: tuple[str, ...]
) -> None:
    by_call: dict[str, list[Snapshot]] = {}
    for snapshot in snapshots:
        call_id = snapshot.get("call_id")
        if isinstance(call_id, str):
            by_call.setdefault(call_id, []).append(snapshot)
    with Session(engine) as session:
        # sqlite3 legacy mode does not start a read transaction on a SELECT.
        session.execute(text("BEGIN"))
        for ids in batched(by_call, 500):
            query = select(ModelRequest).where(ModelRequest.workspace_id == workspace_id, ModelRequest.call_id.in_(ids))
            for row in session.scalars(query):
                for snapshot in by_call[row.call_id]:
                    _restore(snapshot, row, fields)
