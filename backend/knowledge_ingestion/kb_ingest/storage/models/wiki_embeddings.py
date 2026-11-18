"""Derived vectors and durable provider receipts; never source authority."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Index, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kb_ingest.storage.types import UtcDateTime

from .base import Base


class WikiEmbedding(Base):
    __tablename__ = "wiki_embedding_cache"
    __table_args__ = (
        Index("ix_wiki_embedding_cache_project_id", "workspace_id", "project_id"),
        UniqueConstraint("workspace_id", "call_id"),
        CheckConstraint("dimensions >= 1 AND dimensions <= 65536", name="wiki_embedding_dimensions"),
    )
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    dimensions: Mapped[int] = mapped_column(Integer)
    input_version: Mapped[str] = mapped_column(String)
    input_sha256: Mapped[str] = mapped_column(String)
    call_id: Mapped[str] = mapped_column(String)
    request_sha256: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    vector_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    vector_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
