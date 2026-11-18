"""Idempotent additions. Never replace a paper or alter an existing attribute/entity."""

from collections.abc import Sequence

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from kb_ingest.core.clock import now
from kb_ingest.core.serialization import fingerprint
from kb_ingest.storage.models import KbAttribute, KbEntity, KbRelated
from kb_ingest.storage.transactions import write_session

from .entity_store_types import (
    AppendCounts,
    ResolvedAttribute,
    ResolvedGraph,
    ResolvedRelation,
    as_list,
)
from .entity_store_workspace import require_owned


def _append_attribute(session: Session, workspace_id: str, source_id: str, attr: ResolvedAttribute) -> int:
    require_owned(session, workspace_id, attr["entity_id"])
    key = fingerprint(
        [
            workspace_id,
            source_id,
            attr["entity_id"],
            attr["section"],
            attr["text"],
            attr["block_id"],
            attr["quote"],
        ]
    )
    if session.get(KbAttribute, key) is not None:
        return 0
    session.add(
        KbAttribute(
            attribute_id=key,
            entity_id=attr["entity_id"],
            workspace_id=workspace_id,
            section=attr["section"],
            text=attr["text"],
            asset_ref=attr["asset_ref"],
            page=attr["page"],
            block_id=attr["block_id"],
            quote=attr["quote"],
            source_id=source_id,
            block_ids=attr.get("block_ids", []),
            highlight_regions=attr.get("highlight_regions", []),
            refs=attr.get("refs", []),
            claim_group=None,
            citations=attr.get("citations", []),
        )
    )
    session.flush()
    return 1


def _append_related(session: Session, workspace_id: str, source_id: str, links: Sequence[ResolvedRelation]) -> int:
    added = 0
    for link in links:
        require_owned(session, workspace_id, link["entity_a"])
        require_owned(session, workspace_id, link["entity_b"])
        a, b = sorted([link["entity_a"], link["entity_b"]])
        key = fingerprint([workspace_id, a, b])
        row = session.get(KbRelated, key)
        places = [{**place, "source_id": source_id} for place in link["locations"]]
        if row is None:
            row = KbRelated(
                related_id=key,
                workspace_id=workspace_id,
                entity_a=a,
                entity_b=b,
                locations=[],
                kind=link.get("kind"),
            )
            session.add(row)
        existing = as_list(row.locations)
        for place in places:
            if place not in existing:
                existing.append(place)
                added += 1
        row.locations = existing
        session.flush()
    return added


def append_graph(
    engine: Engine,
    workspace_id: str,
    source_id: str,
    graph: ResolvedGraph,
    *,
    session: Session | None = None,
) -> AppendCounts:
    counts: AppendCounts = {"entities": 0, "attributes": 0, "locations": 0}
    with write_session(engine, session) as session:
        for entity in graph["entities"]:
            row = session.get(KbEntity, entity["entity_id"])
            if row is not None:
                if row.workspace_id != workspace_id or row.entity_type != entity["type"]:
                    raise ValueError("entity identity mismatch")
                continue
            created_at = now()
            session.add(
                KbEntity(
                    entity_id=entity["entity_id"],
                    workspace_id=workspace_id,
                    entity_type=entity["type"],
                    canonical_name=entity["name"],
                    aliases=entity["add_aliases"],
                    source_id=entity.get("source_id"),
                    sample_name=entity.get("sample_name"),
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            counts["entities"] += 1
        session.flush()
        for attr in graph["attributes"]:
            counts["attributes"] += _append_attribute(session, workspace_id, source_id, attr)
        counts["locations"] = _append_related(session, workspace_id, source_id, graph["related"])
    return counts
