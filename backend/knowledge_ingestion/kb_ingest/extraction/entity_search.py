"""Read-only entity tools. The workspace comes from the caller, never from the model."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, TypedDict, overload

from pydantic import Field

from kb_ingest.knowledge_contracts import Id, StrictModel
from kb_ingest.storage.entity_store_types import item_get

from .entity_contract import ENTITY_TYPES

if TYPE_CHECKING:
    from kb_ingest.storage.entity_store import EntityStore
    from kb_ingest.storage.entity_store_types import EntityView, SearchEntity

    from .entity_index import _ExpandableIndex, _LoadableIndex, _SearchEntity
    from .entity_index_expand import _Result


EntityType = Literal["material", "method", "property", "organization", "instrument", "formulation"]
QUOTE_LIMIT = 160


class SearchEntitiesArgs(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    entity_type: EntityType | None = None


class SearchAttributesArgs(StrictModel):
    query: str = Field(default="", max_length=200)
    section: str | None = Field(default=None, max_length=80)
    entity_ids: list[Id] = Field(default_factory=list, max_length=40)


class RelatedArgs(StrictModel):
    entity_id: Id
    entity_type: EntityType | None = None
    also_related_to: list[Id] = Field(default_factory=list, max_length=5)


class EntityIdArgs(StrictModel):
    entity_id: Id


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    entity_type: EntityType | None = None
    section: str | None = Field(default=None, max_length=80)
    source: str | None = Field(default=None, max_length=120)


class ExpandArgs(StrictModel):
    entity_ids: list[Id] = Field(min_length=1, max_length=5)
    focus: str = Field(min_length=1, max_length=300)
    to_type: EntityType | None = None
    sections: list[str] = Field(default_factory=list, max_length=6)


class PassageArgs(StrictModel):
    source_id: Id
    block_id: Id
    around: int = Field(default=2, ge=0, le=5)


ENTITY_MODELS = {"search": SearchArgs, "expand": ExpandArgs, "read_entity": EntityIdArgs, "read_passage": PassageArgs}
ENTITY_DESCRIPTIONS = {
    "search": (
        "在知识库的实体和属性中检索：同时按字词（含知识库别名，如 LOI 与极限氧指数）和语义匹配。query "
        "写要找的材料、性能、组分作用或现象，可以是短语或一句话；entity_type 限定类型（formulation 配方、"
        "material 材料、property 性能、method 方法、organization 机构、instrument 仪器）；section "
        "限定属性段（原料、制备过程、性能、测试方法、结论、解释、说明）；source "
        "限定文献（专利号或文件名片段）。结果按实体分组，每个实体给最多 3 行命中属性（带页码）；"
        "没有字词命中时给 near_names（相近名称）。"
    ),
    "search_entities": (
        "按名称或别名查找知识库实体，可限定 entity_type（material 材料、method 方法、property 性能、"
        "organization 机构、instrument 仪器、formulation 配方）。返回 entity_id、类型、规范名和别名。"
    ),
    "search_attributes": (
        "在属性正文里查找。query 中以空格分隔的词都要出现（可为空，只按段名和实体过滤）；section "
        "限定段名（原料、制备过程、性能、测试方法、结论、解释、说明）；entity_ids 限定实体，通常先用 "
        "related_entities 得到配方再传入。返回的 text 带页码、block_id 和原文 quote。"
    ),
    "expand": (
        "从 1–5 个实体沿「相关」走一步，把相连的实体以卡片返回，每张卡片只带与 focus 相关的属性行。focus "
        "写这一步想知道的性质或材料（如「缺口冲击强度 热变形温度」）。"
        "引用种子实体的属性行（如从某性能展开时的测得值）总会给出；其余行只有本身包含 focus "
        '中大部分词时才给出。要组成、工艺等整段内容时用 sections（如 ["原料"]），这些段的行不受 focus '
        "限制。同时连到多个种子的实体排在前面（例如从一种材料和一种性能一起展开，"
        "得到既含该材料又测了该性能的配方）。to_type 只要某类实体（如 formulation）；sections 只看某些段。"
        "卡片按来源成组（groups，每组是一篇文献的配方系列），with 列出该行涉及的其他实体（如测试标准），"
        "not_shown 写明这张卡片还有多少行没给；放不下的实体只在 more_neighbours 列出名称。需要时用 "
        "read_entity 读全，或缩小 to_type、sections 再展开。"
    ),
    "read_passage": (
        "读原文中某个位置前后的段落：source_id 和 block_id 取自 search、expand 或 read_entity "
        "返回的任一属性行，around 为前后各几段。用于看工艺条件、表格整体、作者的推理，"
        "或核对一处数值的上下文。"
    ),
    "related_entities": (
        "沿「相关」从一个实体走到另一端。entity_type 限定另一端类型（如只要 formulation）；also_related_to "
        "给出更多实体编号时，只返回同时与它们全部相关的实体（例如同时含某材料并测了某性能的配方）。"
    ),
    "read_entity": (
        "读取一个实体的全部属性段（按段名）和相关实体，每行带页码和 block_id。配方的用量、条件、测得值在这里。"
    ),
}


# Entity results are only read by the model, so they may exceed the 12 KB research read limit. Under the engine's 64 KB
# per result; a turn holds about twenty of them (1 MiB per request), and past that the engine returns a read-
# limit notice.
RESULT_BYTES = 48000
ATTRIBUTE_KEYS = ("entity_id", "name", "type", "section", "text", "quote", "page", "block_id", "source_id", "asset_ref")


type _Row = Mapping[str, object]


class _Head(TypedDict, total=False):
    entity_id: str
    type: str
    name: str
    aliases: list[str]
    source_id: str
    sample_name: str


class _Truncation(TypedDict, total=False):
    omitted: int
    hint: str


class _Entities[T](_Truncation):
    entities: list[T]


class _Attributes(_Truncation):
    attributes: list[_Row]


class _WithRelated(_Head):
    related: list[_Row]


class _WithAttributes(_Head):
    attributes: list[_Row]


class _Read(_WithRelated, _Attributes):
    pass


class _SearchExtras(TypedDict, total=False):
    semantic: str
    near_names: list[dict[str, object]]


class _Hits[T](_Entities[T], _SearchExtras):
    pass


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


@overload
def _trim(row: EntityView | SearchEntity, keys: Sequence[str]) -> _Head: ...


@overload
def _trim(row: _Row, keys: Sequence[str] = ATTRIBUTE_KEYS) -> _Row: ...


def _trim(row: _Row, keys: Sequence[str] = ATTRIBUTE_KEYS) -> _Row:
    trimmed = {key: row[key] for key in keys if row.get(key) not in (None, "", [])}
    quote = trimmed.get("quote")
    if isinstance(quote, str) and len(quote) > QUOTE_LIMIT:
        trimmed["quote"] = quote[:QUOTE_LIMIT] + "…"
    return trimmed


@overload
def _fit[T](result: _SearchExtras, key: Literal["entities"], rows: Sequence[T], hint: str) -> _Hits[T]: ...


@overload
def _fit(result: _WithRelated, key: Literal["attributes"], rows: Sequence[_Row], hint: str) -> _Read: ...


@overload
def _fit(result: _WithAttributes, key: Literal["related"], rows: Sequence[_Row], hint: str) -> _Read: ...


@overload
def _fit(result: _Row, key: Literal["attributes"], rows: Sequence[_Row], hint: str) -> _Attributes: ...


def _fit[T](result: _Row, key: str, rows: Sequence[T], hint: str) -> _Row:
    """Add rows while the whole result stays inside the chat read limit; say how many were left out."""
    kept: list[T] = []
    for row in rows:
        if _size({**result, key: [*kept, row], "omitted": len(rows), "hint": hint}) > RESULT_BYTES:
            break
        kept.append(row)
    fitted = {**result, key: kept}
    if len(kept) < len(rows):
        fitted.update(omitted=len(rows) - len(kept), hint=hint)
    return fitted


def search_entities(
    store: EntityStore, workspace_id: str, name: str, entity_type: str | None = None
) -> _Entities[_Head]:
    if entity_type is not None and entity_type not in ENTITY_TYPES:
        raise ValueError("unknown entity type")
    rows = [
        _trim(row, ("entity_id", "type", "name", "aliases", "source_id"))
        for row in store.search_entities(workspace_id, name, entity_type)
    ]
    return _fit({}, "entities", rows, "名称更具体，或加上 entity_type")


def search_attributes(
    store: EntityStore,
    workspace_id: str,
    query: str | None = "",
    section: str | None = None,
    entity_ids: Collection[str] | None = None,
) -> _Attributes:
    if not (query or "").strip() and not section and not entity_ids:
        raise ValueError("query, section or entity_ids required")
    rows = [_trim(row) for row in store.search_attributes(workspace_id, query, section, entity_ids, limit=200)]
    return _fit({}, "attributes", rows, "加上 query 词、section 或 entity_ids 缩小范围")


def related_entities(
    store: EntityStore,
    workspace_id: str,
    entity_id: str,
    entity_type: str | None = None,
    also_related_to: Iterable[str] | None = None,
) -> _Entities[_Row]:
    ids = [entity_id, *(also_related_to or [])]
    for seed in ids:
        if store.entity(workspace_id, seed) is None:
            raise KeyError(seed)
    rows = (
        store.related_to_all(workspace_id, ids, entity_type)
        if len(ids) > 1
        else store.related_to(workspace_id, entity_id, entity_type)
    )
    compact: list[_Row] = []
    for row in rows:
        item = {**_trim(row, ("entity_id", "type", "name", "source_id", "kind"))}
        item["location_count"] = len(row["locations"])
        if row["locations"]:
            first = row["locations"][0]
            keys = ("quote", "page", "block_id")
            item["example"] = _trim({key: item_get(first, key) for key in keys}, keys)
        compact.append(item)
    return _fit({}, "entities", compact, "用 entity_type 只看一类，或用 also_related_to 求交集")


def read_entity(store: EntityStore, workspace_id: str, entity_id: object) -> _Read:
    row = store.read_entity(workspace_id, entity_id)
    if row is None:
        raise KeyError(entity_id)
    attributes: list[_Row] = []
    for item in row["attributes"]:
        # No quote: the model reads the text; page and block_id stay for citing and read_passage.
        trimmed = {**_trim(item, ("section", "text", "page", "block_id", "source_id", "asset_ref"))}
        if item.get("citations"):
            # Other places saying the same; only where they are, the text is the attribute's own.
            trimmed["also_at"] = [
                _trim(other, ("page", "block_id", "source_id", "asset_ref")) for other in item["citations"]
            ]
        if item.get("same_claims"):
            trimmed["same_claims"] = [
                _trim(other, ("entity_id", "name", "text", "page", "block_id", "source_id", "asset_ref"))
                for other in item["same_claims"]
            ]
        attributes.append(trimmed)
    related: list[_Row] = [
        {**_trim(item, ("entity_id", "type", "name", "kind")), "location_count": len(item["locations"])}
        for item in row["related"]
    ]
    head = _trim(row, ("entity_id", "type", "name", "aliases", "source_id", "sample_name"))
    result = _fit(
        {**head, "related": related[:60]},
        "attributes",
        attributes,
        "用 expand 从相关实体出发并指定 sections，或用 search 加 section 和 source，读取其余属性",
    )
    if len(related) > 60 or _size(result) > RESULT_BYTES:
        with_attributes: _WithAttributes = {**head, "attributes": result["attributes"]}
        result = _fit(
            with_attributes,
            "related",
            related,
            "用 expand 从本实体出发并指定 to_type 读取其余相关实体",
        )
    return result


def search(
    index: _LoadableIndex, query: str, entity_type: object = None, section: object = None, source: object = None
) -> _Hits[_SearchEntity]:
    result = index.search(query, entity_type=entity_type, section=section, source=source)
    extra: _SearchExtras = {}
    if "semantic" in result:
        extra["semantic"] = result["semantic"]
    if "near_names" in result:
        extra["near_names"] = result["near_names"]
    return _fit(extra, "entities", result["entities"], "加上 entity_type、section 或 source 缩小范围")


def expand(
    index: _ExpandableIndex,
    entity_ids: Iterable[object],
    focus: str,
    to_type: str | None = None,
    sections: Iterable[str] | None = None,
) -> _Result:
    from kb_ingest.extraction.entity_index_expand import expand_graph

    return expand_graph(index, entity_ids, focus, to_type=to_type, sections=sections or None, budget=RESULT_BYTES)
