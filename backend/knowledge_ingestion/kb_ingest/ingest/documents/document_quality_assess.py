"""Package-level document quality assessment, publication and definite-package gates."""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from copy import deepcopy
from typing import Any, Literal

from kb_ingest.core import fingerprint

from .document_quality import VERSION
from .document_quality_facts import (
    _AssessmentWork,
    _assign_assertion_status,
    _conflicting_assertion_ids,
    _item_is_supported,
    _observation_scope_blocks,
    _relation_is_eligible,
    _resolve_derived_assertions,
)
from .document_quality_types import (
    MappingDependencyIndex,
    MappingSupportIndex,
    ProgramQualityReport,
    QualityProblem,
    QualityReport,
    json_member,
)

# A dependency points from the supporting object to its consumer. Never traverse
# both ways: independent samples can legitimately share the same method/material.
_FROM_DEPENDS_ON_TO = {
    "observes_subject",
    "tests_subject",
    "characterizes",
    "uses_method",
    "contains_component",
    "refers_to_material",
    "process_applicability",
    "method_applicability",
    "has_formulation",
    "contains_material",
    "has_component",
    "contains_prepared_flame_retardant",
    "scales_component_ratio_from",
    "derived_from_formulation",
    "inherits_from",
    "uses_input",
    "uses_equipment",
    "inherits_equipment",
    "inherits_test_method",
    "uses_filler_treatment",
    "surface_treated_by",
    "concerns",
    "interprets",
    "about_study",
    "about_sample",
    "depicts_sample",
    "depicts_material",
    "author_supported_by",
}
_TO_DEPENDS_ON_FROM = {
    "produces_observation",
    "recipe_for",
    "tested_by",
    "has_series",
    "reports_output",
    "contains",
    "contains_claim",
    "contains_study",
    "contains_interpretation",
    "reports",
    "defines_formulation",
    "reports_product",
    "defines_internal_flame_retardant",
    "contains_sample",
    "contains_formulation",
    "contains_figure",
    "contains_curve",
}
_CONTEXT_FIELDS = {
    "material.identity",
    "condition.parameter",
    "method.name",
    "method.standard",
    "method.instrument",
    "observation.statement_kind",
    "activity.identifier",
}


def _block_owned_objects(package: dict[str, Any], blocked: set[Hashable]) -> None:
    for obj in package["objects"]:
        if any(obj.get(key) in blocked for key in ("owner_ref", "material_ref", "about_subject_ref", "activity_ref")):
            blocked.add(obj["object_id"])


def _block_dependent_relations(
    package: dict[str, Any], blocked: set[Hashable], from_depends_on_to: set[str], to_depends_on_from: set[str]
) -> None:
    for relation in package["relations"]:
        if relation["predicate"] in from_depends_on_to:
            support, consumer = relation["to_ref"], relation["from_ref"]
        elif relation["predicate"] in to_depends_on_from:
            support, consumer = relation["from_ref"], relation["to_ref"]
        else:
            continue
        if support in blocked or relation["relation_id"] in blocked:
            blocked.add(consumer)


def _block_facts_and_derivations(package: dict[str, Any], blocked: set[Hashable], context_fields: set[str]) -> None:
    for assertion in package["assertions"]:
        if assertion["subject_ref"] in blocked:
            blocked.add(assertion["assertion_id"])
        if assertion["assertion_id"] in blocked and assertion["field_id"] in context_fields:
            blocked.add(assertion["subject_ref"])
    for derivation in package["derivations"]:
        if set(derivation["input_assertion_refs"]) & blocked:
            blocked.add(derivation["output_assertion_ref"])


def _expand_quality_invalidations(
    package: dict[str, Any], blocked: set[Hashable], mapping_dependencies: MappingDependencyIndex
) -> set[Hashable]:
    for issue in package["issues"]:
        if issue["issue_id"] in blocked:
            blocked.update(issue.get("target_refs", []))
    _block_owned_objects(package, blocked)
    _block_dependent_relations(package, blocked, _FROM_DEPENDS_ON_TO, _TO_DEPENDS_ON_FROM)
    _block_facts_and_derivations(package, blocked, _CONTEXT_FIELDS)
    for consumer, dependencies in mapping_dependencies.items():
        if dependencies & blocked:
            blocked.add(consumer)
    return blocked


def invalidated_references(package: dict[str, Any], target_refs: Iterable[Hashable]) -> set[Hashable]:
    """Return precise downstream invalidation for object, edge, issue and fact targets.

    A rejected attribution edge invalidates the consumer (test/observation), not
    its independent supporting method/sample. Comparison edges have no identity
    implication and therefore never invalidate either endpoint.
    """
    if json_member(package["header"], "document_extraction_mode") == "direct-six-v1":
        from kb_ingest.document_direct_revocation import direct_invalidated_references

        blocked: set[Hashable] = direct_invalidated_references(package, target_refs)
        return blocked
    from kb_ingest.document_mapping_proof import mapping_dependency_index

    mapping_dependencies = mapping_dependency_index(package)
    blocked = set(target_refs)
    while True:
        previous = set(blocked)
        blocked = _expand_quality_invalidations(package, blocked, mapping_dependencies)
        if blocked == previous:
            return blocked


def package_content_hash(package: dict[str, Any]) -> str:
    p = deepcopy(package)
    p["header"].pop("document_quality", None)
    return fingerprint(p)


def _unresolved_with_open_errors(package: dict[str, Any], unresolved: Sequence[object]) -> list[object]:
    return list(unresolved) + [
        {"issue_ref": issue["issue_id"], "reason": issue["description"]}
        for issue in package["issues"]
        if issue.get("severity") == "error" and issue.get("resolution_status") != "resolved"
    ]


def _review_disagreements(package: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        issue
        for issue in package["issues"]
        if json_member(package["header"], "document_extraction_mode") == "direct-six-v1"
        and issue.get("category") == "extraction_uncertain"
        and issue.get("severity") == "error"
        and issue.get("source_context", {}).get("origin") == "direct_review_disagreement"
        and issue.get("resolution_status") != "resolved"
    ]


def _initial_blocks(
    package: dict[str, Any], evidence: dict[Hashable, dict[str, Any]], mapping_support: MappingSupportIndex
) -> tuple[set[Hashable], list[QualityProblem]]:
    blocked: set[Hashable] = set()
    problems: list[QualityProblem] = []
    for issue in _review_disagreements(package):
        blocked.update(issue.get("target_refs", []))
        problems.append(
            {
                "targets": issue.get("target_refs", []),
                "issue_ref": issue["issue_id"],
                "reason": "independent_source_reviews_disagree",
            }
        )
    for issue in package["issues"]:
        if issue.get("category") == "source_conflict" and issue.get("resolution_status") != "resolved":
            blocked.update(issue.get("target_refs", []))
            problems.append({"targets": issue.get("target_refs", []), "reason": "source_conflict_requires_review"})
    for obj in package["objects"]:
        if obj["kind"] not in ("source", "content_unit") and not _item_is_supported(obj, evidence, mapping_support):
            blocked.add(obj["object_id"])
    blocked.update(
        assertion["assertion_id"]
        for assertion in package["assertions"]
        if not _item_is_supported(assertion, evidence, mapping_support)
    )
    blocked.update(
        relation["relation_id"]
        for relation in package["relations"]
        if not _relation_is_eligible(relation, evidence, mapping_support)
    )
    return blocked, problems


def _mark_relations(
    package: dict[str, Any],
    blocked: set[Hashable],
    evidence: dict[Hashable, dict[str, Any]],
    mapping_support: MappingSupportIndex,
) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for relation in package["relations"]:
        good = (
            _relation_is_eligible(relation, evidence, mapping_support)
            and not {
                relation["relation_id"],
                relation["from_ref"],
                relation["to_ref"],
            }
            & blocked
        )
        relation["use_status"] = "document_verified" if good else "requires_review"
        if good:
            valid.append(relation)
    return valid


def _block_relations_with_blocked_ends(package: dict[str, Any], blocked: set[Hashable]) -> None:
    for relation in package["relations"]:
        if {relation["from_ref"], relation["to_ref"]} & blocked:
            relation["use_status"] = "requires_review"


def _apply_disagreement_assessments(package: dict[str, Any]) -> None:
    disagreement_targets = {
        issue["issue_id"]: invalidated_references(package, issue.get("target_refs", []))
        for issue in _review_disagreements(package)
    }
    for assessment in package["assessments"]:
        issues = [
            issue
            for issue, targets in disagreement_targets.items()
            if {assessment["assessment_id"], assessment["subject_ref"]} & targets
        ]
        if not issues:
            continue
        context = assessment.setdefault("source_context", {})
        # Keep the first source-review receipt as history, not current certainty.
        context.setdefault(
            "prior_source_review",
            {
                **{key: assessment.get(key) for key in ("handling", "status", "review_state")},
                "missing_status": context.get("missing_status"),
            },
        )
        context["review_disagreement_refs"] = issues
        context["missing_status"] = "uncertain"
        assessment.update(handling="extraction_uncertain", status="unresolved", review_state="unresolved")


def _quality_report(
    package: dict[str, Any],
    *,
    read_gaps: Sequence[object],
    unresolved: Sequence[object],
    problems: Sequence[object],
) -> QualityReport:
    good = sum(assertion["use_status"] == "document_verified" for assertion in package["assertions"])
    outcome: Literal["complete", "partial"] = (
        "complete"
        if good and good == len(package["assertions"]) and not read_gaps and not unresolved and not problems
        else "partial"
    )
    package["header"]["document_pipeline_version"] = "2"
    package["header"]["status"] = "candidate" if outcome == "complete" else "partial"
    package["header"]["processing_summary"] = {
        "outcome": outcome,
        "verified_assertions": good,
        "unresolved_assertions": len(package["assertions"]) - good,
        "human_expert_review": False,
    }
    report: QualityReport = {
        "version": VERSION,
        "outcome": outcome,
        "reading": {"unresolved": read_gaps},
        "semantics_evidence_coverage": {"unresolved": unresolved},
        "relations": {"unresolved": problems},
        "verified_assertions": good,
        "human_expert_review": False,
        "scientific_validation": False,
        "package_content_sha256": package_content_hash(package),
    }
    package["header"]["document_quality"] = report
    return report


def assess_package(
    package: dict[str, Any], *, read_gaps: Sequence[object], unresolved: Sequence[object]
) -> QualityReport | ProgramQualityReport:
    if json_member(package["header"], "document_extraction_mode") == "program-only-v1":
        from kb_ingest.document_program_quality import assess_program_package

        return assess_program_package(package, read_gaps=read_gaps, unresolved=unresolved)
    from kb_ingest.document_mapping_proof import mapping_support_index

    unresolved = _unresolved_with_open_errors(package, unresolved)
    work = _AssessmentWork(
        package,
        {entry["evidence_id"]: entry for entry in package["evidence"]},
        mapping_support_index(package),
    )
    work.blocked, work.problems = _initial_blocks(package, work.evidence, work.mapping_support)
    work.blocked = invalidated_references(package, work.blocked)
    valid_relations = _mark_relations(package, work.blocked, work.evidence, work.mapping_support)
    # An observation is one result under one declared test/condition scope.
    # Conflicts remain stored; they do not become two interchangeable definite values.
    extra_blocked, extra_problems = _observation_scope_blocks(package, valid_relations)
    work.blocked.update(extra_blocked)
    work.problems.extend(extra_problems)
    conflict_facts, conflict_problems = _conflicting_assertion_ids(package)
    work.problems.extend(conflict_problems)
    work.blocked = invalidated_references(package, work.blocked | conflict_facts)
    _assign_assertion_status(work, conflict_facts)
    _resolve_derived_assertions(package)
    _block_relations_with_blocked_ends(package, work.blocked)
    _apply_disagreement_assessments(package)
    return _quality_report(package, read_gaps=read_gaps, unresolved=unresolved, problems=work.problems)


def check_publication(package: dict[str, Any], expected_hash: object) -> object:
    # Stored header quality is an unvalidated JSON field, not a reconstructed TypedDict.
    header = package["header"]
    quality = json_member(header, "document_quality", {})
    if not expected_hash or fingerprint(package) != expected_hash:
        raise ValueError("result preview version changed")
    if json_member(header, "document_extraction_mode") == "program-only-v1":
        if json_member(header, "review_policy") == "human_reviewed_candidate_v1":
            eligible = sum(a.get("use_status") == "human_reviewed" for a in package["assertions"])
            if (
                json_member(quality, "version") != "human_reviewed_candidate_v1"
                or json_member(quality, "package_content_sha256") != package_content_hash(package)
                or json_member(quality, "reviewed_assertions") != eligible
                or not eligible
            ):
                raise ValueError("human-reviewed candidate package is absent or stale")
            return quality
        from kb_ingest.document_program_quality import VERSION as PROGRAM_VERSION

        if (
            json_member(header, "review_policy") != "program_only_v1"
            or json_member(quality, "version") != PROGRAM_VERSION
            or json_member(quality, "package_content_sha256") != package_content_hash(package)
        ):
            raise ValueError("document quality report is absent or stale")
        eligible = sum(
            a.get("use_status") == "program_checked" and a.get("program_check", {}).get("status") == "program_checked"
            for a in package["assertions"]
        )
        if not eligible or json_member(quality, "program_checked_assertions") != eligible:
            raise ValueError("no program_checked facts to publish")
        return quality
    if json_member(quality, "version") != VERSION or json_member(
        quality, "package_content_sha256"
    ) != package_content_hash(package):
        raise ValueError("document quality report is absent or stale")
    if not json_member(quality, "verified_assertions"):
        raise ValueError("no verified facts to publish")
    return quality


def definite_package(package: dict[str, Any]) -> dict[str, Any]:
    header = package["header"]
    if json_member(header, "document_pipeline_version") != "2":
        return package
    quality = json_member(header, "document_quality", {})
    if json_member(quality, "package_content_sha256") != package_content_hash(package):
        raise ValueError("document quality report changed; revalidation required")
    result = deepcopy(package)
    if json_member(header, "document_extraction_mode") == "program-only-v1":
        use_status = (
            "human_reviewed"
            if json_member(header, "review_policy") == "human_reviewed_candidate_v1"
            else "program_checked"
        )
        result["assertions"] = [a for a in result["assertions"] if a.get("use_status") == use_status]
        result["relations"] = [r for r in result["relations"] if r.get("use_status") == use_status]
        valid = {a["assertion_id"] for a in result["assertions"]}
        result["derivations"] = [
            d
            for d in result["derivations"]
            if d["output_assertion_ref"] in valid and set(d["input_assertion_refs"]) <= valid
        ]
        return result
    result["assertions"] = [a for a in result["assertions"] if a.get("use_status") == "document_verified"]
    result["relations"] = [r for r in result["relations"] if r.get("use_status") == "document_verified"]
    valid = {a["assertion_id"] for a in result["assertions"]}
    result["derivations"] = [
        d
        for d in result["derivations"]
        if d["output_assertion_ref"] in valid and set(d["input_assertion_refs"]) <= valid
    ]
    return result
