"""Write and replace one paper's entity graph in the shared store."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from kb_ingest.core.clock import now
from kb_ingest.core.conservation import require_conserved
from kb_ingest.core.contract_entity import cite, normalize_name, present_aliases, same_reading
from kb_ingest.core.serialization import fingerprint
from kb_ingest.storage.models import KbAttribute, KbEntity, KbRelated
from kb_ingest.storage.transactions import write_session

from .entity_store_types import (
    as_list,
    as_record,
    item_get,
    mapping_rows,
    required_iterable,
)
from .entity_store_workspace import handover_attribute, owned_delete, owned_entity, require_owned, resolved_attribute


@dataclass(frozen=True, slots=True)
class WriteCounts:
    """What one paper's write did with its attribute lines: new rows, cites on another paper's row, repeats."""

    attributes_written: int
    attributes_cited: int
    attributes_repeated: int


@dataclass
class WriteBatch:
    session: Session
    workspace_id: str
    source_id: str
    resolved: Mapping[str, object]
    now: datetime
    incoming: set[object]
    formulation_ids: set[object]
    stale: set[object]
    owned: set[object]


def _owned_scope(
    session: Session, workspace_id: str, source_id: str, formulation_ids: set[object]
) -> tuple[set[object], set[object]]:
    stale: set[object] = {
        item
        for item in session.scalars(
            select(KbEntity.entity_id).where(
                KbEntity.workspace_id == workspace_id,
                KbEntity.source_id == source_id,
                KbEntity.entity_type == "formulation",
            )
        ).all()
        if item not in formulation_ids
    }
    owned: set[object] = set(
        session.scalars(
            select(KbEntity.entity_id).where(KbEntity.workspace_id == workspace_id, KbEntity.source_id == source_id)
        ).all()
    )
    return stale, owned


def _upsert_entities(batch: WriteBatch) -> None:
    session = batch.session
    for entity in mapping_rows(batch.resolved, "entities"):
        row = owned_entity(session, entity["entity_id"], batch.workspace_id)
        if row is None:
            session.add(
                KbEntity(
                    entity_id=entity["entity_id"],
                    workspace_id=batch.workspace_id,
                    entity_type=entity["type"],
                    canonical_name=entity["name"],
                    aliases=present_aliases(
                        entity["name"],
                        None if entity["add_aliases"] is None else required_iterable(entity["add_aliases"]),
                    ),
                    source_id=batch.source_id if entity["type"] == "formulation" else entity.get("source_id"),
                    sample_name=entity.get("sample_name"),
                    created_at=batch.now,
                    updated_at=batch.now,
                )
            )
            continue
        aliases = as_list(row.aliases)
        known = {normalize_name(row.canonical_name), *[normalize_name(alias) for alias in aliases]}
        for alias in required_iterable(entity["add_aliases"]):
            if normalize_name(alias) not in known:
                aliases.append(alias)
                known.add(normalize_name(alias))
        row.aliases = aliases
        row.updated_at = batch.now
        if entity["type"] == "formulation":
            for name, value in (("canonical_name", entity["name"]), ("sample_name", entity["sample_name"])):
                setattr(row, name, value)
            row.source_id = batch.source_id


def _replace_owned_attributes(batch: WriteBatch) -> None:
    session = batch.session
    workspace_id = batch.workspace_id
    source_id = batch.source_id
    dropped = batch.formulation_ids | batch.stale
    handed_over: list[tuple[KbAttribute, list[object]]] = []
    for current in session.scalars(select(KbAttribute).where(KbAttribute.workspace_id == workspace_id)).all():
        citations = as_list(current.citations)
        others = [item for item in citations if item_get(item, "source_id") != source_id]
        if current.source_id == source_id:
            if others and current.entity_id not in dropped:
                handed_over.append((current, others))
        elif len(others) != len(citations):
            current.citations = others
    session.execute(owned_delete(KbAttribute, workspace_id, KbAttribute.source_id == source_id))
    session.execute(owned_delete(KbAttribute, workspace_id, KbAttribute.entity_id.in_(dropped)))
    for current, others in handed_over:
        session.add(handover_attribute(workspace_id, current, others[0], others[1:]))


def _prune_owned_locations(batch: WriteBatch) -> None:
    session = batch.session
    source_id = batch.source_id
    dropped = batch.formulation_ids | batch.stale
    for current_link in session.scalars(select(KbRelated).where(KbRelated.workspace_id == batch.workspace_id)).all():
        touches = {current_link.entity_a, current_link.entity_b} & dropped
        places = as_list(current_link.locations)
        kept = [] if touches else [place for place in places if item_get(place, "source_id") != source_id]
        if not kept:
            session.delete(current_link)
        elif len(kept) != len(places):
            current_link.locations = kept


def _shared_attributes(batch: WriteBatch) -> dict[tuple[object, object], list[KbAttribute]]:
    shared = {
        entity["entity_id"] for entity in mapping_rows(batch.resolved, "entities") if entity["type"] != "formulation"
    }
    stored: dict[tuple[object, object], list[KbAttribute]] = {}
    if not shared:
        return stored
    for stored_row in batch.session.scalars(
        select(KbAttribute).where(
            KbAttribute.workspace_id == batch.workspace_id,
            KbAttribute.entity_id.in_(shared),
            KbAttribute.source_id != batch.source_id,
        )
    ).all():
        stored.setdefault((stored_row.entity_id, stored_row.section), []).append(stored_row)
    return stored


def _merge_attributes(batch: WriteBatch) -> WriteCounts:
    session = batch.session
    workspace_id = batch.workspace_id
    source_id = batch.source_id
    stored = _shared_attributes(batch)
    attribute_ids: set[str] = set()
    incoming = mapping_rows(batch.resolved, "attributes")
    written = cited = repeated = 0
    for attribute in incoming:
        require_owned(session, workspace_id, attribute["entity_id"])
        attribute_id = fingerprint(
            [
                workspace_id,
                source_id,
                attribute["entity_id"],
                attribute["section"],
                attribute["text"],
                attribute["block_id"],
                attribute["quote"],
            ]
        )
        if attribute_id in attribute_ids:
            repeated += 1
            continue
        attribute_ids.add(attribute_id)
        here = {**cite(attribute), "source_id": source_id}
        cites = [
            {**as_record(item), "source_id": item_get(item, "source_id") or source_id}
            for item in as_list(attribute.get("citations"))
        ]
        match = next(
            (
                row
                for row in stored.get((attribute["entity_id"], attribute["section"]), [])
                if same_reading(row.text, attribute["text"])
            ),
            None,
        )
        if match is not None:
            # The same words already stand on this shared entity from another paper: this paper becomes a cite.
            match.citations = [*as_list(match.citations), here, *cites]
            cited += 1
            continue
        session.add(resolved_attribute(workspace_id, source_id, attribute, cites))
        written += 1
    require_conserved("write attribute", entered=len(incoming), kept=written, set_aside=cited + repeated)
    return WriteCounts(written, cited, repeated)


def _merge_related(batch: WriteBatch) -> None:
    session = batch.session
    workspace_id = batch.workspace_id
    source_id = batch.source_id
    for link in mapping_rows(batch.resolved, "related"):
        require_owned(session, workspace_id, link["entity_a"])
        require_owned(session, workspace_id, link["entity_b"])
        locations = [
            {**as_record(place), "source_id": item_get(place, "source_id") or source_id}
            for place in required_iterable(link["locations"])
        ]
        existing = session.scalar(
            select(KbRelated).where(
                KbRelated.workspace_id == workspace_id,
                KbRelated.entity_a == link["entity_a"],
                KbRelated.entity_b == link["entity_b"],
            )
        )
        if existing is None:
            session.add(
                KbRelated(
                    related_id=fingerprint([workspace_id, link["entity_a"], link["entity_b"]]),
                    workspace_id=workspace_id,
                    entity_a=link["entity_a"],
                    entity_b=link["entity_b"],
                    locations=locations,
                    kind=link.get("kind"),
                )
            )
            session.flush()
            continue
        merged = as_list(existing.locations)
        for place in locations:
            if place not in merged:
                merged.append(place)
        existing.locations = merged
        if existing.kind and existing.kind != link.get("kind"):
            existing.kind = None


def _delete_unused_owned(batch: WriteBatch) -> None:
    session = batch.session
    workspace_id = batch.workspace_id
    for entity_id in batch.owned - batch.incoming - batch.stale:
        attrs = select(KbAttribute.attribute_id).where(
            KbAttribute.workspace_id == workspace_id, KbAttribute.entity_id == entity_id
        )
        links = select(KbRelated.related_id).where(
            KbRelated.workspace_id == workspace_id,
            or_(KbRelated.entity_a == entity_id, KbRelated.entity_b == entity_id),
        )
        used = session.scalar(attrs.limit(1)) or session.scalar(links.limit(1))
        if not used:
            session.execute(owned_delete(KbEntity, workspace_id, KbEntity.entity_id == entity_id))


class EntityStoreWrite:
    engine: Engine

    def write(
        self,
        workspace_id: str,
        source_id: str,
        resolved: Mapping[str, object],
        *,
        session: Session | None = None,
    ) -> WriteCounts:
        """Insert this paper's entities, attributes and links. Everything this paper wrote before is replaced;
        shared entities stay and keep accumulating aliases. Returns what became of the attribute lines.

        A caller-supplied session must already hold a write transaction (begin_write);
        its caller owns commit and rollback. Standalone writes serialize before reading any shared state.
        """
        entities = mapping_rows(resolved, "entities")
        incoming = {entity["entity_id"] for entity in entities}
        formulation_ids = {entity["entity_id"] for entity in entities if entity["type"] == "formulation"}
        with write_session(self.engine, session) as session:
            stale, owned = _owned_scope(session, workspace_id, source_id, formulation_ids)
            batch = WriteBatch(
                session, workspace_id, source_id, resolved, now(), incoming, formulation_ids, stale, owned
            )
            _upsert_entities(batch)
            session.flush()
            _replace_owned_attributes(batch)
            _prune_owned_locations(batch)
            session.flush()
            if stale:
                session.execute(owned_delete(KbEntity, workspace_id, KbEntity.entity_id.in_(stale)))
            counts = _merge_attributes(batch)
            session.flush()
            _merge_related(batch)
            session.flush()
            _delete_unused_owned(batch)
        return counts
