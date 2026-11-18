"""Workspace entities, their source-backed attributes, and one undirected related link."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kb_ingest.storage.types import UtcDateTime

from .base import Base


class KbEntity(Base):
    __tablename__ = "kb_entity"
    __table_args__ = (UniqueConstraint("workspace_id", "source_id", "sample_name", name="uq_kb_entity_formulation"),)
    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    entity_type: Mapped[str] = mapped_column(String, index=True)
    canonical_name: Mapped[str] = mapped_column(String)
    aliases: Mapped[list[Any]] = mapped_column(JSON)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sample_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class KbAttribute(Base):
    __tablename__ = "kb_attribute"
    attribute_id: Mapped[str] = mapped_column(String, primary_key=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("kb_entity.entity_id"), index=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    section: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    asset_ref: Mapped[str] = mapped_column(String)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_id: Mapped[str] = mapped_column(String)
    quote: Mapped[str] = mapped_column(String)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    highlight_regions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    claim_group: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Other places, in this or other sources, that say the same: [{source_id, asset_ref, page, block_id, quote, ...}].
    citations: Mapped[list[Any]] = mapped_column(JSON, default=list, server_default="[]")


class KbRelated(Base):
    __tablename__ = "kb_related"
    __table_args__ = (UniqueConstraint("workspace_id", "entity_a", "entity_b", name="uq_kb_related_pair"),)
    related_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    entity_a: Mapped[str] = mapped_column(ForeignKey("kb_entity.entity_id"), index=True)
    entity_b: Mapped[str] = mapped_column(ForeignKey("kb_entity.entity_id"), index=True)
    locations: Mapped[list[Any]] = mapped_column(JSON)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
