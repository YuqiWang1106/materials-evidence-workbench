"""Workspace-owned execution ledger for provider requests."""

import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session

from ..core import clock
from . import sqlite
from .engine import unpooled_engine
from .knowledge_rows import knowledge_row
from .model_result_types import (
    ModelRequestState,
    ModelResultBinding,
    ModelResultProtocol,
    ModelResultRecoveryState,
    NormalizedResult,
    SavedModelResult,
    StoredModelResult,
    StoredResultRecord,
)
from .models import ModelRequest, ModelRequestResult

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ModelRequestError(RuntimeError):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Change:
    state: str
    reason: str | None = None
    result_sha256: str | None = None
    receipt_id: str | None = None
    unsent_only: bool = False


def receipt(row: ModelRequest) -> dict[str, Any]:
    result = knowledge_row(row)
    del result["workspace_id"]
    return result


def complete(row: ModelRequest, change: Change) -> bool:
    if row.state == "completed":
        if row.result_sha256 != change.result_sha256:
            raise ModelRequestError("result_identity_conflict")
        return False
    if row.state not in ("sent", "unknown") or change.result_sha256 is None:
        raise ModelRequestError("invalid_transition")
    row.result_sha256 = change.result_sha256
    row.receipt_id = change.receipt_id
    return True


def transition(row: ModelRequest, change: Change) -> None:
    instant = clock.now()
    if change.state == "sent":
        if row.state != "pending":
            raise ModelRequestError("dispatch_already_claimed")
        row.sent_at = instant
    elif change.state == "completed":
        if not complete(row, change):
            return
    elif change.state == "unknown":
        if row.state not in ("sent", "unknown"):
            raise ModelRequestError("invalid_transition")
    elif change.state == "failed":
        allowed = ("pending",) if change.unsent_only else ("pending", "sent")
        if row.state not in allowed:
            raise ModelRequestError("invalid_transition")
    else:
        raise ModelRequestError("invalid_transition")
    row.unknown_reason = change.reason
    row.state = change.state
    row.updated_at = instant


def _request(session: Session, binding: ModelResultBinding) -> ModelRequest:
    row = session.get(ModelRequest, (binding.workspace_id, binding.call_id))
    if row is None or row.project_id != binding.project_id:
        raise ModelRequestError("call_not_found", 404)
    _request_identity(row, binding)
    return row


def _request_identity(row: ModelRequest, binding: ModelResultBinding) -> None:
    if any(
        getattr(row, key) != getattr(binding, key)
        for key in (
            "provider",
            "service_identity",
            "model",
            "request_sha256",
        )
    ):
        raise ModelRequestError("request_identity_conflict")


def _stored(row: ModelRequestResult) -> StoredModelResult:
    try:
        record = StoredResultRecord(
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            call_id=row.call_id,
            provider=row.provider,
            service_identity=row.service_identity,
            model=row.model,
            request_sha256=row.request_sha256,
            format_version=row.format_version,
            protocol=ModelResultProtocol(row.protocol),
            response=NormalizedResult.model_validate(row.response),
            result_sha256=row.result_sha256,
            result_bytes=row.result_bytes,
            receipt_id=row.receipt_id,
            received_at=row.received_at,
        )
        return record.stored()
    except (ValueError, TypeError, OverflowError):
        # Validation errors may contain provider content; expose only a fixed code.
        raise ModelRequestError("model_result_invalid") from None


def _saved(request: ModelRequest, result: ModelRequestResult, binding: ModelResultBinding) -> SavedModelResult:
    stored = _stored(result)
    if stored.binding != binding:
        raise ModelRequestError("result_identity_conflict")
    if request.state not in ("sent", "unknown", "completed"):
        raise ModelRequestError("model_result_state_conflict")
    if request.state == "completed" and (
        request.result_sha256 != stored.result_sha256 or request.receipt_id != stored.receipt_id
    ):
        raise ModelRequestError("result_identity_conflict")
    recovery = (
        ModelResultRecoveryState.COMPLETED
        if request.state == "completed"
        else ModelResultRecoveryState.PRESERVED_PENDING
    )
    return SavedModelResult(stored, ModelRequestState(request.state), recovery)


def _set_no_wait(connection: Connection) -> None:
    raw_connection = connection.connection.driver_connection
    if not isinstance(raw_connection, sqlite3.Connection):
        raise ModelRequestError("unsupported_database", 400)
    sqlite.set_busy_timeout(raw_connection, 0)


class RequestRepository:
    def __init__(self, engine: Engine) -> None:
        if not sqlite.is_sqlite(engine):
            raise ModelRequestError("unsupported_database", 400)
        self.engine = engine
        self._deadline_engine = unpooled_engine(engine.url) if sqlite.is_file_database(engine) else None

    @contextmanager
    def _session(self, *, write: bool, no_wait: bool = False) -> Iterator[Session]:
        deadline = clock.IO_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise ModelRequestError("request_state_timeout")
        target = self._deadline_engine if deadline is not None or no_wait else self.engine
        if target is None:
            raise ModelRequestError("unsupported_database", 400)
        try:
            connection = target.connect()
        except PoolTimeout:
            raise ModelRequestError("request_state_timeout") from None
        except SQLAlchemyError:
            raise ModelRequestError("request_state_unavailable") from None
        with connection as conn:
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ModelRequestError("request_state_timeout")
                with sqlite.query_deadline(conn, deadline), Session(bind=conn) as session:
                    if no_wait:
                        _set_no_wait(conn)
                    if write:
                        sqlite.begin_immediate(session)
                    yield session
                    session.commit()
            except SQLAlchemyError:
                code = "request_state_unavailable"
                if deadline is not None and time.monotonic() >= deadline - 0.005:
                    code = "request_state_timeout"
                raise ModelRequestError(code) from None

    def read(self, workspace_id: str, project_id: str, call_id: str) -> dict[str, Any] | None:
        with self._session(write=False) as session:
            row = session.get(ModelRequest, (workspace_id, call_id))
            return receipt(row) if row is not None and row.project_id == project_id else None

    def prepare(self, workspace_id: str, values: dict[str, str]) -> dict[str, Any]:
        with self._session(write=True) as session:
            row = session.get(ModelRequest, (workspace_id, values["call_id"]))
            if row is not None:
                if any(getattr(row, key) != value for key, value in values.items()):
                    raise ModelRequestError("request_identity_conflict")
            else:
                now = clock.now()
                row = ModelRequest(workspace_id=workspace_id, **values, state="pending", created_at=now, updated_at=now)
                session.add(row)
                session.flush()
            return receipt(row)

    def change(self, workspace_id: str, project_id: str, call_id: str, change: Change) -> dict[str, Any]:
        with self._session(write=True) as session:
            row = session.get(ModelRequest, (workspace_id, call_id))
            if row is None or row.project_id != project_id:
                raise ModelRequestError("call_not_found", 404)
            transition(row, change)
            session.flush()
            return receipt(row)

    def release_proven_unsent(self, binding: ModelResultBinding, sent_at: float) -> None:
        """Release only this exact live claim when its owner has proved transport did not start."""
        with self._session(write=True, no_wait=True) as session:
            row = _request(session, binding)
            if (
                row.state != "sent"
                or row.sent_at is None
                or row.sent_at.timestamp() != sent_at
                or row.updated_at != row.sent_at
                or row.result_sha256 is not None
                or row.receipt_id is not None
                or session.get(ModelRequestResult, (binding.workspace_id, binding.call_id)) is not None
            ):
                raise ModelRequestError("request_identity_conflict")
            row.state = "pending"
            row.sent_at = None
            row.unknown_reason = "deadline_before_transport"
            row.updated_at = clock.now()
            session.flush()

    def unsent_release_confirmed(self, binding: ModelResultBinding) -> bool:
        with self._session(write=False, no_wait=True) as session:
            row = _request(session, binding)
            return (
                row.state == "pending"
                and row.sent_at is None
                and row.unknown_reason == "deadline_before_transport"
                and row.result_sha256 is None
                and row.receipt_id is None
                and session.get(ModelRequestResult, (binding.workspace_id, binding.call_id)) is None
            )

    def read_result(self, binding: ModelResultBinding) -> SavedModelResult | None:
        with self._session(write=False) as session:
            # A single SELECT gives the request and body the same SQLite snapshot.
            query = (
                select(ModelRequest, ModelRequestResult)
                .outerjoin(
                    ModelRequestResult,
                    and_(
                        ModelRequest.workspace_id == ModelRequestResult.workspace_id,
                        ModelRequest.call_id == ModelRequestResult.call_id,
                    ),
                )
                .where(
                    ModelRequest.workspace_id == binding.workspace_id,
                    ModelRequest.call_id == binding.call_id,
                    ModelRequest.project_id == binding.project_id,
                )
            )
            try:
                pair = session.execute(query).one_or_none()
            except (ValueError, TypeError):
                raise ModelRequestError("model_result_invalid") from None
            if pair is None:
                raise ModelRequestError("call_not_found", 404)
            request, result = pair
            _request_identity(request, binding)
            return None if result is None else _saved(request, result, binding)

    def save_result(self, value: StoredModelResult) -> StoredModelResult:
        binding = value.binding
        with self._session(write=True) as session:
            request = _request(session, binding)
            existing = session.get(ModelRequestResult, (binding.workspace_id, binding.call_id))
            if existing is not None:
                previous = _saved(request, existing, binding).stored
                if replace(value, received_at=previous.received_at) != previous:
                    raise ModelRequestError("result_identity_conflict")
                return previous
            if request.state not in ("sent", "unknown"):
                raise ModelRequestError("model_result_state_conflict")
            result = ModelRequestResult(
                workspace_id=binding.workspace_id,
                project_id=binding.project_id,
                call_id=binding.call_id,
                provider=binding.provider,
                service_identity=binding.service_identity,
                model=binding.model,
                request_sha256=binding.request_sha256,
                format_version=value.format_version,
                protocol=value.protocol.value,
                response=value.response(),
                result_sha256=value.result_sha256,
                result_bytes=value.result_bytes,
                received_at=value.received_at,
                receipt_id=value.receipt_id,
            )
            # The independent transaction must commit the body before the ledger can complete.
            session.add(result)
            session.flush()
            return _stored(result)

    def reconcile_result(self, binding: ModelResultBinding) -> SavedModelResult:
        with self._session(write=True) as session:
            request = _request(session, binding)
            result = session.get(ModelRequestResult, (binding.workspace_id, binding.call_id))
            if result is None:
                raise ModelRequestError("model_result_missing")
            saved = _saved(request, result, binding)
            transition(
                request,
                Change("completed", result_sha256=saved.stored.result_sha256, receipt_id=saved.stored.receipt_id),
            )
            session.flush()
            return _saved(request, result, binding)

    def import_reservations(self, workspace_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        count = 0
        with self._session(write=True) as session:
            for old in rows:
                row = session.get(ModelRequest, (workspace_id, old["call_id"]))
                if row is not None:
                    if any(
                        getattr(row, key) != old[key] for key in ("project_id", "provider", "model", "request_sha256")
                    ):
                        raise ModelRequestError("legacy_request_conflict")
                    continue
                state = {"reserved": "pending", "settled": "completed", "canceled": "failed"}.get(
                    old["state"], "unknown"
                )
                session.add(
                    ModelRequest(
                        workspace_id=workspace_id,
                        call_id=old["call_id"],
                        project_id=old["project_id"],
                        provider=old["provider"],
                        model=old["model"],
                        service_identity="legacy:" + old["provider"],
                        request_sha256=old["request_sha256"],
                        state=state,
                        receipt_id=old["receipt_id"],
                        unknown_reason="legacy_process_recovery" if state == "unknown" else None,
                        created_at=_EPOCH,
                        updated_at=_EPOCH,
                        sent_at=_EPOCH if old["state"] in ("sent", "settled", "unknown") else None,
                    )
                )
                count += 1
        return count
