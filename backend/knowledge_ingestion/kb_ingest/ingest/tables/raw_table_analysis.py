"""Pure, bounded descriptive analysis of immutable original-table snapshots.

No model, I/O, interpolation, unit conversion or formula calculation occurs here.
All row references are absolute source coordinates. Statistics use Decimal;
plots are a separately checked finite binary64 projection with exact provenance.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from decimal import Decimal, DecimalException, localcontext

from kb_ingest.typed_values import parse_typed_value

ENGINE_VERSION = "basic-table-analysis-v1"
MAX_CELLS = 1_000_000
MAX_ROWS = MAX_CELLS
MAX_Y_COLUMNS = 32
MAX_GROUP_COLUMNS = 3
MAX_GROUPS = 100
MAX_PLOT_POINTS = 20_000
MAX_NUMBER_DIGITS = 4096
MAX_ADJUSTED_EXPONENT = 10_000
_FORMATS = {"source", "decimal_dot", "decimal_comma"}
_CONFIG_KEYS = {"y_columns", "x_column", "group_columns", "units", "header_row_count", "line_plot", "number_format"}
_COUNT_KEYS = ("valid", "missing", "absent", "unparsed", "non_finite", "non_scalar", "formula", "out_of_range")


class AnalysisResourceLimit(ValueError):
    """An explicit analysis limit; raw snapshot persistence is independent."""


def _column_name(number):
    name = ""
    while number:
        number, digit = divmod(number - 1, 26)
        name = chr(65 + digit) + name
    return name


def _column_number(name):
    value = 0
    for char in name:
        value = value * 26 + ord(char) - 64
    return value


def _snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("原表快照必须是对象")
    match = re.fullmatch(r"([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)", str(snapshot.get("table_range", "")))
    if not match:
        raise ValueError("原表范围无效")
    c1, r1, c2, r2 = _column_number(match[1]), int(match[2]), _column_number(match[3]), int(match[4])
    if r2 < r1 or c2 < c1:
        raise ValueError("原表范围起止无效")
    if r2 - r1 + 1 > MAX_ROWS or (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_CELLS:
        raise AnalysisResourceLimit("原表超过一百万格的程序分析上限；完整原表仍保留")
    columns, rows = snapshot.get("columns"), snapshot.get("rows")
    if (
        not isinstance(columns, list)
        or len(columns) != c2 - c1 + 1
        or not isinstance(rows, list)
        or len(rows) != r2 - r1 + 1
    ):
        raise ValueError("原表快照须包含完整范围内的全部行列")
    for number, column in enumerate(columns, c1):
        if (
            not isinstance(column, dict)
            or type(column.get("column")) is not int
            or column["column"] != number
            or column.get("column_id") != _column_name(number)
        ):
            raise ValueError("原列标识或坐标不一致")
        if column.get("label") is not None and not isinstance(column["label"], str):
            raise ValueError("原列名称必须是文字")
    for number, row in enumerate(rows, r1):
        if (
            not isinstance(row, dict)
            or type(row.get("row")) is not int
            or row["row"] != number
            or not isinstance(row.get("cells"), list)
        ):
            raise ValueError("原行缺失、重复或顺序不一致")
        seen = set()
        for cell in row["cells"]:
            if (
                not isinstance(cell, dict)
                or type(cell.get("column")) is not int
                or not c1 <= cell["column"] <= c2
                or type(cell.get("row")) is not int
                or cell["row"] != number
                or "raw_value" not in cell
            ):
                raise ValueError("原格坐标或原值不完整")
            if cell["column"] in seen or cell.get("address") != _column_name(cell["column"]) + str(number):
                raise ValueError("原格重复或地址不一致")
            seen.add(cell["column"])
    return columns, rows


def normalize_analysis_config(snapshot, config):
    """Validate explicit roles and return a new, complete JSON configuration."""
    columns, rows = _snapshot(snapshot)
    if not isinstance(config, dict) or set(config) - _CONFIG_KEYS:
        raise ValueError("分析设置包含未知字段或不是对象")
    available = {c["column_id"] for c in columns}
    normalized = {}
    for key, limit in [("y_columns", MAX_Y_COLUMNS), ("group_columns", MAX_GROUP_COLUMNS)]:
        value = config.get(key, [])
        if (
            not isinstance(value, list)
            or len(value) > limit
            or any(not isinstance(c, str) or c not in available for c in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("分析列/分组列无效、重复或超过数量上限")
        normalized[key] = list(value)
    if not normalized["y_columns"]:
        raise ValueError("请至少选择一列进行数值分析")
    x = config.get("x_column")
    if x is not None and (not isinstance(x, str) or x not in available):
        raise ValueError("横轴列不存在")
    roles = normalized["y_columns"] + normalized["group_columns"] + ([] if x is None else [x])
    if len(set(roles)) != len(roles):
        raise ValueError("横轴、分析列与分组列不能重复承担不同职责")
    normalized["x_column"] = x
    header = config.get("header_row_count", snapshot.get("default_header_row_count", 0))
    if type(header) is not int or not 0 <= header <= len(rows):
        raise ValueError("表头行数必须是原表范围内的非负整数")
    normalized["header_row_count"] = header
    line = config.get("line_plot", False)
    if type(line) is not bool:
        raise ValueError("按源顺序连线必须是明确的布尔选择")
    normalized["line_plot"] = line
    number_format = config.get("number_format", "source")
    if not isinstance(number_format, str) or number_format not in _FORMATS:
        raise ValueError("数值格式不支持")
    normalized["number_format"] = number_format
    units = config.get("units", {})
    if not isinstance(units, dict) or len(units) > len(available):
        raise ValueError("单位设置必须按原列标识提供")
    for key, value in units.items():
        if (
            key not in available
            or not isinstance(value, str)
            or len(value) > 128
            or not value.strip()
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("单位必须是已存在原列的非空短文字；未注明请省略")
    normalized["units"] = {key: value for key, value in sorted(units.items())}
    return normalized


def _source(snapshot, row, column):
    return {
        "asset_id": snapshot.get("asset_id"),
        "sheet": snapshot.get("sheet"),
        "row": row,
        "column": column,
        "address": _column_name(column) + str(row),
    }


def _number(cell, number_format):
    if cell is None:
        return None, "absent", "absent", "absent"
    raw = cell["raw_value"]
    if isinstance(raw, str) and len(raw.strip()) > MAX_NUMBER_DIGITS:
        return None, "out_of_range", "unparsed", "numeric_text_too_long"
    typed = parse_typed_value(cell, expected="number", number_format=number_format)
    kind, status = typed["kind"], typed["status"]
    if kind == "formula_cached":
        return None, "formula", kind, status
    if kind == "missing":
        return None, "non_finite" if status in ("nan", "non_finite") else "missing", kind, status
    if isinstance(raw, bool) or kind not in ("decimal", "unparsed"):
        return None, "non_scalar", kind, status
    if kind != "decimal" or status != "valid":
        return None, "unparsed", kind, status
    try:
        value = Decimal(typed["decimal"])
        if not value.is_finite():
            return None, "non_finite", kind, status
        if len(value.as_tuple().digits) > MAX_NUMBER_DIGITS or (
            value and abs(value.adjusted()) > MAX_ADJUSTED_EXPONENT
        ):
            return None, "out_of_range", kind, "decimal_resource_limit"
        return value, "valid", kind, status
    except (DecimalException, ValueError, OverflowError):
        return None, "out_of_range", kind, "decimal_resource_limit"


def _decimal_text(value):
    if value.is_zero():
        return "0"
    text = str(value)
    if "E" in text:
        mantissa, exponent = text.split("E")
        return (mantissa.rstrip("0").rstrip(".") if "." in mantissa else mantissa) + "E" + exponent
    if "." in text:
        return text.rstrip("0").rstrip(".")
    return text


def _skipped(code, reason, **extra):
    return {"status": "skipped", "code": code, "reason": reason, **extra}


def _precision(values):
    # Coefficient length alone loses small terms in e.g. 1e100 + 1 - 1e100.
    # Cover the full occupied exponent span before adding division/sqrt guards.
    nonzero = [value for value in values if value]
    required = (
        (
            max(value.adjusted() for value in nonzero) - min(value.as_tuple().exponent for value in nonzero) + 1
            if nonzero
            else 1
        )
        + len(str(len(values)))
        + 20
    )
    if required > MAX_NUMBER_DIGITS:
        raise ValueError("Numerical exponent span exceeds bounded precision")
    return max(50, required)


def _analysis_columns(columns, rows, header_count):
    labels = {column["column"]: [] for column in columns}
    lengths, truncated = Counter(), set()
    for row in rows[:header_count]:
        for cell in row["cells"]:
            number, raw = cell["column"], cell["raw_value"]
            if number in truncated or raw is None or not str(raw).strip():
                continue
            text = str(raw).strip()
            room = 512 - lengths[number]
            if len(text) > room:
                text = text[: max(0, room)] + "…"
                truncated.add(number)
            labels[number].append(text)
            lengths[number] += len(text) + 3
    return [
        {
            **column,
            "label": " / ".join(labels[column["column"]]) or column["column_id"],
            "label_truncated": column["column"] in truncated,
        }
        for column in columns
    ]


def _statistics(values):
    n = len(values)
    if not n:
        return _skipped("no_numeric_values", "没有可用于统计的有限单一数值", n=0)
    ordered = sorted(values)
    try:
        with localcontext() as ctx:
            ctx.prec = _precision(values)

            def quantile(p):
                position = Decimal(n - 1) * p
                index = int(position)
                return (
                    ordered[index]
                    if index == n - 1
                    else ordered[index] + (ordered[index + 1] - ordered[index]) * (position - index)
                )

            mean = sum(values, Decimal(0)) / n
            standard = (sum(((value - mean) ** 2 for value in values), Decimal(0)) / (n - 1)).sqrt() if n > 1 else None
            return {
                "status": "ok",
                "n": n,
                "min": _decimal_text(ordered[0]),
                "max": _decimal_text(ordered[-1]),
                "mean": _decimal_text(mean),
                "median": _decimal_text(quantile(Decimal(".5"))),
                "sample_std": _decimal_text(standard) if standard is not None else None,
                "sample_std_status": "ok" if standard is not None else "skipped",
                "sample_std_reason": None if standard is not None else "样本标准差至少需要两个有效数值",
                "q1": _decimal_text(quantile(Decimal(".25"))),
                "q3": _decimal_text(quantile(Decimal(".75"))),
                "decimal_precision": ctx.prec,
            }
    except (DecimalException, ValueError, OverflowError):
        return _skipped("numeric_resource_limit", "数值范围超过当前统计的安全计算能力", n=n)


def _histogram(values):
    if len(values) < 2:
        return _skipped("insufficient_numeric_values", "分布图至少需要两个有效数值", n=len(values))
    lower, upper = min(values), max(values)
    if lower == upper:
        return _skipped("constant_values", "全部有效数值相同，不划分直方图区间", n=len(values))
    try:
        with localcontext() as ctx:
            ctx.prec = _precision(values)
            count = min(50, math.ceil(math.log2(len(values))) + 1)
            width = (upper - lower) / count
            frequencies = [0] * count
            for value in values:
                frequencies[min(count - 1, max(0, int((value - lower) / width)))] += 1
            bins = [
                {
                    "lower": _decimal_text(lower + width * index),
                    "upper": _decimal_text(upper if index == count - 1 else lower + width * (index + 1)),
                    "count": frequency,
                    "right_inclusive": index == count - 1,
                }
                for index, frequency in enumerate(frequencies)
            ]
            return {"status": "ok", "n": len(values), "bins": bins}
    except (DecimalException, ValueError, OverflowError):
        return _skipped("numeric_resource_limit", "数值范围超过当前分布图的安全计算能力", n=len(values))


def _group_key(cells, columns):
    result = []
    for column in columns:
        cell = cells.get(column["column"])
        if cell is None:
            return None, "absent_group_value"
        raw = cell["raw_value"]
        if (
            cell.get("formula") is not None
            or cell.get("value_type") in ("f", "formula", "array_formula", "e", "error")
            or isinstance(raw, str)
            and raw.lstrip().startswith("=")
        ):
            return None, "invalid_group_value"
        if raw is None or isinstance(raw, str) and not raw.strip():
            return None, "missing_group_value"
        if type(raw) is bool:
            kind = "boolean"
        elif type(raw) in (int, float):
            if isinstance(raw, float) and not math.isfinite(raw):
                return None, "non_finite_group_value"
            kind = "number"
        elif isinstance(raw, str) and len(raw) <= 1024:
            kind = "string"
        else:
            return None, "non_discrete_group_value"
        result.append({"column_id": column["column_id"], "type": kind, "value": raw})
    return result, None


def _plot_number(value):
    try:
        projected = float(value)
        return projected if math.isfinite(projected) and (projected != 0 or value == 0) else None
    except (ValueError, OverflowError):
        return None


def _analyze_column(snapshot, column, records, config, by_id):
    number = column["column"]
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    numeric, excluded, stored = [], [], []
    values_by_row = {}
    for row, cells in records:
        cell = cells.get(number)
        value, code, kind, status = _number(cell, config["number_format"])
        counts[code] += 1
        if value is not None:
            numeric.append(value)
            values_by_row[row] = value
            if cell.get("source_formula"):
                stored.append(row)
        else:
            excluded.append(
                {
                    "source_row": row,
                    "source": _source(snapshot, row, number),
                    "code": code,
                    "kind": kind,
                    "status": status,
                }
            )
    result = {
        "column_id": column["column_id"],
        "column": number,
        "label": column.get("label") or column["column_id"],
        "label_truncated": column.get("label_truncated", False),
        "unit": config["units"].get(column["column_id"]),
        "counts": {"total": len(records), **counts},
        "valid_source_rows": list(values_by_row),
        "excluded": excluded,
        "stored_formula_source_rows": stored,
        "warnings": [
            {
                "code": "stored_formula_cache_unverified",
                "reason": "使用文件已有公式缓存值；程序没有重新计算，缓存新鲜度未验证",
                "count": len(stored),
            }
        ]
        if stored
        else [],
        "statistics": _statistics(numeric),
        "histogram": _histogram(numeric),
    }
    grouping = [by_id[key] for key in config["group_columns"]]
    group_values, group_keys, group_excluded, distinct_groups = {}, {}, [], set()
    too_many_groups = False
    for row, cells in records:
        if not grouping:
            break
        key, issue = _group_key(cells, grouping)
        if issue:
            group_excluded.append({"source_row": row, "code": issue})
            continue
        encoded = json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        distinct_groups.add(encoded)
        if len(distinct_groups) > MAX_GROUPS:
            too_many_groups = True
            group_values.clear()
            group_keys.clear()
            group_excluded.clear()
            break
        group_keys[row] = key
        if row in values_by_row:
            group_values.setdefault(encoded, {"key": key, "values": [], "source_rows": []})
            group_values[encoded]["values"].append(values_by_row[row])
            group_values[encoded]["source_rows"].append(row)
    if not grouping:
        grouped = _skipped("group_not_selected", "未选择分组列")
    elif too_many_groups:
        grouped = _skipped(
            "too_many_groups",
            f"分组超过 {MAX_GROUPS} 组，请改选离散分组列；未截断结果",
            group_count_at_least=MAX_GROUPS + 1,
        )
    elif not group_values:
        grouped = _skipped("no_grouped_numeric_values", "没有同时具备有效分组与数值的原行")
    else:
        grouped = {
            "status": "ok",
            "groups": [
                {"key": group["key"], "source_rows": group["source_rows"], "statistics": _statistics(group["values"])}
                for group in group_values.values()
            ],
        }
    grouped["excluded"] = group_excluded
    grouped["n"] = None if too_many_groups else sum(len(group["values"]) for group in group_values.values())
    grouped["excluded_count"] = None if too_many_groups else len(records) - grouped["n"] if grouping else 0
    result["group_statistics"] = grouped
    x_column = by_id.get(config["x_column"])
    if x_column is None:
        result["scatter"] = _skipped("x_not_selected", "未选择横轴，未生成散点图", n=0)
        result["line"] = (
            _skipped("x_not_selected", "未选择横轴，未生成折线图", n=0)
            if config["line_plot"]
            else _skipped("line_not_requested", "未明确选择按源行顺序连线", n=0)
        )
        return result
    if too_many_groups:
        result["scatter"] = _skipped("too_many_groups", f"分组超过 {MAX_GROUPS} 组，未绘制分组散点图", n=None)
        result["line"] = (
            _skipped("too_many_groups", f"分组超过 {MAX_GROUPS} 组，未绘制分组折线图", n=None)
            if config["line_plot"]
            else _skipped("line_not_requested", "未明确选择按源行顺序连线", n=None)
        )
        return result
    points, pair_excluded, segments = [], [], []
    point_count = 0
    x_stored = []
    projections = ({}, {})
    precision_loss = False
    previous_row = None
    previous_key = None
    for row, cells in records:
        y = values_by_row.get(row)
        x, xcode = _number(cells.get(x_column["column"]), config["number_format"])[:2]
        code = None
        if y is None:
            code = "invalid_y"
        elif x is None:
            code = "invalid_x:" + xcode
        elif grouping and row not in group_keys:
            code = "invalid_group"
        xf, yf = (_plot_number(x), _plot_number(y)) if code is None else (None, None)
        if code is None and (xf is None or yf is None):
            code = "plot_numeric_range"
        if code:
            pair_excluded.append({"source_row": row, "code": code})
            previous_row = None
            previous_key = None
            continue
        point_count += 1
        if cells.get(x_column["column"], {}).get("source_formula"):
            x_stored.append(row)
        if point_count > MAX_PLOT_POINTS:
            points.clear()
            segments.clear()
            continue
        for seen, coordinate, exact in zip(projections, (xf, yf), (x, y)):
            if coordinate in seen and seen[coordinate] != exact:
                precision_loss = True
            seen[coordinate] = exact
        key = group_keys.get(row, [])
        point = {
            "x": xf,
            "y": yf,
            "source_row": row,
            "x_source": _source(snapshot, row, x_column["column"]),
            "y_source": _source(snapshot, row, number),
        }
        if grouping:
            point["group_key"] = key
        points.append(point)
        identity = json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if previous_row is None or row != previous_row + 1 or identity != previous_key:
            segments.append({"group_key": key, "points": []})
        segments[-1]["points"].append(point)
        previous_row = row
        previous_key = identity
    common = {
        "n": point_count,
        "excluded_count": len(pair_excluded),
        "excluded": pair_excluded,
        "x_label": x_column.get("label") or x_column["column_id"],
        "stored_formula_source_rows": {"x": x_stored, "y": stored},
        "warnings": [
            {
                "code": "stored_formula_cache_unverified",
                "reason": "坐标包含文件已有公式缓存；程序没有重新计算，缓存新鲜度未验证",
            }
        ]
        if x_stored or stored
        else [],
        "x_unit": config["units"].get(config["x_column"]),
        "y_label": result["label"],
        "y_unit": result["unit"],
        "projection": "finite_binary64; original decimals remain in source snapshot",
    }
    span_overflow = bool(points) and any(
        not math.isfinite(max(point[axis] for point in points) - min(point[axis] for point in points))
        for axis in ("x", "y")
    )
    if point_count > MAX_PLOT_POINTS:
        scatter = _skipped(
            "plot_resource_limit", f"有效点超过 {MAX_PLOT_POINTS}，未抽样或截断，请缩小原表范围", **common
        )
    elif span_overflow:
        scatter = _skipped("plot_numeric_range", "坐标跨度超过有限绘图范围；统计与原值仍保留", **common)
    elif precision_loss:
        scatter = _skipped(
            "plot_precision_loss", "不同原始数值投影到同一绘图坐标，已跳过以避免虚假重合；统计与原值仍保留", **common
        )
    elif point_count < 2:
        scatter = _skipped("insufficient_pairs", "散点图至少需要两个有效的横轴/结果配对", **common)
    else:
        scatter = {"status": "ok", "points": points, **common}
    if not config["line_plot"]:
        line = _skipped("line_not_requested", "未明确选择按源行顺序连线", **common)
    elif too_many_groups:
        line = _skipped("too_many_groups", f"分组超过 {MAX_GROUPS} 组，未绘制分组折线", **common)
    elif point_count > MAX_PLOT_POINTS:
        line = _skipped("plot_resource_limit", f"有效点超过 {MAX_PLOT_POINTS}，未抽样或截断，请缩小原表范围", **common)
    elif span_overflow:
        line = _skipped("plot_numeric_range", "坐标跨度超过有限绘图范围；统计与原值仍保留", **common)
    elif precision_loss:
        line = _skipped(
            "plot_precision_loss", "不同原始数值投影到同一绘图坐标，已跳过以避免虚假平线；统计与原值仍保留", **common
        )
    elif not any(len(segment["points"]) > 1 for segment in segments):
        line = _skipped(
            "no_connected_pairs", "没有同组且原行相邻的两个有效点，未跨缺口连线", segments=segments, **common
        )
    else:
        line = {"status": "ok", "segments": segments, **common}
    result["scatter"], result["line"] = scatter, line
    return result


def analyze_table(snapshot, config):
    """Analyze all rows below the explicitly declared contiguous header prefix."""
    normalized = normalize_analysis_config(snapshot, config)
    rows = snapshot["rows"]
    columns = _analysis_columns(snapshot["columns"], rows, normalized["header_row_count"])
    records = [
        (row["row"], {cell["column"]: cell for cell in row["cells"]}) for row in rows[normalized["header_row_count"] :]
    ]
    by_id = {column["column_id"]: column for column in columns}
    return {
        "engine_version": ENGINE_VERSION,
        "table_version_id": snapshot.get("table_version_id"),
        "config": normalized,
        "algorithms": {
            "quartiles": "inclusive_linear_interpolation_(n-1)*p",
            "standard_deviation": "sample_n_minus_1",
            "histogram": "equal_width_sturges_ceil(log2(n))+1_max50",
            "line": "source_order; break_on_invalid_or_nonadjacent_row_or_group_change",
            "statistics_representation": "decimal_strings; exponent_span_precision_min50_max4096",
            "unit_conversion": False,
            "formula_value_policy": "use_saved_cache_only_when_projected_by_reader; no_evaluation",
        },
        "overview": {
            "source_row_count": len(rows),
            "header_row_count": normalized["header_row_count"],
            "data_row_count": len(records),
            "column_count": len(columns),
            "stored_formula_cell_count": sum(
                bool(cell.get("source_formula")) for _, cells in records for cell in cells.values()
            ),
            "table_range": snapshot["table_range"],
            "source_order_preserved": True,
        },
        "columns": [
            _analyze_column(snapshot, by_id[key], records, normalized, by_id) for key in normalized["y_columns"]
        ],
    }


def recommend_analysis(snapshot):
    """Offer source-count suggestions only; this does not confirm or execute work."""
    try:
        columns, rows = _snapshot(snapshot)
    except AnalysisResourceLimit as error:
        return {
            "engine_version": ENGINE_VERSION,
            "requires_confirmation": True,
            "status": "skipped",
            "reason": str(error),
            "columns": [],
            "config": {
                "y_columns": [],
                "x_column": None,
                "group_columns": [],
                "units": {},
                "header_row_count": snapshot.get("default_header_row_count", 0),
                "line_plot": False,
                "number_format": "source",
            },
        }
    header = snapshot.get("default_header_row_count", 0)
    if type(header) is not int or not 0 <= header <= len(rows):
        raise ValueError("默认表头行数不在原表范围内")
    columns = _analysis_columns(columns, rows, header)
    maps = [{cell["column"]: cell for cell in row["cells"]} for row in rows[header:]]
    candidates, profiles = [], []
    for column in columns:
        counts = Counter()
        for cells in maps:
            cell = cells.get(column["column"])
            counts[_number(cell, "source")[1]] += 1
        profiles.append(
            {
                "column_id": column["column_id"],
                "label": column.get("label"),
                "counts": {key: counts[key] for key in _COUNT_KEYS},
            }
        )
        observed = sum(counts.values()) - counts["missing"] - counts["absent"]
        if counts["valid"] and counts["valid"] * 2 >= observed:
            candidates.append(column["column_id"])
    too_many = len(candidates) > MAX_Y_COLUMNS
    config = {
        "y_columns": [] if too_many else candidates,
        "x_column": None,
        "group_columns": [],
        "units": {},
        "header_row_count": header,
        "line_plot": False,
        "number_format": "source",
    }
    return {
        "engine_version": ENGINE_VERSION,
        "requires_confirmation": True,
        "status": "ready" if candidates and not too_many else "needs_selection",
        "reason": f"候选数值列超过 {MAX_Y_COLUMNS}，请明确选择分析列"
        if too_many
        else None
        if candidates
        else "未发现占主要比例的有效数值列，请手动选择并核对数值格式",
        "config": config,
        "columns": profiles,
    }
