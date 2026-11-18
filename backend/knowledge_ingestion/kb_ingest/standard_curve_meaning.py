"""Finite curve confirmation compiled into the existing source-bound v2 engine.

The v3 form is a proposal, never a confirmation receipt. Points remain ordered
source values; header meanings do not establish physical samples or test runs.
"""

import re
from copy import deepcopy

from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries

from .core import fingerprint
from .ingest.tables.table_plan import compile_plan
from .typed_values import parse_typed_value

_FORM_KEYS = {"version", "row_kind", "layout", "number_format", "curve"}
_CURVE_KEYS = {"x_column", "x_label", "x_unit", "series", "ignored_columns"}
_SERIES_KEYS = {"result_column", "material", "stage", "metric", "unit"}
_FORMATS = {"source", "decimal_dot", "decimal_comma"}
_STAGES = {"heating", "cooling", "升温", "降温", "升温段", "降温段"}


def _is_stage(value):
    text = str(value).strip().casefold()
    return (
        text in _STAGES
        or re.fullmatch(r"(?:[1-9]\d*(?:st|nd|rd|th)?|first|second|third)\s+(?:heating|cooling)", text) is not None
    )


def _unparsed_point(value):
    return value["kind"] not in ("decimal", "missing") or value.get("status") not in (
        "valid",
        "blank",
        "not_tested",
        "not_detected",
        "not_applicable",
        "not_reported",
        "not_available",
    )


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(label + " must match its finite source contract")


def _column(value, data):
    if type(value) is not int or not data[1] <= value <= data[3]:
        raise ValueError("Curve column lies outside the confirmed data rectangle")
    return value


def _binding(reference, header, cells, *, required=False):
    """Validate and resolve a fixed header reference, retaining merge evidence."""
    if not isinstance(reference, dict):
        raise ValueError("请选择原表来源")
    kind = reference.get("kind")
    if kind in ("absent", "unresolved"):
        _keys(reference, {"kind"}, "Curve reference")
        if kind == "unresolved":
            raise ValueError("尚未确认来源")
        if required:
            raise ValueError("名称必须引用非空原表表头，不能选择未说明")
        return None, None, None
    _keys(reference, {"kind", "cell", "span"} if kind == "cell_span" else {"kind", "cell"}, "Curve reference")
    if kind not in ("cell", "cell_span"):
        raise ValueError("请选择固定表头单元格或其中的文字片段")
    coord = reference["cell"]
    if (
        not isinstance(coord, list)
        or len(coord) != 2
        or any(type(v) is not int for v in coord)
        or not header[0] <= coord[0] <= header[2]
        or not header[1] <= coord[1] <= header[3]
    ):
        raise ValueError("来源超出已确认的表头范围")
    requested = tuple(coord)
    cell = cells.get(requested)
    if cell is None:
        raise ValueError("找不到所选原表表头")
    origin = coordinate_to_tuple(cell["merged_origin"]) if cell.get("merged_origin") else requested
    if not header[0] <= origin[0] <= header[2] or not header[1] <= origin[1] <= header[3]:
        raise ValueError("合并单元格原格超出已确认的表头范围")
    cell = cells.get(origin)
    if cell is None:
        raise ValueError("找不到合并单元格的原格")
    raw = cell.get("raw_value")
    if (
        cell.get("formula")
        or cell.get("value_type") in ("f", "formula", "e", "error")
        or isinstance(raw, str)
        and raw.lstrip().startswith("=")
    ):
        raise ValueError("名称和单位不能使用公式或错误单元格")
    binding = {"row": requested[0], "column": requested[1]}
    if origin != requested:
        binding["merged"] = True
    if kind == "cell_span":
        span = reference["span"]
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(v) is not int for v in span)
            or not isinstance(raw, str)
            or not 0 <= span[0] < span[1] <= min(4096, len(raw))
        ):
            raise ValueError("文字片段为空或超出原单元格文字范围")
        binding["span"] = list(span)
        raw = raw[slice(*span)]
    if raw is None or not str(raw).strip():
        raise ValueError("所选原表表头为空")
    return binding, raw, (origin, tuple(reference.get("span", [])))


def _named_binding(label, reference, header, cells, *, required=False):
    try:
        return _binding(reference, header, cells, required=required)
    except ValueError as exc:
        raise ValueError(f"{label}：{exc}") from exc


def _header_bindings(header, cells, columns):
    """Retain each nonempty level, including merged target-to-origin proof."""
    result = []
    for column in columns:
        for row in range(header[0], header[2] + 1):
            cell = cells.get((row, column), {})
            origin = coordinate_to_tuple(cell["merged_origin"]) if cell.get("merged_origin") else (row, column)
            raw = cells.get(origin, {}).get("raw_value")
            if raw is None or not str(raw).strip():
                continue
            binding, _, _ = _binding({"kind": "cell", "cell": [row, column]}, header, cells)
            if binding not in result:
                result.append(binding)
    return result


def inspect_curve_table(form, asset, region, reader_options=None):
    from .standard_table_meaning import _geometry, _read, build_context

    header, data = _geometry(region)
    _keys(form, _FORM_KEYS, "Curve form")
    if (
        form["version"] != "3"
        or form["row_kind"] != "curve_point"
        or form["layout"] != "wide_rows"
        or not isinstance(form["number_format"], str)
        or form["number_format"] not in _FORMATS
    ):
        raise ValueError("Unsupported curve form version, layout or number format")
    curve = form["curve"]
    _keys(curve, _CURVE_KEYS, "Curve mapping")
    x_column = _column(curve["x_column"], data)
    series = curve["series"]
    ignored = curve["ignored_columns"]
    if not isinstance(series, list) or not 1 <= len(series) <= 49:
        raise ValueError("Curve mapping requires 1..49 finite result series")
    if not isinstance(ignored, list) or len(ignored) > 49:
        raise ValueError("Curve ignored columns require a finite explicit list")
    selected = [x_column] + [_column(c, data) for c in ignored]
    for item in series:
        _keys(item, _SERIES_KEYS, "Curve series")
        selected.append(_column(item["result_column"], data))
    if len(selected) != len(set(selected)) or set(selected) != set(range(data[1], data[3] + 1)):
        raise ValueError("Every curve column must be exactly one shared x, result or explicitly ignored column")
    context = build_context(asset, region, reader_options)
    table, tables = _read(asset, region, reader_options)
    cells = {(c["row"], c["column"]): c for row in table["rows"] for c in row["cells"]}
    x_label, _, _ = _named_binding("横轴名称", curve["x_label"], header, cells, required=True)
    x_unit, _, _ = _named_binding("横轴单位", curve["x_unit"], header, cells)
    scope = "curve"
    view = "table"
    # One bounded iteration emits the declared series bundle. The envelope stays
    # the entire region, so combine_plans retains its existing overlap guard.
    plan = {
        "version": "2",
        "views": [{"id": view, "asset_id": asset["asset_id"], "sheet": region["sheet"]}],
        "scopes": [
            {
                "id": scope,
                "view": view,
                "kind": "curve",
                "rows": [data[0], data[2]],
                "columns": [data[1], data[3]],
                "blocks": [{"rows": [data[0], data[2]], "columns": [x_column, x_column]}],
            }
        ],
        "objects": {},
        "fields": [],
        "relations": [],
    }
    materials = {}
    review_series = []
    unparsed = 0
    numeric_pairs = 0
    x_typed = [
        parse_typed_value(cells[(row, x_column)], expected="number", number_format=form["number_format"])
        for row in range(data[0], data[2] + 1)
    ]
    for item in series:
        column = item["result_column"]
        alias = f"series_{column}"
        label = get_column_letter(column) + " 列"
        material, material_label, material_key = _named_binding(label + "材料", item["material"], header, cells)
        stage, stage_label, _ = _named_binding(label + "阶段", item["stage"], header, cells)
        metric, metric_label, _ = _named_binding(label + "指标", item["metric"], header, cells, required=True)
        unit, _, _ = _named_binding(label + "单位", item["unit"], header, cells)
        # Alias + region/record domain retains distinct source result columns,
        # even when every visible label and numeric value happens to be equal.
        plan["objects"][alias] = {
            "kind": "series",
            "scope": scope,
            "key": [b for b in (material, stage, metric) if b is not None],
        }
        qualifiers = {
            "metric_label": metric,
            "metric_id": {"source_code": "source_column", "source": metric},
            "axis:x_label": x_label,
        }
        if material is not None:
            qualifiers["material_label"] = material
            if material_key not in materials:
                owner = "material_" + fingerprint(material_key)[:12]
                materials[material_key] = owner
                plan["objects"][owner] = {"kind": "material", "scope": scope, "key": [material]}
                origin, span = material_key
                material_source = {"row": origin[0], "column": origin[1]}
                if span:
                    material_source["span"] = list(span)
                plan["fields"].append(
                    {
                        "scope": scope,
                        "target": owner,
                        "field_id": "material.identity",
                        "mode": "scalar",
                        "source": material_source,
                        "expected": "text",
                    }
                )
            plan["relations"].append(
                {"scope": scope, "from": materials[material_key], "to": alias, "predicate": "has_series"}
            )
        if stage is not None:
            qualifiers["condition:stage"] = stage
        field = {
            "scope": scope,
            "target": alias,
            "field_id": "observation.value",
            "expected": "number",
            "source": {"row": "current", "column": column},
            "x_source": {"row": "current", "column": x_column},
            "number_format": form["number_format"],
            "qualifiers": qualifiers,
            "header_sources": _header_bindings(header, cells, [x_column, column]),
        }
        if unit is not None:
            field["unit"] = unit
        if x_unit is not None:
            field["x_unit"] = x_unit
        plan["fields"].append(field)
        ys = [
            parse_typed_value(cells[(row, column)], expected="number", number_format=form["number_format"])
            for row in range(data[0], data[2] + 1)
        ]
        unparsed += sum(_unparsed_point(v) for v in ys)
        numeric_pairs += sum(
            x["kind"] == y["kind"] == "decimal" and x["status"] == y["status"] == "valid" for x, y in zip(x_typed, ys)
        )
        review_series.append(
            {
                "result_column": column,
                "material_label": material_label,
                "stage_label": stage_label,
                "metric_label": metric_label,
            }
        )
    compile_plan(plan, {asset["asset_id"]: tables})
    completion = {
        "series_count": len(series),
        "point_row_count": data[2] - data[0] + 1,
        "complete_numeric_point_count": numeric_pairs,
        "unparsed_point_count": unparsed,
        "unparsed_x_point_count": sum(_unparsed_point(v) for v in x_typed),
        "ignored_columns": list(ignored),
        "ignored_nonempty_cells": sum(
            cells[(r, c)]["raw_value"] not in (None, "") for r in range(data[0], data[2] + 1) for c in ignored
        ),
    }
    return {
        "plan": plan,
        "diagnostics": {"ready": True, "issues": [], "issue_total": 0},
        "completion": completion,
        "review": {
            "requires_human_confirmation": True,
            "row_kind": "curve_point",
            "headers": context["headers"],
            "series": review_series,
        },
    }


def prepare_curve_candidate(form, context):
    """Upgrade old curve proposals without saving, confirming or calling a model.

    Unknown columns stay unclassified, and absent old axis metadata stays
    unresolved. Already edited v3 forms are copied verbatim.
    """
    if not isinstance(form, dict):
        raise ValueError("Curve candidate requires a form")
    if form.get("version") == "3":
        return deepcopy(form)
    if form.get("version") != "2" or form.get("row_kind") != "curve_point":
        raise ValueError("Only version 2 curve proposals can be upgraded")
    c0, r0, c1, r1 = range_boundaries(context["header_ranges"][0])
    header = (r0, c0, r1, c1)
    cells = {(c["row"], c["column"]): c for row in context["headers"] for c in row["cells"]}
    roles = form.get("column_roles", [])
    if not isinstance(roles, list) or len(roles) > 50 or any(not isinstance(r, dict) for r in roles):
        raise ValueError("Curve candidate requires source column roles")
    columns = set()
    for role in roles:
        column = role.get("column")
        if type(column) is not int or not c0 <= column <= c1 or column in columns:
            raise ValueError("候选列坐标必须是表内不重复的整数")
        columns.add(column)
    conditions = [r.get("column") for r in roles if r.get("role") == "condition"]
    x_column = conditions[0] if len(conditions) == 1 else None
    unresolved = lambda: {"kind": "unresolved"}

    def headers(column):
        if type(column) is not int or not c0 <= column <= c1:
            return []
        found = []
        seen = set()
        for row in range(r0, r1 + 1):
            reference = {"kind": "cell", "cell": [row, column]}
            try:
                _, raw, key = _binding(reference, header, cells)
            except ValueError:
                continue
            if key not in seen:
                # Return the original cell, making merged identity suggestions
                # independent of which covered result column was inspected.
                found.append(({"kind": "cell", "cell": list(key[0])}, raw))
                seen.add(key)
        return found

    def valid_reference(reference, *, required=False):
        try:
            _binding(reference, header, cells, required=required)
            return deepcopy(reference)
        except (ValueError, TypeError, KeyError):
            return unresolved()

    axis = headers(x_column)
    candidate = {
        "version": "3",
        "row_kind": "curve_point",
        "layout": "wide_rows",
        "number_format": form.get("number_format", "source"),
        "curve": {
            "x_column": x_column,
            "x_label": axis[-1][0] if axis else unresolved(),
            "x_unit": unresolved(),
            "series": [],
            "ignored_columns": [r["column"] for r in roles if r.get("role") == "ignored"],
        },
    }
    measurements = form.get("measurements", [])
    if not isinstance(measurements, list) or any(not isinstance(m, dict) for m in measurements):
        raise ValueError("Curve candidate requires finite measurements")
    by_column = {}
    for item in measurements:
        column = item.get("result_column")
        if type(column) is not int or column in by_column:
            raise ValueError("Curve candidate contains an invalid or repeated result column")
        by_column[column] = item
    for role in roles:
        if role.get("role") != "result":
            continue
        column = role.get("column")
        old = by_column.get(column, {})
        path = headers(column)
        stage = unresolved()
        material = unresolved()
        stages = [(i, ref) for i, (ref, raw) in enumerate(path) if _is_stage(raw)]
        if len(stages) == 1:
            index, stage = stages[0]
            if index == 1:
                material = path[0][0]
        metric = old.get("metric", unresolved())
        if not isinstance(metric, dict):
            metric = unresolved()
        if metric.get("kind") == "header":
            metric = path[-1][0] if path else unresolved()
            if metric == stage or metric == material:
                metric = unresolved()
        elif metric.get("kind") == "cell":
            metric = valid_reference(metric, required=True)
        else:
            metric = unresolved()
        unit = valid_reference(old.get("unit", unresolved()))
        # A model's whole-cell unit sometimes includes its metric, e.g.
        # "DSC signal (W/g)". Split only explicit source punctuation into exact
        # spans; this does not infer a unit or normalize the source wording.
        if unit.get("kind") in ("cell", "cell_span"):
            whole = {"kind": "cell", "cell": unit["cell"]}
            _, raw, _ = _binding(whole, header, cells)
            match = re.fullmatch(r"\s*(.*?)\s*[\(\[]([^()\[\]]+)[\)\]]\s*", raw) if isinstance(raw, str) else None
            if (
                match
                and match.group(1).strip()
                and match.group(2).strip()
                and (unit["kind"] == "cell" or unit["span"] == list(match.span(2)))
            ):
                coordinate = deepcopy(unit["cell"])
                unit = {"kind": "cell_span", "cell": coordinate, "span": list(match.span(2))}
                if metric == {"kind": "cell", "cell": coordinate}:
                    metric = {"kind": "cell_span", "cell": coordinate, "span": list(match.span(1))}
        candidate["curve"]["series"].append(
            {"result_column": column, "material": material, "stage": stage, "metric": metric, "unit": unit}
        )
    return candidate
