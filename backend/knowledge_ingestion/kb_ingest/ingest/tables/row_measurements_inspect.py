"""Inspect restricted row-measurement definitions against immutable sources."""

import re
from copy import deepcopy

from openpyxl.utils.cell import column_index_from_string, coordinate_from_string, get_column_letter, range_boundaries

from kb_ingest.typed_values import normalize_unit, parse_typed_value

from .measurement_values import simple_measurement
from .table_recipe import keys

DEFINITION_KEYS = {
    "version",
    "asset_id",
    "sheet",
    "header_row",
    "header_rows",
    "data_range",
    "sample_column",
    "metric_source",
    "result_column",
    "result_types",
    "unit_source",
    "method_column",
    "condition_columns",
    "ignored_columns",
}


def _issue(code, message, **location):
    return {"code": code, "message": message, "severity": "error", **location}


def _empty_plan():
    return {"version": "2", "views": [], "scopes": [], "objects": {}, "fields": [], "relations": []}


def _source(source, table, *, allow_missing=False):
    if allow_missing and source == {"not_reported": True}:
        return None
    if not isinstance(source, dict):
        raise ValueError("请选择原表列或原单元格")
    if set(source) == {"column"}:
        if type(source["column"]) is not int or not 1 <= source["column"] <= table["ncols"]:
            raise ValueError("来源列越界")
        return {"row": "current", "column": source["column"]}
    keys(source, {"cell", "span"}, {"cell"}, "source")
    cell = source["cell"]
    if (
        not isinstance(cell, list)
        or len(cell) != 2
        or any(type(x) is not int for x in cell)
        or not 1 <= cell[0] <= table["nrows"]
        or not 1 <= cell[1] <= table["ncols"]
    ):
        raise ValueError("来源单元格越界")
    result = {"row": cell[0], "column": cell[1]}
    if "span" in source:
        span = source["span"]
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(x) is not int for x in span)
            or not 0 <= span[0] < span[1] <= 4096
        ):
            raise ValueError("原文片段范围无效")
        result["span"] = span
    return result


def _resolved(binding, row, cells):
    if binding is None:
        return None, None
    coord = (row if binding["row"] == "current" else binding["row"], binding["column"])
    cell = cells[coord]
    raw = cell["raw_value"]
    if "span" in binding:
        if not isinstance(raw, str) or binding["span"][1] > len(raw):
            raise ValueError("原文片段超出单元格文字范围")
        raw = raw[slice(*binding["span"])]
    if (
        cell.get("formula")
        or cell.get("value_type") in ("f", "e", "error")
        or isinstance(raw, str)
        and raw.lstrip().startswith("=")
    ):
        raise ValueError("来源是公式或错误值，当前入口不支持")
    return raw, cell


def _header_path(cells, rows, column):
    """Return a display path and exact source cells for one confirmed column.

    Merged-cell origin lookup is intentionally limited to declared header rows;
    data bindings never use this helper and therefore never inherit sample or
    result values from blank merged cells.
    """
    labels = []
    sources = []
    seen = set()
    for row in rows:
        cell = cells.get((row, column))
        if cell is None:
            continue
        address = cell.get("merged_origin") or cell["address"]
        letters, origin_row = coordinate_from_string(address)
        origin_column = column_index_from_string(letters)
        origin = cells.get((origin_row, origin_column))
        if origin is None:
            continue
        key = (origin_row, origin_column)
        if key in seen:
            continue
        seen.add(key)
        sources.append(key)
        raw = origin.get("raw_value")
        if raw is not None and str(raw).strip():
            labels.append(str(raw).strip())
    return {"label": " / ".join(labels), "sources": sources}


def inspect_definition(definition, assets, reader_options=None):
    from .table_reading import _selected_tables

    issues = []
    plan = _empty_plan()
    if not isinstance(definition, dict):
        raise ValueError("definition 必须为对象")
    keys(definition, DEFINITION_KEYS, path="definition")
    if definition.get("version", "1") != "1":
        raise ValueError("不支持的行式入口定义版本")
    if len(assets) != 1:
        raise ValueError("行式测试表入口每次只处理一个文件")
    asset = assets[0]
    needed = {
        "asset_id",
        "sheet",
        "header_row",
        "data_range",
        "sample_column",
        "metric_source",
        "result_column",
        "result_types",
        "unit_source",
        "ignored_columns",
    }
    missing = sorted((needed - set(definition)) | {k for k in needed - {"sheet"} if definition.get(k) is None})
    if missing:
        return (
            plan,
            {"ready": False, "issues": [_issue("definition_required", "请完成对应设置", field=k) for k in missing]},
            None,
        )
    if definition["asset_id"] != asset["asset_id"]:
        raise ValueError("读法来源与当前原件不一致")
    tables = _selected_tables(asset, (reader_options or {}).get(asset["asset_id"], {}))
    table = next((t for t in tables if t.get("sheet") == definition["sheet"]), None)
    if table is None:
        return plan, {"ready": False, "issues": [_issue("sheet_required", "请选择当前原件中的工作表")]}, None
    if not table.get("extraction_safe", True):
        return plan, {"ready": False, "issues": [_issue("reader_ambiguity", "原件读取仍有歧义，请确认读取方式")]}, None
    bounds = definition["data_range"]
    header = definition["header_row"]
    if not isinstance(bounds, list) or len(bounds) != 4 or any(type(x) is not int for x in bounds):
        raise ValueError("数据范围需要原表矩形坐标")
    r0, c0, r1, c1 = bounds
    if not 1 <= r0 <= r1 <= table["nrows"] or not 1 <= c0 <= c1 <= table["ncols"]:
        raise ValueError("数据范围越界")
    if r1 - r0 + 1 > 10000 or c1 - c0 + 1 > 50:
        raise ValueError("首版最多 10000 条记录、50 列，请调整范围")
    if type(header) is not int:
        raise ValueError("请选择数据之前的表头行")
    header_rows = definition.get("header_rows", [header])
    if (
        not isinstance(header_rows, list)
        or not 1 <= len(header_rows) <= 2
        or any(type(row) is not int for row in header_rows)
        or header_rows != list(range(header_rows[0], header_rows[-1] + 1))
    ):
        raise ValueError("表头需选择一行或两行连续原行")
    if header != header_rows[-1] or not 1 <= header_rows[0] <= header_rows[-1] < r0:
        raise ValueError("表头行必须连续、位于数据之前，末行需与当前表头行一致")
    cells = {(c["row"], c["column"]): c for row in table["rows"] for c in row["cells"]}
    selected_cols = set(range(c0, c1 + 1))
    used_cols = set()

    def column(name):
        col = definition.get(name)
        if type(col) is not int or col not in selected_cols:
            raise ValueError(name + " 必须在所选区域内")
        used_cols.add(col)
        return col

    sample = column("sample_column")
    result = column("result_column")
    if sample == result:
        raise ValueError("样品编号与测试结果不能使用同一列")
    metric = _source(definition["metric_source"], table)
    unit = _source(definition["unit_source"], table, allow_missing=True)
    for source in (metric, unit):
        if source and source["row"] != "current" and r0 <= source["row"] <= r1 and c0 <= source["column"] <= c1:
            issues.append(
                _issue(
                    "shared_source_in_data",
                    "共用指标或单位请选表头、标题等区域；数据中的逐行内容应选择对应列",
                    row=source["row"],
                    column=source["column"],
                )
            )
        if source and source["row"] == "current":
            if source["column"] not in selected_cols:
                raise ValueError("逐行来源列必须在选区内")
            used_cols.add(source["column"])
    method = column("method_column") if definition.get("method_column") is not None else None
    conditions = definition.get("condition_columns", [])
    ignored = definition["ignored_columns"]
    for name, cols in (("condition_columns", conditions), ("ignored_columns", ignored)):
        if (
            not isinstance(cols, list)
            or any(type(c) is not int or c not in selected_cols for c in cols)
            or len(set(cols)) != len(cols)
        ):
            raise ValueError(name + " 需要所选区域内不重复的列")
    role_columns = (
        [sample, result]
        + ([method] if method else [])
        + conditions
        + [b["column"] for b in (metric, unit) if b and b["row"] == "current"]
    )
    if len(role_columns) != len(set(role_columns)):
        raise ValueError("不同逐行含义不能重叠使用同一列")
    used_cols.update(conditions)
    condition_labels = [_header_path(cells, header_rows, c)["label"] for c in conditions]
    if any(not value for value in condition_labels) or len(set(condition_labels)) != len(condition_labels):
        issues.append(
            _issue(
                "condition_header_ambiguous",
                "条件列完整表头重复或为空，请选择含义明确的条件列",
                row=header,
                columns=conditions,
            )
        )
    if set(ignored) & used_cols:
        raise ValueError("已使用的列不能同时标记为不整理")
    if selected_cols - used_cols - set(ignored):
        issues.append(
            _issue(
                "unassigned_columns",
                "请为剩余列指定含义或确认本次不整理",
                columns=sorted(selected_cols - used_cols - set(ignored)),
            )
        )
    types = definition["result_types"]
    if (
        not isinstance(types, dict)
        or len(types) > 10000
        or any(not isinstance(k, str) or not k or v not in ("number", "category") for k, v in types.items())
    ):
        raise ValueError("每个指标需要指定数值或等级")
    for region in table.get("merged_cells", []):
        mc0, mr0, mc1, mr1 = range_boundaries(region)
        intersects_selection = max(mc0, c0) <= min(mc1, c1) and max(mr0, header_rows[0]) <= min(mr1, r1)
        contained_in_headers = c0 <= mc0 <= mc1 <= c1 and header_rows[0] <= mr0 <= mr1 <= header_rows[-1]
        if intersects_selection and not contained_in_headers:
            issues.append(
                _issue(
                    "merged_cells_unsupported",
                    "合并单元格必须完整位于已确认的表头内；数据区或跨边界的合并不支持",
                    range=region,
                )
            )
    metrics = {}
    unit_counts = {}
    missing_count = 0
    for row in range(r0, r1 + 1):
        coord = lambda c: f"{get_column_letter(c)}{row}"
        sample_cell = cells[(row, sample)]
        try:
            sample_raw, _ = _resolved({"row": "current", "column": sample}, row, cells)
        except ValueError as exc:
            issues.append(_issue("sample_unresolved", str(exc), row=row, column=sample))
            continue
        absence = parse_typed_value(sample_cell, expected="number")
        if sample_raw is None or str(sample_raw).strip() == "" or absence.get("kind") == "missing":
            issues.append(_issue("sample_unresolved", "此行缺少可确认的样品编号", row=row, column=sample))
        try:
            metric_raw, _ = _resolved(metric, row, cells)
            unit_raw, unit_cell = _resolved(unit, row, cells)
        except ValueError as exc:
            issues.append(_issue("source_unresolved", str(exc), row=row))
            continue
        if (
            metric_raw is None
            or not str(metric_raw).strip()
            or parse_typed_value(metric_raw, expected="number").get("kind") == "missing"
        ):
            issues.append(_issue("metric_unresolved", "此行缺少测试指标", row=row, column=metric["column"]))
            continue
        key = str(metric_raw)
        metrics[key] = metrics.get(key, 0) + 1
        expected = types.get(key)
        if expected is None:
            issues.append(_issue("result_type_required", "请确认该指标的结果是数值还是等级", row=row, metric=key))
            continue
        if unit_raw not in (None, ""):
            _normalized = normalize_unit(unit_raw)
            # Unit expressions are tokens, not header sentences. Exact source
            # spans let users select the unit inside "Strength [MPa]".
            if (
                "\n" in str(unit_raw)
                or "[" in str(unit_raw)
                or "]" in str(unit_raw)
                or re.search(r"[A-Za-z]\s+\(", str(unit_raw))
                or len(str(unit_raw)) > 80
            ):
                issues.append(
                    _issue(
                        "unit_source_invalid",
                        "请仅选取单位文字，不能把整段表头作为单位",
                        row=row,
                        column=unit["column"],
                    )
                )
            unit_counts[str(unit_raw)] = unit_counts.get(str(unit_raw), 0) + 1
        typed, inline, problem = simple_measurement(cells[(row, result)], expected, unit_raw)
        if problem:
            issues.append(
                _issue(
                    problem, "此结果超出首版支持范围或单位冲突，请核对原格", row=row, column=result, cell=coord(result)
                )
            )
        if typed.get("kind") == "missing":
            missing_count += 1
        for col in ([method] if method else []) + conditions:
            try:
                _resolved({"row": "current", "column": col}, row, cells)
            except ValueError as exc:
                issues.append(_issue("source_unresolved", str(exc), row=row, column=col))
    cur = lambda col: {"row": "current", "column": col}
    headers = []
    seen_header_sources = set()
    for c in sorted(used_cols):
        for row, source_column in _header_path(cells, header_rows, c)["sources"]:
            if (row, source_column) not in seen_header_sources:
                seen_header_sources.add((row, source_column))
                headers.append({"row": row, "column": source_column})
    qualifiers = {"property": metric, "metric_label": metric}
    if method:
        qualifiers["method"] = cur(method)
    for col, label in zip(conditions, condition_labels):
        qualifiers["condition:" + label] = cur(col)
    value_field = {
        "scope": "measurements",
        "target": "result",
        "field_id": "observation.value",
        "source": cur(result),
        "expected": "number",
        "expected_by": {"qualifier": "property", "types": deepcopy(types)},
        "measurement_mode": "simple",
        "qualifiers": qualifiers,
        "header_sources": headers,
    }
    if unit:
        value_field["unit"] = unit
    plan = {
        "version": "2",
        "views": [{"id": "table", "asset_id": asset["asset_id"], "sheet": definition["sheet"]}],
        "scopes": [{"id": "measurements", "view": "table", "kind": "row", "rows": [r0, r1], "columns": [c0, c1]}],
        "objects": {
            "sample": {"kind": "sample", "scope": "measurements", "key": [cur(sample)], "identity_scope": "scope"},
            "test": {"kind": "reported_test", "scope": "measurements", "key": [cur(sample), metric]},
            "result": {"kind": "observation", "scope": "measurements", "key": [cur(sample), metric]},
        },
        "fields": [
            {
                "scope": "measurements",
                "target": "sample",
                "field_id": "material.identity",
                "source": cur(sample),
                "expected": "text",
            },
            value_field,
        ],
        "relations": [
            {"scope": "measurements", "from": "test", "to": "sample", "predicate": "tests_subject"},
            {"scope": "measurements", "from": "test", "to": "result", "predicate": "produces_observation"},
        ],
    }
    # Inventory does not claim unexplained cells outside this confirmed scope.
    outside = 0
    unselected = []
    handled_fixed = {(b["row"], b["column"]) for b in (metric, unit) if b and b["row"] != "current"}
    for t in tables:
        if t.get("sheet") != definition["sheet"]:
            if any(c["raw_value"] not in (None, "") for row in t["rows"] for c in row["cells"]):
                unselected.append(t.get("sheet"))
            continue
        outside = sum(
            c["raw_value"] not in (None, "")
            and not (r0 <= c["row"] <= r1 and c0 <= c["column"] <= c1)
            and not (header_rows[0] <= c["row"] <= header_rows[-1] and c0 <= c["column"] <= c1)
            and (c["row"], c["column"]) not in handled_fixed
            for row in t["rows"]
            for c in row["cells"]
        )
    ignored_nonempty = sum(
        cells[(row, col)]["raw_value"] not in (None, "") for row in range(r0, r1 + 1) for col in ignored
    )
    completion = {
        "selected_scope_complete": not issues,
        "whole_file_complete": not issues and not outside and not unselected and not ignored_nonempty,
        "selected_records": r1 - r0 + 1,
        "source_missing_results": missing_count,
        "unselected_sheets": unselected,
        "outside_nonempty_cells": outside,
        "ignored_columns": ignored,
        "ignored_nonempty_cells": ignored_nonempty,
    }
    return (
        plan,
        {
            "ready": not issues,
            "issues": issues[:100],
            "issue_total": len(issues),
            "metrics": [{"name": k, "records": v, "result_type": types.get(k)} for k, v in metrics.items()],
        },
        completion,
    )
