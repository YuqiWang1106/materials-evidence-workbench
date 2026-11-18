"""What one extraction set aside, as the review page shows it.

The check, the table conversion and the window merge keep every line they do not pass, with its reason. This
module only reads those records and places each one beside the draft entity it belongs to. Nothing listed here
is written to the knowledge base, and listing it changes neither the draft nor its review versions.
"""

import json
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from kb_ingest.core import fingerprint
from kb_ingest.core.contract_entity import bare_property_name, normalize_name

from . import entity_merge, entity_tables_graph
from .entity_resolve import Row, highlight_regions

CHECK = "entity-check.json"
DRAFT = "entity-graph.json"
BLOCKS = "entity-blocks.json"
# Stable codes for the page's wording; a reason without a code is shown as written.
REASON_CODES = {
    "number is not in the source": "number_not_in_quote",
    "quote names another formulation": "quote_names_another_sample",
    "quote is not in the block": "quote_not_found",
    "entity is not in this paper": "entity_not_kept",
    "mention is not in the block": "mention_not_found",
    "ends must be two different listed entities": "link_ends_invalid",
    entity_merge.UNOWNED: "entity_not_kept",
    entity_merge.UNPLACED: "entity_unplaced",
    entity_tables_graph.UNATTACHED: "table_value_unattached",
    entity_tables_graph.UNPLACED: "entity_unplaced",
}

type Record = Mapping[str, object]


class HeldItem(TypedDict):
    key: str
    kind: str
    reason: str
    reason_code: str | None
    window_id: str | None
    entity_key: str | None
    entity_name: str | None
    entity_type: str | None
    section: str | None
    text: str | None
    numbers: list[str]
    quote: str | None
    block_id: str | None
    page: object
    asset_ref: object
    highlight_regions: list[Row]


class HeldView(TypedDict):
    items: list[HeldItem]
    total: int
    by_reason: dict[str, int]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _records(value: object) -> Iterator[Record]:
    if isinstance(value, list):
        yield from (item for item in value if isinstance(item, Mapping))


def _load(path: Path) -> Record:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, Mapping) else {}


def _names(entity: Record) -> Iterator[str]:
    for value in (entity.get("name"), entity.get("code"), *_strings(entity.get("aliases"))):
        if isinstance(value, str) and value:
            yield value
            if entity.get("type") == "property":
                # The draft keeps a property under its bare name; a window may still name it with its unit.
                yield bare_property_name(value)[0]


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


class _Draft:
    """Finds the draft entity a window-local entity became, by type and any of its names."""

    def __init__(self, draft: Record) -> None:
        self.keys: dict[tuple[object, str], str] = {}
        for entity in _records(draft.get("entities")):
            key = _text(entity.get("local_id"))
            if key is not None:
                for name in _names(entity):
                    self.keys.setdefault((entity.get("type"), normalize_name(name)), key)

    def key_of(self, entity: Record | None) -> str | None:
        if entity is None:
            return None
        return next(
            (
                self.keys[slot]
                for name in _names(entity)
                if (slot := (entity.get("type"), normalize_name(name))) in self.keys
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class _Source:
    """Where a set-aside record was listed: its window (None for the merge) and its position in that list."""

    window_id: str | None
    position: int
    draft: _Draft
    blocks: Record


def _item(record: Record, kind: str, source: _Source, owner: Record | None = None) -> HeldItem:
    window_id, draft, blocks = source.window_id, source.draft, source.blocks
    reason = _text(record.get("reason")) or ""
    block_id = _text(record.get("block_id"))
    block = blocks.get(block_id) if block_id is not None else None
    place: Record = block if isinstance(block, Mapping) else {}
    text = _text(record.get("text")) or _text(record.get("name")) or _text(record.get("entity"))
    return {
        # The position keeps two identical lines of one list apart.
        "key": "held:" + fingerprint([window_id, kind, source.position, text, reason])[:20],
        "kind": kind,
        "reason": reason,
        "reason_code": REASON_CODES.get(reason),
        "window_id": window_id,
        "entity_key": draft.key_of(owner),
        "entity_name": _text(owner.get("name")) if owner else None,
        "entity_type": _text(owner.get("type")) if owner else _text(record.get("type")),
        "section": _text(record.get("section")),
        "text": text,
        "numbers": _strings(record.get("numbers")),
        "quote": _text(record.get("quote")),
        "block_id": block_id,
        "page": place.get("page"),
        "asset_ref": place.get("asset_ref"),
        "highlight_regions": highlight_regions(place) if place else [],
    }


def held_view(root: Path) -> HeldView:
    """Every record the extraction under `root` set aside, in the order the paper was read."""
    draft_file, blocks = _load(root / DRAFT), _load(root / BLOCKS)
    draft = _Draft(draft_file)
    items: list[HeldItem] = []
    for window in _records(_load(root / CHECK).get("windows")):
        window_id = _text(window.get("window_id"))
        local = {entity.get("local_id"): entity for entity in _records(window.get("entities"))}
        for position, record in enumerate(_records(window.get("dropped"))):
            kind = _text(record.get("kind")) or "attribute"
            source = _Source(window_id, position, draft, blocks)
            items.append(_item(record, kind, source, local.get(record.get("local_id"))))
        # A table window lists the values its program read but could not attach to a sample.
        for position, record in enumerate(_records(window.get("skipped"))):
            kind = "entity" if "entity" in record else "table_value"
            items.append(_item(record, kind, _Source(window_id, position, draft, blocks)))
    for position, record in enumerate(_records(draft_file.get("set_aside"))):
        kind = _text(record.get("kind")) or "attribute"
        items.append(_item(record, kind, _Source(None, position, draft, blocks)))
    counts = Counter(item["reason_code"] or item["reason"] for item in items)
    return {"items": items, "total": len(items), "by_reason": dict(counts)}
