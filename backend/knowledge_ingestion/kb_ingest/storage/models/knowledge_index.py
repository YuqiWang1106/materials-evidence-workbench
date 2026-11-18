"""Disposable lexical generations on the existing coordination metadata; no runtime DDL."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kb_ingest.storage.types import UtcDateTime

from .base import Base


class KnowledgeIndexGeneration(Base):
    __tablename__ = "knowledge_index_generations"
    __table_args__ = (UniqueConstraint("scope_key", "request_id"),)
    generation_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String, index=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    source_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_manifest_hash: Mapped[str] = mapped_column(String)
    tokenizer_version: Mapped[str] = mapped_column(String)
    alias_version: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    entry_count: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class KnowledgeIndexEntry(Base):
    __tablename__ = "knowledge_index_entries"
    __table_args__ = (UniqueConstraint("generation_id", "entry_key"),)
    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[str] = mapped_column(ForeignKey("knowledge_index_generations.generation_id"), index=True)
    entry_key: Mapped[str] = mapped_column(String)
    scope_key: Mapped[str] = mapped_column(String)
    workspace_id: Mapped[str] = mapped_column(String)
    layer: Mapped[str] = mapped_column(String)
    collection_id: Mapped[str] = mapped_column(String)
    use_policy: Mapped[str] = mapped_column(String)
    corpus_snapshot_id: Mapped[str] = mapped_column(String)
    ref: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_identity: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_kind: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String)
    document_hash: Mapped[str] = mapped_column(String)
    tokenizer_version: Mapped[str] = mapped_column(String)
    alias_version: Mapped[str] = mapped_column(String)


class KnowledgeIndexCurrent(Base):
    __tablename__ = "knowledge_index_current"
    scope_key: Mapped[str] = mapped_column(String, primary_key=True)
    generation_id: Mapped[str] = mapped_column(ForeignKey("knowledge_index_generations.generation_id"))
