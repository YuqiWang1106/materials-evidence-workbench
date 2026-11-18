"""Table draft dependencies owned by the same knowledge page and revision."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import KnowledgeDependency, KnowledgeItem, KnowledgeRevision


def for_source(session: Session, workspace_id: str, source_id: str) -> Sequence[KnowledgeDependency]:
    query = (
        select(KnowledgeDependency)
        .join(KnowledgeRevision)
        .join(KnowledgeItem)
        .where(
            KnowledgeRevision.workspace_id == workspace_id,
            KnowledgeItem.workspace_id == workspace_id,
            KnowledgeDependency.dependency_kind == "raw_table",
            KnowledgeDependency.payload["ref"]["source_id"].as_string() == source_id,
        )
    )
    return session.scalars(query).all()
