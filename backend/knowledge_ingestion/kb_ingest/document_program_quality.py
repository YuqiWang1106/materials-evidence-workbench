"""Conservative structural checks for program-only document extraction."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .ingest.documents.document_quality_assess import invalidated_references, package_content_hash
from .ingest.documents.document_quality_types import ProgramQualityReport, QualityProblem, json_member

VERSION = "document-program-quality-1"
_CANDIDATE_PREDICATES = {"candidate_correspondence", "candidate_same_as", "candidate_sample"}
_INELIGIBLE_APPLICABILITY = {"unknown", "not_applicable", "superseded", "requires_review", "candidate", "unresolved"}


@dataclass
class _ProgramWork:
    package: dict[str, Any]
    evidence: set[Hashable]
    objects: set[Hashable]
    invalid: set[Hashable] = field(default_factory=set)
    problems: list[QualityProblem] = field(default_factory=list)


def _apply_program_check(work: _ProgramWork, item: dict[str, Any], ref: object) -> str:
    verdict = item.get("program_check") or {}
    status = verdict.get("status", "needs_attention")
    if status not in ("program_checked", "needs_attention", "blocked"):
        status = "blocked"
    refs = item.get("evidence_refs", [])
    if not refs or not set(refs) <= work.evidence:
        status = "blocked"
        work.problems.append({"target": ref, "reason": "missing_or_invalid_evidence_ref"})
    if status != "program_checked":
        work.invalid.add(ref)
    item["program_check"] = {"status": status, "reasons": list(verdict.get("reasons", []))}
    return status


def _program_check_records(work: _ProgramWork) -> None:
    package = work.package
    for obj in package["objects"]:
        if obj["kind"] not in ("source", "content_unit"):
            if not obj.get("evidence_refs") or not set(obj["evidence_refs"]) <= work.evidence:
                work.invalid.add(obj["object_id"])
                work.problems.append({"target": obj["object_id"], "reason": "missing_or_invalid_evidence_ref"})
            if obj.get("program_check") and obj["program_check"].get("status") != "program_checked":
                work.invalid.add(obj["object_id"])
    for assertion in package["assertions"]:
        ref = assertion["assertion_id"]
        _apply_program_check(work, assertion, ref)
        if assertion["subject_ref"] not in work.objects:
            work.invalid.add(ref)
            work.problems.append({"target": ref, "reason": "unknown_subject"})
    for relation in package["relations"]:
        ref = relation["relation_id"]
        _apply_program_check(work, relation, ref)
        if (
            relation["from_ref"] not in work.objects
            or relation["to_ref"] not in work.objects
            or relation["predicate"] in _CANDIDATE_PREDICATES
            or relation.get("applicability") in _INELIGIBLE_APPLICABILITY
        ):
            work.invalid.add(ref)
            work.problems.append({"target": ref, "reason": "invalid_or_uncertain_relation"})


def _program_issue_invalidations(work: _ProgramWork) -> None:
    for issue in work.package["issues"]:
        if issue.get("severity") == "error" and issue.get("resolution_status") != "resolved":
            work.invalid.update(issue.get("target_refs", []))
        if issue.get("resolution_status") != "resolved" and issue.get("program_check", {}).get("status") in (
            "needs_attention",
            "blocked",
        ):
            context = issue.get("source_context", {})
            proposed = context.get("proposed_relation") if isinstance(context, dict) else None
            if proposed:
                predicate = proposed.get("predicate")
                if predicate in ("produces_observation", "observes_subject"):
                    work.invalid.add(
                        proposed.get("to_ref") if predicate == "produces_observation" else proposed.get("from_ref")
                    )
                elif predicate in ("tested_by", "tests_subject", "uses_method"):
                    work.invalid.add(proposed.get("to_ref") if predicate == "tested_by" else proposed.get("from_ref"))
            elif not (isinstance(context, dict) and context.get("proposed_fact")):
                work.invalid.update(issue.get("target_refs", []))
    work.invalid.discard(None)
    work.invalid = invalidated_references(work.package, work.invalid)


def _program_observation_conflicts(work: _ProgramWork) -> None:
    package = work.package
    usable_relations = [
        r
        for r in package["relations"]
        if r["relation_id"] not in work.invalid
        and r["from_ref"] not in work.invalid
        and r["to_ref"] not in work.invalid
    ]
    for obj in package["objects"]:
        if obj["kind"] != "observation":
            continue
        oid = obj["object_id"]
        results = [
            a
            for a in package["assertions"]
            if a["subject_ref"] == oid
            and a["field_id"] == "observation.value"
            and a["statement_kind"] not in ("scope_claim", "author_interpretation")
        ]
        if not results:
            continue
        tests = {
            r["from_ref"] for r in usable_relations if r["predicate"] == "produces_observation" and r["to_ref"] == oid
        }
        subjects = {
            r["to_ref"] for r in usable_relations if r["predicate"] == "observes_subject" and r["from_ref"] == oid
        }
        test_subjects = {
            r["to_ref"] for r in usable_relations if r["predicate"] == "tests_subject" and r["from_ref"] in tests
        }
        test_subjects |= {
            r["from_ref"] for r in usable_relations if r["predicate"] == "tested_by" and r["to_ref"] in tests
        }
        declared_test = obj.get("activity_ref")
        declared_subject = obj.get("about_subject_ref")
        all_tests = tests | ({declared_test} if declared_test else set())
        all_subjects = subjects | test_subjects | ({declared_subject} if declared_subject else set())
        if (
            len(all_tests) > 1
            or len(all_subjects) > 1
            or (declared_test and declared_test not in work.objects)
            or (declared_subject and declared_subject not in work.objects)
        ):
            work.invalid.add(oid)
            work.problems.append({"target": oid, "reason": "conflicting_explicit_observation_ownership"})
    work.invalid = invalidated_references(package, work.invalid)


def _program_assign_status(work: _ProgramWork) -> None:
    package = work.package
    invalid = work.invalid
    for relation in package["relations"]:
        ref = relation["relation_id"]
        relation["use_status"] = (
            "program_checked"
            if not {ref, relation["from_ref"], relation["to_ref"]} & invalid
            else "blocked"
            if relation["program_check"]["status"] == "blocked"
            else "needs_attention"
        )
    for assertion in package["assertions"]:
        ref = assertion["assertion_id"]
        assertion["use_status"] = (
            "program_checked"
            if not {ref, assertion["subject_ref"]} & invalid
            else "blocked"
            if assertion["program_check"]["status"] == "blocked"
            else "needs_attention"
        )
        assertion["review_state"] = "unreviewed"
        assertion["independent_validation"] = False
    # Derived facts need an independently checked base. A cycle cannot certify itself.
    valid = {a["assertion_id"] for a in package["assertions"] if a["use_status"] == "program_checked"}
    outputs = {d["output_assertion_ref"] for d in package["derivations"]}
    resolved = valid - outputs
    while True:
        added = {
            out
            for out in valid & outputs - resolved
            if all(
                d["input_assertion_refs"] and set(d["input_assertion_refs"]) <= resolved
                for d in package["derivations"]
                if d["output_assertion_ref"] == out
            )
        }
        if not added:
            break
        resolved.update(added)
    for assertion in package["assertions"]:
        if assertion["assertion_id"] in valid - resolved:
            assertion["use_status"] = "needs_attention"
            work.problems.append({"target": assertion["assertion_id"], "reason": "unresolved_derivation"})


def _program_quality_report(
    work: _ProgramWork, read_gaps: Sequence[object], unresolved: Sequence[object]
) -> ProgramQualityReport:
    package = work.package
    header = package["header"]
    checked = sum(a["use_status"] == "program_checked" for a in package["assertions"])
    unresolved_issues = [i["issue_id"] for i in package["issues"] if i.get("resolution_status") != "resolved"]
    incomplete_coverage = [
        c["coverage_id"] for c in package["coverage"] if c.get("handling") in ("not_checked", "unresolved", "blocked")
    ]
    complete = (
        checked > 0
        and checked == len(package["assertions"])
        and not read_gaps
        and not unresolved
        and not work.problems
        and not unresolved_issues
        and not incomplete_coverage
    )
    outcome: Literal["complete", "partial"] = "complete" if complete else "partial"
    header["document_pipeline_version"] = "2"
    header["status"] = "candidate" if complete else "partial"
    header["processing_summary"] = {
        "outcome": outcome,
        "program_checked_assertions": checked,
        "unresolved_assertions": len(package["assertions"]) - checked,
        "human_expert_review": False,
    }
    report: ProgramQualityReport = {
        "version": VERSION,
        "outcome": outcome,
        "reading": {"unresolved": list(read_gaps)},
        "semantics_evidence_coverage": {"unresolved": list(unresolved)},
        "relations": {"unresolved": work.problems},
        "verified_assertions": 0,
        "program_checked_assertions": checked,
        "human_expert_review": False,
        "scientific_validation": False,
        "model_semantic_review": False,
        "unresolved_issue_refs": unresolved_issues,
        "incomplete_coverage_refs": incomplete_coverage,
        "package_content_sha256": package_content_hash(package),
    }
    header["document_quality"] = report
    return report


def assess_program_package(
    package: dict[str, Any], *, read_gaps: Sequence[object] = (), unresolved: Sequence[object] = ()
) -> ProgramQualityReport:
    """Annotate usable facts without claiming semantic, source, or human review."""
    header = package["header"]
    if (
        json_member(header, "document_extraction_mode") != "program-only-v1"
        or json_member(header, "review_policy") != "program_only_v1"
    ):
        raise ValueError("incompatible program-only review policy")
    work = _ProgramWork(
        package,
        {e["evidence_id"] for e in package["evidence"]},
        {o["object_id"] for o in package["objects"]},
    )
    _program_check_records(work)
    _program_issue_invalidations(work)
    _program_observation_conflicts(work)
    _program_assign_status(work)
    return _program_quality_report(work, read_gaps, unresolved)
