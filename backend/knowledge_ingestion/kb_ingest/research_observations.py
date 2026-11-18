"""Small read-only task semantics. Source assertions and exact queries stay raw."""

from collections.abc import Mapping

from .contract import PROFILES
from .core import fingerprint
from .knowledge_contracts import RecordResult
from .knowledge_domain import load_domain_profile
from .typed_values import parse_typed_value

OWNER = (
    "workspace_id",
    "corpus_snapshot_id",
    "task_id",
    "source_id",
    "source_revision_id",
    "record_revision_id",
    "record_snapshot_id",
    "package_sha256",
)
# Changes to these gates require a policy version change. The complete policy and
# declared rule contents are hashed in saved task inputs, not in user targets.
POLICY = {
    "version": "research-observations/1",
    "statement_kinds": ["measured", "source_report", "source_reported_measurement"],
    "observation_owners": ["observation", "reported_test", "instrument_run"],
    "required": ["test_owner", "subject", "evidence", "single_typed_value", "unit_basis", "no_conflicting_values"],
    "mapping": "whole_observed_tuple_to_declared_selector",
    "reference_grouping": "whole_catalogue_tuple_to_declared_selector",
    "scientific_validation": False,
}


def _rule(value, rules):
    return next(
        (r for r in rules if value.field_id == r.observed.field_id and value.qualifiers == r.observed.qualifiers), None
    )


def matches_selector(value, field_id, qualifiers, *, rules=None):
    if value.field_id != field_id:
        return False
    if all(value.qualifiers.get(k) == v for k, v in qualifiers.items()):
        return True
    rule = _rule(value, load_domain_profile().observed_mappings if rules is None else rules)
    # Do not merge source extras into a narrower canonical selector, or invent
    # a target condition absent from the declaration. Exact tuple means exact.
    return bool(rule and all(rule.canonical.qualifiers.get(k) == v for k, v in qualifiers.items()))


def observation_reason(row, value):
    """Shared eligibility, before numeric comparison or reference rendering."""
    if value.field_id in {"observation.value", "condition.parameter", "method.standard"} and row.ref.test_ref is None:
        return "test_unconfirmed"
    if value.subject_kind in {"reported_test", "instrument_run"} and value.subject_ref != row.ref.test_ref:
        return "test_owner_mismatch"
    if value.statement_kind not in POLICY["statement_kinds"]:
        return "not_observed_fact"
    if value.field_id == "observation.value" and value.subject_kind not in POLICY["observation_owners"]:
        return "not_observed_fact"
    if not value.evidence_refs or value.subject_ref is None:
        return "evidence_or_owner_missing"
    return None


def scalar(value, expected):
    if value.typed_values is not None:
        return None, "non_scalar_measurement"
    if value.typed_value_version is not None:
        if value.typed_value_version != "2" or not isinstance(value.typed_value, dict):
            return None, "typed_value_unverified"
        typed: Mapping[str, object] = value.typed_value
    else:
        typed = parse_typed_value(value.raw_value, expected=expected)
    if typed.get("kind") not in ({"decimal"} if expected == "number" else {"category", "text"}):
        return None, "non_scalar_measurement"
    if typed.get("status") != "valid":
        return None, "typed_value_unverified"
    return typed, None


def _reference_declarations(profile):
    return [
        {"mapping": m.model_dump(), "profile_qualifiers": PROFILES[m.catalogue_profile_id]["qualifiers"]}
        for m in profile.mappings
        if m.selector.field_id == "observation.value"
    ]


def _reference_qualifiers(value, rules, declarations):
    rule = _rule(value, rules)
    if rule:
        return rule.canonical.qualifiers
    # Only complete, explicitly catalogued spellings are display aliases. A
    # temperature/scope/other qualifier is not discarded to force a match.
    selectors = {
        fingerprint(d["mapping"]["selector"]): d["mapping"]["selector"]["qualifiers"]
        for d in declarations
        if value.field_id == d["mapping"]["selector"]["field_id"] and value.qualifiers == d["profile_qualifiers"]
    }
    if len(selectors) > 1:
        raise ValueError("Ambiguous reference selector")
    return next(iter(selectors.values())) if selectors else value.qualifiers


def reference_values(raw):
    """Only independently usable scalar groups; conflicting observations stay gaps."""
    row = RecordResult.model_validate(raw)
    groups = {}
    result = []
    profile = load_domain_profile()
    rules = profile.observed_mappings
    declarations = _reference_declarations(profile)
    for value in row.values:
        if value.field_id != "observation.value":
            continue
        qualifiers = _reference_qualifiers(value, rules, declarations)
        groups.setdefault(fingerprint([value.field_id, qualifiers]), []).append(value)
    for values in groups.values():
        identities = []
        valid = True
        for value in values:
            expected = (
                "category"
                if value.value_type in {"category", "text"}
                or (isinstance(value.typed_value, dict) and value.typed_value.get("kind") in {"category", "text"})
                else "number"
            )
            typed, reason = scalar(value, expected)
            if observation_reason(row, value) or reason or (expected == "number" and not value.unit):
                valid = False
                break
            identities.append(
                (typed.get("decimal") if typed["kind"] == "decimal" else typed.get("raw"), value.unit, value.basis)
            )
        if valid and len(set(identities)) == 1:
            result.extend(v.model_dump(mode="json") for v in values)
    return result


def semantic_inputs(rows):
    profile = load_domain_profile()
    rules = profile.observed_mappings
    applications = []
    for row in rows:
        for value in row.values:
            rule = _rule(value, rules)
            if rule is None:
                continue
            applications.append(
                {
                    "fact_ref": {
                        "kind": "assertion",
                        **{k: getattr(row.ref, k) for k in OWNER},
                        "assertion_id": value.assertion_id,
                    },
                    "observed": rule.observed.model_dump(),
                    "canonical": rule.canonical.model_dump(),
                    "rule_id": rule.id,
                    "rule_version": rule.version,
                    "rule_sha256": rule.sha256,
                }
            )
    return {
        "policy": POLICY.copy(),
        "rules": [r.model_dump() for r in rules],
        "applications": applications,
        "reference_declarations": _reference_declarations(profile),
    }
