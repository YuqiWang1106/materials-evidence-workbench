"""Bounded lossless reader candidates; no file-specific decoding rules."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from kb_ingest.core import fingerprint

from .table_reading_exclusions import (
    validate_exclusions_against_package as _validate_exclusions_against_package,
)
from .table_reading_options import LIMITS as LIMITS
from .table_reading_options import reader_candidates as _reader_candidates
from .table_reading_options import validate_reader_options as _validate_reader_options

MAX_PREVIEW_CELLS = 2000
CAPABILITIES = {
    "formats": ["csv", "xlsx", "xls"],
    "layouts": ["records", "objects", "key_value", "curves", "channels"],
    "transpose_layouts": ["records", "objects"],
    "reading_plan_versions": ["1", "2"],
    "current_reading_plan_version": "2",
    "plan_layouts": ["row", "column", "matrix", "block", "curve"],
    "plan_virtual_transpose": True,
    "max_preview_cells": MAX_PREVIEW_CELLS,
    "formula_evaluation": False,
    "scientific_certification": False,
    "limitations": [
        "Suggestions are provisional and require explicit confirmation.",
        "Legacy profile transposition supports records and objects; v2 uses original-coordinate virtual views.",
        "XLS exposes cached values without formula expressions.",
        "Merged cells are retained, never forward-filled into invented source values.",
    ],
}


def _registered_path(asset):
    if not isinstance(asset, dict) or not all(asset.get(k) for k in ("asset_id", "local_path", "original_name")):
        raise ValueError("A registered asset is required")
    return Path(asset["local_path"])


def _selected_tables(asset, options=None):
    options = _validate_reader_options(options)
    candidates = _reader_candidates(_registered_path(asset), options)
    if len(candidates) != 1 or candidates[0]["options"] != options:
        raise ValueError("Ambiguous reader interpretations; select explicit reader_options before mapping cells")
    tables = candidates[0]["tables"]
    if not all(table.get("extraction_safe") for table in tables):
        raise ValueError("Source cannot be safely mapped with these reader_options")
    return tables


def _table_info(table):
    return {
        key: deepcopy(table.get(key))
        for key in (
            "sheet",
            "format",
            "nrows",
            "ncols",
            "merged_cells",
            "hidden",
            "hidden_columns",
            "extraction_safe",
            "parse_diagnostics",
            "issues",
            "value_representation",
        )
    }


def list_asset_tables(asset, reader_options=None):
    """Bounded structural metadata, lossless reader choices and provisional rules."""
    from .extractors.experimental import _cell, _detect_profile
    from .table_recipe import validate_profile_list

    reader_options = _validate_reader_options(reader_options)
    candidates = _reader_candidates(_registered_path(asset), reader_options)
    choices, suggestions, suggestion_diagnostics, regions, plans = [], [], [], [], []
    region_diagnostics = []
    region_total = 0
    for candidate in candidates:
        choices.append(
            {
                "candidate_id": candidate["candidate_id"],
                "reader_options": candidate["options"],
                "tables": [_table_info(table) for table in candidate["tables"]],
            }
        )
        for table in candidate["tables"]:
            if table.get("extraction_safe"):
                from .table_discovery import MAX_REGIONS, discover_regions

                discovered = discover_regions(table, asset["asset_id"])
                region_diagnostics.append(
                    {
                        **{key: value for key, value in discovered.items() if key != "regions"},
                        "sheet": table["sheet"],
                        "candidate_id": candidate["candidate_id"],
                    }
                )
                region_total += discovered["region_total"]
                regions.extend({**r, "candidate_id": candidate["candidate_id"]} for r in discovered["regions"])
                regions = regions[:MAX_REGIONS]
            profile = _detect_profile(table, asset) if table.get("extraction_safe") else None
            if profile:
                profile = {k: v for k, v in profile.items() if k not in ("selection", "template_id")}
                if profile["layout"] == "key_value":
                    present = {
                        str(_cell(table, row, profile.get("key_column", 1))["raw_value"])
                        for row in range(
                            profile.get("data_start_row", 1), profile.get("data_end_row", table["nrows"]) + 1
                        )
                    }
                    profile["fields"] = {
                        key: field for key, field in profile.get("fields", {}).items() if key in present
                    }
                try:
                    validate_profile_list([profile], [table], asset["asset_id"])
                except ValueError as error:
                    suggestion_diagnostics.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "sheet": table["sheet"],
                            "status": "requires_manual_mapping",
                            "reason": str(error),
                        }
                    )
                    continue
                suggestions.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "status": "provisional",
                        "basis": "deterministic_structure_signature",
                        "profile": profile,
                    }
                )
                try:
                    from .table_plan import adapt_profiles

                    plan = adapt_profiles([profile], {asset["asset_id"]: candidate["tables"]})
                    plans.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "sheet": table["sheet"],
                            "status": "provisional",
                            "requires_confirmation": True,
                            "reading_plan": plan,
                        }
                    )
                except ValueError as error:
                    suggestion_diagnostics.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "sheet": table["sheet"],
                            "status": "requires_meaning_confirmation",
                            "reason": str(error),
                        }
                    )
    selection_required = len(candidates) != 1 or candidates[0]["options"] != reader_options
    return {
        "asset_id": asset["asset_id"],
        "tables": choices[0]["tables"] if len(choices) == 1 else [],
        "reader_candidates": choices,
        "requires_reader_selection": selection_required,
        "suggested_profiles": suggestions,
        "requires_confirmation": True,
        "suggested_plans": plans,
        "suggestion_diagnostics": suggestion_diagnostics,
        "region_diagnostics": region_diagnostics,
        "regions": regions,
        "region_total": region_total,
        "regions_truncated": region_total > len(regions),
        "capabilities": deepcopy(CAPABILITIES),
    }


def preview_table(
    asset, *, sheet=None, row_start=1, row_end=None, column_start=1, column_end=None, reader_options=None
):
    """Return <=2000 original cells; never reindex a requested viewport."""
    tables = _selected_tables(asset, reader_options)
    table = next((table for table in tables if table["sheet"] == sheet), None)
    if table is None:
        raise ValueError("Unknown original worksheet; choose its exact sheet name")
    if not table["nrows"] or not table["ncols"]:
        return {**_table_info(table), "asset_id": asset["asset_id"], "rows": [], "capabilities": deepcopy(CAPABILITIES)}
    row_end = min(table["nrows"], row_start + 39) if row_end is None else row_end
    column_end = min(table["ncols"], column_start + 19) if column_end is None else column_end
    if (
        any(type(value) is not int for value in (row_start, row_end, column_start, column_end))
        or not 1 <= row_start <= row_end <= table["nrows"]
        or not 1 <= column_start <= column_end <= table["ncols"]
        or (row_end - row_start + 1) * (column_end - column_start + 1) > MAX_PREVIEW_CELLS
    ):
        raise ValueError(f"Preview bounds outside original table or exceed {MAX_PREVIEW_CELLS} cells")
    rows = [
        {**deepcopy(row), "cells": deepcopy(row["cells"][column_start - 1 : column_end])}
        for row in table["rows"][row_start - 1 : row_end]
    ]
    return {
        **_table_info(table),
        "asset_id": asset["asset_id"],
        "rows": rows,
        "bounds": {"row_start": row_start, "row_end": row_end, "column_start": column_start, "column_end": column_end},
        "capabilities": deepcopy(CAPABILITIES),
    }


def _options_by_asset(assets, options):
    if options is None:
        return {}
    if not isinstance(options, dict) or set(options) - {a["asset_id"] for a in assets}:
        raise ValueError("reader_options must be keyed by registered asset_id")
    return {key: _validate_reader_options(value) for key, value in options.items()}


def validate_recipe(recipe, assets, reader_options=None):
    """Validate data-only mappings; an empty recipe remains an unconfirmed draft."""
    from .table_recipe import keys, validate_profile_list, validate_rectangle

    if isinstance(recipe, dict) and "entry_profile" in recipe:
        from .table_region_review import PROFILE as REGION_PROFILE
        from .table_region_review import normalize_recipe as normalize_regions

        if recipe["entry_profile"] == REGION_PROFILE:
            return normalize_regions(recipe, assets, reader_options)
        from .row_measurements import normalize_recipe

        return normalize_recipe(recipe, assets, reader_options)
    keys(
        recipe,
        {"profiles", "excluded_ranges", "supplements", "version", "reading_plan", "reviewed_scopes", "standard_review"},
        path="recipe",
    )
    recipe = deepcopy(recipe)
    if recipe.get("version", "1") not in ("1", "2"):
        raise ValueError("Unsupported reading recipe version")
    is_plan = recipe.get("version") == "2"
    if "standard_review" in recipe:
        binding = recipe["standard_review"]
        expected = {"parent_source_id", "parent_revision", "parent_sha256", "review_version", "review_sha256"}
        if (
            not is_plan
            or not isinstance(binding, dict)
            or set(binding) != expected
            or not isinstance(binding["parent_source_id"], str)
            or not binding["parent_source_id"]
            or any(type(binding[k]) is not int or binding[k] < 1 for k in ("parent_revision", "review_version"))
            or any(
                not isinstance(binding[k], str) or not re.fullmatch("[0-9a-f]{64}", binding[k])
                for k in ("parent_sha256", "review_sha256")
            )
        ):
            raise ValueError("Invalid standard-table review binding")
    if not is_plan and ("reading_plan" in recipe or "reviewed_scopes" in recipe):
        raise ValueError("Version 2 is required for a reading plan")
    profiles = recipe.setdefault("profiles", [])
    excluded = recipe.setdefault("excluded_ranges", [])
    supplements = recipe.setdefault("supplements", [])
    if not isinstance(profiles, list) or len(profiles) > 128:
        raise ValueError("recipe.profiles must contain at most 128 declarative profiles")
    if not isinstance(excluded, list) or len(excluded) > 128:
        raise ValueError("recipe.excluded_ranges must contain at most 128 original ranges")
    if not isinstance(supplements, list) or len(supplements) > 128:
        raise ValueError("recipe.supplements must contain at most 128 independently attributed notes")
    options = _options_by_asset(assets, reader_options)
    asset_map = {asset["asset_id"]: asset for asset in assets}
    if len(asset_map) != len(assets):
        raise ValueError("Duplicate registered asset_id")
    selected = {}
    if is_plan:
        from .table_plan import compile_plan

        if profiles:
            raise ValueError("Version 2 uses one reading plan, not a second legacy profile pipeline")
        plan = recipe.get("reading_plan")
        if not isinstance(plan, dict):
            raise ValueError("Version 2 requires a reading_plan")
        for asset_id, asset in asset_map.items():
            selected[asset_id] = _selected_tables(asset, options.get(asset_id))
        compile_plan(plan, selected)
        reviewed = recipe.setdefault("reviewed_scopes", {})
        if not isinstance(reviewed, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or len(v) != 64 for k, v in reviewed.items()
        ):
            raise ValueError("reviewed_scopes must contain scope digest values")
    for item in [*profiles, *excluded]:
        if not isinstance(item, dict) or not isinstance(item.get("asset_id"), str) or item["asset_id"] not in asset_map:
            raise ValueError("Recipe references an unknown registered asset_id")
        asset_id = item["asset_id"]
        if asset_id not in selected:
            selected[asset_id] = _selected_tables(asset_map[asset_id], options.get(asset_id))
    for asset_id, tables in selected.items():
        validate_profile_list([p for p in profiles if p["asset_id"] == asset_id], tables, asset_id)
    for profile in profiles:
        if "data_start_row" not in profile and "data_ranges" not in profile:
            default_header = 1 if profile["layout"] in ("records", "objects") else 0
            profile["data_start_row"] = (
                1 if profile["layout"] == "key_value" else (profile.get("header_row", default_header) or 0) + 1
            )
    for index, item in enumerate(excluded):
        keys(
            item,
            {"asset_id", "sheet", "range", "reason"},
            {"asset_id", "sheet", "range", "reason"},
            f"excluded_ranges[{index}]",
        )
        table = next((t for t in selected[item["asset_id"]] if t["sheet"] == item["sheet"]), None)
        if table is None:
            raise ValueError("Excluded range references unknown worksheet")
        validate_rectangle(item["range"], table, f"excluded_ranges[{index}].range")
        if not isinstance(item["reason"], str) or not item["reason"].strip() or len(item["reason"]) > 4000:
            raise ValueError("Excluded ranges require a bounded reason")
    for index, item in enumerate(supplements):
        keys(item, {"text", "author", "scope"}, {"text", "author", "scope"}, f"supplements[{index}]")
        for name, limit in (("text", 20000), ("author", 200)):
            if not isinstance(item[name], str) or not item[name].strip() or len(item[name]) > limit:
                raise ValueError(f"supplements[{index}].{name} must be bounded nonempty text")
        scope = item["scope"]
        keys(
            scope,
            {"asset_id", "sheet", "row_start", "row_end", "column_start", "column_end"},
            {"asset_id", "sheet"},
            f"supplements[{index}].scope",
        )
        asset_id = scope["asset_id"]
        if not isinstance(asset_id, str) or asset_id not in asset_map:
            raise ValueError("Supplement scope references an unknown asset")
        if asset_id not in selected:
            selected[asset_id] = _selected_tables(asset_map[asset_id], options.get(asset_id))
        table = next((t for t in selected[asset_id] if t["sheet"] == scope["sheet"]), None)
        if table is None:
            raise ValueError("Supplement scope references an unknown worksheet")
        for start_key, end_key, upper in (
            ("row_start", "row_end", table["nrows"]),
            ("column_start", "column_end", table["ncols"]),
        ):
            if start_key in scope or end_key in scope:
                low, high = scope.get(start_key, 1), scope.get(end_key, upper)
                if type(low) is not int or type(high) is not int or not 1 <= low <= high <= upper:
                    raise ValueError("Supplement scope bounds lie outside original table")
    return recipe


def structure_snapshot(assets, reader_options=None):
    """Compare structure only; no old measurements are carried into a new draft."""
    options = _options_by_asset(assets, reader_options)
    result = []
    for asset in assets:
        tables = []
        for table in _selected_tables(asset, options.get(asset["asset_id"])):
            # Text and types are conservative structure signatures, never reusable facts.
            signature = [
                [
                    {"type": c["value_type"], "text": c["raw_value"] if isinstance(c["raw_value"], str) else None}
                    for c in row["cells"]
                ]
                for row in table["rows"][:10]
            ]
            tables.append(
                {
                    "sheet": table["sheet"],
                    "format": table["format"],
                    "nrows": table["nrows"],
                    "ncols": table["ncols"],
                    "merged_cells": table["merged_cells"],
                    "header_signature": fingerprint(signature),
                }
            )
        result.append({"asset_id": asset["asset_id"], "tables": tables})
    return {"assets": result}


def compare_structures(previous, current):
    """Conservative reuse warning. Compatibility never bypasses confirmation."""
    changes = []
    old, new = previous.get("assets", []), current.get("assets", [])
    if len(old) != len(new):
        changes.append({"scope": "assets", "reason": "asset_count_changed"})
    for index, (before, after) in enumerate(zip(old, new)):
        if before.get("tables") != after.get("tables"):
            changes.append({"asset_index": index, "reason": "sheet_extent_header_or_merge_changed"})
    return {"compatible": not changes, "changes": changes, "requires_confirmation": True}


def apply_plan_exclusions(package, exclusions, confirmation_ref):
    """Project explicit exclusions identically in preview, execution and replay.

    Only uninterpreted coverage is resolved. An identity failure or value issue
    is a separate record and cannot be made usable by excluding its cells.
    """
    from openpyxl.utils.cell import range_boundaries

    from kb_ingest.contract import add_coverage

    _validate_exclusions_against_package({"excluded_ranges": exclusions}, package)
    tables = {
        (e["asset_ref"], e["content"].get("sheet")): e["content"]
        for e in package["evidence"]
        if e.get("content_kind") == "table_transcription"
    }
    for exclusion in exclusions:
        add_coverage(
            package,
            exclusion,
            "not_applicable",
            "研究人员明确选择不整理此范围",
            reading_confirmation_ref=confirmation_ref,
        )
    for item in package["coverage"]:
        scope = item.get("scope")
        if not isinstance(scope, dict) or not (
            scope.get("cell_state") == "not_mapped" or scope.get("scope") == "unselected_table"
        ):
            continue
        rectangles = [
            range_boundaries(e["range"])
            for e in exclusions
            if e["asset_id"] == scope.get("asset_ref") and e["sheet"] == scope.get("sheet")
        ]
        if not rectangles:
            continue
        cells = item.get("cells")
        if cells is None:
            table = tables[(scope["asset_ref"], scope["sheet"])]
            cells = [[c["row"], c["column"]] for row in table["rows"] for c in row["cells"]]
        remaining, excluded = [], []
        for row, col in cells:
            (
                excluded if any(c0 <= col <= c1 and r0 <= row <= r1 for c0, r0, c1, r1 in rectangles) else remaining
            ).append([row, col])
        if excluded:
            item.update(
                cells=remaining,
                excluded_cells=excluded,
                handling="not_checked" if remaining else "checked",
                reason="未整理范围与主动排除范围分别保留",
            )
