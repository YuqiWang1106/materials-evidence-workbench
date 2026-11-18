"""Validate source-coordinate regions without compiling scientific instructions.

Draft shape errors are rejected; incomplete geometry stays editable and is
reported by inspection. Only immutable reader output supplies context values.
"""

import re
from array import array
from copy import deepcopy
from itertools import combinations

from openpyxl.utils.cell import range_boundaries

from kb_ingest.typed_values import parse_typed_value

from .table_recipe import keys, validate_rectangle

PROFILE = "table_regions_v1"
MAX_REGIONS = 128
MAX_RANGES = 128
MAX_RECTANGLES = 512
MAX_ISSUES = 256
RECIPE_KEYS = {"version", "entry_profile", "region_review", "profiles", "excluded_ranges", "supplements"}
REGION_KEYS = {"id", "name", "asset_id", "sheet", "table_range", "header_ranges", "data_ranges", "context_ranges"}
CONTEXT_ROLES = {"title", "note", "uncertain"}


def _array(value, name, maximum=MAX_RANGES):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name}: expected an array of at most {maximum} items")
    return value


def _contains(outer, inner):
    return outer[0] <= inner[0] <= inner[2] <= outer[2] and outer[1] <= inner[1] <= inner[3] <= outer[3]


def _overlap(left, right):
    return max(left[0], right[0]) <= min(left[2], right[2]) and max(left[1], right[1]) <= min(left[3], right[3])


def _inside(cell, rectangle):
    return rectangle[0] <= cell["column"] <= rectangle[2] and rectangle[1] <= cell["row"] <= rectangle[3]


def _intersection(left, right):
    if not _overlap(left, right):
        return None
    return max(left[0], right[0]), max(left[1], right[1]), min(left[2], right[2]), min(left[3], right[3])


def _nonempty(cell):
    value = cell["raw_value"]
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _context_nontext(cell):
    """A narrow exclusion guard, never a classifier of scientific semantics.

    Numeric notation and native typed/formula/error cells cannot be hidden as
    prose. Mixed source prose such as 'Temperature 313 K' stays uninterpreted.
    """
    if not _nonempty(cell):
        return False
    if not isinstance(cell["raw_value"], str):
        return True
    value = parse_typed_value(cell, expected="auto")
    return (
        value["kind"] not in {"text", "missing"}
        or value["kind"] == "missing"
        and value["status"] in {"error", "nan", "non_finite"}
    )


def _roles(region):
    return (
        [("header", name, range_boundaries(name)) for name in region["header_ranges"]]
        + [("data", name, range_boundaries(name)) for name in region["data_ranges"]]
        + [(item["role"], item["range"], range_boundaries(item["range"])) for item in region["context_ranges"]]
    )


def _load(recipe, assets, reader_options):
    from .table_reading import _options_by_asset, _selected_tables

    keys(recipe, RECIPE_KEYS, {"entry_profile"}, "recipe")
    if recipe["entry_profile"] != PROFILE or recipe.get("version", "1") != "1":
        raise ValueError("Unsupported region review profile or version")
    normalized = deepcopy(recipe)
    normalized["version"] = "1"
    for name in ("profiles", "excluded_ranges", "supplements"):
        if normalized.setdefault(name, []) != []:
            raise ValueError(f"Region review cannot contain {name}")
    review = normalized.setdefault("region_review", {"version": "1", "regions": []})
    keys(review, {"version", "regions", "mode", "initialized_sheets"}, {"version", "regions"}, "region_review")
    if review["version"] != "1":
        raise ValueError("Unsupported region review version")
    if "mode" in review and review["mode"] != "sheet-by-sheet":
        raise ValueError("Unsupported region review mode")
    if "initialized_sheets" in review and review.get("mode") != "sheet-by-sheet":
        raise ValueError("Sheet initialization requires sheet-by-sheet mode")
    regions = _array(review["regions"], "region_review.regions", MAX_REGIONS)
    if not isinstance(assets, list) or not 1 <= len(assets) <= 50:
        raise ValueError("Region review requires registered source assets")
    asset_ids = [asset.get("asset_id") for asset in assets if isinstance(asset, dict)]
    if (
        len(asset_ids) != len(assets)
        or any(not isinstance(a, str) or not a for a in asset_ids)
        or len(set(asset_ids)) != len(asset_ids)
    ):
        raise ValueError("Region review requires unique registered asset IDs")
    options = _options_by_asset(assets, reader_options)
    tables, reader_issues = {}, []
    for asset in assets:
        try:
            selected = _selected_tables(asset, options.get(asset["asset_id"], {}))
        except ValueError:
            if regions:
                raise
            # A draft must exist before the user can choose a lossless reader.
            # No source coordinates can be mapped or confirmed in this state.
            reader_issues.append({"asset_id": asset["asset_id"]})
            continue
        tables.update({(asset["asset_id"], table["sheet"]): table for table in selected})
    initialized = review.get("initialized_sheets", [])
    if not isinstance(initialized, list) or len(initialized) > len(tables):
        raise ValueError("Initialized sheets must be a unique list of original worksheets")
    initialized_keys = set()
    for item in initialized:
        keys(item, {"asset_id", "sheet"}, {"asset_id", "sheet"}, "initialized_sheet")
        if not isinstance(item["asset_id"], str) or not (item["sheet"] is None or isinstance(item["sheet"], str)):
            raise ValueError("Initialized sheet must identify an original worksheet")
        key = (item["asset_id"], item["sheet"])
        if key not in tables or key in initialized_keys:
            raise ValueError("Initialized sheet is unknown or repeated")
        initialized_keys.add(key)
    ids = set()
    rectangle_total = 0
    for region in regions:
        keys(region, REGION_KEYS, {"id", "asset_id", "sheet", "table_range"}, "region")
        identifier = region["id"]
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier)
            or identifier in ids
        ):
            raise ValueError("Region ID must be unique bounded text")
        ids.add(identifier)
        if "name" in region and (
            not isinstance(region["name"], str) or not region["name"].strip() or len(region["name"]) > 120
        ):
            raise ValueError("Region name must be nonempty text of at most 120 characters")
        if not isinstance(region["asset_id"], str) or not (region["sheet"] is None or isinstance(region["sheet"], str)):
            raise ValueError("Region must identify an original asset and worksheet")
        table = tables.get((region["asset_id"], region["sheet"]))
        if table is None:
            raise ValueError("Region references an unknown original worksheet")
        outer = validate_rectangle(region["table_range"], table, "region.table_range")
        for name in ("header_ranges", "data_ranges"):
            ranges = _array(region.setdefault(name, []), f"region.{name}")
            seen = set()
            for value in ranges:
                rectangle = validate_rectangle(value, table, f"region.{name}")
                if value in seen or not _contains(outer, rectangle):
                    raise ValueError(f"{name} must be unique ranges inside table_range")
                seen.add(value)
        contexts = _array(region.setdefault("context_ranges", []), "region.context_ranges")
        seen = set()
        for context in contexts:
            keys(context, {"range", "role"}, {"range", "role"}, "context_range")
            if not isinstance(context["role"], str) or context["role"] not in CONTEXT_ROLES:
                raise ValueError("Unsupported original context role")
            validate_rectangle(context["range"], table, "context_range.range")
            if context["range"] in seen:
                raise ValueError("Duplicate context range")
            seen.add(context["range"])
        rectangle_total += 1 + len(region["header_ranges"]) + len(region["data_ranges"]) + len(contexts)
        if rectangle_total > MAX_RECTANGLES:
            raise ValueError(f"Region review supports at most {MAX_RECTANGLES} total rectangles")
    return normalized, tables, reader_issues


def normalize_recipe(recipe, assets, reader_options=None):
    """Normalize a source-only draft; no confirmation or execution is implied."""
    return _load(recipe, assets, reader_options)[0]


class _SourceIndex:
    """One bounded pass per sheet; union counts never rescan cells per region."""

    def __init__(self, table):
        self.width = table["ncols"] + 1
        self.rows = {row["row"]: row for row in table["rows"]}
        size = self.width * (table["nrows"] + 1)
        self.prefixes = {name: array("I", [0]) * size for name in ("nonempty", "comment", "present", "context_nontext")}
        for row_number in range(1, table["nrows"] + 1):
            cells = self.rows.get(row_number, {}).get("cells", [])
            by_column = {cell["column"]: cell for cell in cells}
            running = dict.fromkeys(self.prefixes, 0)
            for column in range(1, table["ncols"] + 1):
                cell = by_column.get(column)
                values = {
                    "present": cell is not None,
                    "nonempty": cell is not None and _nonempty(cell),
                    "comment": cell is not None and bool(cell.get("comment")),
                    "context_nontext": cell is not None and _context_nontext(cell),
                }
                offset = row_number * self.width + column
                for name, prefix in self.prefixes.items():
                    running[name] += values[name]
                    prefix[offset] = prefix[offset - self.width] + running[name]

    def count(self, rectangles, kind="nonempty"):
        prefix, width = self.prefixes[kind], self.width
        return sum(
            prefix[r1 * width + c1]
            - prefix[(r0 - 1) * width + c1]
            - prefix[r1 * width + c0 - 1]
            + prefix[(r0 - 1) * width + c0 - 1]
            for r0, r1, columns in _bands(rectangles)
            for c0, c1 in columns
        )

    def cell(self, row, column):
        cells = self.rows.get(row, {}).get("cells", [])
        if column <= len(cells) and cells[column - 1]["column"] == column:
            return cells[column - 1]
        return next((cell for cell in cells if cell["column"] == column), None)


def inspect_review(recipe, assets, reader_options=None):
    """Check role geometry and expose every source sheet's unassigned content."""
    normalized, tables, reader_issues = _load(recipe, assets, reader_options)
    regions = normalized["region_review"]["regions"]
    issues, results = [], []
    issue_total = 0
    issue_counts = {r["id"]: 0 for r in regions}
    indexes = {key: _SourceIndex(table) for key, table in tables.items()}

    def issue(code, message, region=None, **details):
        nonlocal issue_total
        issue_total += 1
        item = {"code": code, "message": message, "severity": "error", **details}
        if region is not None:
            item.update(region_id=region["id"], asset_id=region["asset_id"], sheet=region["sheet"])
            issue_counts[region["id"]] += 1
        if len(issues) < MAX_ISSUES:
            issues.append(item)

    if not regions:
        issue("regions_required", "请添加至少一个需要核对的表区。")
    for pending in reader_issues:
        issue("reader_selection_required", "原件读取方式尚未确定，请先检查并选择正确的读取方式。", **pending)
    roles_by_id = {r["id"]: _roles(r) for r in regions}
    for region in regions:
        table = tables[(region["asset_id"], region["sheet"])]
        index = indexes[(region["asset_id"], region["sheet"])]
        outer = range_boundaries(region["table_range"])
        roles = roles_by_id[region["id"]]
        if not region["data_ranges"]:
            issue("data_required", "请指定实际数据范围。", region)
        for left, right in combinations(roles, 2):
            if _overlap(left[2], right[2]):
                issue(
                    "role_overlap",
                    "原范围被重复指定了角色，请调整边界。",
                    region,
                    ranges=[left[1], right[1]],
                    roles=[left[0], right[0]],
                )
        if any(role == "uncertain" for role, _, _ in roles):
            issue("uncertain_context", "尚有上下文角色待核对。", region)
        selections = [("table", region["table_range"], outer), *roles]
        for merge in table.get("merged_cells", []):
            merged = range_boundaries(merge)
            for role, name, rectangle in selections:
                if _overlap(rectangle, merged) and not _contains(rectangle, merged):
                    issue(
                        "merged_range_clipped",
                        "所选边界切断了原件合并范围。",
                        region,
                        range=name,
                        role=role,
                        merged_range=merge,
                    )
        covered = [overlap for _, _, rectangle in roles if (overlap := _intersection(rectangle, outer)) is not None]
        nonempty = index.count([outer])
        data_nonempty = index.count([rectangle for role, _, rectangle in roles if role == "data"])
        unassigned = nonempty - index.count(covered)
        unassigned_comments = index.count([outer], "comment") - index.count(covered, "comment")
        context_nontext = index.count(
            [rectangle for role, _, rectangle in roles if role in CONTEXT_ROLES], "context_nontext"
        )
        if context_nontext:
            issue(
                "context_nontext_values",
                "标题或备注范围含数值、公式或其他非文字原值，请重新核对其角色。",
                region,
                count=context_nontext,
            )
        if region["data_ranges"] and not data_nonempty:
            issue("data_empty", "数据范围内没有原件非空值。", region)
        if unassigned:
            issue("unassigned_table_cells", "表区内仍有非空原格尚未指定角色。", region, count=unassigned)
        if unassigned_comments:
            issue("unassigned_table_comments", "表区内仍有原格批注尚未指定角色。", region, count=unassigned_comments)
        results.append(
            {
                "id": region["id"],
                "asset_id": region["asset_id"],
                "sheet": region["sheet"],
                "nonempty_cells": nonempty,
                "data_nonempty_cells": data_nonempty,
                "unassigned_nonempty_cells": unassigned,
                "unassigned_comment_cells": unassigned_comments,
                "context_nontext_cells": context_nontext,
            }
        )
    for left, right in combinations(regions, 2):
        if (left["asset_id"], left["sheet"]) != (right["asset_id"], right["sheet"]):
            continue
        if _overlap(range_boundaries(left["table_range"]), range_boundaries(right["table_range"])):
            for current, other in ((left, right), (right, left)):
                issue("table_overlap", "两个表区相互重叠，请调整边界。", current, other_region_id=other["id"])
        for current, other in ((left, right), (right, left)):
            for context in current["context_ranges"]:
                if _overlap(range_boundaries(context["range"]), range_boundaries(other["table_range"])):
                    issue(
                        "context_table_overlap",
                        "上下文范围覆盖了另一表区。",
                        current,
                        other_region_id=other["id"],
                        range=context["range"],
                    )
    for result in results:
        result["issues"] = [item for item in issues if item.get("region_id") == result["id"]]
        result["issue_total"] = issue_counts[result["id"]]
        result["issues_truncated"] = len(result["issues"]) < result["issue_total"]
        result["ready"] = result["issue_total"] == 0
    sheets = []
    for (asset_id, sheet), table in tables.items():
        source_roles = [
            rectangle
            for region in regions
            if (region["asset_id"], region["sheet"]) == (asset_id, sheet)
            for _, _, rectangle in roles_by_id[region["id"]]
        ]
        index = indexes[(asset_id, sheet)]
        whole = [(1, 1, table["ncols"], table["nrows"])] if table["ncols"] and table["nrows"] else []
        nonempty, assigned = index.count(whole), index.count(source_roles)
        comments, assigned_comments = index.count(whole, "comment"), index.count(source_roles, "comment")
        sheets.append(
            {
                "asset_id": asset_id,
                "sheet": sheet,
                "nonempty_cells": nonempty,
                "assigned_nonempty_cells": assigned,
                "unassigned_nonempty_cells": nonempty - assigned,
                "comment_cells": comments,
                "unassigned_comment_cells": comments - assigned_comments,
                "hidden": table.get("hidden", False),
                "hidden_columns": table.get("hidden_columns", []),
            }
        )
    return {
        "ready": issue_total == 0,
        "issues": issues,
        "issue_total": issue_total,
        "issues_truncated": issue_total > len(issues),
        "regions": results,
        "sheets": sheets,
        "model_calls": 0,
        "meaning_confirmed": False,
    }


def _bands(rectangles):
    """Disjoint row bands and column intervals keep pagination memory bounded."""
    edges = sorted({edge for c0, r0, c1, r1 in rectangles for edge in (r0, r1 + 1)})
    for first, after in zip(edges, edges[1:]):
        intervals = sorted((c0, c1) for c0, r0, c1, r1 in rectangles if r0 <= first <= r1)
        merged = []
        for first_column, last_column in intervals:
            if merged and first_column <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], last_column)
            else:
                merged.append([first_column, last_column])
        if merged:
            yield first, after - 1, merged


def context_page(recipe, assets, reader_options=None, *, region_id, offset=0, limit=100):
    """Return a bounded page of exact source cells, including selected blanks."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Context paging requires offset >= 0 and limit from 1 to 100")
    normalized, tables, _ = _load(recipe, assets, reader_options)
    region = next((r for r in normalized["region_review"]["regions"] if r["id"] == region_id), None)
    if region is None:
        raise ValueError("Unknown original region ID")
    table = tables[(region["asset_id"], region["sheet"])]
    roles = _roles(region)
    rectangles = [
        range_boundaries(region["table_range"]),
        *[range_boundaries(c["range"]) for c in region["context_ranges"]],
    ]
    bands = list(_bands(rectangles))
    selected_total = sum((r1 - r0 + 1) * sum(c1 - c0 + 1 for c0, c1 in columns) for r0, r1, columns in bands)
    source = _SourceIndex(table)
    total = source.count(rectangles, "present")
    cells, skipped = [], 0
    for r0, r1, columns in bands:
        for row in range(r0, r1 + 1):
            original_row = source.rows.get(row, {})
            original_cells = original_row.get("cells", [])
            for c0, c1 in columns:
                # The reader stores contiguous physical CSV fields, including
                # empty fields, but never pads a short row to table width.
                selected = original_cells[c0 - 1 : c1]
                if skipped + len(selected) <= offset:
                    skipped += len(selected)
                    continue
                first = max(0, offset - skipped)
                skipped += first
                for original in selected[first:]:
                    item = deepcopy(original)
                    item.update(
                        asset_id=region["asset_id"],
                        sheet=region["sheet"],
                        row_hidden=bool(original_row.get("hidden")),
                        column_hidden=re.sub(r"\d+", "", item["address"]) in table.get("hidden_columns", []),
                        sheet_hidden=bool(table.get("hidden")),
                    )
                    item["roles"] = list(
                        dict.fromkeys(role for role, _, rectangle in roles if _inside(item, rectangle))
                    ) or ["unassigned"]
                    origin = item.get("merged_origin")
                    if origin:
                        origin_bounds = range_boundaries(origin)
                        original_origin = source.cell(origin_bounds[1], origin_bounds[0])
                        item["merged_origin_cell"] = deepcopy(original_origin)
                        if original_origin:
                            item["merged_origin_cell"].update(
                                asset_id=region["asset_id"],
                                sheet=region["sheet"],
                                roles=list(
                                    dict.fromkeys(
                                        role for role, _, rectangle in roles if _inside(original_origin, rectangle)
                                    )
                                )
                                or ["unassigned"],
                            )
                    cells.append(item)
                    skipped += 1
                    if len(cells) >= limit:
                        break
                if len(cells) >= limit:
                    break
            if len(cells) >= limit:
                break
        if len(cells) >= limit:
            break
    remaining = max(0, total - offset - len(cells))
    return {
        "region_id": region_id,
        "asset_id": region["asset_id"],
        "sheet": region["sheet"],
        "cells": cells,
        "total": total,
        "offset": offset,
        "limit": limit,
        "selected_coordinate_total": selected_total,
        "absent_cell_count": selected_total - total,
        "remaining": remaining,
        "has_more": remaining > 0,
        "reader_version": table.get("reader_version"),
        "model_calls": 0,
        "meaning_confirmed": False,
    }
