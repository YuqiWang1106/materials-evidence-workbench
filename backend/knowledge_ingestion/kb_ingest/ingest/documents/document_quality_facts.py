"""Fact-level quality status: evidence support, observation scope, conflicts and derivations."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kb_ingest.core import fingerprint

from .document_quality_types import MappingSupportIndex, QualityProblem

_CANDIDATE_RELATIONS = {"candidate_correspondence", "candidate_same_as", "candidate_sample"}


@dataclass
class _AssessmentWork:
    package: dict[str, Any]
    evidence: dict[Hashable, dict[str, Any]]
    mapping_support: MappingSupportIndex
    blocked: set[Hashable] = field(default_factory=set)
    problems: list[QualityProblem] = field(default_factory=list)


def _item_is_supported(
    item: dict[str, Any], evidence: dict[Hashable, dict[str, Any]], mapping_support: MappingSupportIndex
) -> bool:
    if item.get("mapping_review_ref"):
        key = next((item[k] for k in ("object_id", "assertion_id", "relation_id") if k in item), None)
        return key in mapping_support
    return bool(item.get("evidence_refs")) and all(
        evidence.get(e, {}).get("support_review") in ("model_semantic_review", "human_source_review")
        for e in item["evidence_refs"]
    )


def _relation_is_eligible(
    relation: dict[str, Any], evidence: dict[Hashable, dict[str, Any]], mapping_support: MappingSupportIndex
) -> bool:
    return (
        _item_is_supported(relation, evidence, mapping_support)
        and relation["predicate"] not in _CANDIDATE_RELATIONS
        and relation.get("basis_kind")
        in ("source_explicit", "structural_expansion", "explicit_inheritance", "explicit_override")
        and relation.get("applicability")
        not in ("unknown", "not_applicable", "superseded", "requires_review", "candidate", "unresolved")
    )


def _observation_scope_blocks(
    package: dict[str, Any], valid_relations: Sequence[dict[str, Any]]
) -> tuple[set[Hashable], list[QualityProblem]]:
    blocked: set[Hashable] = set()
    problems: list[QualityProblem] = []
    for obj in package["objects"]:
        if obj["kind"] != "observation":
            continue
        oid = obj["object_id"]
        facts = [assertion for assertion in package["assertions"] if assertion["subject_ref"] == oid]
        if not facts:
            continue
        subjects = {
            relation["to_ref"]
            for relation in valid_relations
            if relation["from_ref"] == oid and relation["predicate"] == "observes_subject"
        }
        tests = {
            relation["from_ref"]
            for relation in valid_relations
            if relation["to_ref"] == oid and relation["predicate"] == "produces_observation"
        }
        test_subjects = {
            relation["to_ref"]
            for relation in valid_relations
            if relation["from_ref"] in tests and relation["predicate"] == "tests_subject"
        }
        test_subjects |= {
            relation["from_ref"]
            for relation in valid_relations
            if relation["to_ref"] in tests and relation["predicate"] == "tested_by"
        }
        measured = any(assertion["statement_kind"] == "source_reported_measurement" for assertion in facts)
        result = any(assertion["field_id"] == "observation.value" for assertion in facts)
        if (
            len(subjects | test_subjects) != 1
            or (subjects and test_subjects and subjects != test_subjects)
            or (result and (len(tests) != 1 or len(test_subjects) != 1))
        ):
            blocked.add(oid)
            problems.append({"target": oid, "reason": "observation needs unique consistent subject and test"})
        if measured and not any(
            relation["from_ref"] in tests and relation["predicate"] == "uses_method" for relation in valid_relations
        ):
            blocked.add(oid)
            problems.append({"target": oid, "reason": "measured test method is unresolved"})
    return blocked, problems


def _conflicting_assertion_ids(package: dict[str, Any]) -> tuple[set[Hashable], list[QualityProblem]]:
    groups: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for assertion in package["assertions"]:
        if assertion["field_id"] not in ("observation.value", "composition.amount", "condition.parameter"):
            continue
        value = assertion["value"]
        key = fingerprint(
            [
                assertion["subject_ref"],
                assertion["field_id"],
                assertion.get("qualifiers"),
                value.get("raw_unit"),
                value.get("raw_basis"),
            ]
        )
        raw = value.get("raw_value")
        try:
            comparison = str(Decimal(str(raw)).normalize())
        except InvalidOperation:
            comparison = fingerprint(raw)
        groups.setdefault(key, []).append((assertion, comparison))
    conflict_facts: set[Hashable] = set()
    problems: list[QualityProblem] = []
    for rows in groups.values():
        if len({value for _, value in rows}) > 1:
            refs = [assertion["assertion_id"] for assertion, _ in rows]
            conflict_facts.update(refs)
            problems.append({"targets": refs, "reason": "source_conflict_same_subject_field_conditions"})
    return conflict_facts, problems


def _assign_assertion_status(work: _AssessmentWork, conflict_facts: set[Hashable]) -> None:
    package = work.package
    objects = {obj["object_id"]: obj for obj in package["objects"]}
    for assertion in package["assertions"]:
        if (
            assertion["field_id"] == "observation.value"
            and objects[assertion["subject_ref"]]["kind"] != "observation"
            and assertion["statement_kind"] not in ("scope_claim", "author_interpretation")
        ):
            work.blocked.add(assertion["assertion_id"])
            work.problems.append(
                {
                    "target": assertion["assertion_id"],
                    "reason": "result requires a dedicated observation, unique subject and test",
                }
            )
        good = (
            _item_is_supported(assertion, work.evidence, work.mapping_support)
            and not {assertion["subject_ref"], assertion["assertion_id"]} & work.blocked
            and assertion["assertion_id"] not in conflict_facts
        )
        assertion["use_status"] = "document_verified" if good else "requires_review"
        assertion["review_state"] = (
            ("program_mapping_reviewed" if assertion.get("mapping_review_ref") else "model_source_reviewed")
            if good
            else "unreviewed"
        )


def _resolve_derived_assertions(package: dict[str, Any]) -> None:
    # Resolve from independent base facts. Starting with all outputs marked valid
    # would let a cycle self-certify without any independently supported input.
    valid = {
        assertion["assertion_id"]
        for assertion in package["assertions"]
        if assertion["use_status"] == "document_verified"
    }
    outputs = {derivation["output_assertion_ref"] for derivation in package["derivations"]}
    resolved = valid - outputs
    while True:
        new = {
            out
            for out in outputs & valid
            if all(
                derivation["input_assertion_refs"] and set(derivation["input_assertion_refs"]) <= resolved
                for derivation in package["derivations"]
                if derivation["output_assertion_ref"] == out
            )
        } - resolved
        if not new:
            break
        resolved |= new
    for assertion in package["assertions"]:
        if assertion["assertion_id"] in valid - resolved:
            assertion["use_status"] = "requires_review"
            assertion["review_state"] = "unreviewed"
