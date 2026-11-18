"""Shared JSON and quality-report types for document assessment.

`JsonValue` / `JsonObject` are a recursive JSON tree equivalent to the usual
neutral JSON value. They mark an unvalidated serialization boundary: they do
not mean the value has passed `contract.validate_package` or any other
validator. The current contract validator still returns a plain `dict` and
does not produce a trustworthy typed package view.

Walking an unvalidated package still uses the project's existing `dict[str, Any]`
JSON boundary; that does not certify keys or values. Constructed reports use
TypedDicts. Identifier sets and mapping indexes use `Hashable` keys because the
historical runtime accepted whatever `set()` / dict insertion accepted (string
IDs, ints, and `None`; program quality discards only `None`, and only at its
original call site). That is a description of old data, not a new validator.
Unhashable or non-iterable inputs still raise as before.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any, Literal, TypedDict, TypeGuard

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

# Historical ID values accepted by set()/dict insertion on unvalidated packages.
type QualityIdentifier = Hashable
type IdentifierSet = set[Hashable]
type RecordIndex = dict[Hashable, dict[str, Any]]
type MappingSupportIndex = dict[Hashable, str]
type MappingDependencyIndex = dict[Hashable, set[Hashable]]


def json_member(value: object, key: str, default: object = None) -> object:
    """Read a mapping field without changing historical AttributeError on non-mappings."""
    if isinstance(value, dict):
        return value.get(key, default)
    get = object.__getattribute__(value, "get")
    return get(key, default)


class UnresolvedGroup(TypedDict):
    unresolved: object


class QualityReport(TypedDict):
    version: str
    outcome: Literal["complete", "partial"]
    reading: UnresolvedGroup
    semantics_evidence_coverage: UnresolvedGroup
    relations: UnresolvedGroup
    verified_assertions: int
    human_expert_review: bool
    scientific_validation: bool
    package_content_sha256: str


class ProgramQualityReport(TypedDict):
    version: str
    outcome: Literal["complete", "partial"]
    reading: UnresolvedGroup
    semantics_evidence_coverage: UnresolvedGroup
    relations: UnresolvedGroup
    verified_assertions: int
    program_checked_assertions: int
    human_expert_review: bool
    scientific_validation: bool
    model_semantic_review: bool
    unresolved_issue_refs: list[object]
    incomplete_coverage_refs: list[object]
    package_content_sha256: str


class HumanReviewedQualityReport(TypedDict):
    version: Literal["human_reviewed_candidate_v1"]
    outcome: str
    reviewed_assertions: int
    candidate_sha256: object
    review_sha256: object
    human_expert_review: bool
    scientific_validation: bool
    package_content_sha256: str


class TargetedProblem(TypedDict):
    target: object
    reason: str


class ConflictProblem(TypedDict, total=False):
    targets: object
    issue_ref: object
    reason: str


type QualityProblem = TargetedProblem | ConflictProblem
type StoredQualityReport = QualityReport | ProgramQualityReport | HumanReviewedQualityReport


def is_program_quality_report(value: object) -> TypeGuard[ProgramQualityReport]:
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get("version"), str)
        and value.get("outcome") in ("complete", "partial")
        and isinstance(value.get("reading"), dict)
        and isinstance(value.get("semantics_evidence_coverage"), dict)
        and isinstance(value.get("relations"), dict)
        and isinstance(value.get("verified_assertions"), int)
        and isinstance(value.get("program_checked_assertions"), int)
        and isinstance(value.get("human_expert_review"), bool)
        and isinstance(value.get("scientific_validation"), bool)
        and isinstance(value.get("model_semantic_review"), bool)
        and isinstance(value.get("unresolved_issue_refs"), list)
        and isinstance(value.get("incomplete_coverage_refs"), list)
        and isinstance(value.get("package_content_sha256"), str)
    )
