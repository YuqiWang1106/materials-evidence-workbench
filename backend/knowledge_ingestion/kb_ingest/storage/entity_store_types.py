"""Typed records, engine/session protocols, and session helpers for entity stores.

Write transactions go through kb_ingest.storage.transactions.begin_write /
write_session. External session= arguments join that caller's transaction and
must not be committed here.
"""

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import NotRequired, Protocol, TypedDict, runtime_checkable

from sqlalchemy import Engine
from sqlalchemy.orm import Session


class EngineProvider(Protocol):
    engine: Engine


class Location(TypedDict, total=False):
    source_id: object
    asset_ref: object
    page: object
    block_id: object
    quote: object
    block_ids: object
    highlight_regions: object


class ResolvedEntity(TypedDict):
    entity_id: str
    type: str
    name: str
    add_aliases: list[str]
    source_id: NotRequired[str | None]
    sample_name: NotRequired[str | None]
    existing: NotRequired[object]


class ResolvedAttribute(TypedDict):
    entity_id: str
    section: str
    text: str
    block_id: str
    quote: str
    asset_ref: NotRequired[object]
    page: NotRequired[object]
    citations: NotRequired[list[object]]
    block_ids: NotRequired[object]
    highlight_regions: NotRequired[list[object]]
    refs: NotRequired[list[object]]
    claim_group: NotRequired[str | None]


class ResolvedRelation(TypedDict):
    entity_a: str
    entity_b: str
    locations: list[Location]
    kind: NotRequired[str | None]


class ResolvedGraph(TypedDict):
    entities: list[ResolvedEntity]
    attributes: list[ResolvedAttribute]
    related: list[ResolvedRelation]


class AttributeInit(TypedDict, total=False):
    attribute_id: str
    entity_id: str
    section: str
    text: str
    asset_ref: object
    page: object
    block_id: object
    quote: object
    source_id: object
    block_ids: list[object]
    highlight_regions: list[object]
    refs: list[object]
    claim_group: str | None
    citations: list[object]


class AttributeRow(TypedDict):
    attribute_id: str
    entity_id: str
    section: str
    text: str
    asset_ref: str
    page: object
    block_id: str
    quote: str
    source_id: str | None
    refs: list[object]
    claim_group: str | None
    block_ids: NotRequired[list[object]]
    highlight_regions: NotRequired[list[object]]
    citations: NotRequired[list[dict[str, object]]]
    type: NotRequired[str]
    name: NotRequired[str]
    same_claims: NotRequired[list["AttributeRow"]]


class EntityView(TypedDict):
    entity_id: str
    type: str
    name: str
    aliases: list[str]
    source_id: str | None
    sample_name: str | None


class SourceEntity(TypedDict):
    entity_id: str
    type: str
    name: str


class Candidate(TypedDict):
    entity_id: str
    canonical_name: str
    aliases: list[str]


class SearchEntity(TypedDict):
    entity_id: str
    type: str
    name: str
    aliases: list[str]
    source_id: str | None
    code: NotRequired[str]


class RelatedEntity(TypedDict):
    entity_id: str
    type: str
    name: str
    source_id: str | None
    kind: str | None
    locations: list[object]


class GraphNode(TypedDict):
    entity_id: str
    type: str
    name: str
    aliases: list[str]
    source_id: str | None
    attribute_count: int
    degree: int
    source_count: int


class GraphEdge(TypedDict):
    a: str
    b: str
    weight: int
    kind: str | None


class EntityGraph(TypedDict):
    nodes: list[dict[str, object]]
    edges: list[dict[str, object]]
    truncated: NotRequired[bool]


class EntityRead(EntityView):
    attributes: list[AttributeRow]
    related: list[RelatedEntity]


class ClaimRef(TypedDict):
    attribute_id: str
    entity_id: str
    text: str
    quote: str
    source_id: str | None
    claim_group: str | None
    involves: list[str]


class AppendCounts(TypedDict):
    entities: int
    attributes: int
    locations: int


class IndexEntity(TypedDict):
    entity_id: str
    type: str
    name: str
    aliases: list[object]
    source_id: str | None
    updated_at: float


class IndexAttribute(TypedDict):
    attribute_id: str
    entity_id: str
    section: str
    text: str
    page: int | None
    block_id: str
    asset_ref: str
    source_id: str | None
    refs: list[object]


class IndexCorpus(TypedDict):
    entities: list[IndexEntity]
    attributes: list[IndexAttribute]


def entity_session(engine: Engine, session: Session | None) -> AbstractContextManager[Session]:
    if session is None:
        return Session(engine)
    return nullcontext(session)


@runtime_checkable
class SupportsGet(Protocol):
    def get(self, key: str, default: object = None) -> object: ...


def item_get(item: object, key: str) -> object:
    if isinstance(item, SupportsGet):
        return item.get(key)
    message = f"'{type(item).__name__}' object has no attribute 'get'"
    raise AttributeError(message)


def as_list(value: object) -> list[object]:
    items: object = value or []
    if isinstance(items, Iterable):
        return list(items)
    message = f"'{type(items).__name__}' object is not iterable"
    raise TypeError(message)


def as_record(item: object) -> dict[str, object]:
    if isinstance(item, Mapping):
        return dict(item)
    message = f"'{type(item).__name__}' object is not a mapping"
    raise TypeError(message)


def required_iterable(value: object) -> Iterable[object]:
    if isinstance(value, Iterable):
        return value
    message = f"'{type(value).__name__}' object is not iterable"
    raise TypeError(message)


def mapping_rows(record: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for item in required_iterable(record[key]):
        rows.append(as_record(item))
    return rows
