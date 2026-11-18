"""Finite standard-table semantic form and source-bound ReadingPlan bridge.

The form is a proposal. Callers must persist a separate human confirmation
before executing or publishing the returned plan.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from openpyxl.utils.cell import get_column_letter, range_boundaries

from .core import fingerprint
from .ingest.tables.row_measurements import _header_path
from .ingest.tables.table_plan import compile_plan
from .ingest.tables.table_reading import _selected_tables
from .table_meaning import inspect_meaning


def _obj(properties):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(properties)}


_INT = {"type": "integer", "minimum": 1}
_CELL = {"type": "array", "items": _INT, "minItems": 2, "maxItems": 2}
_SPAN = {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2}


def _kind(name):
    return {"type": "string", "const": name}


_UNIT = {
    "anyOf": [
        _obj({"kind": _kind("absent")}),
        _obj({"kind": _kind("column"), "column": _INT}),
        _obj({"kind": _kind("cell"), "cell": _CELL}),
        _obj({"kind": _kind("cell_span"), "cell": _CELL, "span": _SPAN}),
    ]
}
_METRIC = {
    "anyOf": [
        _obj({"kind": _kind("header")}),
        _obj({"kind": _kind("column"), "column": _INT}),
        _obj({"kind": _kind("cell"), "cell": _CELL}),
    ]
}
SUGGESTION_SCHEMA = _obj(
    {
        "version": _kind("2"),
        "row_kind": {
            "type": "string",
            "enum": ["sample_record", "test_record", "curve_point", "statistic", "run_log", "unknown"],
        },
        "layout": {"type": "string", "enum": ["wide_rows", "long_rows"]},
        "identity_mode": {"type": "string", "enum": ["row_independent", "same_id_in_scope", "unresolved"]},
        "number_format": {"type": "string", "enum": ["source", "decimal_dot", "decimal_comma"]},
        "header_conditions": {
            "type": "array",
            "maxItems": 8,
            "items": _obj({"cell": _CELL, "reason": {"type": "string", "minLength": 1, "maxLength": 160}}),
        },
        "column_roles": {
            "type": "array",
            "maxItems": 50,
            "items": _obj(
                {
                    "column": _INT,
                    "role": {
                        "type": "string",
                        "enum": [
                            "identity",
                            "result",
                            "metric",
                            "unit",
                            "condition",
                            "method",
                            "reference",
                            "ignored",
                            "unknown",
                        ],
                    },
                    "reason": {"type": "string", "minLength": 1, "maxLength": 160},
                }
            ),
        },
        "measurements": {
            "type": "array",
            "maxItems": 50,
            "items": _obj(
                {
                    "result_column": _INT,
                    "value_type": {"type": "string", "enum": ["number", "category"]},
                    "metric": _METRIC,
                    "unit": _UNIT,
                }
            ),
        },
    }
)


def _issue(code, message, **location):
    return {"code": code, "message": message, "severity": "error", **location}


def _empty():
    return {"version": "2", "views": [], "scopes": [], "objects": {}, "fields": [], "relations": []}


def _rectangle(name):
    c0, r0, c1, r1 = range_boundaries(name)
    if not all(type(x) is int and x >= 1 for x in (r0, c0, r1, c1)):
        raise ValueError("Region requires bounded rectangular source ranges")
    return r0, c0, r1, c1


def _geometry(region):
    if (
        not isinstance(region, dict)
        or len(region.get("header_ranges", [])) != 1
        or len(region.get("data_ranges", [])) != 1
    ):
        raise ValueError("Standard table requires one header and one data rectangle")
    outer = _rectangle(region["table_range"])
    header = _rectangle(region["header_ranges"][0])
    data = _rectangle(region["data_ranges"][0])
    if (
        header[1] != data[1]
        or header[3] != data[3]
        or header[2] + 1 != data[0]
        or outer != (header[0], header[1], data[2], data[3])
    ):
        raise ValueError("Standard table requires contiguous aligned header and data rectangles")
    if not 1 <= header[2] - header[0] + 1 <= 8 or data[2] - data[0] + 1 > 10_000 or data[3] - data[1] + 1 > 50:
        raise ValueError("Standard table exceeds header, row, or column capacity")
    if region.get("context_ranges"):
        raise ValueError("Off-table context is outside the standard-table scope")
    return header, data


def _read(asset, region, reader_options):
    if region.get("asset_id") != asset.get("asset_id"):
        raise ValueError("Region and original asset differ")
    actual = sha256(Path(asset["local_path"]).read_bytes()).hexdigest()
    if actual != asset.get("sha256"):
        raise ValueError("Original file hash differs from registered asset")
    options = (reader_options or {}).get(asset["asset_id"], {})
    tables = _selected_tables(asset, options)
    matches = [t for t in tables if t.get("sheet") == region.get("sheet")]
    if len(matches) != 1:
        raise ValueError("Region sheet is unavailable or ambiguous")
    return matches[0], tables


def build_context(
    asset: dict,
    region: dict,
    reader_options: dict | None = None,
    *,
    sample_rows: list[int] | None = None,
    sample_range: str | None = None,
    max_bytes: int = 60_000,
):
    """Return the exact, bounded table-only payload shown before model consent."""
    header, data = _geometry(region)
    table, _ = _read(asset, region, reader_options)
    if data[2] > table["nrows"] or data[3] > table["ncols"]:
        raise ValueError("Region exceeds source sheet bounds")
    rows = {r["row"]: r for r in table["rows"]}
    if sample_rows is None:
        selected = sorted({data[0], (data[0] + data[2]) // 2, data[2]})
    else:
        if (
            not isinstance(sample_rows, list)
            or not 1 <= len(sample_rows) <= 24
            or any(type(row) is not int or not data[0] <= row <= data[2] for row in sample_rows)
            or len(set(sample_rows)) != len(sample_rows)
        ):
            raise ValueError("Sample rows must be unique source rows inside the confirmed data range")
        selected = sorted(sample_rows)
    # Crop examples only; the confirmed geometry and complete headers stay intact.
    sample_columns = data[1], data[3]
    if sample_range is not None:
        if (
            not isinstance(sample_range, str)
            or re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]{0,6}:[A-Za-z]{1,3}[1-9][0-9]{0,6}", sample_range) is None
        ):
            raise ValueError("Sample range must be a bounded A1:B2 rectangle")
        r0, c0, r1, c1 = _rectangle(sample_range)
        if not (data[0] <= r0 <= r1 <= data[2] and data[1] <= c0 <= c1 <= data[3]):
            raise ValueError("Sample range must stay inside the confirmed data rectangle without reversing its corners")
        if r1 - r0 + 1 > 24:
            raise ValueError("Sample range may contain at most 24 data rows")
        rectangle_rows = list(range(r0, r1 + 1))
        if sample_rows is not None and selected != rectangle_rows:
            raise ValueError("Sample rows and sample range must select exactly the same rows")
        selected = rectangle_rows
        sample_columns = c0, c1
        sample_range = f"{get_column_letter(c0)}{r0}:{get_column_letter(c1)}{r1}"

    def row_at(n, payload_role, columns):
        return {
            "row": n,
            "cells": [
                {
                    **{
                        key: c[key]
                        for key in (
                            "row",
                            "column",
                            "address",
                            "raw_value",
                            "merged_origin",
                            "formula",
                            "value_type",
                            "missing_kind",
                        )
                        if key in c
                    },
                    "payload_role": payload_role,
                }
                for c in rows[n]["cells"]
                if columns[0] <= c["column"] <= columns[1]
            ],
        }

    result = {
        "region_id": region.get("id"),
        "asset_id": asset["asset_id"],
        "source_sha256": asset["sha256"],
        "sheet": region["sheet"],
        "table_range": region["table_range"],
        "header_ranges": region["header_ranges"],
        "data_ranges": region["data_ranges"],
        "headers": [row_at(r, "table_header", (data[1], data[3])) for r in range(header[0], header[2] + 1)],
        "examples": [row_at(r, "sample_row", sample_columns) for r in selected],
        "sample_rows": selected,
        "row_count": data[2] - data[0] + 1,
        "column_count": data[3] - data[1] + 1,
    }
    # Omit the new field on legacy payloads so their persisted hashes remain valid.
    if sample_range is not None:
        result["sample_range"] = sample_range
    if len(json.dumps(result, ensure_ascii=False, default=str).encode()) > max_bytes:
        raise ValueError("Standard table context exceeds input budget; header labels are not truncated")
    result["payload_sha256"] = fingerprint(result)
    return result


def _validate_form(form, header, data):
    if not isinstance(form, dict) or set(form) != set(SUGGESTION_SCHEMA["properties"]) or form["version"] != "2":
        raise ValueError("Standard table form must match version 2 contract")
    if (
        form["row_kind"] not in SUGGESTION_SCHEMA["properties"]["row_kind"]["enum"]
        or form["layout"] not in ("wide_rows", "long_rows")
        or form["identity_mode"] not in ("row_independent", "same_id_in_scope", "unresolved")
        or form["number_format"] not in ("source", "decimal_dot", "decimal_comma")
    ):
        raise ValueError("Unsupported row kind, layout, or identity mode")
    roles = form["column_roles"]
    measurements = form["measurements"]
    if not isinstance(roles, list) or not isinstance(measurements, list) or len(measurements) > 50:
        raise ValueError("Roles and measurements require finite lists")
    mapping = {}
    for item in roles:
        if (
            not isinstance(item, dict)
            or set(item) != {"column", "role", "reason"}
            or type(item["column"]) is not int
            or item["column"] in mapping
            or not isinstance(item["reason"], str)
            or not 1 <= len(item["reason"].strip()) <= 160
            or item["role"]
            not in SUGGESTION_SCHEMA["properties"]["column_roles"]["items"]["properties"]["role"]["enum"]
        ):
            raise ValueError("Every selected column needs one role and a short explanation")
        mapping[item["column"]] = item["role"]
    if set(mapping) != set(range(data[1], data[3] + 1)):
        raise ValueError("Roles must cover every selected source column exactly once")
    header_conditions = form["header_conditions"]
    if not isinstance(header_conditions, list) or len(header_conditions) > 8:
        raise ValueError("Header conditions must be finite")
    seen = set()
    for condition in header_conditions:
        if (
            not isinstance(condition, dict)
            or set(condition) != {"cell", "reason"}
            or not isinstance(condition["cell"], list)
            or len(condition["cell"]) != 2
            or any(type(v) is not int for v in condition["cell"])
            or not header[0] <= condition["cell"][0] <= header[2]
            or not header[1] <= condition["cell"][1] <= header[3]
            or not isinstance(condition["reason"], str)
            or not 1 <= len(condition["reason"].strip()) <= 160
            or tuple(condition["cell"]) in seen
        ):
            raise ValueError("Header condition needs one unique source header cell")
        seen.add(tuple(condition["cell"]))
    if form["row_kind"] not in ("sample_record", "test_record"):
        return mapping
    if not measurements or "unknown" in mapping.values():
        raise ValueError("Standard records require classified result columns")
    if not 1 <= list(mapping.values()).count("identity") <= 3 or list(mapping.values()).count("method") > 1:
        raise ValueError("One to three identity and at most one method column are required")
    for item in measurements:
        if (
            not isinstance(item, dict)
            or set(item) != {"result_column", "value_type", "metric", "unit"}
            or type(item["result_column"]) is not int
            or mapping.get(item["result_column"]) != "result"
            or item["value_type"] not in ("number", "category")
        ):
            raise ValueError("Measurement must bind a result role")
        metric = item["metric"]
        unit = item["unit"]
        if (
            not isinstance(metric, dict)
            or metric.get("kind") not in ("header", "column", "cell")
            or set(metric)
            != (
                {"kind", "column"}
                if metric["kind"] == "column"
                else {"kind", "cell"}
                if metric["kind"] == "cell"
                else {"kind"}
            )
        ):
            raise ValueError("Metric requires a finite source mode")
        if metric["kind"] == "column" and mapping.get(metric["column"]) != "metric":
            raise ValueError("Metric source must have metric role")
        if (
            form["layout"] == "wide_rows"
            and metric["kind"] != "header"
            or form["layout"] == "long_rows"
            and metric["kind"] == "header"
        ):
            raise ValueError("Metric source conflicts with table layout")
        if (
            not isinstance(unit, dict)
            or unit.get("kind") not in ("absent", "column", "cell", "cell_span")
            or set(unit)
            != (
                {"kind", "column"}
                if unit["kind"] == "column"
                else {"kind", "cell", "span"}
                if unit["kind"] == "cell_span"
                else {"kind", "cell"}
                if unit["kind"] == "cell"
                else {"kind"}
            )
        ):
            raise ValueError("Unit requires a finite source mode")
        if unit["kind"] == "column" and mapping.get(unit["column"]) != "unit":
            raise ValueError("Unit source must have unit role")
        for source in (metric, unit):
            if "cell" in source and (
                not isinstance(source["cell"], list)
                or len(source["cell"]) != 2
                or any(type(v) is not int or v < 1 for v in source["cell"])
            ):
                raise ValueError("Source cell must be an exact positive coordinate")
            if "cell" in source and not (
                header[0] <= source["cell"][0] <= header[2] and header[1] <= source["cell"][1] <= header[3]
            ):
                raise ValueError("Fixed metric and unit cells must lie inside confirmed header rectangle")
        if unit["kind"] == "cell_span" and (
            not isinstance(unit["span"], list)
            or len(unit["span"]) != 2
            or any(type(v) is not int for v in unit["span"])
            or not 0 <= unit["span"][0] < unit["span"][1] <= 4096
        ):
            raise ValueError("Unit span must be bounded and nonempty")
    if {m["result_column"] for m in measurements} != {c for c, r in mapping.items() if r == "result"}:
        raise ValueError("Every result role needs a measurement")
    return mapping


def inspect_standard_table(form: dict, asset: dict, region: dict, reader_options: dict | None = None):
    """Preflight a proposal and return plan, diagnostics, completion, review."""
    if isinstance(form, dict) and form.get("version") == "3":
        from .standard_curve_meaning import inspect_curve_table

        return inspect_curve_table(form, asset, region, reader_options)
    header, data = _geometry(region)
    mapping = _validate_form(form, header, data)
    context = build_context(asset, region, reader_options)
    review = {
        "requires_human_confirmation": True,
        "row_kind": form["row_kind"],
        "column_roles": deepcopy(form["column_roles"]),
        "headers": context["headers"],
    }
    if form["row_kind"] not in ("sample_record", "test_record"):
        return {
            "plan": _empty(),
            "diagnostics": {
                "ready": False,
                "issues": [_issue("row_kind_unsupported", "Only independent sample or test records can execute")],
                "issue_total": 1,
            },
            "completion": None,
            "review": review,
        }
    if form["identity_mode"] == "unresolved":
        return {
            "plan": _empty(),
            "diagnostics": {
                "ready": False,
                "issues": [_issue("identity_unresolved", "Record identity requires review")],
                "issue_total": 1,
            },
            "completion": None,
            "review": review,
        }
    identities = [c for c, r in mapping.items() if r == "identity"]
    methods = [c for c, r in mapping.items() if r == "method"]
    references = [c for c, r in mapping.items() if r == "reference"]
    review["identity_sources"] = identities
    review["identity_examples"] = [
        {
            "row": example["row"],
            "source_values": [
                next(cell["raw_value"] for cell in example["cells"] if cell["column"] == col) for col in identities
            ],
        }
        for example in context["examples"]
    ]
    items = []
    for item in form["measurements"]:
        metric = item["metric"]
        unit = item["unit"]
        converted = deepcopy(item)
        if form["layout"] == "wide_rows":
            converted.pop("metric")
        elif metric["kind"] == "column":
            # Finite long-row vocabulary is derived from source values, not model text.
            table, _ = _read(asset, region, reader_options)
            values = sorted(
                {table["rows"][r - 1]["cells"][metric["column"] - 1]["raw_value"] for r in range(data[0], data[2] + 1)},
                key=str,
            )
            if len(values) > 50 or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("Long metric source requires at most 50 nonblank source labels")
            if unit["kind"] in ("cell", "cell_span"):
                converted["unit"] = {"kind": "cell", "cell": unit["cell"], "applies_to": [item["result_column"]]}
                if unit["kind"] == "cell_span":
                    converted["unit"]["span"] = unit["span"]
            for value in values:
                definition = deepcopy(converted)
                definition["metric"] = {"column": metric["column"], "value": value}
                items.append(definition)
            continue
        elif metric["kind"] == "cell":
            converted["metric"] = {"cell": metric["cell"]}
        if unit["kind"] in ("cell", "cell_span"):
            converted["unit"] = {"kind": "cell", "cell": unit["cell"], "applies_to": [item["result_column"]]}
            if unit["kind"] == "cell_span":
                converted["unit"]["span"] = unit["span"]
        items.append(converted)
    legacy = {
        "version": "2",
        "asset_id": asset["asset_id"],
        "source_sha256": asset["sha256"],
        "sheet": region["sheet"],
        "layout": form["layout"],
        "data_range": list(data),
        "number_format": form["number_format"],
        "header_rows": list(range(header[0], header[2] + 1)),
        "sample_column": identities[0],
        "identity_mode": form["identity_mode"],
        "measurements": items,
        "ignored_columns": [c for c, r in mapping.items() if r == "ignored"] + identities[1:] + references,
        "condition_columns": [c for c, r in mapping.items() if r == "condition"],
    }
    if methods:
        legacy["method_column"] = methods[0]
    plan, diagnostics, completion = inspect_meaning(legacy, [asset], reader_options)
    if diagnostics["ready"] and (len(identities) > 1 or references):
        table, _ = _read(asset, region, reader_options)
        cells = {(c["row"], c["column"]): c for row in table["rows"] for c in row["cells"]}
        for row in range(data[0], data[2] + 1):
            for col in identities[1:]:
                cell = cells[(row, col)]
                raw = cell["raw_value"]
                if (
                    raw is None
                    or str(raw).strip() == ""
                    or cell.get("formula")
                    or cell.get("value_type") in ("e", "error")
                ):
                    diagnostics = {
                        "ready": False,
                        "issues": [
                            _issue(
                                "identity_component_unresolved",
                                "Composite identity has a blank, formula or error component",
                                row=row,
                                column=col,
                            )
                        ],
                        "issue_total": 1,
                    }
                    plan = _empty()
                    completion = None
                    break
            if not diagnostics["ready"]:
                break
        if not diagnostics["ready"]:
            return {"plan": plan, "diagnostics": diagnostics, "completion": completion, "review": review}
        plan["objects"]["sample"]["key"] = [{"row": "current", "column": c} for c in identities]
        for field in plan["fields"]:
            if field["field_id"] != "observation.value":
                continue
            for c in references:
                path = _header_path(cells, list(range(header[0], header[2] + 1)), c)
                label = path["label"]
                if not label:
                    diagnostics = {
                        "ready": False,
                        "issues": [_issue("reference_header_unresolved", "Reference column header is blank", column=c)],
                        "issue_total": 1,
                    }
                    plan = _empty()
                    completion = None
                    break
                field["qualifiers"][f"reference:{label}"] = {"row": "current", "column": c}
            if not diagnostics["ready"]:
                break
        if diagnostics["ready"] and completion is not None:
            actually_ignored = [c for c, r in mapping.items() if r == "ignored"]
            completion["ignored_columns"] = actually_ignored
            completion["ignored_nonempty_cells"] = sum(
                cells[(r, c)]["raw_value"] not in (None, "")
                for r in range(data[0], data[2] + 1)
                for c in actually_ignored
            )
    if diagnostics["ready"] and form["header_conditions"]:
        table, _ = _read(asset, region, reader_options)
        cells = {(c["row"], c["column"]): c for row in table["rows"] for c in row["cells"]}
        for condition in form["header_conditions"]:
            cell = cells[tuple(condition["cell"])]
            if cell["raw_value"] in (None, "") or cell.get("formula") or cell.get("value_type") in ("e", "error"):
                diagnostics = {
                    "ready": False,
                    "issues": [_issue("header_condition_invalid", "Header condition is blank, formula or error")],
                    "issue_total": 1,
                }
                plan = _empty()
                completion = None
                break
        if diagnostics["ready"]:
            for field in plan["fields"]:
                if field["field_id"] == "observation.value":
                    for n, condition in enumerate(form["header_conditions"], 1):
                        field["qualifiers"][f"condition:header_{n}"] = {
                            "row": condition["cell"][0],
                            "column": condition["cell"][1],
                        }
    if diagnostics["ready"] and form["row_kind"] == "test_record":
        plan = deepcopy(plan)
        owner = plan["objects"]["sample"]
        owner["kind"] = "reported_test"
        owner["label"] = owner["key"][0]
        plan["fields"] = [field for field in plan["fields"] if field["field_id"] != "material.identity"]
        if form["layout"] == "long_rows":
            plan["objects"].pop("test")
            plan["relations"] = [r for r in plan["relations"] if r["predicate"] != "tests_subject"]
            for relation in plan["relations"]:
                if relation["predicate"] == "produces_observation":
                    relation["from"] = "sample"
        try:
            _, tables = _read(asset, region, reader_options)
            compile_plan(plan, {asset["asset_id"]: tables})
        except ValueError as exc:
            diagnostics = {"ready": False, "issues": [_issue("plan_capacity_or_binding", str(exc))], "issue_total": 1}
            plan = _empty()
            completion = None
    return {"plan": plan, "diagnostics": diagnostics, "completion": completion, "review": review}


def combine_plans(plans: list[dict]):
    """Join independent region plans with local aliases and reject overlap."""
    if not isinstance(plans, list) or not plans:
        raise ValueError("At least one ready region plan is required")
    result = _empty()
    occupied = []
    for index, plan in enumerate(plans):
        if not plan.get("scopes") or len(plan["views"]) != 1 or len(plan["scopes"]) != 1:
            raise ValueError("Only ready single-region plans can combine")
        view = plan["views"][0]
        scope = plan["scopes"][0]
        box = (*scope["rows"], *scope["columns"])
        for other_view, other_box in occupied:
            if (
                (view["asset_id"], view["sheet"]) == (other_view["asset_id"], other_view["sheet"])
                and max(box[0], other_box[0]) <= min(box[1], other_box[1])
                and max(box[2], other_box[2]) <= min(box[3], other_box[3])
            ):
                raise ValueError("Region data rectangles overlap")
        occupied.append((view, box))
        prefix = f"region_{index + 1}__"
        v = prefix + view["id"]
        s = prefix + scope["id"]
        result["views"].append({**view, "id": v})
        result["scopes"].append({**scope, "id": s, "view": v})
        aliases = {name: prefix + name for name in plan["objects"]}
        for name, obj in plan["objects"].items():
            obj = deepcopy(obj)
            obj["scope"] = s
            for key in ("owner", "material"):
                if key in obj:
                    obj[key] = aliases[obj[key]]
            result["objects"][aliases[name]] = obj
        for field in plan["fields"]:
            field = deepcopy(field)
            field["scope"] = s
            field["target"] = aliases[field["target"]]
            result["fields"].append(field)
        for relation in plan.get("relations", []):
            relation = deepcopy(relation)
            relation["scope"] = s
            relation["from"] = aliases[relation["from"]]
            relation["to"] = aliases[relation["to"]]
            result["relations"].append(relation)
    return result
