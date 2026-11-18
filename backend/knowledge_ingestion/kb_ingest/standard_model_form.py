"""Provider form pins program-known columns; semantic choices stay model-owned."""

from copy import deepcopy

from openpyxl.utils.cell import range_boundaries

from .standard_table_meaning import SUGGESTION_SCHEMA


def _is_coordinate(value):
    return isinstance(value, list) and len(value) == 2 and all(type(part) is int and part >= 1 for part in value)


def _validate_provider_schema(proposal, expected_columns):
    """Validate the provider's fixed JSON contract before making a review candidate."""
    required = set(SUGGESTION_SCHEMA["properties"])
    if not isinstance(proposal, dict) or set(proposal) != required:
        raise ValueError("模型回复缺少必需字段或包含额外字段；可手动填写或显式重试")
    enums = {
        "row_kind": SUGGESTION_SCHEMA["properties"]["row_kind"]["enum"],
        "layout": SUGGESTION_SCHEMA["properties"]["layout"]["enum"],
        "identity_mode": SUGGESTION_SCHEMA["properties"]["identity_mode"]["enum"],
        "number_format": SUGGESTION_SCHEMA["properties"]["number_format"]["enum"],
    }
    for key, values in enums.items():
        if proposal[key] not in values:
            raise ValueError(f"模型回复中的 {key} 不符合固定选项；可手动填写或显式重试")
    if proposal["version"] != "2":
        raise ValueError("模型回复的读法版本不受支持；可手动填写或显式重试")

    roles = proposal["column_roles"]
    if not isinstance(roles, dict) or set(roles) != set(expected_columns):
        raise ValueError("模型未完整返回所选列；请在含义表单中核对或手动填写")
    role_values = SUGGESTION_SCHEMA["properties"]["column_roles"]["items"]["properties"]["role"]["enum"]
    for role in roles.values():
        if (
            not isinstance(role, dict)
            or set(role) != {"role", "reason"}
            or role.get("role") not in role_values
            or not isinstance(role.get("reason"), str)
            or not role["reason"].strip()
            or len(role["reason"]) > 160
        ):
            raise ValueError("模型回复中的列角色或理由不符合固定格式；可手动填写或显式重试")

    conditions = proposal["header_conditions"]
    if not isinstance(conditions, list) or len(conditions) > 8:
        raise ValueError("模型回复中的固定条件列表格式无效；可手动填写或显式重试")
    for condition in conditions:
        if (
            not isinstance(condition, dict)
            or set(condition) != {"cell", "reason"}
            or not _is_coordinate(condition.get("cell"))
            or not isinstance(condition.get("reason"), str)
            or not condition["reason"].strip()
            or len(condition["reason"]) > 160
        ):
            raise ValueError("模型回复中的固定条件格式无效；可手动填写或显式重试")

    measurements = proposal["measurements"]
    if not isinstance(measurements, list) or len(measurements) > 50:
        raise ValueError("模型回复中的结果设置格式无效；可手动填写或显式重试")
    for measurement in measurements:
        if (
            not isinstance(measurement, dict)
            or set(measurement) != {"result_column", "value_type", "metric", "unit"}
            or type(measurement.get("result_column")) is not int
            or measurement["result_column"] < 1
            or measurement.get("value_type") not in ("number", "category")
        ):
            raise ValueError("模型回复中的结果设置不符合固定格式；可手动填写或显式重试")
        metric, unit = measurement["metric"], measurement["unit"]
        if not isinstance(metric, dict) or metric.get("kind") not in ("header", "column", "cell"):
            raise ValueError("模型回复中的指标来源格式无效；可手动填写或显式重试")
        metric_keys = (
            {"kind"}
            if metric["kind"] == "header"
            else {"kind", "column"}
            if metric["kind"] == "column"
            else {"kind", "cell"}
        )
        if (
            set(metric) != metric_keys
            or ("column" in metric and (type(metric["column"]) is not int or metric["column"] < 1))
            or ("cell" in metric and not _is_coordinate(metric["cell"]))
        ):
            raise ValueError("模型回复中的指标来源格式无效；可手动填写或显式重试")
        if not isinstance(unit, dict) or unit.get("kind") not in ("absent", "column", "cell", "cell_span"):
            raise ValueError("模型回复中的单位来源格式无效；可手动填写或显式重试")
        unit_keys = (
            {"kind"}
            if unit["kind"] == "absent"
            else {"kind", "column"}
            if unit["kind"] == "column"
            else {"kind", "cell"}
            if unit["kind"] == "cell"
            else {"kind", "cell", "span"}
        )
        if set(unit) != unit_keys:
            raise ValueError("模型回复中的单位来源格式无效；可手动填写或显式重试")
        if "column" in unit and (type(unit["column"]) is not int or unit["column"] < 1):
            raise ValueError("模型回复中的单位列坐标无效；可手动填写或显式重试")
        if "cell" in unit and not _is_coordinate(unit["cell"]):
            raise ValueError("模型回复中的单位单元格坐标无效；可手动填写或显式重试")
        if unit["kind"] == "cell_span" and (
            not isinstance(unit["span"], list)
            or len(unit["span"]) != 2
            or any(type(part) is not int or part < 0 for part in unit["span"])
        ):
            raise ValueError("模型回复中的单位文字范围无效；可手动填写或显式重试")


def request_schema(context):
    first, _, last, _ = range_boundaries(context["table_range"])
    schema = deepcopy(SUGGESTION_SCHEMA)
    role = deepcopy(schema["properties"]["column_roles"]["items"])
    del role["properties"]["column"]
    role["required"].remove("column")
    keys = [f"column_{n}" for n in range(first, last + 1)]
    schema["properties"]["column_roles"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: deepcopy(role) for key in keys},
        "required": keys,
    }
    return schema


def to_review_form(proposal, context):
    """Validate the complete fixed schema, then decode program-owned positions."""
    expected = request_schema(context)["properties"]["column_roles"]["required"]
    _validate_provider_schema(proposal, expected)
    roles = proposal["column_roles"]
    form = deepcopy(proposal)
    form["column_roles"] = [{"column": int(key.removeprefix("column_")), **roles[key]} for key in expected]
    return form
