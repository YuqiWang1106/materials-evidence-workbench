"""Structure-preserving windows of located blocks, with a soft character target."""

import re
import unicodedata
from itertools import groupby
from typing import Any

# Measured on two papers: a whole-paper window (40,000) made the model stop at ~12k output tokens and keep a
# third of the attributes; 7,000-character windows keep each call's output complete.
WINDOW_CHARS = 7000
_REFERENCES = re.compile(r"^(参考文献|参\s*考\s*文\s*献|references?|bibliography|引用文献)\s*[:：]?\s*$", re.I)
_SKIP_KINDS = {"unread_page"}
_HEADINGS = {"title", "doc_title", "paragraph_title", "section_title", "heading"}
_LAYOUT_NOISE = {"header", "footer", "page_header", "page_footer", "page_number", "page_numbering"}


# A segment ends after sentence punctuation or at a line break (a table row); decimals (1.5) never split.
_SEGMENT_END = re.compile(r"[。！？；!?;]+|\n+|(?<=[A-Za-z0-9)\]])\.(?=\s)")


def segments(text):
    """The block's text as citable pieces: sentences, and lines of a table. Pieces are exact source substrings."""
    text = str(text or "")
    pieces, start = [], 0
    for match in _SEGMENT_END.finditer(text):
        piece = text[start : match.end()].strip()
        if piece:
            pieces.append(piece)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces


def segment_ids(block):
    """(segment id, text) pairs: the block id itself for a one-piece block, else block_id.N."""
    pieces = segments(block.get("text"))
    if len(pieces) == 1:
        return [(block["block_id"], pieces[0])]
    return [(f"{block['block_id']}.{index + 1}", piece) for index, piece in enumerate(pieces)]


def _cited(block):
    """One block's segments on one line, each after its id: `<b4.1> 第一句。 <b4.2> 第二句。`"""
    return " ".join(f"<{segment_id}> {piece}" for segment_id, piece in segment_ids(block))


SPANNED = "〃"


def _span(place, key):
    value = place.get(key)
    return value if isinstance(value, int) and value > 1 else 1


def _table_lines(cells):
    """A table as one line per row, cells separated by ` | ` in their columns. A place covered by a merged cell
    holds 〃, an empty place stays empty. Cells without a grid position follow the grid, one per line. The
    table's id is not shown: only its cells can be cited, never the whole table."""
    anchored, loose = [], []
    for block in cells:
        place = (block.get("anchor") or {}).get("table_cell") or {}
        row, column = place.get("row"), place.get("column")
        if isinstance(row, int) and isinstance(column, int) and row >= 0 and column >= 0:
            anchored.append((row, column, place, block))
        else:
            loose.append(block)
    # Parser spans can exceed the table (colspan=1000): a span only fills places inside the cells' own extent.
    last_row = max((row for row, _, _, _ in anchored), default=-1)
    last_column = max((column for _, column, _, _ in anchored), default=-1)
    grid = {}
    for row, column, place, _ in anchored:
        for down in range(min(_span(place, "row_span"), last_row - row + 1)):
            for right in range(min(_span(place, "col_span"), last_column - column + 1)):
                grid.setdefault((row + down, column + right), SPANNED)
    for row, column, _, block in anchored:
        grid[(row, column)] = block
    lines = ["〔表〕"]
    if grid:
        width = max(column for _, column in grid) + 1
        for row in sorted({row for row, _ in grid}):
            places = [grid.get((row, column), "") for column in range(width)]
            lines.append(" | ".join(_cited(place) if isinstance(place, dict) else place for place in places))
    return lines + [line for line in map(_cited, loose) if line]


def _covered_by_cells(table, cells):
    """The table's own text adds nothing when its cells hold (nearly) all of its characters."""
    own = len("".join(str(table.get("text") or "").split()))
    held = sum(len("".join(str(cell.get("text") or "").split())) for cell in cells)
    return held >= own * 0.9


def window_source(blocks):
    """The window as plain text for the model, which cites segment ids instead of copying quotations (the
    program fills the quotations back in). Every block is one line of `<id> text` segments, a heading starts
    with `#`, a page change adds a `== 第N页 ==` line, and a table with cells is written once as its grid
    (its flattened text would repeat the cells; it is still sent when the cells do not hold all of it). Page
    headers and page numbers are not sent."""
    cells = {}
    for block in blocks:
        if block.get("kind") == "table_cell" and block.get("table_region"):
            cells.setdefault(block["table_region"], []).append(block)
    lines, page, written = [], None, set()
    for block in blocks:
        if block.get("kind") in _LAYOUT_NOISE:
            continue
        table = (
            block.get("table_region")
            if block.get("kind") == "table_cell"
            else (block["block_id"] if block.get("kind") == "table" and block["block_id"] in cells else None)
        )
        line = None if table is not None else _cited(block)
        if table in written or (table is None and not line):
            continue
        if block.get("page") is not None and block.get("page") != page:
            page = block["page"]
            lines.append(f"== 第{page}页 ==")
        if table is not None:
            written.add(table)
            if block.get("kind") == "table" and not _covered_by_cells(block, cells[table]):
                lines.append(_cited(block))
            lines.extend(_table_lines(cells[table]))
        else:
            lines.append(("# " if _heading(block) else "") + line)
    return "\n".join(lines)


def segment_index(blocks):
    """segment id -> (block id, position of the block in `blocks`, segment number, text, is a table cell,
    is a table block)."""
    index = {}
    for position, block in enumerate(blocks):
        for number, (segment_id, piece) in enumerate(segment_ids(block)):
            index[segment_id] = (
                block["block_id"],
                position,
                number,
                piece,
                block.get("kind") == "table_cell",
                block.get("kind") == "table",
            )
    return index


def _box(block):
    region = (block.get("anchor") or {}).get("coordinate_region") or {}
    bbox, width = region.get("bbox"), region.get("width")
    if isinstance(bbox, list) and len(bbox) == 4 and width:
        return [float(value) for value in bbox], float(width)
    return None, None


def reading_order(blocks):
    """Column-aware order for two-column pages read line by line, where the native order alternates columns.

    On a page with both a left and a right column, full-width blocks split the page into bands; inside a band
    the left column is read before the right one. Table cells stay after their table. Other pages keep their order.
    """
    result, page_blocks, key = [], [], None
    for block in blocks:
        current = (block.get("asset_ref"), block.get("page"))
        if current != key and page_blocks:
            result.extend(_page_order(page_blocks))
            page_blocks = []
        key = current
        page_blocks.append(block)
    if page_blocks:
        result.extend(_page_order(page_blocks))
    return result


def _page_order(blocks):
    heads = [block for block in blocks if not block.get("table_region")]
    cells = {}
    for block in blocks:
        if block.get("table_region"):
            cells.setdefault(block["table_region"], []).append(block)
    placed = []
    for block in heads:
        bbox, width = _box(block)
        if bbox is None:
            return blocks
        middle = width / 2
        column = 0 if bbox[2] <= middle + width * 0.04 else 1 if bbox[0] >= middle - width * 0.04 else None
        placed.append((block, bbox, column))
    left = sum(1 for _, _, column in placed if column == 0)
    right = sum(1 for _, _, column in placed if column == 1)
    if left < 3 or right < 3:
        return blocks
    band, ordered = 0, []
    for block, bbox, column in sorted(placed, key=lambda item: item[1][1]):
        if column is None:
            band += 1
            ordered.append(((band, 0, bbox[1]), block))
            band += 1
        else:
            ordered.append(((band, column + 1, bbox[1]), block))
    result = []
    for _, block in sorted(ordered, key=lambda item: item[0]):
        result.append(block)
        result.extend(cells.get(block["block_id"], []))
    return result


def references_start(blocks):
    """Index of a standalone references heading in the second half of the document, if any."""
    half = len(blocks) // 2
    for index, block in enumerate(blocks):
        text = unicodedata.normalize("NFKC", (block.get("text") or "")).strip()
        first = text.splitlines()[0].strip() if text else ""
        if index >= half and block.get("kind") != "table_cell" and _REFERENCES.match(first):
            return index
    return None


def _table_owner(block):
    if block.get("kind") == "table_cell":
        return block.get("table_region")
    if block.get("kind") in {"caption", "footnote"}:
        return block.get("table_context_for")
    return None


def _heading(block):
    return block.get("kind") in _HEADINGS or (
        block.get("kind") == "visual_transcription" and block.get("segment_kind") == "heading"
    )


def _usable(blocks):
    """Apply the existing references test to readable text before retaining empty table parents."""
    readable = [
        (index, block)
        for index, block in enumerate(blocks)
        if block.get("kind") not in _SKIP_KINDS and (block.get("text") or "").strip()
    ]
    cut = references_start([block for _, block in readable])
    end = readable[cut][0] if cut is not None else len(blocks)
    readable = readable[:cut] if cut is not None else readable
    selected = {index for index, _ in readable}
    parents = {_table_owner(block) for _, block in readable}
    return [
        block
        for index, block in enumerate(blocks[:end])
        if index in selected or block.get("kind") == "table" and block["block_id"] in parents
    ]


def readable_runs(blocks):
    """Readable blocks per attachment run, with one shared references exclusion rule."""
    # Partition before filtering: even an empty intervening attachment is a boundary.
    for _, source in groupby(blocks, key=lambda block: block.get("asset_ref")):
        yield _usable(list(source))


def structure_units(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Uncut intervals within one attachment run; never move cells or intervening prose.

    Windows and failed-window supplements both pack these units, so neither splits a structure.

    A recognized heading includes the first content after consecutive headings and
    explicit layout noise. Table links include cells and parser-linked annotations.
    Intersecting intervals join without changing source blocks, order, or IDs.
    """
    ends = list(range(len(blocks)))
    tables = {block["block_id"]: index for index, block in enumerate(blocks) if block.get("kind") == "table"}
    first_content = len(blocks) - 1
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        if _heading(block):
            ends[index] = first_content
        elif block.get("kind") not in _LAYOUT_NOISE:
            first_content = index
        owner = _table_owner(block)
        if owner in tables:
            start, end = sorted((index, tables[owner]))
            ends[start] = max(ends[start], end)
    units, start, end = [], 0, 0
    for index in range(len(blocks)):
        end = max(end, ends[index])
        if index == end:
            units.append(blocks[start : index + 1])
            start = end = index + 1
    return units


def build_windows(blocks, plan=None, *, max_chars=WINDOW_CHARS):
    """Pack complete structures toward max_chars, cut at a page change when one is near.

    A plan with units keeps its own windows (each unit is one window). Blocks after a
    references heading in each attachment are not sent; they remain in document-ir.json.
    A structure larger than the target remains complete in its own window, with its
    actual character count recorded by the pipeline. No block is truncated or split.
    """
    units = (plan or {}).get("units") or []
    if units:
        by_id = {block["block_id"]: block for block in blocks}
        planned: list[dict[str, Any]] = []
        for unit in units:
            chosen = set(unit.get("block_ids") or []) | set(unit.get("context_block_ids") or [])
            chosen |= {block["block_id"] for block in blocks if block.get("table_region") in chosen}
            selected = [by_id[block_id] for block_id in unit.get("block_ids", []) if block_id in by_id]
            selected += [block for block in blocks if block["block_id"] in chosen and block not in selected]
            if selected:
                planned.append({"window_id": unit.get("key") or "w" + str(len(planned) + 1), "blocks": selected})
        return planned
    windows: list[list[dict[str, Any]]] = []
    for source in readable_runs(blocks):
        current: list[dict[str, Any]] = []
        size = 0
        for unit in structure_units(source):
            length = sum(len(block.get("text") or "") for block in unit)
            page_turn = bool(current) and unit[0].get("page") != current[-1].get("page")
            if current and (size + length > max_chars or (page_turn and size > max_chars * 0.7)):
                windows.append(current)
                current, size = [], 0
            current.extend(unit)
            size += length
        if current:
            windows.append(current)
    return [
        {
            "window_id": "w" + str(index + 1),
            "blocks": chosen,
            "pages": sorted({block.get("page") for block in chosen if block.get("page") is not None}),
        }
        for index, chosen in enumerate(windows)
    ]
