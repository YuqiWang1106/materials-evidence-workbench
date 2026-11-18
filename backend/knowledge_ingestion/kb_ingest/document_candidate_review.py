"""Pure validation for immutable Sol facts and separate human decisions."""

import math
import re
import time
from copy import deepcopy

from .core import fingerprint
from .document_program_contract import FACT as FACT_SCHEMA
from .document_program_contract import SAMPLE as SAMPLE_SCHEMA

SAMPLE = r"/studies/(?:0|[1-9][0-9]*)/samples/(?:0|[1-9][0-9]*)"
TEST = SAMPLE + r"/tests/(?:0|[1-9][0-9]*)"
FACT = re.compile(
    "(?:"
    + SAMPLE
    + r"/(?:composition|preparation)/(?:0|[1-9][0-9]*)|"
    + TEST
    + r"/(?:conditions|results)/(?:0|[1-9][0-9]*)|"
    + r"/shared_methods/(?:0|[1-9][0-9]*)/details/(?:0|[1-9][0-9]*)|"
    + r"/statements/(?:0|[1-9][0-9]*))"
)
CORRECTION_FIELDS = set(FACT_SCHEMA["properties"]) - {"ref", "evidence"} | {"material"}


def validate_correction(correction, pointer, target, decision):
    """Apply the converter's field shapes, never scientific interpretation."""
    schema = (
        SAMPLE_SCHEMA["properties"]["composition"]["items"] if pointer.split("/")[-2] == "composition" else FACT_SCHEMA
    )
    properties = schema["properties"]

    def valid(value, rule):
        kinds = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        checks = {
            "string": isinstance(value, str),
            "null": value is None,
            "number": type(value) is int or (type(value) is float and math.isfinite(value)),
            "array": isinstance(value, list),
        }
        if not any(checks.get(kind, False) for kind in kinds):
            return False
        if "enum" in rule and value not in rule["enum"]:
            return False
        return not isinstance(value, list) or all(valid(item, rule["items"]) for item in value)

    for key, value in correction.items():
        if key not in properties or key not in CORRECTION_FIELDS or not valid(value, properties[key]):
            raise ValueError("correction has an invalid field, type or enum: " + key)
    # Incomplete model facts remain visible as uncertain. Accepted facts must
    # satisfy the same shape the converter will require after applying edits.
    if decision in ("confirmed", "corrected"):
        effective = {**target, **correction}
        allowed = {
            **properties,
            **({"about": {"type": "array", "items": {"type": "string"}}} if pointer.startswith("/statements/") else {}),
        }
        for key, value in effective.items():
            if key not in allowed or not valid(value, allowed[key]):
                raise ValueError("correction required for invalid candidate field: " + key)
        for key in ("value", "material"):
            if key in properties and key not in effective:
                raise ValueError("correction required for missing candidate field: " + key)


def resolve(raw, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("candidate reference must be a JSON pointer")
    target = raw
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < len(target):
            target = target[int(key)]
        elif isinstance(target, dict) and key in target:
            target = target[key]
        else:
            raise ValueError("candidate reference does not resolve")
    return target


def ownership(raw, pointer, target_ref):
    """Only explicit, same-group sample/test identities are valid owners."""
    category = pointer.split("/")[-2]
    pattern = (
        SAMPLE
        if category in ("composition", "preparation")
        else TEST
        if category in ("conditions", "results")
        else None
    )
    if pattern is None or not isinstance(target_ref, str) or not re.fullmatch(pattern, target_ref):
        raise ValueError("ownership target must be a matching sample or test in this candidate group")
    target = resolve(raw, target_ref)
    if not isinstance(target, dict) or not isinstance(target.get("ref"), str) or not target["ref"]:
        raise ValueError("ownership target must have an existing object identity")
    if category in target and not isinstance(target[category], list):
        raise ValueError("ownership target has an invalid fact collection")
    return pointer.rsplit("/", 2)[0]


def review_row(group, item, operator, source_hashes, *, allow_excluded=False):
    if not isinstance(item, dict) or set(item) - {
        "candidate_ref",
        "decision",
        "note",
        "correction",
        "ownership_target_ref",
    }:
        raise ValueError("invalid candidate review item")
    pointer, decision = item.get("candidate_ref"), item.get("decision")
    if decision not in ("confirmed", "corrected", "uncertain") and not (allow_excluded and decision == "excluded"):
        raise ValueError("unsupported candidate review decision")
    supplied = item.get("correction")
    if supplied is not None and (not isinstance(supplied, dict) or set(supplied) - CORRECTION_FIELDS):
        raise ValueError("correction may only change displayable scientific fields")
    if not isinstance(pointer, str) or not FACT.fullmatch(pointer):
        raise ValueError("review target must be an individual Sol fact")
    target = resolve(group["raw"], pointer)
    prior = next((row for row in group.get("reviews", []) if row.get("candidate_ref") == pointer), {})
    note = item.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("invalid review note")
    correction = deepcopy(prior.get("correction", {})) if supplied is None else deepcopy(supplied)
    if not isinstance(correction, dict) or set(correction) - CORRECTION_FIELDS:
        raise ValueError("correction may only change displayable scientific fields")
    if not isinstance(target, dict):
        # An invalid source member still belongs to its column. It may only be
        # excluded, never repaired into invented content or moved to an owner.
        if (
            decision != "excluded"
            or correction
            or item.get("ownership_target_ref") is not None
            or prior.get("ownership_target_ref")
        ):
            raise ValueError("non-object candidate facts may only be excluded without edits")
        target = {}
    validate_correction(correction, pointer, target, decision)
    owner = item.get("ownership_target_ref") or prior.get("ownership_target_ref")
    original_owner = ownership(group["raw"], pointer, owner) if owner else None
    # A category can explicitly undo a prior correction in the same atomic
    # submission. Validation above still requires the restored source shape.
    clears_correction = allow_excluded and supplied == {} and bool(prior.get("correction"))
    if decision == "confirmed" and (
        (supplied is not None and correction != prior.get("correction", {}) and not clears_correction)
        or (owner != prior.get("ownership_target_ref"))
    ):
        raise ValueError("new edits require a corrected or uncertain decision")
    if decision == "corrected" and not correction and not owner:
        raise ValueError("corrected review requires corrected fields or ownership")
    from .document_issue_scope import project_issue_annotations

    annotations = group.get("issue_annotations") or project_issue_annotations(group["raw"])
    return {
        "candidate_id": group["candidate_id"],
        "candidate_ref": pointer,
        "decision": decision,
        "issue_scope_sha256": annotations["field_sha256"][pointer],
        "note": note,
        "operator": operator.strip(),
        "correction": correction,
        "original": {key: deepcopy(target.get(key)) for key in correction},
        "original_present": {key: key in target for key in correction},
        "model_ref": target.get("ref") if isinstance(target.get("ref"), str) else None,
        "ownership_target_ref": owner,
        "original_owner_ref": original_owner,
        "candidate_sha256": fingerprint(group["raw"]),
        "source_sha256": source_hashes,
        "updated_at": time.time(),
        "scientific_validation": False,
    }


def review_scopes(raw, reviews=()):
    """Canonical, read-only ownership projection; source pointers are identities.

    Ambiguous model refs never resolve by label. Shared facts have exactly one
    home regardless of applies_to. Invalid fact shapes stay visible for repair.
    """
    scopes = []
    owners = {}
    identities = {}

    def rows(value):
        return value if isinstance(value, list) else []

    def add(kind, pointer, label):
        scope = dict(kind=kind, source_ref=pointer, label=label, candidate_refs=[])
        scopes.append(scope)
        return scope

    def identity(node, owner):
        if isinstance(node, dict) and isinstance(node.get("ref"), str) and node["ref"]:
            identities.setdefault(node["ref"], []).append(owner)

    def facts(node, field, pointer, scope):
        for i, fact in enumerate(rows(node.get(field))):
            # Facts share the response-local ref namespace, but are not owners.
            # Count them for ambiguity without inferring an owning sample.
            identity(fact, None)
            owners[f"{pointer}/{field}/{i}"] = scope

    for si, study in enumerate(rows(raw.get("studies"))):
        if not isinstance(study, dict):
            continue
        identity(study, f"/studies/{si}")
        for i, sample in enumerate(rows(study.get("samples"))):
            if not isinstance(sample, dict):
                continue
            pointer = f"/studies/{si}/samples/{i}"
            scope = add("sample", pointer, sample.get("label"))
            identity(sample, pointer)
            for field in ("composition", "preparation"):
                facts(sample, field, pointer, scope)
            for j, test in enumerate(rows(sample.get("tests"))):
                if isinstance(test, dict):
                    identity(test, pointer)
                    for field in ("conditions", "results"):
                        facts(test, field, f"{pointer}/tests/{j}", scope)
    for i, method in enumerate(rows(raw.get("shared_methods"))):
        if isinstance(method, dict):
            pointer = f"/shared_methods/{i}"
            identity(method, pointer)
            facts(method, "details", pointer, add("shared_method", pointer, method.get("label")))
    cross = add("cross_sample_statements", "/statements", "跨样品陈述")
    unclassified = add("unclassified", "/", "待归属内容")
    samples = {s["source_ref"]: s for s in scopes if s["kind"] == "sample"}
    methods = {s["source_ref"]: s for s in scopes if s["kind"] == "shared_method"}
    for statement in rows(raw.get("statements")):
        identity(statement, None)
    for i, statement in enumerate(rows(raw.get("statements"))):
        about = statement.get("about") if isinstance(statement, dict) else None
        resolved = []
        if (
            isinstance(about, list)
            and about
            and all(isinstance(ref, str) and len(identities.get(ref, [])) == 1 for ref in about)
        ):
            resolved = list({identities[ref][0] for ref in about})
        scope = unclassified
        if resolved and all(
            isinstance(ref, str) and (ref in samples or re.fullmatch(r"/studies/(?:0|[1-9][0-9]*)", ref))
            for ref in resolved
        ):
            scope = samples[resolved[0]] if len(resolved) == 1 and resolved[0] in samples else cross
        elif len(resolved) == 1 and resolved[0] in methods:
            scope = methods[resolved[0]]
        owners[f"/statements/{i}"] = scope
    for row in reviews:
        pointer, target = row.get("candidate_ref"), row.get("ownership_target_ref")
        if target and pointer in owners:
            try:
                ownership(raw, pointer, target)
                sample_pointer = target.split("/tests/")[0]
                owners[pointer] = samples[sample_pointer]
            except (ValueError, KeyError):
                owners[pointer] = unclassified
    for pointer, scope in owners.items():
        scope["candidate_refs"].append(pointer)
    return scopes


def scope_members(scopes, selector):
    if (
        not isinstance(selector, dict)
        or not {"kind", "source_ref"} <= set(selector)
        or set(selector) - {"kind", "source_ref", "category"}
    ):
        raise ValueError("invalid review scope selector")
    if "category" in selector and selector["category"] not in ("composition", "preparation", "tests", "statements"):
        raise ValueError("invalid review scope category")
    scope = next(
        (s for s in scopes if s["kind"] == selector["kind"] and s["source_ref"] == selector["source_ref"]), None
    )
    if scope is None:
        raise ValueError("review scope not found")
    members = set(scope["candidate_refs"])
    if "category" in selector:
        fields = {
            "composition": ("composition",),
            "preparation": ("preparation",),
            "tests": ("conditions", "results", "details"),
            "statements": ("statements",),
        }
        members = {pointer for pointer in members if pointer.split("/")[-2] in fields[selector["category"]]}
    return members
