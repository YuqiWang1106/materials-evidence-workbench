"""Private complete provider results, separate from ordinary public receipts."""

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..types import UtcDateTime
from .base import Base


class ModelRequestResult(Base):
    __tablename__ = "model_request_results"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "call_id"], ["model_requests.workspace_id", "model_requests.call_id"]),
        Index("ix_model_request_results_project_id", "workspace_id", "project_id"),
        CheckConstraint("format_version = 1", name="model_result_format"),
        CheckConstraint("protocol IN ('responses','chat_completions')", name="model_result_protocol"),
        CheckConstraint("result_bytes > 0 AND result_bytes <= 33554432", name="model_result_size"),
        CheckConstraint("length(result_sha256) = 64", name="model_result_digest"),
    )
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    call_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    service_identity: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    request_sha256: Mapped[str] = mapped_column(String)
    format_version: Mapped[int] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(String)
    response: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    result_sha256: Mapped[str] = mapped_column(String)
    result_bytes: Mapped[int] = mapped_column(Integer)
    receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime())
