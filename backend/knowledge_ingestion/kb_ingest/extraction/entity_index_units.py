"""Index units built from stored entity and attribute rows."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import NotRequired, TypedDict

from kb_ingest.core.contract_entity import DESCRIPTION_SECTION
from kb_ingest.storage.entity_store_types import IndexAttribute, IndexEntity

Label = Callable[[object], object]

TYPE_LABELS = {
    "formulation": "配方",
    "material": "材料",
    "method": "方法",
    "property": "性能",
    "organization": "机构",
    "instrument": "仪器",
}


class EntityRow(TypedDict):
    entity_id: str
    type: str
    name: str
    aliases: Sequence[str]
    source_id: object


class AttributeRow(TypedDict):
    entity_id: str
    section: str
    text: str
    attribute_id: str
    source_id: NotRequired[object]
    refs: NotRequired[Sequence[object]]
    page: NotRequired[object]
    block_id: NotRequired[object]
    asset_ref: NotRequired[object]


class Unit:
    __slots__ = (
        "key",
        "kind",
        "entity_id",
        "type",
        "name",
        "source_id",
        "section",
        "text",
        "page",
        "block_id",
        "index_text",
        "own_text",
        "asset_ref",
    )
    key: object
    kind: object
    entity_id: object
    type: object
    name: object
    source_id: object
    section: object
    text: object
    page: object
    block_id: object
    index_text: str
    own_text: str | None
    asset_ref: object

    def __init__(self, **values: object) -> None:
        for name in self.__slots__:
            setattr(self, name, values.get(name))


_PATENT = re.compile(r"(?<![A-Za-z])(?:CN|US|WO|EP|JP|KR)\s?\d{6,}\s?[A-Z]\d?(?![A-Za-z0-9])", re.I)


EntityRows = Sequence[EntityRow]
AttributeRows = Sequence[AttributeRow]
StoredEntityRows = Sequence[IndexEntity]
StoredAttributeRows = Sequence[IndexAttribute]
UnitRows = EntityRows | StoredEntityRows
UnitAttrs = AttributeRows | StoredAttributeRows


def short_source(label: object) -> str:
    """The patent number in a source title; other titles are left out of the indexed text (their words would
    match every line of that paper). The full title still serves the source filter."""
    match = _PATENT.search(str(label or ""))
    return match.group(0).replace(" ", "").upper() if match else ""


def build_units(rows: UnitRows, attributes: UnitAttrs, source_label: Label | None = None) -> list[Unit]:
    """Index units from entity rows and attribute rows (plain dicts)."""
    label = source_label or (lambda source_id: source_id)
    by_id: dict[object, EntityRow | IndexEntity] = {row["entity_id"]: row for row in rows}
    first_description: dict[str, str] = {}
    for item in attributes:
        if item["section"] == DESCRIPTION_SECTION and item["entity_id"] not in first_description:
            first_description[item["entity_id"]] = item["text"]
    units: list[Unit] = []
    for row in rows:
        parts = [TYPE_LABELS.get(row["type"], row["type"]), row["name"]]
        if row["aliases"]:
            parts.append("别名 " + "、".join(str(alias) for alias in row["aliases"]))
        if row["entity_id"] in first_description:
            parts.append(first_description[row["entity_id"]])
        units.append(
            Unit(
                key="e:" + row["entity_id"],
                kind="entity",
                entity_id=row["entity_id"],
                type=row["type"],
                name=row["name"],
                source_id=row["source_id"],
                index_text=" · ".join(parts),
            )
        )
    for item in attributes:
        owner = by_id.get(item["entity_id"])
        if owner is None:
            continue
        source = item.get("source_id") or owner["source_id"]
        short = short_source(label(source)) if source else ""
        head = owner["name"] + (f"〔{short}〕" if short else "")
        refs = [by_id[ref]["name"] for ref in item.get("refs") or [] if ref in by_id]
        parts = [head, item["section"], item["text"]] + (["、".join(refs)] if refs else [])
        # The owner name makes a line findable on its own; own_text is the line without it, for judging the line itself.
        units.append(
            Unit(
                key="a:" + item["attribute_id"],
                kind="attribute",
                entity_id=owner["entity_id"],
                type=owner["type"],
                name=owner["name"],
                source_id=source,
                section=item["section"],
                text=item["text"],
                page=item.get("page"),
                block_id=item.get("block_id"),
                asset_ref=item.get("asset_ref"),
                index_text=" · ".join(parts),
                own_text=" · ".join(parts[1:]),
            )
        )
    return units


def alias_groups(rows: UnitRows) -> list[list[str]]:
    """Each entity's name and aliases are one synonym group (only groups with more than one label count)."""
    groups: list[list[str]] = []
    for row in rows:
        labels = sorted({str(label) for label in [row["name"], *row["aliases"]] if label and len(str(label)) <= 128})
        if len(labels) > 1:
            groups.append(labels[:100])
    return groups[:1000]
