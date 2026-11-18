"""Bounded structural suggestions; source roles and meaning stay unconfirmed."""

from decimal import Decimal, InvalidOperation

from openpyxl.utils.cell import get_column_letter, range_boundaries

MAX_REGIONS = 128
MAX_METADATA_CELLS = 128
DISCOVERY_VERSION = "table-discovery-2"


def _runs(values):
    result = []
    for value in sorted(values):
        if result and value == result[-1][1] + 1:
            result[-1][1] = value
        else:
            result.append([value, value])
    return result


def _number(value):
    if isinstance(value, bool) or value is None:
        return False
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _range(c0, r0, c1, r1):
    return f"{get_column_letter(c0)}{r0}:{get_column_letter(c1)}{r1}"


def _text(cell):
    return (
        isinstance(cell["raw_value"], str)
        and bool(cell["raw_value"].strip())
        and not _number(cell["raw_value"])
        and not cell.get("formula")
        and cell.get("value_type") in ("s", "string", "inlineStr", "str")
    )


def _context(address, role, basis):
    return {"range": address, "role": role, "basis": basis, "requires_confirmation": True}


def _region(cells, active_merges, merges, c0, c1, table, asset_id):
    # Bounds belong to this column block, not its taller neighbouring blocks.
    r0 = min([c["row"] for c in cells] + [box[1] for _, box in active_merges])
    r1 = max([c["row"] for c in cells] + [box[3] for _, box in active_merges])
    numeric_rows = [c["row"] for c in cells if _number(c["raw_value"])]
    data_start = min(numeric_rows, default=None)
    headers = list(range(r0, data_start)) if data_start is not None and 0 < data_start - r0 <= 32 else []
    address = _range(c0, r0, c1, r1)
    kind = "table_candidate" if c1 > c0 and r1 > r0 else "uncertain"
    contexts = []
    ambiguities = ["table_boundary_requires_confirmation"]
    if not headers:
        ambiguities.append("header_boundary_unresolved")
    else:
        ambiguities.append("header_numeric_transition_provisional")
    if len(cells) == 1 and _text(cells[0]):
        kind = "annotation_candidate"
        contexts.append(_context(address, "uncertain", "isolated_single_text_origin"))
        ambiguities.append("annotation_role_unconfirmed")
    else:
        by_row = {}
        for cell in cells:
            by_row.setdefault(cell["row"], []).append(cell)
        # A sparse edge row may be a title/note OR a real header/data record.
        # Propose its role, retaining it inside the original candidate bounds.
        if any(len(row) > 1 for row in by_row.values()):
            merge_by_origin = {(box[1], box[0]): box for _, box in active_merges}
            for row, role, basis in (
                (r0, "title", "leading_single_text_row"),
                (r1, "note", "trailing_single_text_row"),
            ):
                edge = by_row.get(row, [])
                if len(edge) != 1 or not _text(edge[0]):
                    continue
                cell = edge[0]
                box = merge_by_origin.get((row, cell["column"]), (cell["column"], row, cell["column"], row))
                if box[1] != box[3]:
                    continue
                contexts.append(_context(_range(*box), role, basis))
            if contexts:
                kind = "uncertain"
                ambiguities.append("possible_attached_annotation")
    return {
        "asset_id": asset_id,
        "sheet": table["sheet"],
        "range": address,
        "rows": [r0, r1],
        "columns": [c0, c1],
        "suggested_header_rows": headers,
        "suggested_data_rows": [data_start, r1] if data_start is not None else None,
        "merged_ranges": [
            name for name, (a, top, b, bottom) in merges if a <= c1 and b >= c0 and top <= r1 and bottom >= r0
        ],
        "basis": "nonempty_geometry_and_numeric_transition",
        "requires_meaning_confirmation": True,
        "region_kind": kind,
        "suggested_header_ranges": [_range(c0, headers[0], c1, headers[-1])] if headers else [],
        "suggested_data_ranges": [_range(c0, data_start, c1, r1)] if data_start is not None else [],
        "context_candidates": contexts,
        "ambiguities": ambiguities,
    }


def discover_regions(table, asset_id):
    """Scan every row, including changes after the first preview page.

    Empty row/column separators only propose rectangles. Each is trimmed using
    its own original cells and nonempty merged origins. Empty styling cannot
    enlarge it. Classification and numeric transitions are provisional: title
    proposals never remove original rows or assert scientific applicability.
    """
    occupied, metadata_only, metadata_total = {}, [], 0
    for row in table["rows"]:
        for cell in row["cells"]:
            if cell["raw_value"] is not None and str(cell["raw_value"]).strip():
                occupied[(cell["row"], cell["column"])] = cell
            elif cell.get("comment"):
                # Comments have provenance but are not source measurement values.
                # Report even distant comments without joining/extending tables.
                metadata_total += 1
                if len(metadata_only) < MAX_METADATA_CELLS:
                    metadata_only.append(
                        {
                            "range": _range(cell["column"], cell["row"], cell["column"], cell["row"]),
                            "kind": "comment",
                            "status": "not_used_for_region_bounds",
                        }
                    )
    merges = [(name, range_boundaries(name)) for name in table.get("merged_cells", [])]
    active_merges = [(name, box) for name, box in merges if (box[1], box[0]) in occupied]
    active_rows = {r for r, _ in occupied}
    for _, (_, r0, _, r1) in active_merges:
        active_rows.update(range(r0, r1 + 1))
    row_runs = _runs(active_rows)
    row_band = {r: i for i, (r0, r1) in enumerate(row_runs) for r in range(r0, r1 + 1)}
    bands = [{"cells": [], "merges": []} for _ in row_runs]
    for (row, _), cell in occupied.items():
        bands[row_band[row]]["cells"].append(cell)
    for merge in active_merges:
        bands[row_band[merge[1][1]]]["merges"].append(merge)
    regions, total = [], 0
    for band in bands:
        columns = {c["column"] for c in band["cells"]}
        for _, (a, _, b, _) in band["merges"]:
            columns.update(range(a, b + 1))
        column_runs = _runs(columns)
        total += len(column_runs)
        if len(regions) == MAX_REGIONS:
            continue
        column_block = {c: i for i, (c0, c1) in enumerate(column_runs) for c in range(c0, c1 + 1)}
        blocks = [{"cells": [], "merges": []} for _ in column_runs]
        for cell in band["cells"]:
            blocks[column_block[cell["column"]]]["cells"].append(cell)
        for merge in band["merges"]:
            blocks[column_block[merge[1][0]]]["merges"].append(merge)
        for (c0, c1), block in zip(column_runs, blocks):
            if len(regions) == MAX_REGIONS:
                break
            regions.append(_region(block["cells"], block["merges"], merges, c0, c1, table, asset_id))
    # Pairwise associations are bounded by MAX_REGIONS, never all source cells.
    for region in regions:
        if total > 1:
            region["ambiguities"].append("blank_separator_may_be_internal")
        if region["region_kind"] == "annotation_candidate":
            continue
        r0, r1 = region["rows"]
        c0, c1 = region["columns"]
        aligned = [other for other in regions if other["columns"][0] <= c1 and other["columns"][1] >= c0]
        above = [other for other in aligned if other["rows"][1] < r0]
        below = [other for other in aligned if other["rows"][0] > r1]
        nearest_above = max((other["rows"][1] for other in above), default=None)
        nearest_below = min((other["rows"][0] for other in below), default=None)
        for other in above:
            if other["rows"][1] == nearest_above and other["region_kind"] == "annotation_candidate":
                region["context_candidates"].append(_context(other["range"], "title", "isolated_text_above_candidate"))
        for other in below:
            if other["rows"][0] == nearest_below and other["region_kind"] == "annotation_candidate":
                region["context_candidates"].append(_context(other["range"], "note", "isolated_text_below_candidate"))
    return {
        "discovery_version": DISCOVERY_VERSION,
        "regions": regions,
        "region_total": total,
        "regions_truncated": total > MAX_REGIONS,
        "metadata_only_cells": metadata_only,
        "metadata_only_cell_total": metadata_total,
        "metadata_only_cells_truncated": metadata_total > MAX_METADATA_CELLS,
    }
