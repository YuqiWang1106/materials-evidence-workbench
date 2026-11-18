"""Local coordination only: scientific record snapshots live in local archive."""

from typing import Any

from sqlalchemy import JSON, Float, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, synonym

from .base import Base


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("workspace_id", "sha256"),)
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    sha256: Mapped[str] = mapped_column(String)
    original_name: Mapped[str] = mapped_column(Text)
    storage_name: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class AssetUpload(Base):
    """File ownership retained until publication or cleanup commits."""

    __tablename__ = "asset_uploads"
    __table_args__ = (UniqueConstraint("workspace_id", "upload_id"),)
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    sha256: Mapped[str] = mapped_column(String, primary_key=True)
    upload_id: Mapped[str] = mapped_column(String)
    original_name: Mapped[str] = mapped_column(Text)
    storage_name: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    # POSIX device/inode values may exceed the maximum signed SQLite INTEGER.
    st_dev: Mapped[str] = mapped_column(String(20))
    st_ino: Mapped[str] = mapped_column(String(20))


class Source(Base):
    __tablename__ = "sources"
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)
    active_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    published_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lifecycle: Mapped[str] = mapped_column(String, default="active", server_default="active")
    visibility_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key"),
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.source_id"]),
        ForeignKeyConstraint(
            ["workspace_id", "confirmation_version_id"],
            ["reading_versions.workspace_id", "reading_versions.version_id"],
            name="fk_task_confirmation",
        ),
        Index("ix_tasks_confirmation", "workspace_id", "confirmation_version_id"),
        Index("ix_tasks_source_id", "workspace_id", "source_id"),
        Index("ix_tasks_state", "workspace_id", "state"),
    )
    workspace_id: Mapped[str] = mapped_column(String)
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    source_id: Mapped[str] = mapped_column(String)
    confirmation_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    generation: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[str] = mapped_column(String)
    asset_ids: Mapped[list[str]] = mapped_column(JSON)
    options: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String)
    progress: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer)
    attempt_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    package_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class PackageRevision(Base):
    __tablename__ = "package_revisions"
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    record_revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    package_sha256: Mapped[str] = mapped_column(String)


class TableDraft(Base):
    __tablename__ = "table_drafts"
    __table_args__ = (
        Index("ix_table_drafts_updated", "workspace_id", "updated_at"),
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.source_id"]),
    )
    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    asset_ids: Mapped[list[str]] = mapped_column(JSON)
    asset_hashes: Mapped[dict[str, Any]] = mapped_column(JSON)
    recipe: Mapped[dict[str, Any]] = mapped_column(JSON)
    reader_options: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    draft_sha256: Mapped[str] = mapped_column(String)
    recipe_sha256: Mapped[str] = mapped_column(String)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class ReadingVersion(Base):
    """Append-only confirmations and reusable recipes share one version ledger."""

    __tablename__ = "reading_versions"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.source_id"]),
        UniqueConstraint("workspace_id", "version_id"),
        UniqueConstraint("workspace_id", "source_id", "draft_revision", name="uq_source_confirmation_revision"),
        UniqueConstraint("workspace_id", "name", "version", name="uq_reusable_recipe_version"),
    )
    workspace_id: Mapped[str] = mapped_column(String)
    version_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer)
    recipe: Mapped[dict[str, Any]] = mapped_column(JSON)
    reader_options: Mapped[dict[str, Any]] = mapped_column(JSON)
    structure: Mapped[dict[str, Any]] = mapped_column(JSON)
    recipe_sha256: Mapped[str] = mapped_column(String)
    operator: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    draft_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    asset_hashes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    draft_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    __mapper_args__ = {"polymorphic_on": kind}


class SourceConfirmation(ReadingVersion):
    confirmation_version_id = synonym("version_id")
    __mapper_args__ = {"polymorphic_identity": "source_confirmation"}


class ReusableRecipe(ReadingVersion):
    recipe_version_id = synonym("version_id")
    __mapper_args__ = {"polymorphic_identity": "reusable_recipe"}


class SourceOperation(Base):
    """Durable source commands; manifests contain identities, never original text."""

    __tablename__ = "source_operations"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.source_id"]),
        UniqueConstraint("workspace_id", "request_id"),
        Index("ix_source_operations_source_id", "workspace_id", "source_id"),
    )
    workspace_id: Mapped[str] = mapped_column(String)
    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String)
    request_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
