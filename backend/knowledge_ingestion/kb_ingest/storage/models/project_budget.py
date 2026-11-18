"""Shared project accounting on the ingestion Base; no separate authority."""

from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProjectBudget(Base):
    __tablename__ = "project_budgets"
    __table_args__ = (CheckConstraint("cap_micro_usd >= 0 AND settled_micro_usd >= 0"),)
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    cap_micro_usd: Mapped[int] = mapped_column(BigInteger)
    settled_micro_usd: Mapped[int] = mapped_column(BigInteger, default=0)
    breach_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProjectCallReservation(Base):
    __tablename__ = "project_call_reservations"
    __table_args__ = (
        CheckConstraint("reserved_micro_usd >= 0"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id"], ["project_budgets.workspace_id", "project_budgets.project_id"]
        ),
        UniqueConstraint("workspace_id", "project_id", "call_id"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id", "parent_call_id"],
            [
                "project_call_reservations.workspace_id",
                "project_call_reservations.project_id",
                "project_call_reservations.call_id",
            ],
        ),
        Index("ix_project_call_reservations_project_id", "workspace_id", "project_id"),
    )
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    call_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    price_version: Mapped[str] = mapped_column(String)
    request_sha256: Mapped[str] = mapped_column(String)
    reserved_micro_usd: Mapped[int] = mapped_column(BigInteger)
    parent_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String)
    actual_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    usage_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    unknown_reason: Mapped[str | None] = mapped_column(String, nullable=True)
