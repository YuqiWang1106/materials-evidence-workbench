"""Public index corpus snapshot for one workspace."""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from kb_ingest.storage.models import KbAttribute, KbEntity

from .entity_store_types import IndexAttribute, IndexCorpus, IndexEntity


def load_index_corpus(engine: Engine, workspace_id: str) -> IndexCorpus:
    """Load the current workspace entities and attributes as index records."""
    with Session(engine) as session:
        entities = session.scalars(select(KbEntity).where(KbEntity.workspace_id == workspace_id)).all()
        attributes = session.scalars(select(KbAttribute).where(KbAttribute.workspace_id == workspace_id)).all()
        return {
            "entities": [
                IndexEntity(
                    entity_id=row.entity_id,
                    type=row.entity_type,
                    name=row.canonical_name,
                    aliases=list(row.aliases or []),
                    source_id=row.source_id,
                    updated_at=row.updated_at.timestamp(),
                )
                for row in entities
            ],
            "attributes": [
                IndexAttribute(
                    attribute_id=row.attribute_id,
                    entity_id=row.entity_id,
                    section=row.section,
                    text=row.text,
                    page=row.page,
                    block_id=row.block_id,
                    asset_ref=row.asset_ref,
                    source_id=row.source_id,
                    refs=list(row.refs or []),
                )
                for row in attributes
            ],
        }
