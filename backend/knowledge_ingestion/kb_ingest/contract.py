"""Portable six-category records. Validation proves structure, not scientific truth.

Profiles can be explicitly extended inside a versioned package. This retains the
shared catalogue without forcing unknown measures into a superficially similar one.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .core import serialization
from .core.paths import PACKAGE_ROOT
from .core.serialization import canonical

ROOT = PACKAGE_ROOT / "catalogue"
CATALOGUE = json.loads((ROOT / "fields.json").read_text())
FIELDS = {f["field_id"]: f for f in CATALOGUE["fields"]}
PROFILES = {p["profile_id"]: p for p in CATALOGUE["profiles"]}
RELATIONS = {r["predicate"]: r for r in json.loads((ROOT / "relations.json").read_text())["relations"]}
SECTIONS = {
    "sources": "source_id",
    "units": "unit_id",
    "objects": "object_id",
    "assertions": "assertion_id",
    "relations": "relation_id",
    "evidence": "evidence_id",
    "assets": "asset_id",
    "assessments": "assessment_id",
    "reviews": "review_id",
    "issues": "issue_id",
    "coverage": "coverage_id",
    "derivations": "derivation_id",
    "use_decisions": "decision_id",
    "changes": "change_id",
}


class ContractError(ValueError):
    pass


def strict_equal(left: Any, right: Any) -> bool:
    """Unlike Python equality, false is not zero; null and absent remain distinct."""
    return canonical(left) == canonical(right)


def _id(package: dict, section: str, identity: Any) -> str:
    return f"{package['header']['source_id']}:{section}:{serialization.fingerprint(identity)[:20]}"


def _append(package: dict, section: str, row: dict) -> str:
    key = SECTIONS[section]
    existing = next((r for r in package[section] if r[key] == row[key]), None)
    if existing is not None and not strict_equal(existing, row):
        raise ContractError(f"Conflicting identity in {section}: {row[key]}")
    if existing is None:
        package[section].append(row)
    return row[key]


def new_package(
    source_id: str, source_type: str, assets: list[dict], processing_run_id: str, record_revision_id: str | None = None
) -> dict:
    if not source_id or not assets:
        raise ContractError("A source identity and at least one registered asset are required")
    source_revision = (
        f"{source_id}:source:{serialization.fingerprint(sorted((a['asset_id'], a['sha256']) for a in assets))[:20]}"
    )
    revision = record_revision_id or f"{source_id}:record:{processing_run_id}"
    p = {key: [] for key in SECTIONS}
    p.update(
        header={
            "record_package_id": f"{source_id}:package",
            "record_revision_id": revision,
            "source_revision_id": source_revision,
            "source_id": source_id,
            "format_version": "1.0.0",
            "catalogue_version": "six-category-v1",
            "vocabulary_version": CATALOGUE["version"],
            "status": "candidate",
            "scope": "declared_extraction_scope",
            "processing_run_id": processing_run_id,
            "human_expert_review": False,
            "independent_experiment_validation": False,
            "is_blind_holdout": False,
            "nomad_schema_deployed": False,
            "profile_extensions": [],
        },
        legacy_manifest=None,
    )
    # A portable scientific package must not expose a machine-local filesystem path.
    p["assets"] = [{k: deepcopy(v) for k, v in a.items() if k not in ("local_path", "path")} for a in assets]
    p["sources"] = [
        {
            "source_id": source_id,
            "source_revision_id": source_revision,
            "title": None,
            "source_type": source_type,
            "asset_refs": [a["asset_id"] for a in assets],
            "metadata_origin": "registered_original_assets",
        }
    ]
    add_object(p, "source", source_id, object_id=f"{source_id}:source")
    add_coverage(p, "registered_assets", "not_checked", "Extraction has not assessed the declared scope")
    return p


def add_object(p: dict, kind: str, label: str, **extra) -> str:
    evidence = extra.pop("evidence_refs", [])
    oid = extra.pop("object_id", _id(p, "object", [kind, label, extra]))
    row = {
        "object_id": oid,
        "kind": kind,
        "label": label,
        "source_id": p["header"]["source_id"],
        "record_revision_id": p["header"]["record_revision_id"],
        "origin": "extraction",
        "evidence_refs": evidence,
        "legacy": None,
        **extra,
    }
    return _append(p, "objects", row)


def add_evidence(p: dict, asset_ref: str, anchor: dict, content: Any, content_kind: str = "verbatim", **extra) -> str:
    row = {
        "evidence_id": _id(p, "evidence", [asset_ref, anchor, content, content_kind]),
        "asset_ref": asset_ref,
        "anchor": deepcopy(anchor),
        "content": content,
        "content_kind": content_kind,
        **extra,
    }
    return _append(p, "evidence", row)


def _profile(p: dict, field: str, owner: str, qualifiers: dict) -> str:
    if field not in FIELDS:
        raise ContractError(f"Unregistered field: {field}")
    if owner not in FIELDS[field]["owner_kinds"]:
        raise ContractError(f"Field {field} cannot belong to {owner}")
    for profile in PROFILES.values():
        if profile["field_id"] == field and owner in profile["owner_kinds"] and profile["qualifiers"] == qualifiers:
            return profile["profile_id"]
    extension = {
        "profile_id": "EXT-" + serialization.fingerprint([field, owner, qualifiers])[:20],
        "field_id": field,
        "qualifiers": deepcopy(qualifiers),
        "owner_kinds": [owner],
        "definition": "Explicit extraction profile; original terminology is retained",
        "version": "1",
        "mapping_status": "requires_review",
    }
    if extension not in p["header"].setdefault("profile_extensions", []):
        p["header"]["profile_extensions"].append(extension)
    return extension["profile_id"]


def add_assertion(
    p: dict,
    subject_ref: str,
    field_id: str,
    raw_value: Any,
    evidence_refs: list[str],
    *,
    raw_unit=None,
    raw_basis=None,
    qualifiers=None,
    statement_kind="source_report",
    **extra,
) -> str:
    subject = next((o for o in p["objects"] if o["object_id"] == subject_ref), None)
    if subject is None:
        raise ContractError("Assertion subject must exist")
    qualifiers = qualifiers or {}
    profile = extra.pop("profile_id", None) or _profile(p, field_id, subject["kind"], qualifiers)
    kind = extra.pop("value_kind", None) or (
        "scalar"
        if isinstance(raw_value, (float, int)) and not isinstance(raw_value, bool)
        else "record"
        if isinstance(raw_value, dict)
        else "list"
        if isinstance(raw_value, list)
        else "text"
    )
    categories = {
        "source": "S",
        "material": "C",
        "composition": "C",
        "process": "P",
        "condition": "T",
        "observation": "T",
        "test": "T",
        "series": "T",
        "claim": "K",
    }
    groups = {g[0] for g in FIELDS[field_id].get("groups", []) if g}
    category = next(iter(groups)) if len(groups) == 1 else categories.get(field_id.split(".")[0], "E")
    aid = extra.pop(
        "assertion_id", _id(p, "assertion", [subject_ref, field_id, qualifiers, raw_value, raw_unit, evidence_refs])
    )
    row = {
        "assertion_id": aid,
        "subject_ref": subject_ref,
        "field_id": field_id,
        "profile_id": profile,
        "category": category,
        "legacy_group": None,
        "qualifiers": deepcopy(qualifiers),
        "source_context": {},
        "value": {
            "kind": kind,
            "raw_value": raw_value,
            "raw_unit": raw_unit,
            "raw_basis": raw_basis,
            "raw_origin": "source_extraction",
            "parsed": None,
            "normalized_value": None,
        },
        "formation": "extracted",
        "statement_kind": statement_kind,
        "source_revision_id": p["header"]["source_revision_id"],
        "evidence_refs": evidence_refs,
        "review_state": "unreviewed",
        "independent_validation": False,
        "use_status": "not_evaluated",
        "issue_refs": [],
        "legacy": None,
        **extra,
    }
    return _append(p, "assertions", row)


def add_relation(p: dict, from_ref: str, predicate: str, to_ref: str, evidence_refs: list[str], **extra) -> str:
    rid = extra.pop("relation_id", _id(p, "relation", [from_ref, predicate, to_ref, evidence_refs, extra]))
    return _append(
        p,
        "relations",
        {
            "relation_id": rid,
            "from_ref": from_ref,
            "predicate": predicate,
            "to_ref": to_ref,
            "source_revision_id": p["header"]["source_revision_id"],
            "evidence_refs": evidence_refs,
            "basis_kind": "source_explicit",
            "applicability": "as_reported",
            "use_status": "not_evaluated",
            "legacy": None,
            **extra,
        },
    )


def add_issue(p: dict, description: str, target_refs=None, evidence_refs=None, severity="warning", **extra) -> str:
    row = {
        "issue_id": _id(p, "issue", [description, target_refs, evidence_refs]),
        "severity": severity,
        "target_refs": target_refs or [],
        "description": description,
        "treatment": "retain_and_review",
        "resolution_status": "open",
        "evidence_refs": evidence_refs or [],
        **extra,
    }
    return _append(p, "issues", row)


def add_coverage(p: dict, scope: Any, handling: str, reason: str, **extra) -> str:
    row = {
        "coverage_id": _id(p, "coverage", scope),
        "source_revision_id": p["header"]["source_revision_id"],
        "scope": deepcopy(scope),
        "handling": handling,
        "reason": reason,
        **extra,
    }
    old = next((r for r in p["coverage"] if r["coverage_id"] == row["coverage_id"]), None)
    if old:
        p["coverage"].remove(old)
    return _append(p, "coverage", row)


def validate_package(p: dict) -> dict:
    try:
        return _validate_package(p)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ContractError("Malformed record structure or reference type") from exc


def _validate_package(p: dict) -> dict:
    errors = []

    def check(condition, message):
        if not condition:
            errors.append(message)

    check(isinstance(p, dict), "Package must be an object")
    if not isinstance(p, dict):
        raise ContractError("; ".join(errors))
    check(set(SECTIONS) | {"header", "legacy_manifest"} <= set(p), "Missing required sections")
    if errors:
        raise ContractError("; ".join(errors))
    header = p["header"]
    if not isinstance(header, dict):
        raise ContractError("Header must be an object")
    for key in (
        "source_id",
        "record_package_id",
        "record_revision_id",
        "source_revision_id",
        "format_version",
        "status",
    ):
        check(isinstance(header.get(key), str) and bool(header[key]), "Missing header " + key)
    v2_marked = (
        header.get("reading_plan_version") is not None
        or header.get("table_quality_version") == "tabular-quality-2"
        or header.get("typed_value_version") == "2"
        or "reading_plan" in header
        or any(
            isinstance(a, dict) and a.get("value", {}).get("typed_value_version") == "2"
            for a in p.get("assertions", [])
            if isinstance(p.get("assertions"), list)
        )
    )
    if v2_marked:
        check(
            header.get("reading_plan_version") == "2" and header.get("table_quality_version") == "tabular-quality-2",
            "Reading plan version requires matching v2 quality policy",
        )
    indices = {}
    for section, key in SECTIONS.items():
        rows = p[section]
        check(isinstance(rows, list), section + " must be an array")
        if not isinstance(rows, list):
            continue
        ids = [r.get(key) if isinstance(r, dict) else None for r in rows]
        check(all(isinstance(i, str) and i for i in ids), section + " identity missing")
        check(len(set(ids)) == len(ids), section + " duplicate identity")
        indices[section] = {r[key]: r for r in rows if isinstance(r, dict) and key in r}
    if errors:
        raise ContractError("; ".join(errors))
    all_ids = set().union(*(set(index) for index in indices.values()), {header["record_package_id"]})
    check(
        bool(p["sources"]) and bool(p["objects"]) and bool(p["assets"]) and bool(p["coverage"]),
        "Source, object, asset and coverage required",
    )
    profiles = dict(PROFILES)
    for ext in header.get("profile_extensions", []):
        field = FIELDS.get(ext.get("field_id"))
        check(ext.get("profile_id", "").startswith("EXT-"), "Extension identity must be namespaced")
        check(
            field is not None and set(ext.get("owner_kinds", [])) <= set(field["owner_kinds"]),
            "Invalid profile extension owner",
        )
        check(ext.get("profile_id") not in profiles, "Duplicate profile extension")
        profiles[ext.get("profile_id")] = ext
    for section in SECTIONS:
        for row in p[section]:
            if section not in ("changes", "use_decisions", "reviews"):
                for revision_key in ("source_revision_id", "record_revision_id"):
                    if revision_key in row:
                        check(row[revision_key] == header[revision_key], f"{section}: stale {revision_key}")
            for key, target in (
                ("evidence_refs", "evidence"),
                ("issue_refs", "issues"),
                ("input_assertion_refs", "assertions"),
            ):
                check(set(row.get(key, [])) <= set(indices[target]), f"{section}: dangling {key}")
            check(set(row.get("target_refs", [])) <= all_ids, f"{section}: dangling target")
    for source in p["sources"]:
        check(source.get("source_id") == header["source_id"], "Source identity disagrees with header")
        check(source.get("source_revision_id") == header["source_revision_id"], "Source revision disagrees with header")
        check(set(source.get("asset_refs", [])) <= set(indices["assets"]), "Source asset missing")
    for unit in p["units"]:
        check(unit.get("anchor_object_ref") in indices["objects"], "Unit anchor missing")
    for obj in p["objects"]:
        check(obj.get("source_id") == header["source_id"], "Cross-source object in package")
        for key in ("owner_ref", "material_ref", "about_subject_ref", "activity_ref"):
            check(not obj.get(key) or obj[key] in indices["objects"], "Object dangling " + key)
        if obj.get("kind") == "component":
            check(bool(obj.get("owner_ref")) and bool(obj.get("material_ref")), "Component owner/material required")
            owner = indices["objects"].get(obj.get("owner_ref"), {})
            material = indices["objects"].get(obj.get("material_ref"), {})
            check(owner.get("kind") in ("formulation", "patent_claim"), "Component owner must be formulation or claim")
            check(
                material.get("kind") in ("material", "formulation"),
                "Component input must be material or inner formulation",
            )
    for row in p["assertions"] + p["assessments"]:
        subject = indices["objects"].get(row.get("subject_ref", row.get("target_ref")))
        field, profile = FIELDS.get(row.get("field_id")), profiles.get(row.get("profile_id"))
        check(subject is not None, "Assertion/assessment subject missing")
        check(field is not None and profile is not None, "Unregistered field or profile")
        if subject and field and profile:
            check(profile.get("field_id") == row["field_id"], "Profile field mismatch")
            # An absence assessment belongs to an existing context; it must not
            # invent the missing observation/component merely to satisfy a type.
            if "value" in row:
                check(subject["kind"] in profile["owner_kinds"], "Field owner mismatch")
            check(
                all(row.get("qualifiers", {}).get(k) == v for k, v in profile["qualifiers"].items()),
                "Profile qualifier mismatch",
            )
            if "value" in row:
                value = row["value"]
                check(
                    {"kind", "raw_value", "raw_unit", "raw_basis", "raw_origin", "parsed", "normalized_value"}
                    <= set(value),
                    "Value members missing",
                )
                check(value.get("kind") in field["value_kinds"], "Unsupported value kind for field")
                check(value.get("raw_value") is not None, "Missing value must be an assessment")
                check(bool(row.get("evidence_refs")), "Assertion evidence required")
                if subject["kind"] == "patent_claim":
                    check(
                        row.get("statement_kind") != "source_reported_measurement",
                        "Patent claim cannot be measured result",
                    )
    for relation in p["relations"]:
        left, right = indices["objects"].get(relation.get("from_ref")), indices["objects"].get(relation.get("to_ref"))
        rule = RELATIONS.get(relation.get("predicate"))
        check(left is not None and right is not None, "Dangling relation endpoint")
        check(rule is not None, "Unregistered relation")
        if left and right and rule:
            check(
                left["kind"] in rule["allowed_from_kinds"] and right["kind"] in rule["allowed_to_kinds"],
                "Relation endpoint kind mismatch",
            )
            check(
                not rule.get("evidence_required") or bool(relation.get("evidence_refs")), "Relation evidence required"
            )
    for evidence in p["evidence"]:
        check(evidence.get("asset_ref") in indices["assets"], "Evidence asset missing")
        check(isinstance(evidence.get("anchor"), dict) and bool(evidence["anchor"]), "Evidence anchor required")
        page = evidence.get("anchor", {}).get("page")
        if page is not None:
            check(type(page) is int and page > 0, "PDF page is one-based")
    for asset in p["assets"]:
        check(isinstance(asset.get("sha256"), str) and len(asset["sha256"]) == 64, "Asset SHA-256 required")
    for derivation in p["derivations"]:
        check(derivation.get("output_assertion_ref") in indices["assertions"], "Derivation output missing")
    for decision in p["use_decisions"]:
        check(decision.get("scope_ref") in all_ids, "Use decision scope missing")
        if decision.get("status") == "allowed":
            check(
                bool(decision.get("review_refs")) and set(decision.get("review_refs", [])) <= set(indices["reviews"]),
                "Permission requires explicit review references",
            )
            check(
                decision.get("dependency_sha256") == decision_dependencies(p), "Permission dependency snapshot differs"
            )
            check(
                bool(decision.get("policy_version")) and bool(decision.get("reason")),
                "Permission policy and reason required",
            )
    if header.get("status") in ("complete", "verified"):
        check(
            all(
                c.get("handling") in ("checked", "extracted", "not_applicable", "source_uncertain")
                for c in p["coverage"]
            ),
            "Incomplete required coverage",
        )
        check(
            not any(i.get("severity") == "error" and i.get("resolution_status") != "resolved" for i in p["issues"]),
            "Blocking issue remains",
        )
    try:
        serialization.fingerprint(p)
    except (ValueError, TypeError):
        errors.append("Package contains non-JSON value or non-finite number")
    if errors:
        raise ContractError("; ".join(dict.fromkeys(errors)))
    if header.get("table_quality_version"):
        from .ingest.tables.table_quality import QUALITY_VERSION, verify_table_bindings

        if header["table_quality_version"] not in (QUALITY_VERSION, "tabular-quality-2"):
            raise ContractError("Unsupported table quality policy version")
        verify_table_bindings(p)
    return {
        "valid": True,
        "counts": {s: len(p[s]) for s in SECTIONS},
        "scientific_quality": "not_established_by_structure",
        "package_sha256": serialization.fingerprint(p),
    }


def decision_dependencies(p: dict) -> str:
    return serialization.fingerprint(
        {
            "record_revision_id": p["header"]["record_revision_id"],
            **{
                s: p[s]
                for s in (
                    "sources",
                    "objects",
                    "assertions",
                    "relations",
                    "evidence",
                    "assets",
                    "assessments",
                    "issues",
                    "coverage",
                    "derivations",
                )
            },
        }
    )


def can_use(p: dict, purpose: str, scope_ref: str | None = None, *, trusted_reviews=None) -> bool:
    """Fail closed unless external authority binds review to this exact dependency snapshot.

    Merely trusting an ID from an imported package would permit replay against a
    changed revision. Authority records must come from outside the scientific input.
    """
    if any(i.get("severity") == "error" and i.get("resolution_status") != "resolved" for i in p["issues"]):
        return False
    scope = scope_ref or p["header"]["record_package_id"]
    decisions = [
        d
        for d in p["use_decisions"]
        if d.get("purpose") == purpose
        and d.get("scope_ref") == scope
        and d.get("record_revision_id") == p["header"]["record_revision_id"]
    ]
    reviews = {r["review_id"]: r for r in p["reviews"]}
    expected = {
        "source_id": p["header"]["source_id"],
        "record_revision_id": p["header"]["record_revision_id"],
        "scope_ref": scope,
        "purpose": purpose,
        "dependency_sha256": decision_dependencies(p),
        "outcome": "approved",
    }

    def authorized(review_id):
        authority = (trusted_reviews or {}).get(review_id, {})
        return all(authority.get(key) == value for key, value in expected.items())

    return bool(decisions) and all(
        d.get("status") == "allowed"
        and d.get("dependency_sha256") == decision_dependencies(p)
        and bool(d.get("review_refs"))
        and all(authorized(r) for r in d["review_refs"])
        and all(reviews.get(r, {}).get("outcome") == "approved" for r in d["review_refs"])
        for d in decisions
    )
