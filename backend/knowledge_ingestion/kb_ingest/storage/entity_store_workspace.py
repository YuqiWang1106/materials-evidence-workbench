"""Workspace guards used when writing entity and attribute rows."""

from collections.abc import Mapping, Sequence

from sqlalchemy import delete
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete
from sqlalchemy.sql.elements import ColumnElement

from kb_ingest.core.serialization import fingerprint
from kb_ingest.storage.models import KbAttribute, KbEntity, KbRelated

from .entity_store_types import as_list, item_get


def owned_entity(session: Session, entity_id: object, workspace_id: str) -> KbEntity | None:
    row = session.get(KbEntity, entity_id)
    if row is not None and row.workspace_id != workspace_id:
        message = "entity belongs to another workspace"
        raise ValueError(message)
    return row


def require_owned(session: Session, workspace_id: str, entity_id: object) -> KbEntity:
    row = owned_entity(session, entity_id, workspace_id)
    if row is None:
        message = "entity does not belong to this workspace"
        raise ValueError(message)
    return row


def owned_delete(
    model: type[KbEntity] | type[KbAttribute] | type[KbRelated],
    workspace_id: str,
    *clauses: ColumnElement[bool],
) -> Delete:
    return delete(model).where(model.workspace_id == workspace_id, *clauses)


def new_attribute(workspace_id: str, **fields: object) -> KbAttribute:
    row = KbAttribute(workspace_id=workspace_id)
    for name, value in fields.items():
        setattr(row, name, value)
    return row


def handover_attribute(workspace_id: str, row: KbAttribute, first: object, citations: list[object]) -> KbAttribute:
    return new_attribute(
        workspace_id,
        attribute_id=fingerprint(
            [
                workspace_id,
                item_get(first, "source_id"),
                row.entity_id,
                row.section,
                row.text,
                item_get(first, "block_id"),
                item_get(first, "quote"),
            ]
        ),
        entity_id=row.entity_id,
        section=row.section,
        text=row.text,
        asset_ref=item_get(first, "asset_ref") or "",
        page=item_get(first, "page"),
        block_id=item_get(first, "block_id") or "",
        quote=item_get(first, "quote") or "",
        source_id=item_get(first, "source_id"),
        block_ids=as_list(item_get(first, "block_ids")),
        highlight_regions=as_list(item_get(first, "highlight_regions")),
        refs=as_list(row.refs),
        claim_group=None,
        citations=citations,
    )


def resolved_attribute(
    workspace_id: str, source_id: str, attribute: Mapping[str, object], cites: Sequence[object]
) -> KbAttribute:
    return new_attribute(
        workspace_id,
        attribute_id=fingerprint(
            [
                workspace_id,
                source_id,
                attribute["entity_id"],
                attribute["section"],
                attribute["text"],
                attribute["block_id"],
                attribute["quote"],
            ]
        ),
        entity_id=attribute["entity_id"],
        section=attribute["section"],
        text=attribute["text"],
        asset_ref=attribute.get("asset_ref") or "",
        page=attribute.get("page"),
        block_id=attribute["block_id"],
        quote=attribute["quote"],
        source_id=source_id,
        block_ids=as_list(attribute.get("block_ids")),
        highlight_regions=as_list(attribute.get("highlight_regions")),
        refs=as_list(attribute.get("refs")),
        claim_group=attribute.get("claim_group"),
        citations=cites,
    )
