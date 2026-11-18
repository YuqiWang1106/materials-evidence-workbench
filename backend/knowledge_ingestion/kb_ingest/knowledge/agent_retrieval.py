"""Project index results and entity records through their current, published contributions."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypedDict

from ..core.contract_entity import normalize_name
from ..extraction.entity_contract import ENTITY_TYPES
from ..extraction.entity_index import _ExpandableIndex
from ..extraction.entity_index_expand import expand_graph
from ..mcp_contract.protocol import AgentKnowledgeError, encode_evidence_ref
from ..mcp_contract.requests import check_pagination, checked_strings, checked_text
from ..storage.entity_store import EntityStore
from .agent_evidence import AgentEvidence, location_key
from .agent_publications import (
    AgentPublications,
    ContributionView,
    PublicationCache,
    PublicationPart,
    Record,
    Scope,
    records,
    string_list,
    text_field,
)


@dataclass(frozen=True, slots=True)
class SearchQuery:
    query: str
    entity_type: str | None
    section: str | None
    source: str | None
    offset: int
    limit: int | None
    source_names: list[str] | None
    source_ids: list[str] | None


@dataclass(frozen=True, slots=True)
class ExpandQuery:
    entity_ids: list[str]
    focus: str
    to_type: str | None
    sections: list[str] | None
    offset: int
    limit: int | None
    source_ids: list[str] | None


@dataclass(frozen=True, slots=True)
class EntityQuery:
    entity_id: str
    sections: list[str] | None
    offset: int
    limit: int | None
    source_ids: list[str] | None


class _Page(TypedDict):
    returned: int
    omitted: int
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class Projection:
    data: Record
    page: _Page
    warnings: list[str]


class _SearchTiming(Protocol):
    def stage(self, name: str) -> None: ...
    def finish(self, scope: Scope, raw: Mapping[str, object], page: _Page, mode: str) -> None: ...


@dataclass(slots=True)
class _Selection:
    cache: PublicationCache = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    view: ContributionView | None = None


type _Match = tuple[PublicationPart, Record, Record]


@dataclass(frozen=True, slots=True)
class _BoundAttribute:
    stored: Record
    matches: list[_Match]


class _Link(TypedDict):
    entity_id: str
    source_ids: list[str]
    relation: str


@dataclass(frozen=True, slots=True)
class _Entity:
    head: Record
    attributes: list[_BoundAttribute]
    links: list[_Link]


def _window(items: list[Record], offset: int, limit: int | None) -> tuple[list[Record], _Page]:
    result = items[offset:] if limit is None else items[offset : offset + limit]
    end = offset + len(result)
    return result, {
        "returned": len(result),
        "omitted": len(items) - len(result),
        "next_offset": end if end < len(items) else None,
    }


def _places(attribute: Record) -> list[Record]:
    return [attribute, *records(attribute.get("citations", []))]


def _attribute_matches(
    publication: PublicationPart, entity_id: object, stored: Record, location: Record
) -> Iterable[_Match]:
    for attribute in publication["graph"]["attributes"]:
        if (attribute.get("entity_id"), attribute.get("section"), attribute.get("text")) == (
            entity_id,
            stored["section"],
            stored["text"],
        ):
            for place in _places(attribute):
                if location_key(place) == location_key(location):
                    yield publication, attribute, place


def _related_location(related: list[Record], ends: set[object], location: Record) -> bool:
    return any(
        location_key(location) == location_key(saved_place)
        for saved in related
        if {saved.get("entity_a"), saved.get("entity_b")} == ends
        for saved_place in records(saved.get("locations", []))
    )


def _line_key(line: Mapping[str, object]) -> tuple[object, ...]:
    return line["section"], line["text"], line.get("block_id"), line.get("source_id")


def _retrieval_mode(raw: Mapping[str, object], warnings: list[str]) -> str:
    status = raw.get("semantic", "embedding_not_configured")
    if isinstance(status, str) and status.startswith("used"):
        if status != "used":
            warnings.append("PARTIAL_EMBEDDINGS: Lexical matching also covers units without prepared embeddings.")
        return "hybrid"
    warnings.append("LEXICAL_ONLY: Semantic retrieval is unavailable; lexical matching was used.")
    return "lexical"


_CATEGORY_NAMES = set(ENTITY_TYPES) | {name[:-1] + "ies" if name.endswith("y") else name + "s" for name in ENTITY_TYPES}


@dataclass(frozen=True, slots=True)
class AgentRetrieval:
    workspace_id: str
    entity_store: EntityStore
    publications: AgentPublications
    evidence: AgentEvidence
    scoped_index: Callable[[Scope, PublicationCache], tuple[ContributionView | None, _ExpandableIndex]]

    def _attributes(self, entity: Record, selection: _Selection) -> list[_BoundAttribute]:
        bound = []
        for stored in records(entity.get("attributes", [])):
            matches: list[_Match] = []
            for location in _places(stored):
                source_id = location.get("source_id") or stored.get("source_id")
                pub = self.publications.optional(source_id, selection.cache, selection.warnings)
                if pub:
                    for part in pub["publications"]:
                        matches.extend(_attribute_matches(part, entity["entity_id"], stored, location))
            if matches:
                bound.append(_BoundAttribute(stored, matches))
            else:
                selection.warnings.append(
                    "SOURCE_NOT_READY: An attribute does not match its published graph and was omitted."
                )
        return bound

    def _links(self, entity: Record, selection: _Selection) -> list[_Link]:
        links: list[_Link] = []
        for link in records(entity.get("related", [])):
            sources = set()
            ends = {entity["entity_id"], link["entity_id"]}
            for location in records(link.get("locations", [])):
                pub = self.publications.optional(location.get("source_id"), selection.cache, selection.warnings)
                if pub and _related_location(pub["graph"]["related"], ends, location):
                    sources.add(text_field(location, "source_id"))
            if sources:
                # Relationship kinds and names in the live store can belong to an unpublished update.
                links.append(
                    {"entity_id": text_field(link, "entity_id"), "source_ids": sorted(sources), "relation": "RELATED"}
                )
        return links

    def _published_head(self, entity: Record, sources: set[str], selection: _Selection) -> Record:
        if entity.get("source_id"):
            sources.add(text_field(entity, "source_id"))
        heads: list[Record] = []
        for source_id in sorted(sources):
            pub = self.publications.optional(source_id, selection.cache, selection.warnings)
            if pub:
                heads.extend(row for row in pub["graph"]["entities"] if row.get("entity_id") == entity["entity_id"])
        if not heads:
            raise AgentKnowledgeError("SOURCE_NOT_READY", "The entity has no readable published contribution.")
        aliases = {name for head in heads for name in string_list(head.get("add_aliases", []))}
        return {
            "entity_id": entity["entity_id"],
            "type": heads[0]["type"],
            "name": heads[0]["name"],
            "aliases": sorted(aliases),
            "source_ids": sorted(sources),
        }

    def _entity(self, entity_id: object, selection: _Selection) -> _Entity:
        if selection.view is None:
            stored = self.entity_store.read_entity(self.workspace_id, entity_id)
            entity = dict(stored) if stored is not None else None
        else:
            entity = selection.view.read_entity(self.workspace_id, entity_id)
            if entity is None and self.entity_store.entity(self.workspace_id, entity_id) is not None:
                raise AgentKnowledgeError(
                    "SOURCE_OUT_OF_SCOPE", "The entity has no contribution in the selected sources."
                )
        if entity is None:
            raise AgentKnowledgeError("REFERENCE_NOT_FOUND", "The entity was not found in this workspace.")
        bound = self._attributes(entity, selection)
        links = self._links(entity, selection)
        if selection.view is not None:
            head = {key: entity[key] for key in ("entity_id", "type", "name", "aliases", "source_ids")}
        else:
            sources = {text_field(part["task"], "source_id") for attribute in bound for part, _, _ in attribute.matches}
            sources.update(source for link in links for source in link["source_ids"])
            head = self._published_head(entity, sources, selection)
        return _Entity(head, bound, links)

    def _evidence_line(
        self, bound: _BoundAttribute, warnings: list[str], *, with_location: bool = False
    ) -> Record | None:
        refs, sources = [], set()
        for pub, attribute, place in bound.matches:
            try:
                refs.append(encode_evidence_ref(self.evidence.payload(pub, attribute, place)))
                sources.add(text_field(pub["task"], "source_id"))
            except AgentKnowledgeError as exc:
                warnings.append(f"{exc.code}: An unreadable evidence location was omitted.")
        if not refs:
            return None
        line = {key: bound.stored[key] for key in ("section", "text")}
        if with_location:
            line.update(page=bound.stored.get("page"), block_id=bound.stored.get("block_id"))
        line.update(source_ids=sorted(sources), evidence_refs=list(dict.fromkeys(refs)))
        return line

    def _lines(self, entity: _Entity, candidate: Mapping[str, object], selection: _Selection) -> list[Record]:
        lines = []
        for line in records(candidate.get("lines", [])):
            match = next((bound for bound in entity.attributes if _line_key(line) == _line_key(bound.stored)), None)
            if match and (evidence := self._evidence_line(match, selection.warnings, with_location=True)):
                lines.append(evidence)
        return lines

    def _cards(
        self, candidates: Iterable[Mapping[str, object]], selection: _Selection, seeds: set[str] | None = None
    ) -> list[Record]:
        entities = []
        for candidate in candidates:
            try:
                entity = self._entity(candidate["entity_id"], selection)
            except AgentKnowledgeError as exc:
                selection.warnings.append(f"{exc.code}: An unpublished or unavailable entity was omitted.")
                continue
            if seeds is not None and not any(link["entity_id"] in seeds for link in entity.links):
                continue
            lines = self._lines(entity, candidate, selection)
            if candidate.get("lines") and not lines:
                continue
            entities.append({**entity.head, "lines": lines})
        return entities

    def read_entity(self, request: EntityQuery) -> Projection:
        entity_id = checked_text(request.entity_id, "entity_id")
        sections = checked_strings(request.sections, "sections")
        check_pagination(request.offset, request.limit)
        selection = _Selection()
        scope = self.publications.scope(request.source_ids, selection.cache)
        selection.view = self.publications.contributions(scope, selection.cache)
        entity = self._entity(entity_id, selection)
        keys = [normalize_name(section) for section in sections or []]
        attributes = []
        for bound in entity.attributes:
            if keys and not any(key in normalize_name(bound.stored["section"]) for key in keys):
                continue
            if evidence := self._evidence_line(bound, selection.warnings):
                attributes.append(evidence)
        attributes, page = _window(attributes, request.offset, request.limit)
        return Projection(
            {"entity": entity.head, "attributes": attributes, "related": entity.links}, page, selection.warnings
        )

    def search(self, request: SearchQuery, timer: Callable[[], _SearchTiming]) -> Projection:
        query = checked_text(request.query, "query")
        check_pagination(request.offset, request.limit)
        selection = _Selection()
        if request.section and normalize_name(request.section) in _CATEGORY_NAMES:
            selection.warnings.append(
                "SUSPECT_SECTION_ENTITY_TYPE: section matches an entity category; "
                "section filters chapter text, while entity_type filters entity categories."
            )
        timing = timer()
        scope = self.publications.scope(request.source_ids, selection.cache, request.source_names)
        timing.stage("scope")
        selection.view, index = self.scoped_index(scope, selection.cache)
        timing.stage("view")
        raw = index.search(
            query,
            entity_type=request.entity_type,
            section=request.section,
            source=request.source,
            depth=None,
            lines_per_entity=None,
        )
        timing.stage("index")
        entities = self._cards(raw["entities"], selection)
        timing.stage("cards")
        entities, page = _window(entities, request.offset, request.limit)
        timing.stage("page")
        mode = _retrieval_mode(raw, selection.warnings)
        timing.finish(scope, raw, page, mode)
        return Projection({"entities": entities, "scope": scope, "retrieval_mode": mode}, page, selection.warnings)

    def expand(self, request: ExpandQuery) -> Projection:
        seeds = checked_strings(request.entity_ids, "entity_ids")
        focus = checked_text(request.focus, "focus")
        sections = checked_strings(request.sections, "sections")
        check_pagination(request.offset, request.limit)
        if not seeds:
            raise AgentKnowledgeError("INVALID_ARGUMENT", "At least one entity_id is required.")
        selection = _Selection()
        scope = self.publications.scope(request.source_ids, selection.cache)
        selection.view, index = self.scoped_index(scope, selection.cache)
        for seed in seeds:
            self._entity(seed, selection)
        raw = expand_graph(
            index, seeds, focus, to_type=request.to_type, sections=sections, budget=None, neighbour_limit=None
        )
        candidates = (
            {
                "entity_id": card["entity_id"],
                "lines": [
                    {**line, "source_id": line.get("source_id", group.get("source_id"))} for line in card["lines"]
                ],
            }
            for group in raw["groups"]
            for card in group["cards"]
        )
        entities = self._cards(candidates, selection, set(seeds))
        entities, page = _window(entities, request.offset, request.limit)
        mode = _retrieval_mode(raw, selection.warnings)
        return Projection({"entities": entities, "retrieval_mode": mode}, page, selection.warnings)
