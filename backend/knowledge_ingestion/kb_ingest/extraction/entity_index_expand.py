"""Expand a seed entity graph with neighbouring evidence lines, workspace-scoped."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NotRequired, TypedDict

from kb_ingest.core.contract_entity import normalize_name, numbers
from kb_ingest.storage.entity_store_types import AttributeRow, IndexEntity

from .entity_index import _ExpandableIndex, _GraphStore, _IndexState, fuse
from .entity_index_units import EntityRow

# expand fills its budget beyond the direct evidence only with lines whose own words (not their owner's name) cover this
# share of the focus, unless the model named the sections it wants.
EXPAND_MINIMUM = 0.6

EXPAND_BYTES = 48000  # entity_search.RESULT_BYTES
_SAMPLE_CODE = re.compile(r"(?:实施例|对比例|比较例|对照例|样品)\s*[0-9A-Za-z]*")

NEIGHBOUR_LIMIT = 60
# Sections holding one recipe's own ingredients and results (older names included).
OWN_DATA_SECTIONS = ("原料", "性能", "具体数据", "测试与结果")


type _Rows = dict[object, IndexEntity | EntityRow]


_Line = TypedDict(
    "_Line",
    {
        "section": str,
        "text": str,
        "page": object,
        "block_id": str | None,
        "source_id": NotRequired[str | None],
        "asset_ref": NotRequired[str],
        "with": NotRequired[list[str]],
    },
)


class _Card(TypedDict):
    entity_id: str
    type: str
    name: str
    linked_to: list[str]
    lines: list[_Line]
    same_as_above: NotRequired[int]
    not_shown: NotRequired[int]


class _Group(TypedDict):
    source: NotRequired[object]
    source_id: NotRequired[object]
    cards: list[_Card]


class _Result(TypedDict):
    seeds: list[str]
    groups: list[_Group]
    semantic: str
    more_neighbours: NotRequired[list[str]]
    more_neighbours_count: NotRequired[int]


type _Reading = tuple[frozenset[str], set[str]]


def _reading(item: AttributeRow, names: Iterable[str]) -> _Reading:
    """What a line reports, without its wording: its values (sample codes and standard numbers removed) and its
    character pairs. Two lines of one card with the same values and shared wording are the same reading."""
    text = _SAMPLE_CODE.sub(" ", item["text"])
    flat = re.sub(r"[\d.,:%％\s]+", "|", normalize_name(text))
    return frozenset(numbers(text, names)), {
        flat[i : i + 2] for i in range(len(flat) - 1) if "|" not in flat[i : i + 2]
    }


def _size(value: object) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


@dataclass
class _Link:
    seeds: list[object] = field(default_factory=list)
    locations: int = 0


def _links(store: _GraphStore, workspace: str, seeds: list[object], to_type: str | None) -> dict[str, _Link]:
    linked: dict[str, _Link] = {}
    for seed in seeds:
        for item in store.related_to(workspace, seed, to_type):
            if item["entity_id"] in seeds:
                continue
            entry = linked.setdefault(item["entity_id"], _Link())
            entry.seeds.append(seed)
            entry.locations += len(item["locations"])
    return linked


def _candidates(
    store: _GraphStore, workspace: str, linked: Iterable[str], sections: list[str]
) -> dict[str, list[AttributeRow]]:
    return {
        entity_id: [
            item
            for item in store.attributes_of(workspace, entity_id)
            if not sections or any(key in normalize_name(item["section"]) for key in sections)
        ]
        for entity_id in linked
    }


@dataclass
class _Ranking:
    seeds: set[object]
    relevance: dict[object, float]
    fill: set[object] | None
    status: str

    def score(self, item: AttributeRow) -> tuple[bool, float]:
        direct = bool(self.seeds & set(item.get("refs") or []))
        return direct, self.relevance.get("a:" + item["attribute_id"], 0.0)


@dataclass
class _Focus:
    text: str
    sections: list[str]
    deadline: float | None


def _rank(
    index: _ExpandableIndex,
    state: _IndexState,
    candidates: dict[str, list[AttributeRow]],
    focus: _Focus,
    seeds: list[object],
) -> _Ranking:
    keys = {"a:" + item["attribute_id"] for lines in candidates.values() for item in lines}
    depth = max(len(keys), 1)
    lexical = state["lexical"].rank(focus.text, keys, depth=depth)
    units = [state["by_key"][key] for key in keys if key in state["by_key"]]
    semantic, status = index._semantic(focus.text, units, focus.deadline or time.monotonic() + 10, depth=depth)
    relevance = fuse(lexical, semantic)
    fill = (
        None
        if focus.sections
        else set(state["lexical"].rank(focus.text, keys, depth=depth, minimum=EXPAND_MINIMUM, own=True))
    )
    ranking = _Ranking(set(seeds), relevance, fill, status)
    for lines in candidates.values():
        lines.sort(key=ranking.score, reverse=True)
    return ranking


def _order(
    rows: _Rows, linked: dict[str, _Link], candidates: dict[str, list[AttributeRow]], ranking: _Ranking
) -> list[str]:
    # Prefer more seeds in common, then the best line, then the total number of link locations.
    return sorted(
        linked,
        key=lambda entity_id: (
            -len(linked[entity_id].seeds),
            tuple(-value for value in ranking.score(candidates[entity_id][0])) if candidates[entity_id] else (0, 0),
            -linked[entity_id].locations,
            rows[entity_id]["name"],
        ),
    )


def _line(item: AttributeRow, rows: _Rows, seeds: set[object]) -> _Line:
    row: _Line = {
        "section": item["section"],
        "text": item["text"],
        "page": item.get("page"),
        "block_id": item.get("block_id"),
        "source_id": item.get("source_id"),
    }
    if asset := item.get("asset_ref"):
        row["asset_ref"] = asset
    names = [rows[ref]["name"] for ref in item.get("refs") or [] if ref in rows and ref not in seeds]
    if names:
        row["with"] = names[:4]
    return row


def _group_line(item: _Line, source_id: object) -> _Line:
    row = item.copy()
    if row["source_id"] == source_id:
        del row["source_id"]
    return row


def _group(source_id: object, state: _IndexState) -> _Group:
    if source_id is None:
        return {"cards": []}
    source = state["label"](source_id) if source_id else None
    if source is None:
        return {"source_id": source_id, "cards": []}
    return {"source": source, "source_id": source_id, "cards": []}


@dataclass
class _Neighbour:
    card: _Card
    queue: list[AttributeRow]
    readings: list[tuple[str, frozenset[str], set[str]]] = field(default_factory=list)

    def repeats(self, section: str, values: frozenset[str], pairs: set[str]) -> bool:
        return bool(values) and any(
            section == seen_section and values == seen and pairs & seen_pairs
            for seen_section, seen, seen_pairs in self.readings
        )

    def render(self, source_id: object) -> _Card:
        card = self.card.copy()
        card["lines"] = [_group_line(item, source_id) for item in card["lines"]]
        if self.queue:
            card["not_shown"] = len(self.queue)
        return card


@dataclass
class _Expansion:
    rows: _Rows
    seeds: list[object]
    state: _IndexState
    neighbours: dict[str, _Neighbour]
    ranking: _Ranking
    budget: int | None
    shown: set[tuple[str, str]] = field(default_factory=set)

    def result(self) -> _Result:
        # Group by the best-ranked card of each source. Name neighbours that still have no evidence line.
        groups: dict[object, _Group] = {}
        waiting: list[str] = []
        for entity_id, neighbour in self.neighbours.items():
            row = self.rows[entity_id]
            if not neighbour.card["lines"]:
                waiting.append(row["name"])
                continue
            source_id = row["source_id"]
            group = groups.setdefault(source_id, _group(source_id, self.state))
            group["cards"].append(neighbour.render(source_id))
        final: _Result = {
            "seeds": [self.rows[seed]["name"] for seed in self.seeds],
            "groups": list(groups.values()),
            "semantic": self.ranking.status,
        }
        if waiting:
            final["more_neighbours"] = waiting
        return final

    def take(self, entity_id: str, attribute_id: str | None = None) -> bool:
        """Move the next unseen line (or the named one) onto its card if the result still fits."""
        neighbour = self.neighbours[entity_id]
        card, lines = neighbour.card, neighbour.queue
        while lines:
            position = (
                0
                if attribute_id is None
                else next((i for i, item in enumerate(lines) if item["attribute_id"] == attribute_id), None)
            )
            if position is None:
                return False
            item = lines[position]
            key = (item["section"], normalize_name(item["text"]))
            names = [self.rows[ref]["name"] for ref in item.get("refs") or [] if ref in self.rows]
            values, pairs = _reading(item, names)
            # Ingredients and results are each recipe's own data, even when another card has the same sentence.
            if (key in self.shown and item["section"] not in OWN_DATA_SECTIONS) or neighbour.repeats(
                item["section"], values, pairs
            ):
                lines.pop(position)
                card["same_as_above"] = card.get("same_as_above", 0) + 1
                if attribute_id is not None:
                    return False
                continue
            card["lines"].append(_line(item, self.rows, self.ranking.seeds))
            if self.budget is not None and _size(self.result()) > self.budget:
                card["lines"].pop()
                return False
            lines.pop(position)
            self.shown.add(key)
            if values:
                neighbour.readings.append((item["section"], values, pairs))
            return True
        return False

    def distribute(self) -> None:
        # First share direct evidence in rounds, then give empty cards their best line, then fill by relevance.
        pending = list(self.neighbours)
        while pending:
            pending = [
                eid
                for eid in pending
                if (queue := self.neighbours[eid].queue) and self.ranking.score(queue[0])[0] and self.take(eid)
            ]
        for eid, neighbour in self.neighbours.items():
            if not neighbour.card["lines"]:
                self.take(eid)
        remaining = sorted(
            (
                (self.ranking.score(item)[1], eid, item["attribute_id"])
                for eid, neighbour in self.neighbours.items()
                for item in neighbour.queue
                if self.ranking.fill is None or "a:" + item["attribute_id"] in self.ranking.fill
            ),
            reverse=True,
        )
        for _, entity_id, attribute_id in remaining:
            self.take(entity_id, attribute_id)


def expand_graph(
    index: _ExpandableIndex,
    seed_ids: Iterable[object],
    focus: str,
    *,
    to_type: str | None = None,
    sections: Iterable[str] | None = None,
    budget: int | None = EXPAND_BYTES,
    deadline: float | None = None,
    neighbour_limit: int | None = NEIGHBOUR_LIMIT,
) -> _Result:
    """Follow 相关 one step from the seeds and return each neighbour as a card of the attributes worth reading.

    Which attributes: an attribute that refers to a seed is direct evidence (expanding from 极限氧指数, the lines
    that refer to it are the LOI values) and always comes first; the rest are ranked by relevance to `focus`
    (word match and meaning, merged by rank). Each neighbour gets its best line before any gets a second; a line
    already shown on another card is not repeated. Every card says what it left out, so nothing is hidden.
    """
    state = index._load()
    rows: _Rows = {row["entity_id"]: row for row in state["rows"]}
    seeds = [seed for seed in dict.fromkeys(seed_ids) if seed in rows]
    if not seeds:
        message = "no known entity among the seeds"
        raise KeyError(message)
    store, workspace = index.entity_store, index.workspace_id
    if store is None:
        message = "'NoneType' object has no attribute 'related_to'"
        raise AttributeError(message)
    linked = _links(store, workspace, seeds, to_type)
    query = _Focus(focus, [normalize_name(section) for section in sections or []], deadline)
    candidates = _candidates(store, workspace, linked, query.sections)
    ranking = _rank(index, state, candidates, query, seeds)
    order = _order(rows, linked, candidates, ranking)[:neighbour_limit]
    neighbours: dict[str, _Neighbour] = {}
    for entity_id in order:
        row = rows[entity_id]
        card: _Card = {
            "entity_id": entity_id,
            "type": row["type"],
            "name": row["name"],
            "linked_to": [rows[seed]["name"] for seed in linked[entity_id].seeds],
            "lines": [],
        }
        neighbours[entity_id] = _Neighbour(card, list(candidates[entity_id]))
    expansion = _Expansion(rows, seeds, state, neighbours, ranking, budget)
    expansion.distribute()
    final = expansion.result()
    if len(linked) > len(order):
        final["more_neighbours_count"] = len(linked) - len(order)
    return final
