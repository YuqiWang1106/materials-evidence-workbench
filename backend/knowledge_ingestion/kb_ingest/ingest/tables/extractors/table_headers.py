"""One source-grounded header resolver for scalar fields, curves and previews.

Header scopes preserve context only. Semantic inheritance requires an explicit
binding; even merged or blank cells are never forward filled.
"""

from copy import deepcopy

from kb_ingest.core import fingerprint
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries

from .table_orientation import original_cell


def classify_header_cell(cell):
    """Titles are literal text, not measurements with a missing-value lexicon.

    Source formula/error metadata and explicit symbol placeholders remain
    invalid. Alphabetic tokens are never interpreted as missing titles.
    """
    from .table_semantics import classify_cell

    result = classify_cell(cell, interpret_missing=False)
    raw = cell["raw_value"]
    if result["status"] == "value" and isinstance(raw, str):
        if raw.lstrip().startswith("="):
            return {"status": "formula_requires_review", "parsed": None}
        if raw.strip() in {"—", "–", "-", "--", "…", "...", "?"}:
            return {"status": "ambiguous_placeholder", "parsed": None}
    return result


def resolve_headers(asset, table, profile, column, spec=None, *, row=None, header_cell=None, axis="value"):
    from .experimental import _address, _cell, _column
    from .table_semantics import binding, resolve_unit

    spec = spec or {}
    column = _column(column)
    header_row = profile.get("header_row", None if profile.get("layout") in ("key_value", "curves", "channels") else 1)
    primary = (
        _address(table, spec["header_cell"])
        if spec.get("header_cell")
        else header_cell
        if header_cell is not None
        else _cell(table, header_row, column)
        if header_row
        else None
    )
    headers = [_cell(table, r, column) for r in sorted(set(profile.get("header_rows", [])))]
    if primary is not None:
        headers.append(primary)
    scope = []
    if spec.get("header_scope"):
        c0, r0, c1, r1 = range_boundaries(spec["header_scope"])
        scope = [_cell(table, r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

    def unique(cells):
        return list({(c["row"], c["column"]): c for c in cells if not c.get("_absent_original")}.values())

    headers = unique(headers)
    units = []
    if spec.get("unit_cell"):
        units.append(_address(table, spec["unit_cell"]))
    if spec.get("unit_column") is not None and row is not None:
        units.append(_cell(table, row, _column(spec["unit_column"])))
    unit_row = spec.get("unit_row", profile.get("unit_row"))
    if unit_row:
        units.append(_cell(table, unit_row, column))
    roles, role_cells, issues = [], [], []
    primary_status = classify_header_cell(primary)["status"] if primary is not None else "source_blank"
    if primary_status not in ("value", "source_blank"):
        issues.append({"code": "invalid_primary_header", "source_cell": original_cell(primary)["address"]})

    def describe(cell):
        original = original_cell(cell)
        return {
            "source_cell": original["address"],
            "profile_cell": cell["address"],
            "raw_value": original["raw_value"],
            "status": classify_header_cell(cell)["status"],
            "source": {k: v for k, v in binding(asset, table, cell, "header").items() if k != "role"},
        }

    for rule in profile.get("header_bindings", []):
        if column not in [_column(c) for c in rule["columns"]]:
            continue
        cell = _address(table, rule["source_cell"])
        role_cells.append(cell)
        role = {
            **describe(cell),
            "role": rule["role"],
            "columns": [get_column_letter(_column(c)) for c in rule["columns"]],
            "interpretation_basis": "user_confirmed_header_binding",
        }
        if role not in roles:
            roles.append(role)
        if role["status"] != "value":
            issues.append({"code": "invalid_header_binding", "role": rule["role"], "source_cell": role["source_cell"]})
        if rule["role"] == "unit":
            units.append(cell)
    for role in ("material", "stage", "metric"):
        values = {fingerprint(r["raw_value"]) for r in roles if r["role"] == role and r["status"] == "value"}
        if len(values) > 1:
            issues.append({"code": "conflicting_header_labels", "role": role})
    unit_role = "x_unit" if axis == "x" else "unit"
    unit = resolve_unit(asset, table, column, None, unique(units), unit_role)
    semantic_sources = {r["profile_cell"] for r in roles if r["role"] in ("material", "stage", "note")}
    metric_sources = [
        cell
        for cell in role_cells
        if any(r["profile_cell"] == cell["address"] and r["role"] == "metric" for r in roles)
    ]
    unit_headers = unique(([primary] if primary is not None else []) + metric_sources)
    for header in unit_headers:
        if header["address"] in semantic_sources:
            continue
        additional = resolve_unit(asset, table, column, header, (), unit_role)
        unit["candidates"].extend(additional["candidates"])
        unit["bindings"].extend(additional["bindings"])
        unit["ambiguous"] |= additional["ambiguous"]
    unit["candidates"] = list(dict.fromkeys(unit["candidates"]))
    unit["bindings"] = list({fingerprint(b): b for b in unit["bindings"]}.values())
    unit["ambiguous"] |= len(unit["candidates"]) > 1
    unit["unit"] = None if unit["ambiguous"] or not unit["candidates"] else unit["candidates"][0]
    if unit["ambiguous"]:
        issues.append({"code": "conflicting_header_units", "role": "unit"})
    context = {
        "column": get_column_letter(column),
        "axis": axis,
        "header_path": [describe(c) for c in headers],
        "scope_cells": [describe(c) for c in unique(scope)],
        "roles": roles,
        "unit": {
            "value": unit["unit"],
            "candidates": unit["candidates"],
            "ambiguous": unit["ambiguous"],
            "sources": unit["bindings"],
        },
        "issues": issues,
        "status": "blocked" if issues else "resolved" if unit["unit"] or roles else "unknown",
    }
    if spec.get("header_scope"):
        context["header_scope"] = spec["header_scope"]
    metric = next((r["raw_value"] for r in roles if r["role"] == "metric" and r["status"] == "value"), None)
    if any(i.get("role") == "metric" for i in issues):
        metric = None
    return {
        "context": context,
        "unit": unit,
        "metric": metric,
        "primary_label": primary["raw_value"] if primary_status == "value" else None,
        "cells": unique(headers + scope + units + role_cells),
    }


def bounded_header_context(contexts, *, confirmed=True):
    """Bound all user-controlled text/lists without mutating stored history."""

    def bounded(value):
        if isinstance(value, str):
            return value[:512]
        if isinstance(value, list):
            return [bounded(v) for v in value[:128]]
        if isinstance(value, dict):
            return {k: bounded(v) for k, v in value.items()}
        return value

    result = []
    for original in contexts[:8]:
        context = bounded(deepcopy(original))
        for name, maximum in [("header_path", 32), ("scope_cells", 20), ("roles", 32), ("issues", 32)]:
            entries = original.get(name, [])
            context[name] = bounded(entries[:maximum])
            context[name + "_total"] = len(entries)
            context[name + "_truncated"] = len(entries) > maximum
        context["reading_confirmed"] = confirmed
        sources = original.get("unit", {}).get("sources", [])
        context["unit"]["sources"] = bounded(sources[:32])
        context["unit"]["sources_total"] = len(sources)
        context["unit"]["sources_truncated"] = len(sources) > 32
        if not confirmed:
            for role in context["roles"]:
                role["interpretation_basis"] = "draft_header_binding"
        result.append(context)
    return result


def apply_metric(qualifiers, resolved):
    """An explicit metric role participates in the field's scientific label."""
    metric = resolved["metric"]
    if metric is None:
        return
    if qualifiers.get("metric_label") is not None and qualifiers["metric_label"] != metric:
        resolved["context"]["issues"].append({"code": "conflicting_header_metric", "role": "metric"})
        resolved["context"]["status"] = "blocked"
        return
    qualifiers["metric_label"] = str(metric)
    qualifiers.setdefault("metric_id", "source_header:" + str(metric))
