"""Complete source-cell snapshots, independent of scientific interpretation."""

from copy import deepcopy

from openpyxl.utils.cell import get_column_letter, range_boundaries

from kb_ingest.core import fingerprint

from .table_recipe import validate_rectangle

SNAPSHOT_VERSION = "raw-table-v1"


class RawTableRangeConflict(ValueError):
    def __init__(self, overlaps):
        messages = [
            f"{item['left']['name']}（{item['left']['range']}）与"
            f"{item['right']['name']}（{item['right']['range']}）重叠：{item['intersection_range']}"
            for item in overlaps
        ]
        self.detail = {
            "confirmed": False,
            "overlaps": overlaps,
            "issues": [
                {
                    "code": "table_overlap",
                    "message": message,
                    "severity": "error",
                    "region_id": item["left"]["id"],
                    "other_region_id": item["right"]["id"],
                    "asset_id": item["asset_id"],
                    "sheet": item["sheet"],
                }
                for message, item in zip(messages, overlaps, strict=True)
            ],
        }
        super().__init__("\n".join(messages))


def _overlap(a, b):
    return max(a[0], b[0]) <= min(a[2], b[2]) and max(a[1], b[1]) <= min(a[3], b[3])


def _inside(row, column, bounds):
    return bounds[0] <= column <= bounds[2] and bounds[1] <= row <= bounds[3]


def snapshot_version(snapshot):
    return "rawtable-" + fingerprint({key: value for key, value in snapshot.items() if key != "table_version_id"})


def build_raw_snapshot(draft, asset, region, table):
    """The full reader output is authoritative; never consume a preview page."""
    bounds = validate_rectangle(region["table_range"], table, "table_range")
    c0, r0, c1, r1 = bounds
    contexts = [validate_rectangle(item["range"], table, "context_range") for item in region.get("context_ranges", [])]
    overlaps = []
    for other in draft["recipe"]["region_review"]["regions"]:
        if other["id"] == region["id"] or (other["asset_id"], other["sheet"]) != (region["asset_id"], region["sheet"]):
            continue
        other_bounds = range_boundaries(other["table_range"])
        pairs = [(bounds, other_bounds, region["table_range"], other["table_range"])]
        pairs.extend(
            (box, other_bounds, item["range"], other["table_range"])
            for box, item in zip(contexts, region.get("context_ranges", []), strict=True)
        )
        pairs.extend(
            (bounds, range_boundaries(item["range"]), region["table_range"], item["range"])
            for item in other.get("context_ranges", [])
        )
        for left, right, left_range, right_range in pairs:
            if not _overlap(left, right):
                continue
            col0, row0, col1, row1 = (
                max(left[0], right[0]),
                max(left[1], right[1]),
                min(left[2], right[2]),
                min(left[3], right[3]),
            )
            overlaps.append(
                {
                    "asset_id": region["asset_id"],
                    "sheet": region["sheet"],
                    "left": {"id": region["id"], "name": region.get("name", region["id"]), "range": left_range},
                    "right": {"id": other["id"], "name": other.get("name", other["id"]), "range": right_range},
                    "intersection_range": f"{get_column_letter(col0)}{row0}:{get_column_letter(col1)}{row1}",
                }
            )
    if overlaps:
        raise RawTableRangeConflict(overlaps)
    for merged in table.get("merged_cells", []):
        box = range_boundaries(merged)
        if _overlap(bounds, box) and not (c0 <= box[0] <= box[2] <= c1 and r0 <= box[1] <= box[3] <= r1):
            raise ValueError("当前表格范围切断了原件合并单元格，请调整范围")
    source_rows = {row["row"]: row for row in table["rows"]}
    rows = []
    for number in range(r0, r1 + 1):
        original = source_rows[number]
        rows.append(
            {
                **deepcopy(original),
                "cells": [deepcopy(cell) for cell in original["cells"] if c0 <= cell["column"] <= c1],
            }
        )
    headers = [range_boundaries(value) for value in region.get("header_ranges", [])]
    header_count = 0
    for number in range(r0, r1 + 1):
        if not any(box[1] <= number <= box[3] for box in headers):
            break
        header_count += 1
    columns = []
    for column in range(c0, c1 + 1):
        values = [
            cell["raw_value"]
            for row in rows[:header_count]
            for cell in row["cells"]
            if cell["column"] == column and cell.get("raw_value") not in (None, "")
        ]
        label = str(values[-1]) if values else get_column_letter(column)
        columns.append({"column_id": get_column_letter(column), "column": column, "label": label, "unit": None})
    metadata = deepcopy({key: value for key, value in table.items() if key != "rows"})
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "table_id": "table-" + fingerprint({"source_id": draft["source_id"], "region_id": region["id"]}),
        "source_id": draft["source_id"],
        "source_region_id": region["id"],
        "asset_id": asset["asset_id"],
        "asset_sha256": asset["sha256"],
        "original_name": asset["original_name"],
        "sheet": region["sheet"],
        "table_range": region["table_range"],
        "name": region.get("name", region["id"]),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "column_count": len(columns),
        "header_ranges": deepcopy(region.get("header_ranges", [])),
        "data_ranges": deepcopy(region.get("data_ranges", [])),
        "context_ranges": deepcopy(region.get("context_ranges", [])),
        "context_cells": [
            deepcopy(cell)
            for row in table["rows"]
            for cell in row["cells"]
            if not _inside(cell["row"], cell["column"], bounds)
            and any(_inside(cell["row"], cell["column"], box) for box in contexts)
        ],
        "default_header_row_count": header_count,
        "reader_options": deepcopy(draft["reader_options"].get(asset["asset_id"], {})),
        "reader_version": table["reader_version"],
        "source_table": metadata,
    }
    snapshot["table_version_id"] = snapshot_version(snapshot)
    return snapshot
