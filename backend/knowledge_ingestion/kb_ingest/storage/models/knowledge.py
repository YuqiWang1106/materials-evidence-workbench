"""Eight shared knowledge tables on the ingestion SQLAlchemy metadata."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kb_ingest.storage.types import UtcDateTime

from .base import Base


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"
    page_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    item_kind: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    current_revision: Mapped[int] = mapped_column(Integer)
    current_revision_id: Mapped[str] = mapped_column(String)
    published_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    availability: Mapped[str] = mapped_column(String)
    availability_reason: Mapped[list[Any]] = mapped_column(JSON)
    epoch: Mapped[str] = mapped_column(String)
    last_event_seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (UniqueConstraint("page_id", "revision"), UniqueConstraint("workspace_id", "request_id"))
    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)
    page_id: Mapped[str] = mapped_column(ForeignKey("knowledge_items.page_id"))
    revision: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String)
    dependency_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    dependency_manifest_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class KnowledgeSupportRow(Base):
    __tablename__ = "knowledge_supports"
    revision_id: Mapped[str] = mapped_column(ForeignKey("knowledge_revisions.revision_id"), primary_key=True)
    support_id: Mapped[str] = mapped_column(String, primary_key=True)
    ref_kind: Mapped[str] = mapped_column(String)
    ref_hash: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class KnowledgeDependency(Base):
    __tablename__ = "knowledge_dependencies"
    revision_id: Mapped[str] = mapped_column(ForeignKey("knowledge_revisions.revision_id"), primary_key=True)
    dependency_id: Mapped[str] = mapped_column(String, primary_key=True)
    dependency_kind: Mapped[str] = mapped_column(String)
    dependency_hash: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class KnowledgeReviewEvent(Base):
    __tablename__ = "knowledge_review_events"
    __table_args__ = (UniqueConstraint("workspace_id", "request_id"),)
    review_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)
    page_id: Mapped[str] = mapped_column(ForeignKey("knowledge_items.page_id"))
    revision_id: Mapped[str] = mapped_column(ForeignKey("knowledge_revisions.revision_id"))
    request_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    dependency_manifest_hash: Mapped[str] = mapped_column(String)
    decision: Mapped[str] = mapped_column(String)
    reviewer: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class KnowledgeEvent(Base):
    __tablename__ = "knowledge_event_outbox"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, unique=True)
    workspace_id: Mapped[str] = mapped_column(String)
    page_id: Mapped[str] = mapped_column(String, index=True)
    epoch: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class KnowledgeEventConsumer(Base):
    __tablename__ = "knowledge_event_consumers"
    consumer_id: Mapped[str] = mapped_column(String, primary_key=True)
    epoch: Mapped[str] = mapped_column(String)
    last_sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)


class KnowledgePublication(Base):
    __tablename__ = "knowledge_publications"
    __table_args__ = (UniqueConstraint("workspace_id", "request_id"),)
    publication_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String)
    page_id: Mapped[str] = mapped_column(ForeignKey("knowledge_items.page_id"))
    revision_id: Mapped[str] = mapped_column(ForeignKey("knowledge_revisions.revision_id"))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String)
    dependency_manifest_hash: Mapped[str] = mapped_column(String)
    review_id: Mapped[str] = mapped_column(String)
    request_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    package: Mapped[dict[str, Any]] = mapped_column(JSON)
    asset_hashes: Mapped[dict[str, Any]] = mapped_column(JSON)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    epoch: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
