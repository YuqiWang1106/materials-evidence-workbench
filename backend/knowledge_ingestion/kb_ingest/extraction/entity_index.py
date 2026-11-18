"""Hybrid search over the entity knowledge base: word match (BM25) and meaning (embeddings), merged by rank.

Index units are built from what the knowledge base stores, each written with its owner so one line stands alone:
- an entity: type · name · aliases · first description line;
- an attribute: owner name〔source〕 · section · text · names of the entities it refers to.

Word match uses the shared IndexTokenizer (Chinese character pairs, English words, decimals, units) with the
knowledge base's own names and aliases as synonym groups, so LOI, 极限氧指数 and 氧指数 count as one word.
Embeddings use the project embedding dispatcher and are cached on disk by input hash; a unit whose vector is not
prepared yet is still found by word match. Without a dispatcher, search is word match only and says so.
"""

from __future__ import annotations

import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Final, NewType, NotRequired, Protocol, TypedDict, TypeGuard

from kb_ingest.core import fingerprint
from kb_ingest.core.contract_entity import normalize_name
from kb_ingest.knowledge_index_tokens import IndexTokenizer as IndexTokenizer
from kb_ingest.storage.entity_store_types import AttributeRow, IndexCorpus, IndexEntity, RelatedEntity

from . import entity_index_lexical as _eil
from . import entity_index_units as _eiu
from . import entity_index_vectors as _eiv
from .entity_index_cache import ScopedIndexCache
from .entity_index_lexical import ROUTE_DEPTH
from .entity_index_units import Unit

# A search hit must clear a score, not only rank. These thresholds are inherited
# from the source snapshot and are not independently validated for this release.
LEXICAL_MINIMUM = 0.4
SEMANTIC_MINIMUM = 0.3
RRF_K = 60
LINES_PER_ENTITY = 3
MAX_INPUT_BYTES = 4000


class _NameRow(TypedDict):
    entity_id: str
    type: str
    name: str
    aliases: Sequence[str]


NameRows = Sequence[_NameRow | IndexEntity | _eiu.EntityRow]


class _IndexStore(Protocol):
    def index_corpus(self, workspace_id: str) -> IndexCorpus: ...


class _GraphStore(Protocol):
    def attributes_of(self, workspace_id: str, entity_id: object) -> list[AttributeRow]: ...
    def related_to(
        self, workspace_id: str, entity_id: object, entity_type: str | None = None
    ) -> list[RelatedEntity]: ...


class _WorkspaceStore(_IndexStore, _GraphStore, Protocol):
    """A cold graph expansion reads the corpus before following links."""


class _Prepared(TypedDict):
    units: list[Unit]
    by_key: dict[object, Unit]
    lexical: _eil.Lexical


PreparedState = _Prepared


class _SearchLine(TypedDict):
    section: object
    text: object
    page: object
    block_id: object
    source_id: object
    asset_ref: NotRequired[object]


class _SearchEntity(TypedDict):
    entity_id: object
    type: object
    name: object
    aliases: NotRequired[object]
    source: NotRequired[object]
    lines: NotRequired[list[_SearchLine]]
    lines_shown_above: NotRequired[int]


class _SearchResult(TypedDict):
    entities: list[_SearchEntity]
    semantic: str
    near_names: NotRequired[list[dict[str, object]]]


class _IndexState(_Prepared):
    rows: Sequence[IndexEntity] | _eiu.EntityRows
    label: _eiu.Label


class _Group(TypedDict):
    score: float
    lines: list[_SearchLine]
    same: int


_PREPARED_CACHE: ScopedIndexCache[_Prepared] = ScopedIndexCache[_Prepared]()


def _norm(text: object) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def _bigrams(text: object) -> set[str]:
    text = normalize_name(text)
    return {text[index : index + 2] for index in range(len(text) - 1)} or {text}


def _edit_distance(a: str, b: str) -> int:
    """Damerau–Levenshtein (adjacent transposition) distance."""
    previous_row, row = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        before, previous_row, row = previous_row, row, [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            row[j] = min(previous_row[j] + 1, row[j - 1] + 1, previous_row[j - 1] + (a[i - 1] != b[j - 1]))
            if before and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], before[j - 2] + 1)
    return row[-1]


def near_names(
    query: str,
    rows: NameRows,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Names that look like a mistyped or shortened query: IOL -> LOI, 氧指 -> 氧指数."""
    words = [normalize_name(word) for word in re.split(r"[\s,，、;；]+", query) if normalize_name(word)]
    found: list[tuple[float, object, str, _NameRow | IndexEntity | _eiu.EntityRow]] = []
    for row in rows:
        for label in [row["name"], *row["aliases"]]:
            target = normalize_name(label)
            best = 0.0
            for word in words:
                if re.fullmatch(r"[a-z0-9\-]+", word) and re.fullmatch(r"[a-z0-9\-]+", target):
                    if sorted(word) == sorted(target) or _edit_distance(word, target) <= max(1, len(word) // 4):
                        best = max(best, 0.9)
                else:
                    a, b = _bigrams(word), _bigrams(target)
                    best = max(best, len(a & b) / len(a | b))
            if best >= 0.4:
                found.append((best, row["entity_id"], label, row))
    found.sort(key=lambda item: (-item[0], len(item[2])))
    result, seen = [], set()
    for _, entity_id, label, row in found:
        if entity_id not in seen:
            seen.add(entity_id)
            result.append({"entity_id": entity_id, "type": row["type"], "name": row["name"], "matched": label})
        if len(result) >= limit:
            break
    return result


def fuse(*routes: Iterable[object]) -> dict[object, float]:
    """Reciprocal rank fusion: each route adds 1/(60 + rank)."""
    scores: dict[object, float] = {}
    for route in routes:
        for rank, key in enumerate(route, 1):
            scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank)
    return scores


def _unit_allowed(
    unit: Unit,
    entity_type: object,
    section_key: str | None,
    source_key: str | None,
    label: _eiu.Label,
) -> bool:
    if entity_type and unit.type != entity_type:
        return False
    if section_key and (unit.kind != "attribute" or section_key not in normalize_name(unit.section)):
        return False
    if source_key:
        names = [unit.source_id or "", label(unit.source_id) if unit.source_id else ""]
        if not any(source_key in _norm(name) for name in names):
            return False
    return True


def _accumulate_groups(
    scores: dict[object, float],
    by_key: Mapping[object, Unit],
    lines_per_entity: int | None,
) -> dict[object, _Group]:
    grouped: dict[object, _Group] = {}
    shown: dict[tuple[object, object, str], object] = {}
    for key in sorted(scores, key=lambda item: (-scores[item], item)):
        unit = by_key[key]
        group = grouped.setdefault(unit.entity_id, {"score": scores[key], "lines": [], "same": 0})
        if unit.kind != "attribute" or (lines_per_entity is not None and len(group["lines"]) >= lines_per_entity):
            continue
        # Share copied sentences only within a known source.
        line_key = (unit.source_id, unit.section, normalize_name(unit.text))
        if unit.source_id and line_key in shown and shown[line_key] != unit.entity_id:
            group["same"] += 1
            continue
        if unit.source_id:
            shown[line_key] = unit.entity_id
        line: _SearchLine = {
            "section": unit.section,
            "text": unit.text,
            "page": unit.page,
            "block_id": unit.block_id,
            "source_id": unit.source_id,
        }
        if unit.asset_ref:
            line["asset_ref"] = unit.asset_ref
        group["lines"].append(line)
    return grouped


def _format_entities(
    grouped: dict[object, _Group],
    rows: Sequence[IndexEntity] | _eiu.EntityRows,
    label: _eiu.Label,
) -> list[_SearchEntity]:
    by_id: dict[object, IndexEntity | _eiu.EntityRow] = {row["entity_id"]: row for row in rows}
    entities: list[_SearchEntity] = []
    for entity_id, group in sorted(grouped.items(), key=lambda item: -item[1]["score"]):
        row = by_id[entity_id]
        item: _SearchEntity = {"entity_id": entity_id, "type": row["type"], "name": row["name"]}
        if row["aliases"]:
            item["aliases"] = row["aliases"]
        if row["source_id"]:
            item["source"] = label(row["source_id"])
        if group["lines"]:
            item["lines"] = group["lines"]
        if group["same"]:
            item["lines_shown_above"] = group["same"]
        entities.append(item)
    return entities


def _near_name_hits(
    query: str,
    rows: NameRows,
    entity_type: object,
    grouped: Mapping[object, object],
) -> list[dict[str, object]]:
    return [
        item
        for item in near_names(query, [row for row in rows if not entity_type or row["type"] == entity_type])
        if item["entity_id"] not in grouped
    ]


class EntityIndex[Store: _IndexStore | _GraphStore | None]:
    _scoped_cache: ScopedIndexCache[_Prepared]
    _request_state: _IndexState

    def __init__(
        self,
        entity_store: Store,
        workspace_id: str,
        *,
        vectors_root: str | Path | None = None,
        dispatcher: _eiv._EmbeddingDispatcher | None = None,
        source_labels: Callable[[], Mapping[str, object]] | Callable[[], Mapping[object, object]] | None = None,
        scoped_cache: ScopedIndexCache[_Prepared] | None = None,
    ) -> None:
        """source_labels() returns {source_id: title} (file name or patent number), read once per corpus load."""
        self.entity_store: Final = entity_store
        self.workspace_id = workspace_id
        self.vectors = _eiv.VectorCache(vectors_root, dispatcher, workspace_id=workspace_id) if vectors_root else None
        self.source_labels = source_labels or dict
        self._cached: tuple[str, _IndexState] | None = None
        self._preparing = threading.Lock()
        self._loading = threading.Lock()
        self._scoped_cache = _PREPARED_CACHE if scoped_cache is None else scoped_cache

    def request_view(
        self,
        rows: _eiu.EntityRows,
        attributes: _eiu.AttributeRows,
        entity_store: _GraphStore | None,
        source_labels: Mapping[str, object],
        *,
        source_ids: Iterable[object],
    ) -> _RequestIndex:
        """Reuse retrieval and vectors over an already clipped, request-local contribution corpus."""
        view = EntityIndex(
            entity_store, self.workspace_id, source_labels=lambda: source_labels, scoped_cache=self._scoped_cache
        )
        view.vectors = self.vectors

        labels: dict[object, object] = {}
        for key, value in source_labels.items():
            labels[key] = value

        def label(source_id: object) -> object:
            return labels.get(source_id, source_id)

        key = fingerprint(
            {
                "workspace_id": self.workspace_id,
                "source_ids": list(source_ids),
                "rows": rows,
                "attributes": attributes,
                "source_labels": source_labels,
                "tokenizer_version": IndexTokenizer.version,
                "index_version": "scoped-entity/v2",
            }
        )

        def build() -> _Prepared:
            units = _eiu.build_units(rows, attributes, label)
            return {
                "units": units,
                "by_key": {unit.key: unit for unit in units},
                "lexical": _eil.Lexical(units, _eiu.alias_groups(rows)),
            }

        derived = self._scoped_cache.get_or_build(key, build)
        # The current contribution view and publication authority stay request-local.
        view._request_state = {**derived, "rows": rows, "label": label}
        return _RequestIndex(view)

    def _load(self: _LoadableIndex) -> _IndexState:
        if not _needs_corpus(self):
            return self._request_state
        # Only one cold rebuild per instance; waiting readers reuse its completed state.
        with self._loading:
            return self._load_current()

    def _load_current(self: EntityIndex[_IndexStore | None]) -> _IndexState:
        store = self.entity_store
        if store is None:
            message = "'NoneType' object has no attribute 'index_corpus'"
            raise AttributeError(message)
        corpus = store.index_corpus(self.workspace_id)
        rows = corpus["entities"]
        items = corpus["attributes"]
        # A write replaces attribute rows, so their ids and the entity update times identify the corpus version.
        version = sha256(
            repr(
                (
                    sorted((row["entity_id"], row["updated_at"], row["name"], tuple(row["aliases"])) for row in rows),
                    sorted(item["attribute_id"] for item in items),
                )
            ).encode()
        ).hexdigest()
        labels: dict[object, object] = {}
        for key, value in self.source_labels().items():
            labels[key] = value
        version = sha256((version + repr(sorted(labels.items()))).encode()).hexdigest()
        cached = self._cached
        if cached and cached[0] == version:
            return cached[1]

        def label(source_id: object) -> object:
            return labels.get(source_id, source_id)

        units = _eiu.build_units(rows, items, label)
        state: _IndexState = {
            "rows": rows,
            "units": units,
            "by_key": {unit.key: unit for unit in units},
            "label": label,
            "lexical": _eil.Lexical(units, _eiu.alias_groups(rows)),
        }
        self._cached = (version, state)
        return state

    def prepare(self: _LoadableIndex, *, deadline: float | None = None) -> dict[str, object]:
        """Embed every unit that has no vector yet (after a confirm, or to backfill). One run at a time."""
        if self.vectors is None or self.vectors.dispatcher is None:
            return {"embedded": 0, "failures": ["embedding_not_configured"]}
        if not self._preparing.acquire(blocking=False):
            return {"embedded": 0, "failures": ["already_preparing"]}
        try:
            state = self._load()
            done, failures = self.vectors.ensure(
                [unit.index_text[:MAX_INPUT_BYTES] for unit in state["units"]],
                deadline=deadline or time.monotonic() + 600,
            )
            return {"embedded": done, "units": len(state["units"]), "failures": failures}
        finally:
            self._preparing.release()

    def prepare_in_background(self: _LoadableIndex) -> None:
        if hasattr(self, "_request_state"):
            return  # A scoped read does not prepare a new persistent corpus.
        if self.vectors is not None and self.vectors.dispatcher is not None and not self._preparing.locked():
            threading.Thread(target=self.prepare, daemon=True, name="entity-index-prepare").start()

    def _semantic(
        self: _LoadableIndex,
        query: str,
        units: Sequence[Unit],
        deadline: float,
        depth: int | None = ROUTE_DEPTH,
        minimum: float = 0.0,
    ) -> tuple[list[object], str]:
        if self.vectors is None or self.vectors.dispatcher is None:
            return [], "embedding_not_configured"
        done, failures = self.vectors.ensure([query], deadline=deadline, workers=1)
        query_vector = self.vectors.get(query)
        if query_vector is None:
            return [], (failures or ["embedding_unavailable"])[0]
        scored: list[tuple[float, object]] = []
        prepared = 0
        for unit in units:
            vector = self.vectors.get(unit.index_text[:MAX_INPUT_BYTES])
            if vector is None or len(vector) != len(query_vector):
                continue
            prepared += 1
            scored.append((math.fsum(a * b for a, b in zip(query_vector, vector, strict=True)), unit.key))
        scored.sort(key=lambda item: (-item[0], item[1]))
        status = "used" if prepared == len(units) else f"used ({prepared}/{len(units)} prepared)"
        if prepared < len(units):
            # After a restart or an interrupted preparation; word match covers the rest meanwhile.
            self.prepare_in_background()
        return [key for score, key in scored[:depth] if score > 0 and score >= minimum], status

    def search(
        self: _LoadableIndex,
        query: str,
        *,
        entity_type: object = None,
        section: object = None,
        source: object = None,
        deadline: float | None = None,
        depth: int | None = ROUTE_DEPTH,
        lines_per_entity: int | None = LINES_PER_ENTITY,
    ) -> _SearchResult:
        state = self._load()
        source_key = _norm(source) if source else None
        section_key = normalize_name(section) if section else None
        units = [
            unit for unit in state["units"] if _unit_allowed(unit, entity_type, section_key, source_key, state["label"])
        ]
        keys = {unit.key for unit in units}
        bound = depth if depth is not None else len(units)
        lexical = state["lexical"].rank(query, keys, depth=bound, minimum=LEXICAL_MINIMUM)
        semantic, status = self._semantic(
            query, units, deadline or time.monotonic() + 10, depth=bound, minimum=SEMANTIC_MINIMUM
        )
        grouped = _accumulate_groups(fuse(lexical, semantic), state["by_key"], lines_per_entity)
        result: _SearchResult = {
            "entities": _format_entities(grouped, state["rows"], state["label"]),
            "semantic": status,
        }
        if not lexical:
            suggestions = _near_name_hits(query, state["rows"], entity_type, grouped)
            if suggestions:
                result["near_names"] = suggestions
        return result


# A request view earns this type only after its complete, scoped state has been installed.
_RequestIndex = NewType("_RequestIndex", EntityIndex[_GraphStore | None])
type _LoadableIndex = EntityIndex[_IndexStore | None] | _RequestIndex
type _ExpandableIndex = EntityIndex[_WorkspaceStore | None] | _RequestIndex


def _needs_corpus(index: _LoadableIndex) -> TypeGuard[EntityIndex[_IndexStore | None]]:
    return not hasattr(index, "_request_state")
