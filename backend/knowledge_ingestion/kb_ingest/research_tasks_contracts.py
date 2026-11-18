"""Closed deterministic task contracts. Trusted hosts supply original user messages."""

import re
from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from .core import fingerprint
from .knowledge_contracts import AssertionRef, Hash, Id, SampleTestContextRef, StrictModel, ValueProjection
from .knowledge_domain import load_domain_profile

POLICY_VERSION = "research-tasks/1"
OPERATORS = {"=": "eq", "==": "eq", ">=": "gte", "≥": "gte", ">": "gt", "<=": "lte", "≤": "lte", "<": "lt"}


class UserMessage(StrictModel):
    message_id: Id
    role: Literal["user"]
    text: str = Field(min_length=1, max_length=8000)
    text_sha256: Hash

    @model_validator(mode="after")
    def hash_matches(self):
        if sha256(self.text.encode()).hexdigest() != self.text_sha256:
            raise ValueError("User message hash differs")
        return self


class TargetSelection(StrictModel):
    profile_id: Id
    start: int = Field(ge=0, le=8000)
    end: int = Field(gt=0, le=8000)


class TargetFragment(TargetSelection):
    text: str = Field(min_length=1, max_length=512)


class Constraint(StrictModel):
    constraint_id: Id
    profile_id: Id
    field_id: Id
    qualifiers: dict[str, str]
    op: Literal["eq", "gte", "gt", "lte", "lt"]
    value: str = Field(min_length=1, max_length=64)
    value_type: Literal["number", "category"]
    unit: str | None = None
    basis: str | None = None


def parse_fragment(fragment):
    mappings = [m for m in load_domain_profile().mappings if m.catalogue_profile_id == fragment.profile_id]
    if not mappings:
        raise ValueError("Unsupported target profile")
    parsed = None
    for mapping in mappings:
        expression = (
            re.escape(mapping.source_label)
            + r"\s*(>=|<=|==|=|>|<|≥|≤)\s*([^\s;]{1,64})(?:\s+([^\s;=]{1,32}))?(?:\s+basis=([^\s;=]{1,64}))?"
        )
        match = re.fullmatch(expression, fragment.text)
        if match:
            parsed = (mapping, match)
            break
    if parsed is None:
        raise ValueError("Target must be an exact supported user clause")
    mapping, match = parsed
    op, value, unit, basis = match.groups()
    numeric = bool(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value))
    if not numeric and (
        OPERATORS[op] != "eq"
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value)
        or value.lower() in {"nan", "inf", "infinity"}
    ):
        raise ValueError("Unsupported target value")
    return Constraint(
        constraint_id="constraint-" + fingerprint(fragment.model_dump()),
        profile_id=fragment.profile_id,
        field_id=mapping.selector.field_id,
        qualifiers=mapping.selector.qualifiers,
        op=OPERATORS[op],
        value=value,
        value_type="number" if numeric else "category",
        unit=unit,
        basis=basis,
    )


class UserTarget(StrictModel):
    id: Id
    origin: Literal["user_target"] = "user_target"
    message: UserMessage
    message_sha256: Hash
    fragments: list[TargetFragment] = Field(min_length=1, max_length=20)
    constraints: list[Constraint] = Field(min_length=1, max_length=20)
    policy_version: Literal["research-tasks/1"] = POLICY_VERSION
    scientific_validation: Literal[False] = False

    @model_validator(mode="after")
    def bound(self):
        if self.message_sha256 != self.message.text_sha256:
            raise ValueError("Target message differs")
        spans = []
        for f in self.fragments:
            if f.end <= f.start or f.end > len(self.message.text) or self.message.text[f.start : f.end] != f.text:
                raise ValueError("Target fragment differs")
            if any(f.start < end and start < f.end for start, end in spans):
                raise ValueError("Overlapping target fragments")
            spans.append((f.start, f.end))
        if [parse_fragment(f) for f in self.fragments] != self.constraints:
            raise ValueError("Target was not parsed from user text")
        if self.id != "target-" + fingerprint(self.model_dump(mode="json", exclude={"id"})):
            raise ValueError("Target identity differs")
        return self


class TaskScope(StrictModel):
    workspace_id: Id
    collection_id: Id
    corpus_snapshot_id: Id
    use_policy: Literal["source_reviewed", "saved_extractions", "history"]


class Coverage(StrictModel):
    mode: Literal["checked_candidates", "exact_query"]
    exhaustive: bool
    checked: int = Field(ge=0)
    total: int | None = None
    query_receipt_hashes: list[Hash] = Field(default_factory=list)
    query_definition: dict | None = None
    population: Literal["query_matches", "checked_candidates"] = "checked_candidates"
    reason: str | None = None


class ConstraintCheck(StrictModel):
    constraint_id: Id
    status: Literal["satisfied", "not_satisfied", "unknown"]
    reason: str | None = None
    fact_refs: list[AssertionRef] = Field(default_factory=list)


class CandidateEvaluation(StrictModel):
    candidate_id: Id
    ref: SampleTestContextRef
    label: str | None = None
    status: Literal["satisfied", "not_satisfied", "unknown"]
    checks: list[ConstraintCheck]


class TaskResult(StrictModel):
    kind: Literal["constraint_evaluation"] = "constraint_evaluation"
    goal_id: Id
    scope: TaskScope
    coverage: Coverage
    candidates: list[CandidateEvaluation]
    policy_version: Literal["research-tasks/1"] = POLICY_VERSION
    input_hash: Hash
    inputs: dict
    scientific_validation: Literal[False] = False


class PerformanceReference(StrictModel):
    kind: Literal["performance_reference"] = "performance_reference"
    status: Literal["reference_only"] = "reference_only"
    model_status: Literal["model_not_available"] = "model_not_available"
    prediction: None = None
    model_version: None = None
    scope: TaskScope
    references: list[SampleTestContextRef]
    comparisons: list[dict]
    missing_readiness: list[str]
    input_hash: Hash
    inputs: dict
    scientific_validation: Literal[False] = False


class PlanParameter(StrictModel):
    origin: Literal["source_fact"] = "source_fact"
    ref: AssertionRef
    value: ValueProjection
    process_ref: Id
    step_ref: Id | None


class ResearchPlan(StrictModel):
    kind: Literal["research_plan"] = "research_plan"
    goal_id: Id
    scope: TaskScope
    candidate_refs: list[SampleTestContextRef]
    parameters: list[PlanParameter]
    process_order: Literal["unconfirmed"] = "unconfirmed"
    pending_parameters: list[str]
    next_steps: list[str]
    execution_status: Literal["not_submitted"] = "not_submitted"
    input_hash: Hash
    inputs: dict
    scientific_validation: Literal[False] = False
