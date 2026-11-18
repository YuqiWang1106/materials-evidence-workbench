"""Replace each paper's resolved graph and read the current workspace entities."""

from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from kb_ingest.core.contract_entity import (
    CLAIM_SECTIONS,
    DESCRIPTION_SECTION,
    FORMULATION_SECTIONS,
    contains_name,
    normalize_name,
    present_aliases,
)
from kb_ingest.storage.models import KbAttribute, KbEntity, KbRelated
from kb_ingest.storage.transactions import write_session

from .entity_store_confirm import EntityConfirmation
from .entity_store_types import (
    AttributeRow,
    Candidate,
    ClaimRef,
    EngineProvider,
    EntityGraph,
    EntityRead,
    EntityView,
    RelatedEntity,
    SearchEntity,
    SourceEntity,
    as_list,
    entity_session,
    item_get,
)
from .entity_store_write import EntityStoreWrite

SECTION_ORDER = {
    name: index for index, name in enumerate(("文中名称", "目的", "区别", *FORMULATION_SECTIONS, DESCRIPTION_SECTION))
}


def _terms(query: object) -> list[str]:
    return [normalize_name(term) for term in str(query or "").split() if normalize_name(term)]


def _attribute_row(attribute: KbAttribute, entity: KbEntity | None = None) -> AttributeRow:
    row: AttributeRow = {
        "attribute_id": attribute.attribute_id,
        "entity_id": attribute.entity_id,
        "section": attribute.section,
        "text": attribute.text,
        "asset_ref": attribute.asset_ref,
        "page": attribute.page,
        "block_id": attribute.block_id,
        "quote": attribute.quote,
        "source_id": attribute.source_id,
        "refs": list(attribute.refs or []),
        "claim_group": attribute.claim_group,
    }
    if attribute.block_ids:
        row["block_ids"] = list(attribute.block_ids)
    if attribute.highlight_regions:
        row["highlight_regions"] = list(attribute.highlight_regions)
    if attribute.citations:
        row["citations"] = list(attribute.citations)
    if entity is not None:
        row["type"] = entity.entity_type
        row["name"] = entity.canonical_name
    return row


def _section_key(row: AttributeRow) -> tuple[int, str, object]:
    return (SECTION_ORDER.get(row["section"], len(SECTION_ORDER)), row["section"], row.get("page") or 0)


@dataclass(frozen=True)
class GraphQuery:
    source_id: str | None
    entity_types: Collection[str] | None
    limit: int
    asset_id: str | None


@dataclass
class GraphIncidence:
    entities: dict[str, KbEntity]
    counts: Counter[str]
    sources: dict[str, set[object]]
    degree: Counter[str]
    edges: list[dict[str, object]]
    chosen: set[str]


def _load_graph_rows(
    engine: Engine, workspace_id: str
) -> tuple[dict[str, KbEntity], list[KbRelated], list[tuple[str, str | None, str, object]]]:
    with Session(engine) as session:
        entities = {
            row.entity_id: row
            for row in session.scalars(select(KbEntity).where(KbEntity.workspace_id == workspace_id)).all()
        }
        links = list(session.scalars(select(KbRelated).where(KbRelated.workspace_id == workspace_id)).all())
        query = select(KbAttribute.entity_id, KbAttribute.source_id, KbAttribute.asset_ref, KbAttribute.citations)
        query = query.join(KbEntity, KbEntity.entity_id == KbAttribute.entity_id)
        attributes = list(session.execute(query.where(KbEntity.workspace_id == workspace_id)).tuples().all())
    return entities, links, attributes


def _graph_incidence(
    entities: dict[str, KbEntity],
    links: list[KbRelated],
    attributes: list[tuple[str, str | None, str, object]],
    query: GraphQuery,
) -> GraphIncidence:
    counts: Counter[str] = Counter()
    sources: defaultdict[str, set[object]] = defaultdict(set)
    source_id = query.source_id
    asset_id = query.asset_id
    for entity_id, attribute_source, asset_ref, citations in attributes:
        if asset_id is not None and not (
            asset_ref == asset_id
            or any(
                item_get(item, "asset_ref") == asset_id
                and (source_id is None or item_get(item, "source_id") == source_id)
                for item in as_list(citations)
            )
        ):
            continue
        counts[entity_id] += 1
        sources[entity_id].add(attribute_source)
    edges: list[dict[str, object]] = []
    chosen: set[str] = set()
    degree: Counter[str] = Counter()
    if source_id is not None:
        if asset_id is None:
            chosen |= {key for key, row in entities.items() if row.source_id == source_id}
        chosen |= {key for key, found in sources.items() if source_id in found}
    for link in links:
        places = [
            place for place in as_list(link.locations) if asset_id is None or item_get(place, "asset_ref") == asset_id
        ]
        matches_source = source_id is None or any(item_get(place, "source_id") == source_id for place in places)
        if not matches_source or link.entity_a not in entities or link.entity_b not in entities:
            continue
        chosen |= {link.entity_a, link.entity_b}
        for end in (link.entity_a, link.entity_b):
            degree[end] += 1
            sources[end].update(item_get(place, "source_id") for place in places)
        edges.append({"a": link.entity_a, "b": link.entity_b, "weight": len(places), "kind": link.kind})
    if source_id is None:
        chosen = set(entities)
    return GraphIncidence(entities, counts, sources, degree, edges, chosen)


def _graph_view(incidence: GraphIncidence, entity_types: Collection[str] | None, limit: int) -> EntityGraph:
    entities = incidence.entities
    chosen = incidence.chosen
    if entity_types:
        chosen = {key for key in chosen if entities[key].entity_type in entity_types}
    ranked = sorted(
        chosen,
        key=lambda key: (
            entities[key].entity_type != "formulation",
            -incidence.degree.get(key, 0),
            entities[key].canonical_name,
        ),
    )
    kept = set(ranked[:limit])
    nodes: list[dict[str, object]] = [
        {
            "entity_id": key,
            "type": entities[key].entity_type,
            "name": entities[key].canonical_name,
            "aliases": present_aliases(entities[key].canonical_name, entities[key].aliases),
            "source_id": entities[key].source_id,
            "attribute_count": incidence.counts.get(key, 0),
            "degree": incidence.degree.get(key, 0),
            "source_count": len({item for item in incidence.sources.get(key, set()) if item}),
        }
        for key in ranked
        if key in kept
    ]
    edges = [edge for edge in incidence.edges if edge["a"] in kept and edge["b"] in kept]
    return {"nodes": nodes, "edges": edges, "truncated": len(chosen) > len(kept)}


def _name_search_hits(rows: Sequence[KbEntity], query_text: str, needle: str) -> list[tuple[int, int, SearchEntity]]:
    matches: list[tuple[int, int, SearchEntity]] = []
    for row in rows:
        names = [row.canonical_name, *(row.aliases or [])]
        if needle in {normalize_name(item) for item in names}:
            rank = 0
        elif any(contains_name(query_text, item) for item in names):
            rank = 1
        elif any(contains_name(item, query_text) for item in names):
            rank = 2
        else:
            continue
        matches.append(
            (
                rank,
                len(row.canonical_name),
                {
                    "entity_id": row.entity_id,
                    "type": row.entity_type,
                    "name": row.canonical_name,
                    "aliases": present_aliases(row.canonical_name, row.aliases),
                    "source_id": row.source_id,
                },
            )
        )
    return matches


def _code_search_hits(
    rows: Sequence[tuple[KbAttribute, KbEntity]], needle: str, found: set[str]
) -> list[tuple[int, int, SearchEntity]]:
    matches: list[tuple[int, int, SearchEntity]] = []
    for attribute, row in rows:
        if row.entity_id not in found and normalize_name(attribute.text) == needle:
            found.add(row.entity_id)
            matches.append(
                (
                    0,
                    len(row.canonical_name),
                    {
                        "entity_id": row.entity_id,
                        "type": row.entity_type,
                        "name": row.canonical_name,
                        "aliases": present_aliases(row.canonical_name, row.aliases),
                        "source_id": row.source_id,
                        "code": attribute.text,
                    },
                )
            )
    return matches


class EntityStore(EntityStoreWrite, EntityConfirmation):
    def __init__(self, store: Engine | EngineProvider) -> None:
        if isinstance(store, Engine):
            self.engine = store
            self.workspace_id = None
        else:
            self.engine = store.engine
            workspace_id = getattr(store, "workspace_id", None)
            self.workspace_id = workspace_id if isinstance(workspace_id, str) else None

    def candidates(self, workspace_id: str, entity_type: str) -> list[Candidate]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(KbEntity).where(KbEntity.workspace_id == workspace_id, KbEntity.entity_type == entity_type)
            ).all()
            return [
                {"entity_id": row.entity_id, "canonical_name": row.canonical_name, "aliases": list(row.aliases or [])}
                for row in rows
            ]

    def entity(self, workspace_id: str, entity_id: object) -> EntityView | None:
        with Session(self.engine) as session:
            row = session.get(KbEntity, entity_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return {
                "entity_id": row.entity_id,
                "type": row.entity_type,
                "name": row.canonical_name,
                "aliases": present_aliases(row.canonical_name, row.aliases),
                "source_id": row.source_id,
                "sample_name": row.sample_name,
            }

    def entities_for_source(self, workspace_id: str, source_id: str) -> list[SourceEntity]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(KbEntity)
                .where(KbEntity.workspace_id == workspace_id, KbEntity.source_id == source_id)
                .order_by(KbEntity.entity_type, KbEntity.canonical_name)
            ).all()
            return [{"entity_id": row.entity_id, "type": row.entity_type, "name": row.canonical_name} for row in rows]

    def formulations_for_source(self, workspace_id: str, source_id: str) -> list[SourceEntity]:
        return [row for row in self.entities_for_source(workspace_id, source_id) if row["type"] == "formulation"]

    def graph(
        self,
        workspace_id: str,
        source_id: str | None = None,
        *,
        entity_types: Collection[str] | None = None,
        limit: int = 600,
        asset_id: str | None = None,
    ) -> EntityGraph:
        """Nodes and undirected edges. With source_id: what that paper wrote (its formulations, entities it
        described, and every entity at the end of a link it supports)."""
        entities, links, attributes = _load_graph_rows(self.engine, workspace_id)
        return _graph_view(
            _graph_incidence(entities, links, attributes, GraphQuery(source_id, entity_types, limit, asset_id)),
            entity_types,
            limit,
        )

    def graph_for_source(self, workspace_id: str, source_id: str) -> list[dict[str, object]]:
        """Entities for one paper as a flat list, formulations first."""
        return [
            {"entity_id": node["entity_id"], "type": node["type"], "name": node["name"]}
            for node in self.graph(workspace_id, source_id)["nodes"]
        ]

    def attributes_of(self, workspace_id: str, entity_id: object) -> list[AttributeRow]:
        clause = (KbAttribute.workspace_id == workspace_id) & (KbAttribute.entity_id == entity_id)
        with Session(self.engine) as session:
            return sorted(map(_attribute_row, session.scalars(select(KbAttribute).where(clause))), key=_section_key)

    def related_to(self, workspace_id: str, entity_id: object, entity_type: str | None = None) -> list[RelatedEntity]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(KbRelated).where(
                    KbRelated.workspace_id == workspace_id,
                    or_(KbRelated.entity_a == entity_id, KbRelated.entity_b == entity_id),
                )
            ).all()
            result: list[RelatedEntity] = []
            for row in rows:
                other_id = row.entity_b if row.entity_a == entity_id else row.entity_a
                other = session.get(KbEntity, other_id)
                if other is None or other.workspace_id != workspace_id:
                    continue
                if entity_type and other.entity_type != entity_type:
                    continue
                result.append(
                    {
                        "entity_id": other.entity_id,
                        "type": other.entity_type,
                        "name": other.canonical_name,
                        "source_id": other.source_id,
                        "kind": row.kind,
                        "locations": as_list(row.locations),
                    }
                )
            return sorted(result, key=lambda item: (item["type"] != "formulation", item["type"], item["name"]))

    def related_to_all(
        self, workspace_id: str, entity_ids: Iterable[str], entity_type: str | None = None
    ) -> list[RelatedEntity]:
        """Entities related to every given entity (for example formulations with both a material and a property)."""
        found: set[str] | None = None
        rows: dict[str, RelatedEntity] = {}
        for entity_id in entity_ids:
            linked = {item["entity_id"]: item for item in self.related_to(workspace_id, entity_id, entity_type)}
            for key, item in linked.items():
                current = rows.setdefault(key, {**item, "locations": []})
                current["locations"].extend(item["locations"])
            found = set(linked) if found is None else found & set(linked)
        return [rows[key] for key in rows if key in (found or set())]

    def search_entities(
        self, workspace_id: str, name: str, entity_type: str | None = None, limit: int = 20
    ) -> list[SearchEntity]:
        """Exact name or alias first, then a name that contains the query, then a name the query contains.
        Latin abbreviations match as whole words, so PP does not find APP."""
        query_text = " ".join(str(name).split())
        needle = normalize_name(query_text)
        if not needle:
            return []
        with Session(self.engine) as session:
            query = select(KbEntity).where(KbEntity.workspace_id == workspace_id)
            if entity_type:
                query = query.where(KbEntity.entity_type == entity_type)
            rows = session.scalars(query).all()
        matches = _name_search_hits(rows, query_text, needle)
        found = {item["entity_id"] for _, _, item in matches}
        # A sample is also found by how the paper names it (实施例3), kept as its 文中名称 attribute.
        if entity_type in (None, "formulation"):
            with Session(self.engine) as session:
                coded = session.execute(
                    select(KbAttribute, KbEntity)
                    .join(KbEntity, KbEntity.entity_id == KbAttribute.entity_id)
                    .where(KbAttribute.workspace_id == workspace_id, KbAttribute.section == "文中名称")
                ).all()
            matches.extend(_code_search_hits(coded, needle, found))
        return [item for _, _, item in sorted(matches, key=lambda value: value[:2])][:limit]

    def search_attributes(
        self,
        workspace_id: str,
        query: object,
        section: str | None = None,
        entity_ids: Collection[str] | None = None,
        limit: int = 30,
    ) -> list[AttributeRow]:
        """Every whitespace-separated term must occur in the attribute text or quote."""
        terms = _terms(query)
        allowed = set(entity_ids or [])
        section_key = normalize_name(section) if section else None
        with Session(self.engine) as session:
            statement = (
                select(KbAttribute, KbEntity)
                .join(KbEntity, KbEntity.entity_id == KbAttribute.entity_id)
                .where(KbAttribute.workspace_id == workspace_id, KbEntity.workspace_id == workspace_id)
            )
            if allowed:
                statement = statement.where(KbAttribute.entity_id.in_(allowed))
            rows = session.execute(statement).all()
        matches: list[AttributeRow] = []
        for attribute, entity in rows:
            if section_key and section_key not in normalize_name(attribute.section):
                continue
            haystack = normalize_name(attribute.text + " " + attribute.quote)
            if not all(term in haystack for term in terms):
                continue
            matches.append(_attribute_row(attribute, entity))
        return sorted(matches, key=lambda row: (row["name"], *_section_key(row)))[:limit]

    def same_claims(
        self, workspace_id: str, claim_groups: Iterable[str | None]
    ) -> dict[str | None, list[AttributeRow]]:
        groups = {group for group in claim_groups if group}
        if not groups:
            return {}
        with Session(self.engine) as session:
            rows = session.execute(
                select(KbAttribute, KbEntity)
                .join(KbEntity, KbEntity.entity_id == KbAttribute.entity_id)
                .where(KbAttribute.workspace_id == workspace_id, KbAttribute.claim_group.in_(groups))
            ).all()
        result: dict[str | None, list[AttributeRow]] = {}
        for attribute, entity in rows:
            result.setdefault(attribute.claim_group, []).append(_attribute_row(attribute, entity))
        return result

    def claims_by_refs(self, workspace_id: str, *, session: Session | None = None) -> list[ClaimRef]:
        """Every 结论 and 解释 attribute with the set of entities it involves (its owner and its refs)."""
        with entity_session(self.engine, session) as session:
            rows = (
                session.execute(
                    select(KbAttribute)
                    .join(KbEntity, KbEntity.entity_id == KbAttribute.entity_id)
                    .where(KbAttribute.workspace_id == workspace_id, KbAttribute.section.in_(CLAIM_SECTIONS))
                )
                .scalars()
                .all()
            )
            return [
                {
                    "attribute_id": row.attribute_id,
                    "entity_id": row.entity_id,
                    "text": row.text,
                    "quote": row.quote,
                    "source_id": row.source_id,
                    "claim_group": row.claim_group,
                    "involves": sorted({row.entity_id, *(row.refs or [])}),
                }
                for row in rows
            ]

    def set_claim_groups(
        self, workspace_id: str, groups: dict[str, str | None], *, session: Session | None = None
    ) -> None:
        if not groups:
            return
        with write_session(self.engine, session) as session:
            for attribute_id, group in groups.items():
                row = session.get(KbAttribute, attribute_id)
                if row is not None and row.workspace_id == workspace_id:
                    row.claim_group = group

    def read_entity(self, workspace_id: str, entity_id: object) -> EntityRead | None:
        entity = self.entity(workspace_id, entity_id)
        if entity is None:
            return None
        attributes = self.attributes_of(workspace_id, entity_id)
        same = self.same_claims(workspace_id, [row["claim_group"] for row in attributes])
        for row in attributes:
            if row["claim_group"]:
                # Only other documents count as corroboration; copies of one sentence on sibling formulations do not.
                row["same_claims"] = [
                    other
                    for other in same.get(row["claim_group"], [])
                    if other["attribute_id"] != row["attribute_id"] and other["source_id"] != row["source_id"]
                ]
        return {**entity, "attributes": attributes, "related": self.related_to(workspace_id, entity_id)}
