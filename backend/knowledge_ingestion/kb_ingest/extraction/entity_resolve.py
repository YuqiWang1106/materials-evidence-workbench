"""Decide each temporary entity against the knowledge base, then rewrite ids."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NotRequired, TypedDict, Unpack

import kb_ingest.core.contract_entity as entity_keys
from kb_ingest.core import fingerprint
from kb_ingest.core.contract_entity import normalize_name as normalize_name
from kb_ingest.core.contract_entity import present_aliases

from .entity_resolve_locations import (
    Record as Record,
)
from .entity_resolve_locations import (
    Registry as Registry,
)
from .entity_resolve_locations import (
    Row as Row,
)
from .entity_resolve_locations import (
    _Location,
    _Passage,
    _record,
)
from .entity_resolve_locations import (
    highlight_regions as highlight_regions,
)
from .entity_resolve_locations import (
    location as location,
)

type Graph = Mapping[str, Sequence[Record]]


def _values(value: object) -> Iterable[object]:
    if isinstance(value, Iterable):
        return value
    msg = "Expected iterable graph values"
    raise TypeError(msg)


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    msg = "Expected a graph identifier or name"
    raise TypeError(msg)


def _aliases(record: Record) -> Iterable[object]:
    return _values(record.get("aliases") or [])


def _mention(entity: Record) -> Record:
    mentions = entity["mentions"]
    if isinstance(mentions, Sequence):
        return _record(mentions[0])
    msg = "Expected a sequence of entity mentions"
    raise TypeError(msg)


class _SavedEntity(TypedDict):
    entity_id: str
    type: str
    name: str
    add_aliases: list[str]
    sample_name: str | None
    source_id: str | None
    existing: bool


class _SavedAttribute(_Location):
    entity_id: str
    section: object
    text: object
    refs: list[str]
    citations: NotRequired[list[_Location]]


class _Related(TypedDict):
    entity_a: str
    entity_b: str
    locations: list[_Location]
    kind: NotRequired[str]


class _Decision(TypedDict):
    local_id: str
    type: str
    name: str
    decision: str
    candidates: list[Row]
    entity_id: str


class _Resolved(TypedDict):
    entities: list[_SavedEntity]
    attributes: list[_SavedAttribute]
    related: list[_Related]
    decisions: list[_Decision]


type Lookup = Callable[[str], Iterable[Record]]
type Choose = Callable[[list[Row]], Iterable[object] | None]
type Similar = Callable[[list[Record]], Mapping[str, Sequence[str]] | None]
type Pending = tuple[Record, list[Record]]


class _Services(TypedDict):
    lookup: Lookup
    choose: Choose
    similar: NotRequired[Similar | None]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Resolvers:
    lookup: Lookup
    choose: Choose
    similar: Similar | None = None


def ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def names_of(entity: Record) -> list[str]:
    return [_text(entity.get("name") or entity.get("canonical_name") or ""), *(_text(a) for a in _aliases(entity))]


def name_overlap(names: Iterable[str], candidate: Record) -> tuple[bool, bool]:
    """(exact, partial) between this paper's names and one stored entity's names and aliases."""
    if isinstance(names, str):
        names = [names]
    mine = [name for name in names if normalize_name(name)]
    stored = [
        name
        for name in [_text(candidate.get("canonical_name", "")), *(_text(a) for a in _aliases(candidate))]
        if normalize_name(name)
    ]
    keys = {normalize_name(name) for name in stored}
    exact = any(normalize_name(name) in keys for name in mine)
    partial = not exact and any(
        entity_keys.contains_name(a, b) or entity_keys.contains_name(b, a) for a in mine for b in stored
    )
    return exact, partial


def _fresh_id(workspace_id: str, entity_type: str, name: str, source_id: str, salt: str) -> str:
    return entity_type + ":" + fingerprint([workspace_id, entity_type, normalize_name(name), source_id, salt])


def _alias_needed(name: str, candidate: Record) -> bool:
    known = {normalize_name(candidate.get("canonical_name", ""))}
    known.update(normalize_name(alias) for alias in _aliases(candidate))
    return bool(name) and normalize_name(name) not in known


def _stored_aliases(name: str, aliases: Iterable[object] | None, candidate: Record | None = None) -> list[str]:
    base = candidate.get("canonical_name") if candidate else name
    incoming = present_aliases(base, [name, *(aliases or [])])
    if candidate is None:
        return incoming
    return [alias for alias in incoming if _alias_needed(alias, candidate)]


def _remember(rows: dict[str, _SavedEntity], entity: _SavedEntity) -> None:
    current = rows.get(entity["entity_id"])
    if current is None:
        rows[entity["entity_id"]] = {**entity, "add_aliases": list(entity["add_aliases"])}
        return
    for alias in entity["add_aliases"]:
        if alias not in current["add_aliases"]:
            current["add_aliases"].append(alias)


def formulation_id(workspace_id: str, source_id: str, name: str) -> str:
    return "formulation:" + fingerprint([workspace_id, source_id, normalize_name(name)])


CHARACTER_SIMILARITY = 0.5
CHARACTER_CANDIDATES = 5


def _pairs(name: str) -> set[str]:
    text = normalize_name(name)
    return {text[index : index + 2] for index in range(len(text) - 1)}


def character_similarity(names: Iterable[str], candidate: Record) -> float:
    """Best Dice score of adjacent character pairs between any two names: 三聚氰胺氰尿酸盐 and 氰尿酸三聚氰胺 share
    most pairs although neither contains the other (word order); 氰脲酸 / 氰尿酸 differ in one character."""
    best = 0.0
    for mine in names:
        a = _pairs(mine)
        for theirs in [_text(candidate.get("canonical_name", "")), *(_text(a) for a in _aliases(candidate))]:
            b = _pairs(theirs)
            if len(a) >= 2 and len(b) >= 2:
                best = max(best, 2 * len(a & b) / (len(a) + len(b)))
    return best


def candidates_for(entity: Record, stored: Iterable[Record]) -> list[Record]:
    """Stored entities whose names match this one exactly, contain it or are contained in it, plus up to five
    whose names share most character pairs with it (marked by_characters). Only an exact match can merge without
    the model."""
    rows: list[Record] = []
    similar: list[tuple[float, str, Record]] = []
    for candidate in stored:
        exact, partial = name_overlap(names_of(entity), candidate)
        if exact or partial:
            rows.append({**candidate, "exact": exact})
            continue
        score = character_similarity(names_of(entity), candidate)
        if score >= CHARACTER_SIMILARITY:
            similar.append((score, _text(candidate["entity_id"]), candidate))
    similar.sort(key=lambda item: (-item[0], item[1]))
    return rows + [
        {**candidate, "exact": False, "by_characters": True} for _, _, candidate in similar[:CHARACTER_CANDIDATES]
    ]


def _automatic(found: Sequence[Record]) -> bool:
    named = [candidate for candidate in found if not candidate.get("by_characters")]
    return len(named) == 1 and bool(named[0]["exact"])


@dataclass
class _Candidates:
    stored: dict[str, list[Record]]
    literal: dict[object, list[Record]]
    meaning: Mapping[str, Sequence[str]]

    def for_entity(self, entity: Record) -> list[Record]:
        local_id = _text(entity["local_id"])
        named = self.literal[local_id]
        known = {c["entity_id"] for c in named}
        by_id = {c["entity_id"]: c for c in self.stored[_text(entity["type"])]}
        return named + [
            {**by_id[key], "exact": False, "by_meaning": True}
            for key in self.meaning.get(local_id, [])
            if key in by_id and key not in known
        ]


def _candidate_lists(graph: Graph, services: _Resolvers) -> _Candidates:
    stored: dict[str, list[Record]] = {}
    literal: dict[object, list[Record]] = {}
    for entity in graph.get("entities", []):
        kind = _text(entity["type"])
        if kind != "formulation":
            if kind not in stored:
                stored[kind] = list(services.lookup(kind))
            literal[entity["local_id"]] = candidates_for(entity, stored[kind])
    open_items = [
        e for e in graph.get("entities", []) if e["local_id"] in literal and not _automatic(literal[e["local_id"]])
    ]
    meaning = (services.similar(open_items) or {}) if services.similar and open_items else {}
    return _Candidates(stored, literal, meaning)


@dataclass(slots=True)
class _Resolution:
    workspace_id: str
    source_id: str
    mapping: dict[object, str] = field(default_factory=dict, init=False)
    rows: dict[str, _SavedEntity] = field(default_factory=dict, init=False)
    extra: list[Record] = field(default_factory=list, init=False)
    decisions: list[_Decision] = field(default_factory=list, init=False)

    def formulation(self, entity: Record) -> None:
        # The sample code identifies a formulation even after editing its description.
        code = _text(entity.get("code") or entity["name"])
        key = formulation_id(self.workspace_id, self.source_id, code)
        self.mapping[entity["local_id"]] = key
        _remember(
            self.rows,
            {
                "entity_id": key,
                "type": "formulation",
                "name": _text(entity["name"]),
                "add_aliases": _stored_aliases(_text(entity["name"]), _aliases(entity)),
                "sample_name": normalize_name(code),
                "source_id": self.source_id,
                "existing": False,
            },
        )

    def decide(self, entity: Record, decision: str, candidates: list[Record], chosen: Record | None = None) -> None:
        mention = _mention(entity)
        if chosen is None:
            salt = "new" if decision == "new" else "uncertain:" + _text(mention["quote"])
            key = _fresh_id(self.workspace_id, _text(entity["type"]), _text(entity["name"]), self.source_id, salt)
        else:
            key = _text(chosen["entity_id"])
        self.mapping[entity["local_id"]] = key
        _remember(
            self.rows,
            {
                "entity_id": key,
                "type": _text(entity["type"]),
                "name": _text(entity["name"]) if chosen is None else _text(chosen["canonical_name"]),
                "add_aliases": _stored_aliases(_text(entity["name"]), _aliases(entity), chosen),
                "sample_name": None,
                "source_id": self.source_id if chosen is None else None,
                "existing": chosen is not None,
            },
        )
        self.decisions.append(
            {
                "local_id": _text(entity["local_id"]),
                "type": _text(entity["type"]),
                "name": _text(entity["name"]),
                "decision": decision,
                "candidates": [
                    {
                        "entity_id": c["entity_id"],
                        "canonical_name": c["canonical_name"],
                        **({"by_meaning": True} if c.get("by_meaning") else {}),
                        **({"by_characters": True} if c.get("by_characters") else {}),
                    }
                    for c in candidates
                ],
                "entity_id": key,
            }
        )
        if decision == "uncertain":
            self.extra.extend(
                {
                    "a": key,
                    "b": c["entity_id"],
                    "block_id": mention["block_id"],
                    "quote": mention["quote"],
                    "kind": "uncertain_identity",
                }
                for c in candidates
            )


def _ask(pending: list[Pending], choose: Choose, state: _Resolution) -> None:
    items: list[Row] = [
        {
            "item_id": f"i{index}",
            "type": _text(entity["type"]),
            "name": _text(entity["name"]),
            "aliases": list(_aliases(entity)),
            "quote": _mention(entity)["quote"],
            "candidates": [
                {
                    "entity_id": c["entity_id"],
                    "canonical_name": c["canonical_name"],
                    "aliases": list(_aliases(c)),
                }
                for c in candidates
            ],
        }
        for index, (entity, candidates) in enumerate(pending, start=1)
    ]
    answers: dict[object, Mapping[str, object]] = {}
    for row in choose(items) or []:
        if isinstance(row, dict) and row.get("item_id"):
            answers.setdefault(row["item_id"], row)
    for item, (entity, candidates) in zip(items, pending, strict=False):
        answer = answers.get(item["item_id"]) or {}
        known: dict[object, Record] = {c["entity_id"]: c for c in candidates}
        decision = answer.get("decision")
        chosen = known.get(answer.get("entity_id")) if decision == "existing" else None
        if chosen is not None:
            state.decide(entity, "existing", candidates, chosen)
        else:
            state.decide(entity, "new" if decision == "new" else "uncertain", candidates)


def _passage(mention: Record) -> _Passage:
    ids = mention.get("block_ids")
    block_ids = [_text(i) for i in _values(ids)] if ids else None
    return _Passage(_text(mention["block_id"]), _text(mention["quote"]), block_ids)


def _place(registry: Registry, mention: Record, source_id: str) -> _Location:
    row: _Location = {}
    _passage(mention).attach(row, registry, source_id)
    return row


def _attributes(attributes: Sequence[Record], state: _Resolution, registry: Registry) -> list[_SavedAttribute]:
    seen: set[tuple[object, ...]] = set()
    rows: list[_SavedAttribute] = []
    for attr in attributes:
        entity_id = state.mapping.get(attr["local_id"])
        if entity_id is None:
            continue
        refs: list[str] = []
        for ref in _values(attr.get("refs") or []):
            target = state.mapping.get(ref)
            if target and target != entity_id and target not in refs:
                refs.append(target)
        row: _SavedAttribute = {
            "entity_id": entity_id,
            "section": attr["section"],
            "text": attr["text"],
            "refs": refs,
        }
        _passage(attr).attach(row, registry, state.source_id)
        cites = [
            _place(registry, c, state.source_id)
            for c in map(_record, _values(attr.get("citations") or []))
            if c.get("block_id") in registry
        ]
        if cites:
            row["citations"] = cites
        key = (entity_id, attr["section"], attr["text"], attr["block_id"], attr["quote"])
        if key not in seen:
            seen.add(key)
            rows.append(row)
    return rows


def _related(links: Sequence[Record], state: _Resolution, registry: Registry) -> list[_Related]:
    incoming: list[Record] = []
    for link in links:
        places = link.get("locations") or [
            {"block_id": link.get("block_id"), "quote": link.get("quote"), "block_ids": link.get("block_ids")}
        ]
        incoming.extend({"a": link["a"], "b": link["b"], **place} for place in map(_record, _values(places)))
    grouped: dict[tuple[str, str], _Related] = {}
    for link in [*incoming, *state.extra]:
        left = state.mapping.get(link["a"], link["a"])
        right = state.mapping.get(link["b"], link["b"])
        if left == right:
            continue
        low, high = ordered_pair(_text(left), _text(right))
        place = _place(registry, link, state.source_id)
        bucket = grouped.setdefault((low, high), {"entity_a": low, "entity_b": high, "locations": []})
        if link.get("kind"):
            bucket["kind"] = _text(link["kind"])
        if place not in bucket["locations"]:
            bucket["locations"].append(place)
    return list(grouped.values())


def resolve_graph(
    graph: Graph,
    *,
    workspace_id: str,
    source_id: str,
    blocks: Sequence[Record] | Registry,
    **services: Unpack[_Services],
) -> _Resolved:
    """Resolve unique matches directly, then ask once about every ambiguous entity."""
    callbacks = _Resolvers(**services)
    registry = blocks if isinstance(blocks, Mapping) else {_text(block["block_id"]): block for block in blocks}
    state = _Resolution(workspace_id, source_id)
    candidates = _candidate_lists(graph, callbacks)
    pending: list[Pending] = []
    for entity in graph.get("entities", []):
        if entity["type"] == "formulation":
            state.formulation(entity)
            continue
        found = candidates.for_entity(entity)
        if _automatic(found):
            state.decide(entity, "existing", found, found[0])
        elif not found:
            state.decide(entity, "new", found)
        else:
            pending.append((entity, found))
    if pending:
        _ask(pending, callbacks.choose, state)
    return {
        "entities": list(state.rows.values()),
        "attributes": _attributes(graph.get("attributes", []), state, registry),
        "related": _related(graph.get("related", []), state, registry),
        "decisions": state.decisions,
    }
