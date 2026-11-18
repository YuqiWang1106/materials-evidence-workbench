"""Conservative input selection and bounded source batches for direct extraction.

These helpers never modify IR blocks. A covered native reading remains in the
selection audit; compact projections are request data, not replacement evidence.
Character limits measure ``json.dumps(..., ensure_ascii=False)`` of source lists,
so callers must still budget their instructions and response schema separately.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any

Block = dict[str, Any]
_TITLES = {"title", "paragraph_title", "section_title", "heading"}
_METHOD = re.compile(
    r"\b(methods?|experimental|materials? and methods?|procedure|testing)\b|实验|试验|测试|制备|材料与方法", re.I
)


def _document_ids(blocks: list[Block]) -> dict[str, str]:
    return {
        value: f"d{i + 1}" for i, value in enumerate(dict.fromkeys(str(block.get("asset_ref", "")) for block in blocks))
    }


def _geometry(block: Block) -> tuple[list[float], str, str] | None:
    region = block.get("anchor", {}).get("coordinate_region", {})
    bbox = region.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        values = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values) or values[0] >= values[2] or values[1] >= values[3]:
        return None
    # Missing origin is the document IR's rendered page default.
    return values, region.get("unit", ""), region.get("origin", "top_left")


def _contains_reading(text: str, reading: str) -> bool:
    """Whitespace is the only normalization; don't match 20 inside 120 or -20."""
    if not reading:
        return False
    start = 0
    while (index := text.find(reading, start)) >= 0:
        left = text[index - 1] if index else ""
        right = text[index + len(reading) : index + len(reading) + 1]
        # ASCII token boundaries protect numeric values and split English words.
        left_bad = bool(left and re.match(r"[A-Za-z0-9.,+−-]", reading[0]) and re.match(r"[A-Za-z0-9.,+−-]", left))
        right_bad = bool(right and re.match(r"[A-Za-z0-9.,]", reading[-1]) and re.match(r"[A-Za-z0-9.,]", right))
        if not left_bad and not right_bad:
            return True
        start = index + 1
    return False


def select_direct_blocks(blocks: list[Block]) -> tuple[list[Block], dict[str, Any]]:
    """Cover only redundant native readings proven by source and geometry."""
    alternatives: list[dict[str, Any]] = []
    covered: set[str] = set()
    candidates: dict[tuple[Any, Any], list[Block]] = defaultdict(list)
    for block in blocks:
        if block.get("kind") != "native_text" and block.get("source") != "pymupdf":
            candidates[(block.get("asset_ref"), block.get("page"))].append(block)
    for native in blocks:
        if (
            native.get("kind") != "native_text"
            or native.get("source") != "pymupdf"
            or native.get("requires_image_verification")
        ):
            continue
        geometry = _geometry(native)
        if geometry is None or native.get("page") is None or not native.get("asset_ref"):
            continue
        reading = re.sub(r"\s+", " ", str(native.get("text", ""))).strip()
        for parsed in candidates[(native.get("asset_ref"), native.get("page"))]:
            other_geometry = _geometry(parsed)
            if (
                other_geometry is None
                or geometry[1:] != other_geometry[1:]
                or parsed.get("requires_image_verification")
            ):
                continue
            inner, outer = geometry[0], other_geometry[0]
            if not (outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]):
                continue
            text = re.sub(r"\s+", " ", str(parsed.get("text", ""))).strip()
            if parsed.get("kind") == "table_cell":
                # IR cells carry their parent table's box, not cell geometry.
                continue
            if parsed.get("kind") in {"table", "image", "chart", "figure"} and (inner != outer or reading != text):
                continue
            if not _contains_reading(text, reading):
                continue
            covered.add(native["block_id"])
            alternatives.append(
                {
                    "block_id": native["block_id"],
                    "covered_by": parsed["block_id"],
                    "reason": "same_page_contained_geometry_verbatim_reading",
                    "normalization": "whitespace_only",
                    "asset_ref": native["asset_ref"],
                    "page": native["page"],
                    "native_bbox": inner,
                    "covering_bbox": outer,
                }
            )
            break
    # Geometry assigns a native reading to a table region, never to its cells
    # and never proves agreement. Keep it separate from verbatim redundancies.
    cell_parents = {b.get("table_region") for b in blocks if b.get("kind") == "table_cell"}
    tables = [b for b in blocks if b.get("kind") == "table" and b["block_id"] in cell_parents]
    table_readings = []
    selected = []
    fragments = []
    for block in blocks:
        if block["block_id"] in covered:
            continue
        geometry = _geometry(block)
        if (
            block.get("kind") == "native_text"
            and block.get("source") == "pymupdf"
            and geometry is not None
            and geometry[1]
            and block.get("asset_ref")
            and block.get("page") is not None
        ):
            matches = []
            for table in tables:
                outer = _geometry(table)
                if (
                    outer is None
                    or geometry[1:] != outer[1:]
                    or (block["asset_ref"], block["page"]) != (table.get("asset_ref"), table.get("page"))
                ):
                    continue
                a, b = geometry[0], outer[0]
                if b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3]:
                    matches.append(table)
            if len(matches) == 1:
                parent = matches[0]["block_id"]
                block = {**block, "table_reading_alternative": parent}
                table_readings.append(
                    {
                        "block_id": block["block_id"],
                        "table_region": parent,
                        "asset_ref": block["asset_ref"],
                        "page": block["page"],
                        "anchor": block["anchor"],
                        "text_agreement": "not_established",
                        "row_alignment": "not_established",
                    }
                )
        selected.append(block)
        if block.get("kind") != "table":
            continue
        # These are literal parser metadata, not text guessed from the table
        # body. Keep the parent region locator; do not invent cell geometry.
        for kind in ("caption", "footnote"):
            texts = []
            for key in (kind, kind + "s"):
                values = block.get(key, [])
                if values is None:
                    continue
                for text in [values] if isinstance(values, str) else values:
                    if isinstance(text, str) and text.strip() and text not in texts:
                        texts.append(text)
            for index, text in enumerate(texts):
                fragment = {
                    key: block[key]
                    for key in ("asset_ref", "page", "source", "anchor", "ir_region", "requires_image_verification")
                    if key in block
                }
                fragment.update(
                    block_id=f"{block['block_id']}:{kind}:{index}",
                    kind="table_" + kind,
                    text=text,
                    table_region=block["block_id"],
                    source_parent_block_id=block["block_id"],
                )
                selected.append(fragment)
                fragments.append(
                    {
                        "block_id": fragment["block_id"],
                        "source_parent_block_id": block["block_id"],
                        "metadata_kind": kind,
                        "metadata_index": index,
                    }
                )
    return selected, {
        "input_blocks": len(blocks),
        "selected_blocks": len(selected),
        "covered_alternatives": alternatives,
        "document_ids": _document_ids(blocks),
        "table_metadata_fragments": fragments,
        "table_reading_alternatives": table_readings,
        "scope": (
            "Only proven redundant readings are covered; contained table alternatives "
            "remain distinct and are audited if not aligned to split rows."
        ),
    }


def compact_public(blocks: list[Block], *, document_ids: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Project evidence with original block IDs and stable short document IDs.

    Pass the selection audit's full ``document_ids`` for every subset in a run.
    Missing aliases fail instead of silently reassigning provenance.
    """
    aliases = _document_ids(blocks) if document_ids is None else document_ids
    result = []
    for block in blocks:
        row = {"block_id": block["block_id"], "asset_ref": aliases[str(block.get("asset_ref", ""))]}
        for key in ("page", "kind", "source", "text", "table_region", "reading_sha256", "reading_risks", "diagnostics"):
            if block.get(key) is not None:
                row[key] = block[key]
        if block.get("requires_image_verification"):
            row["requires_image_verification"] = True
        cell = block.get("anchor", {}).get("table_cell")
        if cell:
            row["cell"] = {
                key: cell[key]
                for key in ("row", "column", "row_span", "col_span")
                if key in cell and (key in {"row", "column"} or cell[key] != 1)
            }
        result.append(row)
    return result


def direct_input_batches(
    blocks: list[Block],
    *,
    max_chars: int = 160000,
    context_chars: int = 16000,
    document_ids: dict[str, str] | None = None,
    max_context_blocks: int = 40,
    max_table_cells: int = 24,
) -> list[dict[str, Any]]:
    """Keep one document whole when its serialized source fits the input budget.

    Larger documents use section and table batches, splitting large tables
    by row. Complete tables run first; neighboring prose can share one batch
    across physical pages of the same original. Each source cell occurs exactly once as a target;
    split table parents are accounted for by their cells and explicit metadata, not resubmitted
    as full bodies. Oversized indivisible
    evidence is returned with a blocking reason, never truncated or discarded.
    Source blocks omitted from optional context are listed for explicit scope.
    """
    if max_chars < 1 or context_chars < 0 or max_context_blocks < 0:
        raise ValueError("Target budget must be positive and context limits nonnegative")
    if type(max_table_cells) is not int or max_table_cells < 1:
        raise ValueError("The table output cell limit must be a positive integer")
    aliases = _document_ids(blocks) if document_ids is None else document_ids

    def size(rows: list[Block]) -> int:
        return len(json.dumps(compact_public(rows, document_ids=aliases), ensure_ascii=False))

    def cell_count(rows: list[Block]) -> int:
        return sum(block.get("kind") == "table_cell" for block in rows)

    if (
        blocks
        and len({block.get("asset_ref") for block in blocks}) == 1
        and size(blocks) <= max_chars
        and cell_count(blocks) <= max_table_cells
    ):
        return [
            {
                "targets": blocks[:],
                "context": [],
                "context_not_included": [],
                "blocked_reason": None,
                "target_table_cells": cell_count(blocks),
                "max_table_cells": max_table_cells,
                "context_only_table_regions": [],
                "table_context_limits": [],
                "unresolved_table_readings": [],
                "target_json_chars": size(blocks),
                "context_json_chars": 0,
                "required_context_not_included": [],
            }
        ]

    by_id = {block["block_id"]: block for block in blocks}
    positions = {block["block_id"]: i for i, block in enumerate(blocks)}
    table_cells: dict[str, list[Block]] = defaultdict(list)
    table_metadata: dict[str, list[Block]] = defaultdict(list)
    table_readings: dict[str, list[Block]] = defaultdict(list)
    for block in blocks:
        if block.get("kind") == "table_cell" and by_id.get(block.get("table_region"), {}).get("kind") == "table":
            table_cells[block["table_region"]].append(block)
        if (
            block.get("source_parent_block_id")
            and by_id.get(block["source_parent_block_id"], {}).get("kind") == "table"
        ):
            table_metadata[block["source_parent_block_id"]].append(block)
        if by_id.get(block.get("table_reading_alternative"), {}).get("kind") == "table":
            table_readings[block["table_reading_alternative"]].append(block)
    batches: list[dict[str, Any]] = []
    current: list[Block] = []

    def emit(targets: list[Block], required: list[Block] | None = None, *, parent: str | None = None) -> None:
        target_size = size(targets)
        count = cell_count(targets)
        unread = [b["block_id"] for b in table_readings.get(parent, [])]
        batches.append(
            {
                "targets": targets,
                "_required": required or [],
                "target_table_cells": count,
                "max_table_cells": max_table_cells,
                "context_only_table_regions": [parent] if parent else [],
                "table_context_limits": ([parent + ":header_rows_not_explicitly_identified"] if parent else [])
                + ([parent + ":alternative_reading_not_aligned"] if unread else []),
                "unresolved_table_readings": unread,
                "target_json_chars": target_size,
                "blocked_reason": (
                    "target_exceeds_table_output_budget"
                    if count > max_table_cells
                    else "target_exceeds_character_budget"
                    if target_size > max_chars
                    else None
                ),
            }
        )

    def flush() -> None:
        if current:
            emit(current[:])
            current.clear()

    for block in blocks:
        if block.get("kind") == "table_cell" and block.get("table_region") in table_cells:
            continue
        if block.get("source_parent_block_id") in table_metadata:
            continue
        if block.get("table_reading_alternative") in table_readings:
            continue
        if block.get("kind") == "table":
            cells = table_cells.get(block["block_id"], [])
            metadata = table_metadata.get(block["block_id"], [])
            group = [block, *metadata, *cells, *table_readings.get(block["block_id"], [])]
            if (size(group) <= max_chars and len(cells) <= max_table_cells) or not cells:
                emit(group)
                continue
            # Overlapping row spans are indivisible, including a multirow header.
            rows: dict[int, list[Block]] = defaultdict(list)
            for item in cells:
                row = item.get("anchor", {}).get("table_cell", {}).get("row")
                if not isinstance(row, int):
                    raise ValueError("Table cells require integer source row coordinates")
                rows[row].append(item)
            row_groups: list[list[Block]] = []
            end_row = -1
            for row, items in sorted(rows.items()):
                if not row_groups or row > end_row:
                    row_groups.append([])
                row_groups[-1].extend(items)
                end_row = max(
                    end_row,
                    *(
                        row + max(1, int(item.get("anchor", {}).get("table_cell", {}).get("row_span", 1))) - 1
                        for item in items
                    ),
                )
            # With no explicit header-row roles, repeat the leading source row
            # as positional context without claiming it is the complete header.
            leading = row_groups[0]
            required = [*metadata, *leading]
            packed: list[Block] = metadata[:]
            for row_group in row_groups:
                if cell_count(packed) and (
                    size([*packed, *row_group]) > max_chars or cell_count(packed) + len(row_group) > max_table_cells
                ):
                    emit(packed, required, parent=block["block_id"])
                    packed = []
                packed.extend(row_group)
            if packed:
                emit(packed, required, parent=block["block_id"])
            continue
        if current and (
            (block.get("asset_ref"), block.get("page")) != (current[-1].get("asset_ref"), current[-1].get("page"))
            or (block.get("kind") in _TITLES and current[0].get("kind") not in _TITLES)
            or size([*current, block]) > max_chars
            or cell_count([*current, block]) > max_table_cells
        ):
            flush()
        current.append(block)
    flush()
    batches.sort(key=lambda batch: not any(block.get("kind") in {"table", "table_cell"} for block in batch["targets"]))
    # Page/title boundaries preserve locators but need not each spend a model
    # request. Pack neighboring prose from one original within the same bound;
    # complete tables and rows with required header context remain atomic.
    merged: list[dict[str, Any]] = []
    for batch in batches:
        if merged:
            previous = merged[-1]
            combined = previous["targets"] + batch["targets"]
            if (
                not previous["blocked_reason"]
                and not batch["blocked_reason"]
                and not previous["_required"]
                and not batch["_required"]
                and not previous["context_only_table_regions"]
                and not batch["context_only_table_regions"]
                and not any(block.get("kind") in {"table", "table_cell"} for block in combined)
                and len({block.get("asset_ref") for block in combined}) == 1
                and size(combined) <= max_chars
            ):
                previous["targets"] = combined
                previous["target_json_chars"] = size(combined)
                continue
        merged.append(batch)
    batches = merged

    # Method-section membership is a generic source heading relation, not a task graph.
    method_blocks: dict[str, int] = {}
    in_methods: dict[str, bool] = {}
    method_offsets: dict[str, int] = {}
    for block in blocks:
        asset = str(block.get("asset_ref", ""))
        if block.get("kind") in _TITLES:
            in_methods[asset] = bool(_METHOD.search(str(block.get("text", ""))))
            method_offsets[asset] = 0
        if in_methods.get(asset) and block.get("kind") not in {
            "table",
            "table_cell",
            "native_text",
            "header",
            "ref_text",
        }:
            method_blocks[block["block_id"]] = method_offsets[asset]
            method_offsets[asset] += 1

    for batch in batches:
        targets = batch["targets"]
        target_ids = {block["block_id"] for block in targets}
        required = list(
            {
                block["block_id"]: block for block in batch.pop("_required") if block["block_id"] not in target_ids
            }.values()
        )
        context: list[Block] = []
        required_omitted = []
        for block in required:
            if len(context) < max_context_blocks and size([*context, block]) <= context_chars:
                context.append(block)
            else:
                required_omitted.append(block["block_id"])
        if required_omitted:
            batch["blocked_reason"] = batch["blocked_reason"] or "required_table_context_exceeds_budget"
        required_ids = {block["block_id"] for block in required}
        assets = {block.get("asset_ref") for block in targets}
        pages = {block.get("page") for block in targets}
        center = min(positions[block["block_id"]] for block in targets)
        optional = [
            block
            for block in blocks
            if block.get("asset_ref") in assets and block["block_id"] not in target_ids | required_ids
        ]
        optional.sort(
            key=lambda block: (
                method_blocks.get(block["block_id"], math.inf),
                block.get("page") not in pages,
                block.get("kind") in {"native_text", "table_cell"},
                abs(positions[block["block_id"]] - center),
            )
        )
        omitted = required_omitted[:]
        for block in optional:
            # Table bodies and unselected cells must not undo row partitioning
            # through optional context. Required leading rows were added above.
            if (
                block.get("kind") not in {"table", "table_cell"}
                and not block.get("table_reading_alternative")
                and len(context) < max_context_blocks
                and size([*context, block]) <= context_chars
            ):
                context.append(block)
            else:
                omitted.append(block["block_id"])
        batch.update(
            context=context,
            context_not_included=omitted,
            required_context_not_included=required_omitted,
            context_json_chars=size(context) if context else 0,
        )
    return batches
