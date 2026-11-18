"""Workspace-scoped persistence of Wiki embedding vectors and recovery receipts."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kb_ingest.core import clock

from .knowledge_rows import knowledge_row
from .models import WikiEmbedding


def _require(session: Session, workspace_id: str, key: str) -> WikiEmbedding:
    row = session.get(WikiEmbedding, (workspace_id, key))
    if row is None:
        raise KeyError(key)
    return row


def read(session: Session, workspace_id: str, key: str) -> dict[str, Any]:
    return knowledge_row(_require(session, workspace_id, key))


def prepare(session: Session, workspace_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    row = session.get(WikiEmbedding, (workspace_id, binding["cache_key"]))
    if row is None:
        now = clock.now()
        row = WikiEmbedding(**binding, workspace_id=workspace_id, state="pending", created_at=now, updated_at=now)
        session.add(row)
        session.flush()
    return knowledge_row(row)


def mark_ready(session: Session, workspace_id: str, key: str) -> None:
    row = _require(session, workspace_id, key)
    row.state = "ready"
    row.updated_at = clock.now()


def mark_unknown(session: Session, workspace_id: str, key: str) -> None:
    row = _require(session, workspace_id, key)
    if row.state == "pending":
        row.state = "unknown"
        row.updated_at = clock.now()


def receive(session: Session, workspace_id: str, key: str, vector: tuple[bytes, str], receipt: dict[str, Any]) -> None:
    row = _require(session, workspace_id, key)
    row.state = "received"
    row.vector_blob, row.vector_sha256 = vector
    row.receipt = receipt
    row.updated_at = clock.now()


def ready(
    session: Session, workspace_id: str, keys: list[str], project_id: str, encoding: tuple[str, int, str]
) -> Iterable[tuple[str, str | None]]:
    model, dimensions, version = encoding
    query = select(WikiEmbedding.cache_key, WikiEmbedding.vector_sha256).where(
        WikiEmbedding.workspace_id == workspace_id,
        WikiEmbedding.cache_key.in_(keys),
        WikiEmbedding.project_id == project_id,
        WikiEmbedding.model == model,
        WikiEmbedding.dimensions == dimensions,
        WikiEmbedding.input_version == version,
        WikiEmbedding.state == "ready",
    )
    return session.execute(query).tuples().all()


def blocked(session: Session, workspace_id: str, keys: list[str], project_id: str) -> Iterable[str]:
    return session.scalars(
        select(WikiEmbedding.cache_key).where(
            WikiEmbedding.workspace_id == workspace_id,
            WikiEmbedding.cache_key.in_(keys),
            WikiEmbedding.project_id == project_id,
            WikiEmbedding.state == "unknown",
        )
    ).all()


def unfinished(session: Session, workspace_id: str, project_id: str, model: str, version: str) -> list[str]:
    return list(
        session.scalars(
            select(WikiEmbedding.cache_key).where(
                WikiEmbedding.workspace_id == workspace_id,
                WikiEmbedding.project_id == project_id,
                WikiEmbedding.model == model,
                WikiEmbedding.input_version == version,
                WikiEmbedding.state.in_(["pending", "received"]),
            )
        )
    )
