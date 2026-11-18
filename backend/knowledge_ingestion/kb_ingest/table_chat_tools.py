"""Read-only, bounded table tools; program artifacts retain the complete result."""

from __future__ import annotations

import copy
import json

from .ingest.tables.raw_table_analysis import (
    _snapshot,
    analyze_table,
    normalize_analysis_config,
    recommend_analysis,
)

MAX_TOOL_OUTPUT_BYTES = 48_000
MAX_COLUMNS = 32
MAX_ROWS = 50


class ToolInputError(ValueError):
    """A safe, correctable tool argument or resource error."""


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


_COLUMN = {"type": "string", "pattern": "^[A-Z]+$", "maxLength": 8}
_TABLE = {"type": "string", "minLength": 1, "maxLength": 128}
_COLUMNS = {"anyOf": [_array(_COLUMN, MAX_COLUMNS, 1), {"type": "null"}]}
_CONFIG = _object(
    {
        "y_columns": _array(_COLUMN, 32, 1),
        "x_column": {"anyOf": [_COLUMN, {"type": "null"}]},
        "group_columns": _array(_COLUMN, 3),
        "units": _array(
            _object({"column_id": _COLUMN, "unit": {"type": "string", "minLength": 1, "maxLength": 128}}), 36
        ),
        "header_row_count": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
        "line_plot": {"type": "boolean"},
        "number_format": {"type": "string", "enum": ["source", "decimal_dot", "decimal_comma"]},
    }
)
_ANALYSIS = _object({"table_version_id": _TABLE, "config": _CONFIG})
TOOL_SCHEMAS = [
    {"type": "function", "name": name, "description": description, "parameters": parameters, "strict": True}
    for name, description, parameters in [
        (
            "inspect_table",
            "查看已选择表的原坐标、表头、最多12行样例和程序推荐。样例不是全表；未注明单位不得猜测。columns为null时最多前32列，覆盖范围会明确返回。",
            _object({"table_version_id": _TABLE, "columns": _COLUMNS}),
        ),
        (
            "read_table_rows",
            "按原始源行号分页读取已选择表，最多50行32列；保留原格类型、0、空白与缺格。columns为null时最多前32列。",
            _object(
                {
                    "table_version_id": _TABLE,
                    "start_row": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "columns": _COLUMNS,
                }
            ),
        ),
        (
            "analyze_table",
            "在完整已保存快照上进行描述统计及基础绘图，不是对样例计算。指定原列、表头行数；units数组仅标注已知单位，不转换。完整图和原格坐标保存为结果，返回精简摘要。",
            _ANALYSIS,
        ),
        (
            "compare_tables",
            "对2至10张已选择表分别计算，按各自单位并排比较；不联表、不自动对齐行、不换算单位。",
            _object({"tables": _array(_ANALYSIS, 10, 2)}),
        ),
    ]
]


def _bounded(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ToolInputError("工具结果包含不支持的数据类型") from None
    if len(encoded) > MAX_TOOL_OUTPUT_BYTES:
        raise ToolInputError("工具返回超过上下文大小上限，请减少读取行列；原始值未截断")
    return value


def _fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ToolInputError("工具参数字段缺失、未知或格式不正确")


def _compact(value):
    """Explicit omissions apply only to summaries, never saved results or raw cells."""
    if isinstance(value, str) and len(value) > 256:
        return {"omitted": True, "reason": "内容过长，完整值保存在程序结果中", "characters": len(value)}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items()}
    return value


def _operation(value):
    return _compact(
        {
            k: v
            for k, v in value.items()
            if k not in {"points", "segments", "excluded", "stored_formula_source_rows", "bins", "groups"}
        }
    )


def _summary(result):
    columns = []
    for column in result["columns"]:
        item = {
            key: _compact(column[key]) for key in ("column_id", "label", "unit", "counts", "statistics", "warnings")
        }
        for key in ("histogram", "scatter", "line", "group_statistics"):
            item[key] = _operation(column[key])
        bins = column["histogram"].get("bins", [])
        item["histogram"]["bin_count"] = len(bins)
        groups = column["group_statistics"].get("groups", [])
        item["group_statistics"].update(
            {
                "groups": [_compact({"key": g["key"], "statistics": g["statistics"]}) for g in groups[:8]],
                "group_count": len(groups),
                "omitted_groups": max(0, len(groups) - 8),
            }
        )
        columns.append(item)
    return {
        "ok": True,
        "table_version_id": result["table_version_id"],
        "engine_version": result["engine_version"],
        "config": result["config"],
        "overview": result["overview"],
        "algorithms": result["algorithms"],
        "columns": columns,
        "coverage": {
            "full_snapshot": True,
            "data_row_count": result["overview"]["data_row_count"],
            "summary_only": True,
            "full_result_in_artifact": True,
        },
    }


class TableChatTools:
    def __init__(self, snapshots):
        if not isinstance(snapshots, dict) or not 1 <= len(snapshots) <= 10:
            raise ToolInputError("请先选择1至10张已保存原表")
        for identifier, snapshot in snapshots.items():
            if (
                not isinstance(identifier, str)
                or not identifier
                or not isinstance(snapshot, dict)
                or snapshot.get("table_version_id") != identifier
            ):
                raise ToolInputError("原表版本标识不一致")
            try:
                _snapshot(snapshot)
            except (ValueError, TypeError):
                raise ToolInputError("已保存原表快照不完整或超过程序处理上限") from None
        self.snapshots = snapshots

    def _table(self, identifier):
        if not isinstance(identifier, str) or identifier not in self.snapshots:
            raise ToolInputError("该表不在本轮已选择的原表中，请先选择原表")
        return self.snapshots[identifier]

    def _columns(self, table, requested):
        columns = table["columns"]
        ids = [c["column_id"] for c in columns]
        if requested is None:
            return columns[:MAX_COLUMNS]
        if (
            not isinstance(requested, list)
            or not 1 <= len(requested) <= MAX_COLUMNS
            or any(not isinstance(c, str) or c not in ids for c in requested)
            or len(set(requested)) != len(requested)
        ):
            raise ToolInputError("读取列不存在、重复或超过32列上限")
        selected = set(requested)
        return [c for c in columns if c["column_id"] in selected]

    def _rows(self, rows, columns):
        numbers = {c["column"] for c in columns}
        return [
            {"row": row["row"], "cells": [copy.deepcopy(cell) for cell in row["cells"] if cell["column"] in numbers]}
            for row in rows
        ]

    def _read(self, name, args):
        allowed = {"table_version_id", "columns"} | ({"start_row", "limit"} if name == "read_table_rows" else set())
        _fields(args, allowed, {"table_version_id"} | ({"start_row", "limit"} if name == "read_table_rows" else set()))
        table = self._table(args["table_version_id"])
        columns = self._columns(table, args.get("columns"))
        rows = table["rows"]
        first = rows[0]["row"]
        last = rows[-1]["row"]
        if name == "read_table_rows":
            start, limit = args["start_row"], args["limit"]
            if (
                type(start) is not int
                or not first <= start <= last
                or type(limit) is not int
                or not 1 <= limit <= MAX_ROWS
            ):
                raise ToolInputError("起始行须为表内原行号，每次读取1至50行")
            selected = rows[start - first : start - first + limit]
            next_row = selected[-1]["row"] + 1 if selected[-1]["row"] < last else None
        else:
            header = table.get("default_header_row_count", 0)
            # Include the header prefix, early data and the tail. The exact rows
            # and omitted counts are explicit; computation never uses this sample.
            indices = sorted(
                set(
                    [
                        *range(min(header, 6)),
                        *range(header, min(header + 3, len(rows))),
                        *range(max(0, len(rows) - 3), len(rows)),
                    ]
                )
            )
            selected = [rows[i] for i in indices]
            next_row = None
        output = {
            "ok": True,
            "table_version_id": args["table_version_id"],
            "table_range": table["table_range"],
            "original_name": table.get("original_name"),
            "sheet": table.get("sheet"),
            "columns": copy.deepcopy(columns),
            "rows": self._rows(selected, columns),
            "coverage": {
                "total_rows": len(rows),
                "returned_rows": len(selected),
                "source_first_row": first,
                "source_last_row": last,
                "sampled": len(selected) != len(rows),
                "full_snapshot": len(selected) == len(rows) and len(columns) == len(table["columns"]),
                "total_columns": len(table["columns"]),
                "omitted_columns": len(table["columns"]) - len(columns),
                "next_row": next_row,
            },
        }
        if name == "inspect_table":
            recommendation = recommend_analysis(table)
            # Public tool config uses a list; raw analysis keeps its internal map.
            unit_map = recommendation["config"].get("units", {})
            recommendation["config"]["units"] = [{"column_id": key, "unit": value} for key, value in unit_map.items()]
            chosen = {c["column_id"] for c in columns}
            recommendation["columns"] = [c for c in recommendation.get("columns", []) if c["column_id"] in chosen]
            output["recommendations"] = recommendation
            output["recommendations_coverage"] = {
                "counts_use_full_snapshot": True,
                "column_profiles_limited_to_returned_columns": True,
            }
            output["default_header_row_count"] = table.get("default_header_row_count", 0)
        return {"output": _bounded(output)}

    def _analysis_args(self, args):
        _fields(args, {"table_version_id", "config"}, {"table_version_id", "config"})
        table = self._table(args["table_version_id"])
        config = args["config"]
        if not isinstance(config, dict):
            raise ToolInputError("分析设置必须是对象")
        units = config.get("units", [])
        if not isinstance(units, list) or len(units) > 36:
            raise ToolInputError("单位需按原列提供列表，未注明请留空")
        adapted = {}
        for item in units:
            _fields(item, {"column_id", "unit"}, {"column_id", "unit"})
            key = item["column_id"]
            if not isinstance(key, str) or key in adapted:
                raise ToolInputError("单位列标识无效或重复")
            adapted[key] = item["unit"]
        try:
            normalized = normalize_analysis_config(table, {**config, "units": adapted})
        except ValueError as error:
            # This pure validator emits fixed diagnostic strings, never source values.
            raise ToolInputError(str(error)) from None
        return table, normalized

    def run(self, name, arguments):
        if name in ("inspect_table", "read_table_rows"):
            return self._read(name, arguments)
        if name == "analyze_table":
            table, config = self._analysis_args(arguments)
            result = analyze_table(table, config)
            return {
                "output": _bounded(_summary(result)),
                "artifact": {
                    "kind": "table_analysis",
                    "table_version_id": table["table_version_id"],
                    "config": result["config"],
                    "result": result,
                },
            }
        if name == "compare_tables":
            _fields(arguments, {"tables"}, {"tables"})
            tables = arguments["tables"]
            if not isinstance(tables, list) or not 2 <= len(tables) <= 10:
                raise ToolInputError("并排比较需要2至10张原表")
            validated = [self._analysis_args(item) for item in tables]
            if len({table["table_version_id"] for table, _ in validated}) != len(validated):
                raise ToolInputError("比较表版本不能重复")
            results = [analyze_table(table, config) for table, config in validated]
            output = {
                "ok": True,
                "comparison_mode": "side_by_side",
                "joined": False,
                "unit_conversion": False,
                "tables": [_summary(result) for result in results],
            }
            artifact = {
                "kind": "table_comparison",
                "comparison_mode": "side_by_side",
                "joined": False,
                "unit_conversion": False,
                "tables": [
                    {"table_version_id": result["table_version_id"], "config": result["config"], "result": result}
                    for result in results
                ],
            }
            return {"output": _bounded(output), "artifact": artifact}
        raise ToolInputError("不支持该工具；只能检查、读取、分析或并排比较已选择的表")
