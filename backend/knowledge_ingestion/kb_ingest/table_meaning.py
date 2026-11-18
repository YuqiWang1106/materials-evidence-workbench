"""Finite source-bound table meanings compiled into shared ReadingPlan v2."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from openpyxl.utils.cell import range_boundaries

from .ingest.tables.measurement_values import simple_measurement
from .ingest.tables.row_measurements import _header_path, _resolved
from .ingest.tables.table_plan import compile_plan
from .ingest.tables.table_reading import _selected_tables

_TOP = {
    "version",
    "asset_id",
    "source_sha256",
    "sheet",
    "layout",
    "data_range",
    "header_rows",
    "sample_column",
    "identity_mode",
    "measurements",
    "ignored_columns",
    "method_column",
    "condition_columns",
    "number_format",
}
_UNKNOWN = {"nd", "na", "n/a", "nt", "null", "nan"}


def _keys(value, allowed, required, name):
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise ValueError(f"{name} has missing or unsupported keys")


def _column(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} requires a positive column")


def _pair(value, name, *, start=1):
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(x) is not int for x in value)
        or value[0] < start
        or value[1] < start
    ):
        raise ValueError(f"{name} requires a bounded coordinate pair")


def _source(source, *, column_value=False):
    if not isinstance(source, dict):
        raise ValueError("Source must be an object")
    if "column" in source:
        _keys(
            source,
            {"column", "value"} if column_value else {"column"},
            {"column", "value"} if column_value else {"column"},
            "source",
        )
        _column(source["column"], "source")
        if column_value and (not isinstance(source["value"], str) or not source["value"].strip()):
            raise ValueError("Metric value must be nonblank text")
    else:
        _keys(source, {"cell", "span"}, {"cell"}, "source")
        _pair(source["cell"], "source cell")
        if "span" in source:
            span = source["span"]
            _pair(span, "source span", start=0)
            if not 0 <= span[0] < span[1] <= 4096:
                raise ValueError("Source span is invalid")


def _schema(form):
    _keys(form, _TOP, _TOP - {"method_column", "condition_columns", "number_format"}, "meaning form")
    if form["version"] not in ("1", "2") or form["layout"] not in ("long_rows", "wide_rows"):
        raise ValueError("Unsupported meaning version or layout")
    if form["version"] == "1" and "number_format" in form:
        raise ValueError("Version 1 meaning does not declare number format")
    if form.get("number_format", "source") not in ("source", "decimal_dot", "decimal_comma"):
        raise ValueError("Unsupported simple number format")
    if not isinstance(form["asset_id"], str) or not form["asset_id"]:
        raise ValueError("Asset id is required")
    if not isinstance(form["source_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", form["source_sha256"]):
        raise ValueError("Source hash must be lower-case SHA-256")
    if form["sheet"] is not None and not isinstance(form["sheet"], str):
        raise ValueError("Sheet must be name or null")
    box = form["data_range"]
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(type(x) is not int or x < 1 for x in box)
        or box[0] > box[2]
        or box[1] > box[3]
    ):
        raise ValueError("Data rectangle must be ordered one-based coordinates")
    headers = form["header_rows"]
    header_limit = 2 if form["version"] == "1" else 8
    if (
        not isinstance(headers, list)
        or not 1 <= len(headers) <= header_limit
        or any(type(x) is not int or x < 1 for x in headers)
        or headers != list(range(headers[0], headers[-1] + 1))
    ):
        raise ValueError(f"Header rows must be one to {header_limit} contiguous rows")
    _column(form["sample_column"], "sample column")
    if form["identity_mode"] not in ("row_independent", "same_id_in_scope", "unresolved"):
        raise ValueError("Unknown identity mode")
    if form["layout"] == "wide_rows" and form["identity_mode"] == "same_id_in_scope":
        raise ValueError("Wide rows cannot group identity across rows")
    for name in ("ignored_columns", "condition_columns"):
        cols = form.get(name, [])
        if not isinstance(cols, list) or len(cols) != len(set(x for x in cols if type(x) is int)):
            raise ValueError(f"{name} must be a distinct column list")
        for col in cols:
            _column(col, name)
    if form.get("method_column") is not None:
        _column(form["method_column"], "method column")
    items = form["measurements"]
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("Measurements require 1 to 50 finite definitions")
    for item in items:
        expected = {"result_column", "value_type", "unit"} | ({"metric"} if form["layout"] == "long_rows" else set())
        _keys(item, expected, expected, "measurement")
        _column(item["result_column"], "result column")
        if item["value_type"] not in ("number", "category"):
            raise ValueError("Result type must be number or category")
        if "metric" in item:
            if not isinstance(item["metric"], dict):
                raise ValueError("Metric source must be an object")
            _source(item["metric"], column_value="column" in item["metric"])
        unit = item["unit"]
        if not isinstance(unit, dict) or unit.get("kind") not in ("column", "cell", "absent"):
            raise ValueError("Unit must declare column, cell, or absence")
        if unit["kind"] == "absent":
            _keys(unit, {"kind"}, {"kind"}, "unit")
        elif unit["kind"] == "column":
            _keys(unit, {"kind", "column"}, {"kind", "column"}, "unit")
            _column(unit["column"], "unit column")
        else:
            _keys(unit, {"kind", "cell", "span", "applies_to"}, {"kind", "cell", "applies_to"}, "unit")
            _source({k: v for k, v in unit.items() if k in ("cell", "span")})
            targets = unit["applies_to"]
            if (
                not isinstance(targets, list)
                or not targets
                or len(targets) != len(set(x for x in targets if type(x) is int))
            ):
                raise ValueError("Unit applicability must be distinct nonempty columns")
            for col in targets:
                _column(col, "unit applicability")
            if item["result_column"] not in targets:
                raise ValueError("Unit applicability must include result column")


def _issue(code, message, **where):
    return {"code": code, "message": message, "severity": "error", **where}


def _status(issues):
    return {"ready": not issues, "issues": issues[:100], "issue_total": len(issues)}


def _empty():
    return {"version": "2", "views": [], "scopes": [], "objects": {}, "fields": [], "relations": []}


def _binding(source):
    if "column" in source:
        return {"row": "current", "column": source["column"]}
    b = {"row": source["cell"][0], "column": source["cell"][1]}
    if "span" in source:
        b["span"] = source["span"]
    return b


def _metric_path(cells, headers, column, excluded):
    path = _header_path(cells, headers, column)
    kept = []
    for coord in path["sources"]:
        if coord not in excluded:
            kept.append(coord)
    labels = [
        str(cells[coord]["raw_value"]).strip()
        for coord in kept
        if cells[coord]["raw_value"] is not None and str(cells[coord]["raw_value"]).strip()
    ]
    return {"label": " / ".join(labels), "sources": kept, "all_sources": path["sources"]}


def inspect_meaning(form: dict, assets: list[dict], reader_options: dict | None = None):
    """Return generated plan, blocked/ready diagnostics, and scope completion."""
    _schema(form)
    if not isinstance(assets, list) or len(assets) != 1 or assets[0].get("asset_id") != form["asset_id"]:
        raise ValueError("Exactly one matching registered asset is required")
    asset = assets[0]
    issues = []
    plan = _empty()
    try:
        actual_sha = sha256(Path(asset["local_path"]).read_bytes()).hexdigest()
    except (OSError, KeyError):
        issues.append(_issue("source_unavailable", "Original file cannot be read"))
        return plan, _status(issues), None
    if actual_sha != asset.get("sha256") or actual_sha != form["source_sha256"]:
        issues.append(_issue("source_hash_mismatch", "Form does not match original file bytes"))
    try:
        tables = _selected_tables(asset, (reader_options or {}).get(asset["asset_id"], {}))
    except ValueError as exc:
        issues.append(_issue("reader_ambiguity", str(exc)))
        return plan, _status(issues), None
    table = next((t for t in tables if t.get("sheet") == form["sheet"]), None)
    if table is None:
        issues.append(_issue("sheet_unavailable", "Selected sheet is unavailable"))
        return plan, _status(issues), None
    r0, c0, r1, c1 = form["data_range"]
    headers = form["header_rows"]
    if r1 > table["nrows"] or c1 > table["ncols"] or headers[-1] >= r0 or headers[-1] > table["nrows"]:
        issues.append(_issue("source_bounds", "Header or data rectangle is outside source bounds"))
    if r1 - r0 + 1 > 10_000 or c1 - c0 + 1 > 50:
        issues.append(_issue("selection_capacity", "Selection exceeds 10000 rows or 50 columns"))
    if issues:
        return plan, _status(issues), None
    cells = {(c["row"], c["column"]): c for row in table["rows"] for c in row["cells"]}
    selected = set(range(c0, c1 + 1))
    items = form["measurements"]
    for row in range(headers[-1] + 1, r0):
        if any(cells[(row, col)]["raw_value"] not in (None, "") for col in selected):
            issues.append(
                _issue(
                    "unconfirmed_header_layer",
                    "Nonempty row between header and data needs a new bounded selection",
                    row=row,
                )
            )
    roles = [form["sample_column"]]
    if form.get("method_column") is not None:
        roles.append(form["method_column"])
    roles += form.get("condition_columns", [])
    roles += [m["result_column"] for m in items] if form["layout"] == "wide_rows" else [items[0]["result_column"]]
    if form["layout"] == "long_rows" and "column" in items[0]["metric"]:
        roles.append(items[0]["metric"]["column"])
    roles += sorted({m["unit"]["column"] for m in items if m["unit"]["kind"] == "column"})
    ignored = set(form["ignored_columns"])
    if len(roles) != len(set(roles)):
        issues.append(_issue("overlapping_roles", "Selected columns have overlapping roles"))
    if set(roles) - selected or ignored - selected:
        issues.append(_issue("source_bounds", "A role or exclusion is outside selected columns"))
    if ignored & set(roles):
        issues.append(_issue("overlapping_roles", "Ignored column has an active role"))
    unassigned = sorted(selected - set(roles) - ignored)
    if unassigned:
        issues.append(_issue("unassigned_columns", "Selected columns require roles or exclusion", columns=unassigned))
    for region in table.get("merged_cells", []):
        mc0, mr0, mc1, mr1 = range_boundaries(region)
        intersects = max(mc0, c0) <= min(mc1, c1) and max(mr0, headers[0]) <= min(mr1, r1)
        contained = c0 <= mc0 <= mc1 <= c1 and headers[0] <= mr0 <= mr1 <= headers[-1]
        if intersects and not contained:
            issues.append(
                _issue("merged_cells_unsupported", "Data or cross-boundary merge is unsupported", range=region)
            )
    result_cols = [m["result_column"] for m in items]
    if form["layout"] == "wide_rows" and len(result_cols) != len(set(result_cols)):
        issues.append(_issue("duplicate_result_column", "Wide result columns must be distinct"))
    if form["layout"] == "long_rows":
        metrics = [m["metric"] for m in items]
        if len(set(result_cols)) != 1 or len({"column" in m for m in metrics}) != 1:
            issues.append(_issue("long_definition_conflict", "Long results require one column and metric source mode"))
        elif "column" in metrics[0]:
            if len({m["column"] for m in metrics}) != 1 or len({m["value"] for m in metrics}) != len(metrics):
                issues.append(_issue("long_definition_conflict", "Long metrics require distinct values in one column"))
            if len({repr(m["unit"]) for m in items}) != 1:
                issues.append(
                    _issue(
                        "long_unit_source_not_supported",
                        "Shared row scope cannot select different unit sources by metric",
                    )
                )
        elif len(metrics) != 1:
            issues.append(_issue("long_definition_conflict", "Fixed metric cell requires one definition"))
    shared = {}
    for item in items:
        unit = item["unit"]
        if unit["kind"] != "cell":
            continue
        origin = (tuple(unit["cell"]), tuple(unit.get("span", [])))
        for col in unit["applies_to"]:
            if col not in result_cols:
                issues.append(_issue("unit_applicability_unassigned", "Unit covers undeclared result", column=col))
            if col in shared and shared[col] != origin:
                issues.append(_issue("unit_applicability_conflict", "Shared unit sources overlap", column=col))
            shared[col] = origin
    for item in items:
        col = item["result_column"]
        unit = item["unit"]
        if col in shared and (
            unit["kind"] != "cell" or shared[col] != (tuple(unit["cell"]), tuple(unit.get("span", [])))
        ):
            issues.append(
                _issue(
                    "unit_applicability_conflict",
                    "Result unit conflicts with declared shared applicability",
                    column=col,
                )
            )
    if form["identity_mode"] == "unresolved":
        issues.append(_issue("identity_unresolved", "Sample identity is unresolved"))
    if any(i["code"] in ("source_bounds", "overlapping_roles", "long_definition_conflict") for i in issues):
        return plan, _status(issues), None
    paths = {col: _header_path(cells, headers, col) for col in set(roles)}
    checked_header_origins = set()
    for col, path in paths.items():
        for row, origin_col in path["sources"]:
            if (row, origin_col) in checked_header_origins:
                continue
            checked_header_origins.add((row, origin_col))
            try:
                _resolved({"row": row, "column": origin_col}, r0, cells)
            except ValueError as exc:
                issues.append(_issue("header_source_invalid", str(exc), row=row, column=origin_col))
    for col, path in paths.items():
        if not path["label"]:
            issues.append(_issue("header_unresolved", "Role header is blank", column=col))
    condition_labels = [paths[col]["label"] for col in form.get("condition_columns", [])]
    if len(condition_labels) != len(set(condition_labels)):
        issues.append(_issue("condition_header_ambiguous", "Condition columns need distinct full header paths"))
    excluded_unit_headers = {
        tuple(m["unit"]["cell"])
        for m in items
        if m["unit"]["kind"] == "cell" and "span" not in m["unit"] and m["unit"]["cell"][0] in headers
    }
    metric_paths = (
        {m["result_column"]: _metric_path(cells, headers, m["result_column"], excluded_unit_headers) for m in items}
        if form["layout"] == "wide_rows"
        else {}
    )
    if form["layout"] == "wide_rows":
        labels = [metric_paths[col]["label"] for col in result_cols]
        if any(not label for label in labels):
            issues.append(_issue("metric_unresolved", "Metric header is blank"))
        if len(labels) != len(set(labels)):
            issues.append(_issue("duplicate_metric_path", "Full metric paths repeat without distinction"))
    for item in items:
        for name, source in (("metric", item.get("metric")), ("unit", item["unit"])):
            if source is None or source.get("kind") == "absent":
                continue
            if "column" in source:
                if source["column"] not in selected:
                    issues.append(_issue("source_bounds", name + " column outside selection"))
                continue
            row, col = source["cell"]
            if row > table["nrows"] or col > table["ncols"]:
                issues.append(_issue("source_bounds", name + " cell outside source", row=row, column=col))
                continue
            if r0 <= row <= r1 and c0 <= col <= c1:
                issues.append(_issue("shared_source_in_data", name + " fixed cell is inside data", row=row, column=col))
            try:
                raw, _ = _resolved(_binding(source), r0, cells)
                if raw is None or str(raw).strip() == "":
                    issues.append(_issue("source_unresolved", name + " cell is blank", row=row, column=col))
            except ValueError as exc:
                issues.append(_issue("source_unresolved", str(exc), row=row, column=col))
    if any(i["code"] == "source_bounds" for i in issues):
        return plan, _status(issues), None
    metric_map = (
        {m["metric"]["value"]: m for m in items}
        if form["layout"] == "long_rows" and "column" in items[0]["metric"]
        else {}
    )
    missing = 0
    identity_displays = {}
    for row in range(r0, r1 + 1):
        sample = cells[(row, form["sample_column"])]
        sample_raw = sample["raw_value"]
        if (
            sample_raw is None
            or str(sample_raw).strip() == ""
            or sample.get("formula")
            or sample.get("value_type") in ("e", "error")
        ):
            issues.append(
                _issue(
                    "sample_unresolved",
                    "Sample identity is blank, formula or error",
                    row=row,
                    column=form["sample_column"],
                )
            )
        elif form["identity_mode"] == "same_id_in_scope":
            display = str(sample_raw)
            signature = (type(sample_raw).__name__, repr(sample_raw))
            prior = identity_displays.get(display)
            if prior is not None and prior[0] != signature:
                issues.append(
                    _issue(
                        "identity_display_collision",
                        "Equal displayed sample ids have different original representations",
                        row=row,
                        column=form["sample_column"],
                        first_row=prior[1],
                    )
                )
            else:
                identity_displays[display] = (signature, row)
        applicable = items
        if form["layout"] == "long_rows":
            if metric_map:
                col = items[0]["metric"]["column"]
                metric_cell = cells[(row, col)]
                value = metric_cell["raw_value"]
                chosen = metric_map.get(value) if isinstance(value, str) else None
                if metric_cell.get("formula") or metric_cell.get("value_type") in ("e", "error") or chosen is None:
                    issues.append(
                        _issue("metric_unassigned", "Metric source lacks finite definition", row=row, column=col)
                    )
                applicable = [chosen] if chosen else []
            else:
                applicable = items[:1]
        for item in applicable:
            col = item["result_column"]
            unit = item["unit"]
            unit_raw = None
            if unit["kind"] != "absent":
                try:
                    unit_raw, _ = _resolved(_binding(unit), row, cells)
                except ValueError as exc:
                    issues.append(_issue("unit_unresolved", str(exc), row=row, column=col))
                if unit_raw not in (None, "") and (
                    "\n" in str(unit_raw) or "[" in str(unit_raw) or "]" in str(unit_raw) or len(str(unit_raw)) > 80
                ):
                    issues.append(_issue("unit_source_invalid", "Select only unit text", row=row, column=col))
            cell = cells[(row, col)]
            raw = cell["raw_value"]
            if isinstance(raw, str) and raw.strip().casefold() in (
                _UNKNOWN - {"nan"} if form["version"] == "2" else _UNKNOWN
            ):
                issues.append(
                    _issue(
                        "unknown_missing_abbreviation",
                        "Missing abbreviation needs traceable meaning",
                        row=row,
                        column=col,
                    )
                )
                continue
            typed, inline, problem = simple_measurement(
                cell, item["value_type"], unit_raw, number_format=form.get("number_format", "source")
            )
            if problem:
                issues.append(_issue(problem, "Unsupported simple result", row=row, column=col))
            elif inline and unit["kind"] == "absent":
                issues.append(
                    _issue("unit_unresolved", "Inline unit conflicts with declared absence", row=row, column=col)
                )
            if typed.get("kind") == "missing":
                missing += 1
        for col in ([form["method_column"]] if form.get("method_column") else []) + form.get("condition_columns", []):
            try:
                _resolved({"row": "current", "column": col}, row, cells)
            except ValueError as exc:
                issues.append(_issue("source_unresolved", str(exc), row=row, column=col))
    if issues:
        return plan, _status(issues), None
    cur = lambda col: {"row": "current", "column": col}
    sample = cur(form["sample_column"])
    sid = "measurements"
    objects = {
        "sample": {
            "kind": "sample",
            "scope": sid,
            "key": [sample],
            "identity_scope": "scope" if form["identity_mode"] == "same_id_in_scope" else "record",
        }
    }
    fields = [
        {
            "scope": sid,
            "target": "sample",
            "field_id": "material.identity",
            "source": sample,
            "expected": "text",
            "header_sources": [{"row": r, "column": c} for r, c in paths[form["sample_column"]]["sources"]],
        }
    ]
    relations = []
    all_headers = []
    seen = set()
    for col in roles:
        for r, c in paths[col]["sources"]:
            if (r, c) not in seen:
                seen.add((r, c))
                all_headers.append({"row": r, "column": c})

    def qualifiers(metric):
        result = {"metric_label": metric, "property": metric}
        if form.get("method_column"):
            result["method"] = cur(form["method_column"])
        for col in form.get("condition_columns", []):
            result["condition:" + paths[col]["label"]] = cur(col)
        return result

    def add_graph(test_alias, result_alias):
        objects[test_alias] = {"kind": "reported_test", "scope": sid, "key": []}
        objects[result_alias] = {"kind": "observation", "scope": sid, "key": []}
        relations.extend(
            [
                {"scope": sid, "from": test_alias, "to": "sample", "predicate": "tests_subject"},
                {"scope": sid, "from": test_alias, "to": result_alias, "predicate": "produces_observation"},
            ]
        )

    if form["layout"] == "long_rows":
        item = items[0]
        add_graph("test", "result")
        field = {
            "scope": sid,
            "target": "result",
            "field_id": "observation.value",
            "source": cur(item["result_column"]),
            "expected": item["value_type"],
            "measurement_mode": "simple",
            "qualifiers": qualifiers(_binding(item["metric"])),
            "header_sources": all_headers,
        }
        if form["version"] == "2":
            field["number_format"] = form.get("number_format", "source")
        if metric_map:
            field["expected_by"] = {
                "qualifier": "property",
                "types": {m["metric"]["value"]: m["value_type"] for m in items},
            }
        if item["unit"]["kind"] != "absent":
            field["unit"] = _binding(item["unit"])
        fields.append(field)
    else:
        for index, item in enumerate(items):
            path = metric_paths[item["result_column"]]
            q = qualifiers({"semantic": path["label"]})
            for level, (r, c) in enumerate(path["sources"]):
                q[f"metric_header_{level + 1}"] = {"row": r, "column": c}
            field = {
                "scope": sid,
                "target": "sample",
                "field_id": "observation.value",
                "source": cur(item["result_column"]),
                "expected": item["value_type"],
                "measurement_mode": "simple",
                "qualifiers": q,
                "header_sources": all_headers,
            }
            if form["version"] == "2":
                field["number_format"] = form.get("number_format", "source")
            if item["unit"]["kind"] != "absent":
                field["unit"] = _binding(item["unit"])
            fields.append(field)
    plan = {
        "version": "2",
        "views": [{"id": "table", "asset_id": asset["asset_id"], "sheet": form["sheet"]}],
        "scopes": [{"id": sid, "view": "table", "kind": "row", "rows": [r0, r1], "columns": [c0, c1]}],
        "objects": objects,
        "fields": fields,
        "relations": relations,
    }
    try:
        compile_plan(plan, {asset["asset_id"]: tables})
    except ValueError as exc:
        issues.append(_issue("plan_capacity_or_binding", str(exc)))
        return _empty(), _status(issues), None
    ignored_nonempty = sum(
        cells[(row, col)]["raw_value"] not in (None, "") for row in range(r0, r1 + 1) for col in ignored
    )
    completion = {
        "selected_scope_complete": True,
        "selected_records": r1 - r0 + 1,
        "source_missing_results": missing,
        "ignored_columns": sorted(ignored),
        "ignored_nonempty_cells": ignored_nonempty,
    }
    return plan, _status([]), completion
