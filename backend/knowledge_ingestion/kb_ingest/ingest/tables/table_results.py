"""Bounded user-facing projections; the full scientific package stays unchanged."""

from collections import Counter
from copy import deepcopy

from kb_ingest.contract import FIELDS
from kb_ingest.core import fingerprint

from .table_quality import blocked_objects, inactive
from .table_result_curves import _curve_counts as _curve_counts_view
from .table_result_curves import _curve_result as _curve_result_view
from .table_result_projections import _ownership_projection as _ownership_projection_view
from .table_result_projections import _relationship_view as _relationship_view_fn

KNOWN_ABSENCE = {
    "source_blank",
    "source_not_tested",
    "source_not_detected",
    "source_not_applicable",
    "not_applicable",
    "source_not_reported",
}


def unresolved_assessments(package):
    return [
        a
        for a in package["assessments"]
        if a.get("handling") not in KNOWN_ABSENCE and a.get("resolution_status") != "resolved"
    ]


def unresolved_coverage(package):
    return [
        c
        for c in package["coverage"]
        if c.get("handling") == "not_checked" and c.get("scope") != "scientific_interpretation"
    ]


def processing_summary(package):
    blocked = blocked_objects(package)
    active = [a for a in package["assertions"] if not inactive(a) and a["subject_ref"] not in blocked]
    unresolved = [i for i in package["issues"] if i.get("resolution_status") not in ("resolved", "dismissed")]
    gaps = unresolved_coverage(package)
    uncertain = unresolved_assessments(package)
    partial = bool(unresolved or uncertain or gaps or len(active) != len(package["assertions"]))
    return {
        "outcome": "partial" if partial else "complete",
        "reading_confirmed": bool(package["header"].get("reading_confirmation")),
        "model_calls": 0,
        "human_expert_review": package["header"].get("human_expert_review", False),
        "usable_values": len(active),
        "unresolved_items": len(unresolved) + len(uncertain),
        "assessment_count": len(package["assessments"]),
        "coverage_gaps": len(gaps),
        "object_counts": dict(Counter(o["kind"] for o in package["objects"])),
    }


def result_view(package, task, *, offset=0, limit=100, subject_ref=None, scope_id=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("结果分页范围无效")
    objects = {o["object_id"]: o for o in package["objects"]}
    program_only = package["header"].get("document_extraction_mode") == "program-only-v1"
    blocked = blocked_objects(package)
    project_contexts = _ownership_projection_view(package, objects, blocked)
    entries = [
        a
        for a in package["assertions"]
        if (not subject_ref or a["subject_ref"] == subject_ref)
        and (scope_id is None or a.get("source_context", {}).get("record_scope", {}).get("scope") == scope_id)
    ]
    standard = package["header"].get("standard_table_review")
    if package["header"].get("entry_profile") == "row_measurements_v1" or standard:
        entries = [a for a in entries if a["field_id"] == "observation.value"]
        entries += [
            {
                **a,
                "assertion_id": a["assessment_id"],
                "value": {
                    "raw_value": a.get("raw_value"),
                    "raw_unit": a.get("raw_unit"),
                    "raw_basis": a.get("raw_basis"),
                    "typed_value_version": "2",
                    "typed_value": a.get("typed_value"),
                },
            }
            for a in package["assessments"]
            if a["field_id"] == "observation.value"
            and (not subject_ref or a["subject_ref"] == subject_ref)
            and (scope_id is None or a.get("source_context", {}).get("record_scope", {}).get("scope") == scope_id)
        ]
        entries.sort(key=lambda a: a.get("source_context", {}).get("record_scope", {}).get("row", 0))
    rows = []
    for a in entries[offset : offset + limit]:
        value = a["value"]
        owner = objects[a["subject_ref"]]
        row = {
            "id": a["assertion_id"],
            "subject_ref": a["subject_ref"],
            "subject": owner["label"],
            "kind": owner["kind"],
            "field_id": a["field_id"],
            "field": a.get("qualifiers", {}).get("metric_label")
            or a.get("qualifiers", {}).get("parameter_label")
            or a.get("qualifiers", {}).get("metric_id")
            or FIELDS[a["field_id"]]["label"],
            "unit": value.get("raw_unit"),
            "basis": value.get("raw_basis"),
            "evidence_refs": a.get("evidence_refs", []),
            # An assessment supplies no usable measurement, but its independently
            # admitted ownership can still explain whose missing result this is.
            "usable": "assessment_id" not in a and not inactive(a) and a["subject_ref"] not in blocked,
            "value": value["raw_value"] if not isinstance(value["raw_value"], list) else None,
        }
        if program_only:
            row["use_status"] = a.get("use_status", "needs_attention")
            row["program_check"] = deepcopy(a.get("program_check", {"status": "needs_attention", "reasons": []}))
            row["usable"] = row["usable"] and row["use_status"] == "program_checked"
        row.update(project_contexts(a))
        if value.get("typed_value_version") == "2":
            row["typed_value"] = deepcopy(value.get("typed_value"))
            bindings = a.get("source_context", {}).get("bindings", {})
            row["source_bindings"] = {
                role: deepcopy(binding[:20] if isinstance(binding, list) else binding)
                for role, binding in bindings.items()
            }
            row["source_binding_counts"] = {
                role: len(binding) if isinstance(binding, list) else 1 for role, binding in bindings.items()
            }
        from .extractors.table_headers import bounded_header_context

        headers = a.get("source_context", {}).get("header_context", [])
        row["header_context"] = bounded_header_context(
            headers, confirmed=bool(package["header"].get("reading_confirmation"))
        )
        row["header_context_total"] = len(headers)
        row["header_context_truncated"] = len(headers) > len(row["header_context"])
        if "series_data" in a:
            row.update(_curve_result_view(a))
            row["point_count"] = len(value["raw_value"]) if isinstance(value["raw_value"], list) else 0
        elif isinstance(value["raw_value"], list):
            row["value"] = value["raw_value"][:20]
            row["value_count"] = len(value["raw_value"])
        rows.append(row)
    summary = package["header"].get("processing_summary") or processing_summary(package)
    curve_counts = _curve_counts_view(entries)
    summary = {
        **summary,
        **curve_counts,
        "saved": task["state"] == "saved",
        "original_cell_check": "passed" if package["header"].get("table_quality_version") else "not_applicable",
    }
    curve_unresolved = bool(curve_counts["unparsed_x_point_count"] or curve_counts["unparsed_point_count"])
    if curve_unresolved:
        summary["outcome"] = "partial"
    if program_only:
        counts = Counter(a.get("use_status", "needs_attention") for a in package["assertions"])
        summary.update(
            document_extraction_mode="program-only-v1",
            review_policy=package["header"].get("review_policy"),
            program_checked_assertions=counts["program_checked"],
            needs_attention_assertions=counts["needs_attention"],
            blocked_assertions=counts["blocked"],
        )
    issues = [
        {
            "id": i["issue_id"],
            "code": i.get("code"),
            "description": i.get("description"),
            "severity": i.get("severity"),
            "evidence_refs": i.get("evidence_refs", []),
            "target_refs": i.get("target_refs", []),
        }
        for i in package["issues"]
        if i.get("resolution_status") not in ("resolved", "dismissed")
    ]
    gaps = unresolved_coverage(package)
    result = {
        "task_id": task["task_id"],
        "source_id": task["source_id"],
        "title": package["sources"][0].get("title") or task["source_id"],
        "package_sha256": fingerprint(package),
        "summary": summary,
        "rows": rows,
        "total": len(entries),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < len(entries),
        "issues": issues[:100],
        "issue_total": len(issues),
        "coverage_gaps_detail": [
            {
                "coverage_id": c["coverage_id"],
                "scope": c.get("scope"),
                "reason": c.get("reason"),
                "cell_addresses": c.get("cell_addresses", [])[:20],
                "cells": c.get("cells", [])[:20],
                "cell_count": len(c.get("cell_addresses", c.get("cells", []))),
            }
            for c in gaps[:100]
        ],
        "coverage_gap_total": len(gaps),
        "assessments": [
            {
                k: a.get(k)
                for k in (
                    "assessment_id",
                    "target_ref",
                    "field_id",
                    "handling",
                    "raw_value",
                    "typed_value",
                    "reason",
                    "evidence_refs",
                )
            }
            for a in package["assessments"][:100]
        ],
        "assessment_total": len(package["assessments"]),
        "assets": [{k: a.get(k) for k in ("asset_id", "original_name", "role")} for a in package["assets"]],
        "supplements": [
            {"evidence_id": e["evidence_id"], **e["content"]}
            for e in package["evidence"]
            if e.get("content_kind") == "human_supplied_statement"
        ],
    }
    if program_only:
        result["document_extraction_mode"] = "program-only-v1"
        result["review_policy"] = package["header"].get("review_policy")
    if package["header"].get("table_quality_version") == "tabular-quality-2":
        result["relationship_view"] = _relationship_view_fn(package, rows, objects, blocked)
    if package["header"].get("entry_profile"):
        result["entry_profile"] = package["header"]["entry_profile"]
        result["completion"] = deepcopy(package["header"].get("row_measurements_completion", {}))
    if standard:
        # The immutable scientific package is unchanged. This is a results-only
        # display projection; ignored/context columns are not ordinary outcomes.
        observations = [a for a in package["assertions"] if a["field_id"] == "observation.value"]
        absences = [a for a in package["assessments"] if a["field_id"] == "observation.value"]
        unknown = [a for a in absences if a.get("handling") not in KNOWN_ABSENCE | {"source_nan"}]
        result["entry_profile"] = "standard_tables_v1"
        result["standard_review"] = deepcopy(standard)
        result["completion"] = {
            "selected_scope_complete": bool(observations or absences)
            and not unknown
            and not issues
            and not curve_unresolved
            and all(not inactive(a) and a["subject_ref"] not in blocked for a in observations),
            **curve_counts,
            "source_missing_results": len(absences),
            "whole_file_complete": False,
            "scope": "confirmed_result_columns_only",
            "scientific_expert_review": False,
        }
    return result


def matched_values_projection(assertions, match, limit=20):
    """Only project already matched observations; never expand a scientific join."""
    selected = [
        assertions[ref]
        for ref in match.get("assertion_refs", [])
        if ref in assertions and assertions[ref]["field_id"] == "observation.value"
    ]
    rows = []
    for assertion in selected[:limit]:
        value = assertion["value"]
        bindings = assertion.get("source_context", {}).get("bindings", {})
        wanted = ("value", "unit", "qualifier:metric_label", "qualifier:property")
        sources = {
            k: deepcopy(v[:20] if isinstance(v, list) else v)
            for k, v in bindings.items()
            if k in wanted or k.startswith("qualifier:condition:") or k == "qualifier:method"
        }
        for binding in sources.values():
            for source in binding if isinstance(binding, list) else [binding]:
                if isinstance(source.get("raw"), str) and len(source["raw"]) > 512:
                    source["raw"] = source["raw"][:512]
                    source["raw_truncated"] = True
        raw = value["raw_value"]
        truncated = isinstance(raw, str) and len(raw) > 512
        typed = deepcopy(value.get("typed_value"))
        if typed and isinstance(typed.get("raw"), str) and len(typed["raw"]) > 512:
            typed["raw"] = typed["raw"][:512]
            typed["raw_truncated"] = True
        rows.append(
            {
                "id": assertion["assertion_id"],
                "subject_ref": assertion["subject_ref"],
                "field_id": assertion["field_id"],
                "field": assertion.get("qualifiers", {}).get("metric_label"),
                "value": raw[:512] if truncated else raw,
                "value_truncated": truncated,
                "unit": value.get("raw_unit"),
                "typed_value": typed,
                "evidence_refs": assertion.get("evidence_refs", [])[:20],
                "source_bindings": sources,
            }
        )
    return {
        "matched_values": rows,
        "matched_value_total": len(selected),
        "matched_values_truncated": len(selected) > limit,
    }
