"""Retain complete normalized model responses without changing request states."""

import sqlalchemy as sa
from alembic import op
from kb_ingest.storage.types import UtcDateTime

revision = "kbs_413_model_results"
down_revision = "kbs_412_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_request_results",
        sa.Column("workspace_id", sa.String(), primary_key=True),
        sa.Column("call_id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("service_identity", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("request_sha256", sa.String(), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("result_sha256", sa.String(), nullable=False),
        sa.Column("result_bytes", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.String(), nullable=True),
        sa.Column("received_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "call_id"], ["model_requests.workspace_id", "model_requests.call_id"]),
        sa.CheckConstraint("format_version = 1", name="model_result_format"),
        sa.CheckConstraint("protocol IN ('responses','chat_completions')", name="model_result_protocol"),
        sa.CheckConstraint("result_bytes > 0 AND result_bytes <= 33554432", name="model_result_size"),
        sa.CheckConstraint("length(result_sha256) = 64", name="model_result_digest"),
    )
    op.create_index("ix_model_request_results_project_id", "model_request_results", ["workspace_id", "project_id"])


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM model_request_results LIMIT 1").first() is not None:
        message = "Saved model results must be preserved; downgrade refused."
        raise RuntimeError(message)
    op.drop_index("ix_model_request_results_project_id", table_name="model_request_results")
    op.drop_table("model_request_results")
