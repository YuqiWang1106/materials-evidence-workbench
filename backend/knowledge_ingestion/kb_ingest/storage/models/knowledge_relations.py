"""Rebuildable published Wiki-to-anchor projection; never evidence authority."""

from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relation_projection"
    __table_args__ = (
        Index("ix_relation_anchor", "workspace_id", "projection_version", "relation_kind", "anchor_key"),
        Index("ix_relation_page", "workspace_id", "page_id", "revision_id"),
    )
    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)
    page_id: Mapped[str] = mapped_column(String)
    revision_id: Mapped[str] = mapped_column(ForeignKey("knowledge_revisions.revision_id"))
    content_hash: Mapped[str] = mapped_column(String)
    dependency_manifest_hash: Mapped[str] = mapped_column(String)
    projection_version: Mapped[str] = mapped_column(String)
    relation_kind: Mapped[str] = mapped_column(String)
    anchor_key: Mapped[str] = mapped_column(String)
    target_binding: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    basis: Mapped[dict[str, Any]] = mapped_column(JSON)
