"""Website-owned request execution state, independent of monetary accounting."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..types import UtcDateTime
from .base import Base


class ModelRequest(Base):
    __tablename__ = "model_requests"
    __table_args__ = (
        Index("ix_model_requests_project_id", "workspace_id", "project_id"),
        CheckConstraint("state IN ('pending','sent','completed','failed','unknown')", name="model_request_state"),
    )
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    call_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    service_identity: Mapped[str] = mapped_column(String)
    request_sha256: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    result_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    unknown_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
