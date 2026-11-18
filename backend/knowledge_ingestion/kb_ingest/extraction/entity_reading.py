"""Choose entity extraction input without changing the full document IR or its IDs.

``select_primary_reading(ir_blocks(documents))`` returns ``(selected, audit)``.
The audit can be saved as entity-reading-selection.json: every decision names an
existing block ID, and suppressed native blocks name their covering MinerU IDs.
It is a reading preference, not a claim that either reading is scientifically
verified. Callers must allocate supplemental block IDs from the full input.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Buffer, Container, Mapping, Sequence
from typing import Required, SupportsFloat, SupportsIndex, TypedDict

Box = tuple[float, float, float, float]
Geometry = tuple[Box, tuple[float, float]]
FloatInput = str | Buffer | SupportsFloat | SupportsIndex  # the argument set of float()


class ReadingRegion(TypedDict, total=False):
    """Region keys read here; none is validated. Only width and height are typed narrowly.

    They must be what float() accepts (FloatInput, typeshed's ConvertibleToFloat). At run time a value that
    float() rejects, such as None or a list, only makes _geometry return no geometry, so the
    runtime is more tolerant than this type, which rejects such values statically.
    """

    bbox: object
    unit: object
    origin: object
    coordinate_space: object
    precision: object
    page: object
    width: FloatInput
    height: FloatInput


class ReadingAnchor(TypedDict, total=False):
    """The anchor key read here. A missing or None region means no geometry."""

    coordinate_region: ReadingRegion | None


class ReadingBlock(TypedDict, total=False):
    """Preconditions select_primary_reading relies on; none of these values is validated.

    block_id is the only key indexed directly. Every other key is read with get(), so a
    missing key behaves like None. Region values, geometry included, are raw document values
    that _geometry converts inside its try block. Extra producer fields belong in a TypedDict
    that extends ReadingBlock; select_primary_reading returns that same type.
    """

    block_id: Required[object]
    asset_ref: object
    page: object
    kind: object
    source: object
    text: object
    table_region: object
    ir_region: object
    reading_risks: Container[object] | None
    anchor: ReadingAnchor | None


class ReadingAudit(TypedDict):
    version: str
    selected_block_ids: list[object]
    suppressed_block_ids: list[object]
    decisions: list[dict[str, object]]


def _geometry(block: ReadingBlock) -> Geometry | None:
    region = (block.get("anchor") or {}).get("coordinate_region") or {}
    raw = region.get("bbox")
    page = block.get("page")
    if (
        region.get("unit") != "pt"
        or region.get("origin", "top_left") != "top_left"
        or region.get("coordinate_space", "rendered_page") != "rendered_page"
        or region.get("precision") in ("unknown", "page_bbox")
        or not isinstance(raw, (list, tuple))
        or len(raw) != 4
        or not block.get("asset_ref")
        or not isinstance(page, int)
        or page < 1
        or region.get("page", page) != page
    ):
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in raw)
        width, height = float(region["width"]), float(region["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not all(math.isfinite(value) for value in (x0, y0, x1, y1, width, height))
        or not 0 <= x0 < x1 <= width
        or not 0 <= y0 < y1 <= height
    ):
        return None
    return (x0, y0, x1, y1), (width, height)


def _readable(block: ReadingBlock, cells: Mapping[object, list[ReadingBlock]]) -> bool:
    kind = str(block.get("kind") or "").lower()
    content = cells.get(block["block_id"], []) if kind == "table" else [block]
    # Flattened visual text may contain only a caption, label or equation number.
    return (
        block.get("source") in ("mineru", "local_ocr")
        and not block.get("table_region")
        and kind not in ("unknown", "unread_page")
        and not any(word in kind for word in ("image", "figure", "chart", "formula", "equation"))
        and "table_structure_unreliable" not in (block.get("reading_risks") or [])
        and any(character.isalnum() for row in content for character in str(row.get("text") or ""))
    )


def _covered_area(box: Box, candidates: Sequence[Box]) -> float:
    """Exact rectangle-union coverage, so overlaps cannot conceal a missing strip."""
    x0, y0, x1, y1 = box
    clipped = [
        (max(x0, a), max(y0, b), min(x1, c), min(y1, d))
        for a, b, c, d in candidates
        if max(x0, a) < min(x1, c) and max(y0, b) < min(y1, d)
    ]
    xs = sorted({edge for a, _, c, _ in clipped for edge in (a, c)})
    area = 0.0
    for left, right in zip(xs, xs[1:], strict=False):
        intervals = sorted((b, d) for a, b, c, d in clipped if a <= left and c >= right)
        top, bottom = 0.0, 0.0
        covered = 0.0
        for start, end in intervals:
            if start > bottom:
                covered += bottom - top
                top, bottom = start, end
            else:
                bottom = max(bottom, end)
        area += (right - left) * (covered + bottom - top)
    return area


def _native_coverage(
    block: ReadingBlock, candidates: Mapping[tuple[object, object], list[tuple[ReadingBlock, Geometry]]]
) -> tuple[str, list[object], float | None]:
    """Decide one native block: unresolved, uncovered, partially covered, or replaced by MinerU IDs."""
    replacements: list[object] = []
    coverage = None
    geometry = _geometry(block)
    reason = "native_geometry_unresolved" if geometry is None else "native_not_covered"
    if geometry:
        box, dimensions = geometry
        overlapping = [
            (other, other_box)
            for other, (other_box, other_dimensions) in candidates.get((block["asset_ref"], block["page"]), [])
            if dimensions == other_dimensions
            and max(box[0], other_box[0]) < min(box[2], other_box[2])
            and max(box[1], other_box[1]) < min(box[3], other_box[3])
        ]
        area = (box[2] - box[0]) * (box[3] - box[1])
        coverage = min(1.0, _covered_area(box, [bbox for _, bbox in overlapping]) / area)
        # Only float roundoff is tolerated; genuine region gaps keep the fallback.
        if math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-9):
            reason = "native_covered_by_mineru"
            replacements = [other["block_id"] for other, _ in overlapping]
        elif coverage:
            reason = "native_partially_covered"
    return reason, replacements, coverage


def select_primary_reading[BlockT: ReadingBlock](blocks: Sequence[BlockT]) -> tuple[list[BlockT], ReadingAudit]:
    """Keep MinerU plus native fallbacks; never mutate, reorder, or renumber input.

    Native text is omitted only when readable MinerU rectangles fully cover it
    on the same asset/page and coordinate system. Text equality is irrelevant:
    both conflicting and agreeing alternatives remain in the unchanged IR.
    Partial, missing, or unreliable coverage retains the complete native block.
    Tables and their cells are kept together with their original ownership IDs.
    """
    cells: defaultdict[object, list[ReadingBlock]] = defaultdict(list)
    for block in blocks:
        if block.get("table_region"):
            cells[block["table_region"]].append(block)
    candidates: defaultdict[tuple[object, object], list[tuple[ReadingBlock, Geometry]]] = defaultdict(list)
    for block in blocks:
        geometry = _geometry(block)
        if _readable(block, cells) and geometry:
            candidates[(block["asset_ref"], block["page"])].append((block, geometry))
    selected, decisions, suppressed = [], [], []
    for block in blocks:
        native = (
            block.get("source") == "pymupdf" and block.get("kind") == "native_text" and not block.get("table_region")
        )
        reason, replacements, coverage = (
            _native_coverage(block, candidates) if native else ("kept_source_block", [], None)
        )
        if replacements:
            suppressed.append(block["block_id"])
        else:
            selected.append(block)
        decisions.append(
            {
                "block_id": block["block_id"],
                "asset_ref": block.get("asset_ref"),
                "page": block.get("page"),
                "ir_region": block.get("ir_region"),
                "selected": not bool(replacements),
                "reason": reason,
                "replacement_block_ids": replacements,
                "coverage_fraction": coverage,
            }
        )
    return selected, {
        "version": "entity-primary-reading-v1",
        "selected_block_ids": [block["block_id"] for block in selected],
        "suppressed_block_ids": suppressed,
        "decisions": decisions,
    }
