"""Restricted delivery profile: compile confirmations to the shared v2 engine.

No parallel extraction path. This module owns entry bounds and source-backed
meaning checks; table_plan owns the actual records, links, values and proofs.
"""

from copy import deepcopy

from openpyxl.utils.cell import get_column_letter

from .row_measurements_inspect import _header_path as _header_path
from .row_measurements_inspect import _resolved as _resolved
from .row_measurements_inspect import inspect_definition as _inspect_definition
from .table_recipe import keys

PROFILE = "row_measurements_v1"
RECIPE_KEYS = {
    "version",
    "entry_profile",
    "definition",
    "reading_plan",
    "profiles",
    "excluded_ranges",
    "supplements",
    "reviewed_scopes",
}


def is_profile(recipe):
    return recipe.get("entry_profile") == PROFILE


def normalize_recipe(recipe, assets, reader_options=None):
    keys(recipe, RECIPE_KEYS, path="recipe")
    if recipe.get("entry_profile") != PROFILE:
        raise ValueError("不支持的表格入口 profile")
    if recipe.get("version", "2") != "2":
        raise ValueError("行式入口需要 ReadingPlan v2")
    if any(recipe.get(k) for k in ("profiles", "supplements")):
        raise ValueError("行式入口不能添加其他读法或排除范围")
    definition = deepcopy(recipe.get("definition", {"version": "1"}))
    plan, _, _ = _inspect_definition(definition, assets, reader_options)
    exclusions = []
    if plan["scopes"]:
        r0, c0, r1, c1 = definition["data_range"]
        header_rows = definition.get("header_rows", [definition["header_row"]])
        fixed = {
            tuple(s["cell"])
            for s in (definition.get("metric_source", {}), definition.get("unit_source", {}))
            if "cell" in s
        }
        for column in definition["ignored_columns"]:
            letter = get_column_letter(column)
            rectangles = [f"{letter}{r0}:{letter}{r1}"]
            if not any((row, column) in fixed for row in header_rows):
                rectangles.append(f"{letter}{header_rows[0]}:{letter}{header_rows[-1]}")
            for rectangle in rectangles:
                exclusions.append(
                    {
                        "asset_id": definition["asset_id"],
                        "sheet": definition["sheet"],
                        "range": rectangle,
                        "reason": "人员确认此列本次不整理",
                    }
                )
    if recipe.get("excluded_ranges") and recipe["excluded_ranges"] != exclusions:
        raise ValueError("行式排除范围只能由确认的忽略列生成")
    if "reading_plan" in recipe and recipe["reading_plan"] != plan:
        raise ValueError("执行计划只能由行式定义生成 / generated plan required")
    reviewed = recipe.get("reviewed_scopes", {})
    if not isinstance(reviewed, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or len(v) != 64 for k, v in reviewed.items()
    ):
        raise ValueError("核对版本无效")
    return {
        "version": "2",
        "entry_profile": PROFILE,
        "definition": definition,
        "reading_plan": plan,
        "profiles": [],
        "excluded_ranges": exclusions,
        "supplements": [],
        "reviewed_scopes": deepcopy(reviewed),
    }


def inspect_draft(service, draft):
    return _inspect_definition(
        draft["recipe"]["definition"], service._table_assets(draft["asset_ids"]), draft["reader_options"]
    )


def require_ready(service, draft):
    plan, status, completion = inspect_draft(service, draft)
    if plan != draft["recipe"].get("reading_plan"):
        raise ValueError("行式定义与执行计划不一致")
    if not status["ready"]:
        raise ValueError("所选范围尚未完成核对：" + status["issues"][0]["message"])
    return status, completion


def annotate(package, recipe, completion):
    package["header"]["entry_profile"] = PROFILE
    package["header"]["row_measurements_definition"] = deepcopy(recipe["definition"])
    package["header"]["row_measurements_completion"] = deepcopy(completion)


def check_package(service, draft, package):
    """Publication gate rechecks the confirmed scope against immutable sources."""
    from kb_ingest.core import StateConflict

    from .table_quality import blocked_objects, inactive

    _, completion = require_ready(service, draft)
    header = package["header"]
    if (
        header.get("entry_profile") != PROFILE
        or header.get("row_measurements_definition") != draft["recipe"]["definition"]
        or header.get("reading_plan") != draft["recipe"]["reading_plan"]
        or header.get("selected_scope") != draft["recipe"]
        or header.get("row_measurements_completion") != completion
    ):
        raise StateConflict("行式入口记录与确认范围完整性不一致 / completion mismatch")
    results = [a for a in package["assertions"] if a["field_id"] == "observation.value"]
    missing = [a for a in package["assessments"] if a["field_id"] == "observation.value"]
    r0, _, r1, _ = draft["recipe"]["definition"]["data_range"]
    rows = [a.get("source_context", {}).get("record_scope", {}).get("row") for a in results + missing]
    if sorted(rows) != list(range(r0, r1 + 1)):
        raise StateConflict("所选范围记录不完整，不能保存")
    blocked = blocked_objects(package)
    if any(inactive(a) or a["subject_ref"] in blocked for a in results) or any(
        i.get("severity") == "error" and i.get("resolution_status") not in ("resolved", "dismissed")
        for i in package["issues"]
    ):
        raise StateConflict("所选范围仍有关键问题，不能保存")
