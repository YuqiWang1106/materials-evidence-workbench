"""Durable unreviewed study identities, immutable inputs and fenced Wiki jobs."""

from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class StudyUnit(Base):
    __tablename__ = "study_units"
    __table_args__ = (UniqueConstraint("workspace_id", "source_id", "extraction_batch", "candidate_id", "study_ref"),)
    unit_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    extraction_batch: Mapped[str] = mapped_column(String)
    candidate_id: Mapped[str] = mapped_column(String)
    study_ref: Mapped[str] = mapped_column(String)
    source_task_id: Mapped[str] = mapped_column(String)
    current_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    page_id: Mapped[str | None] = mapped_column(String, nullable=True)
    auto_generation_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    current_available: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class StudyUnitRevision(Base):
    __tablename__ = "study_unit_revisions"
    __table_args__ = (UniqueConstraint("unit_id", "revision"),)
    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("study_units.unit_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    source_task_id: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class StudyWikiJob(Base):
    __tablename__ = "study_wiki_jobs"
    __table_args__ = (UniqueConstraint("unit_id", "input_hash", "rule_version"),)
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("study_units.unit_id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("study_unit_revisions.snapshot_id"))
    input_hash: Mapped[str] = mapped_column(String)
    rule_version: Mapped[str] = mapped_column(String)
    model: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, index=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    input_payload_hash: Mapped[str] = mapped_column(String)
    provider_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    provider_receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    base_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
