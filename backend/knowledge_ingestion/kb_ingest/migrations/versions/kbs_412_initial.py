"""Create the ingestion database on an empty SQLite data root."""

from alembic import op
from kb_ingest.migrations.initial_knowledge_tables import TABLES as KNOWLEDGE_TABLES
from kb_ingest.migrations.initial_tables import TABLES as RECORD_TABLES
from kb_ingest.storage.sqlite import create_fts_index, create_reading_version_triggers

revision = "kbs_412_initial"
down_revision = None
branch_labels = None
depends_on = None

INDEXES = (
    "CREATE INDEX ix_knowledge_index_entries_generation_id ON knowledge_index_entries (generation_id)",
    (
        "CREATE INDEX ix_relation_anchor ON knowledge_relation_projection (workspace_id, projection_version, "
        "relation_kind, anchor_key)"
    ),
    "CREATE INDEX ix_relation_page ON knowledge_relation_projection (workspace_id, page_id, revision_id)",
    "CREATE INDEX ix_study_units_workspace_id ON study_units (workspace_id)",
    "CREATE INDEX ix_study_units_source_id ON study_units (source_id)",
    "CREATE INDEX ix_study_unit_revisions_unit_id ON study_unit_revisions (unit_id)",
    "CREATE INDEX ix_study_wiki_jobs_unit_id ON study_wiki_jobs (unit_id)",
    "CREATE INDEX ix_study_wiki_jobs_status ON study_wiki_jobs (status)",
    "CREATE INDEX ix_kb_related_workspace_id ON kb_related (workspace_id)",
    "CREATE INDEX ix_kb_related_entity_a ON kb_related (entity_a)",
    "CREATE INDEX ix_kb_related_entity_b ON kb_related (entity_b)",
    "CREATE INDEX ix_tasks_source_id ON tasks (workspace_id, source_id)",
    "CREATE INDEX ix_tasks_state ON tasks (workspace_id, state)",
    "CREATE INDEX ix_source_operations_source_id ON source_operations (workspace_id, source_id)",
    ("CREATE INDEX ix_project_call_reservations_project_id ON project_call_reservations (workspace_id, project_id)"),
    "CREATE INDEX ix_table_drafts_updated ON table_drafts (workspace_id, updated_at)",
    "CREATE INDEX ix_tasks_confirmation ON tasks (workspace_id, confirmation_version_id)",
    "CREATE INDEX ix_kb_attribute_entity_id ON kb_attribute (entity_id)",
    "CREATE INDEX ix_kb_attribute_source_id ON kb_attribute (source_id)",
    "CREATE INDEX ix_kb_attribute_claim_group ON kb_attribute (claim_group)",
    "CREATE INDEX ix_kb_attribute_workspace_id ON kb_attribute (workspace_id)",
    "CREATE INDEX ix_kb_entity_entity_type ON kb_entity (entity_type)",
    "CREATE INDEX ix_kb_entity_workspace_id ON kb_entity (workspace_id)",
    "CREATE INDEX ix_knowledge_items_workspace_id ON knowledge_items (workspace_id)",
    "CREATE INDEX ix_knowledge_event_outbox_page_id ON knowledge_event_outbox (page_id)",
    "CREATE INDEX ix_knowledge_index_generations_scope_key ON knowledge_index_generations (scope_key)",
    "CREATE INDEX ix_wiki_embedding_cache_project_id ON wiki_embedding_cache (workspace_id, project_id)",
    "CREATE INDEX ix_model_requests_project_id ON model_requests (workspace_id, project_id)",
)


def upgrade() -> None:
    connection = op.get_bind()
    for statement in RECORD_TABLES:
        connection.exec_driver_sql(statement)
    create_reading_version_triggers(connection)
    create_fts_index(connection)
    for statement in (*KNOWLEDGE_TABLES, *INDEXES):
        connection.exec_driver_sql(statement)


def downgrade() -> None:
    message = "The initial schema cannot be downgraded without losing ingestion records. Restore a verified backup."
    raise RuntimeError(message)
