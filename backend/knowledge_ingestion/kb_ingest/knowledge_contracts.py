"""Versioned materials read contracts, without service or scientific inference."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_serializer, model_validator

from .contract import FIELDS, PROFILES

VERSION = "materials-knowledge/v1"
Id = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^/\\\s\x00-\x1f]+$")]
Version = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(ge=0)]
Operation = Literal["side_by_side", "condition_diff", "numeric_delta", "category_compare", "rank_observed"]
Policy = Literal["source_reviewed", "saved_extractions", "history", "explore"]
FormalPolicy = Literal["source_reviewed", "saved_extractions", "history"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class ScopedRef(StrictModel):
    workspace_id: Id
    corpus_snapshot_id: Id


class RecordRef(ScopedRef):
    kind: Literal["record_revision"]
    task_id: Id
    source_id: Id
    source_revision_id: Id
    record_revision_id: Id
    record_snapshot_id: Id
    package_sha256: Hash


class SampleTestContextRef(RecordRef):
    kind: Literal["sample_test_context"]
    sample_ref: Id
    test_ref: Id | None


class AssertionRef(RecordRef):
    kind: Literal["assertion"]
    assertion_id: Id


class EvidenceRef(RecordRef):
    kind: Literal["evidence"]
    evidence_id: Id
    asset_sha256: Hash


class SourceSnapshotRef(StrictModel):
    kind: Literal["source_snapshot"]
    workspace_id: Id
    source_snapshot_id: Id
    source_id: Id
    source_revision_id: Id
    source_asset_sha256: Hash
    source_index_version: Version
    source_index_sha256: Hash
    manifest_sha256: Hash


class BoundingBox(StrictModel):
    x0: float
    y0: float
    x1: float
    y1: float
    coordinate_space: Literal["page_pixels", "normalized_page"]
    page_image_sha256: Hash

    @model_validator(mode="after")
    def bounds(self):
        import math

        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(math.isfinite(v) and v >= 0 for v in values) or self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("Invalid bounding box")
        if self.coordinate_space == "normalized_page" and max(values) > 1:
            raise ValueError("Normalized bounding box exceeds page")
        return self


class SourcePassageRef(ScopedRef):
    kind: Literal["source_passage"]
    source_snapshot_ref: SourceSnapshotRef
    segment_id: Id
    quote_sha256: Hash
    physical_page: Annotated[int, Field(gt=0)] | None
    location_status: Literal["located", "page_only", "unresolved"]
    bbox: BoundingBox | None = None

    @model_validator(mode="after")
    def location(self):
        if self.workspace_id != self.source_snapshot_ref.workspace_id:
            raise ValueError("WORKSPACE_MISMATCH")
        if self.location_status == "unresolved":
            if self.physical_page is not None or self.bbox is not None:
                raise ValueError("Unresolved location has no page or bbox")
        elif self.physical_page is None:
            raise ValueError("Located page is required")
        if (self.location_status == "located") != (self.bbox is not None):
            raise ValueError("Exact location requires bbox")
        return self


class WikiRevisionRef(ScopedRef):
    kind: Literal["wiki_revision"]
    page_id: Id
    revision_id: Id
    content_hash: Hash
    dependency_manifest_hash: Hash


class TableSnapshotRef(ScopedRef):
    """A raw table identity; deliberately has no scientific task/record owner."""

    kind: Literal["table_snapshot"]
    source_id: Id
    asset_id: Id
    asset_sha256: Hash
    table_id: Id
    table_version_id: Id
    source_region_id: Id
    table_range: str = Field(min_length=1, max_length=256)
    snapshot_sha256: Hash


class TableAnalysisArtifactRef(ScopedRef):
    chat_kind: Literal["research"] | None = None

    @model_serializer(mode="wrap")
    def compatible_ref(self, handler):
        value = handler(self)
        if self.chat_kind is None:
            value.pop("chat_kind", None)
        return value

    kind: Literal["table_analysis_artifact"]
    chat_id: Id
    turn_id: Id
    artifact_id: Id
    table_version_ids: Annotated[list[Id], Field(min_length=1)]
    artifact_sha256: Hash


class StudyUnitSnapshotRef(StrictModel):
    kind: Literal["study_unit_snapshot"]
    workspace_id: Id
    unit_id: Id
    snapshot_id: Id
    content_hash: Hash
    source_id: Id
    fact_id: str | None = None


KnowledgeRef = Annotated[
    Union[
        RecordRef,
        SampleTestContextRef,
        AssertionRef,
        EvidenceRef,
        SourcePassageRef,
        WikiRevisionRef,
        TableAnalysisArtifactRef,
        TableSnapshotRef,
        StudyUnitSnapshotRef,
    ],
    Field(discriminator="kind"),
]
SupportRef = KnowledgeRef


class QueryFilter(StrictModel):
    field_id: str
    qualifiers: dict[str, str] = Field(default_factory=dict)
    op: Literal["eq", "contains", "exists", "gte", "gt", "lte", "lt"] = "eq"
    value: str | None = None
    value_type: Literal["number", "text", "category", "date", "time", "datetime", "duration"] | None = None
    unit: str | None = None
    basis: str | None = None
    allow_formula_cached: bool = False
    allow_uncertainty_center: bool = False

    @model_validator(mode="after")
    def known_and_typed(self):
        if self.field_id not in FIELDS:
            raise ValueError("INVALID_QUERY: unknown field")
        keys = {k for p in PROFILES.values() if p["field_id"] == self.field_id for k in p["qualifiers"]}
        if set(self.qualifiers) - keys:
            raise ValueError("INVALID_QUERY: unknown qualifier")
        if self.op != "exists" and self.value is None:
            raise ValueError("Filter value required")
        if self.op == "exists" and self.value is not None:
            raise ValueError("Presence filter has no value")
        if self.value_type == "number" and self.op != "exists":
            if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", self.value or ""):
                raise ValueError("Finite decimal string required")
        if self.op in {"gte", "gt", "lte", "lt"} and self.value_type not in {
            "number",
            "date",
            "time",
            "datetime",
            "duration",
        }:
            raise ValueError("Explicit comparable value type required")
        if self.op == "contains" and self.value_type not in {None, "text", "category"}:
            raise ValueError("contains requires text")
        if (self.allow_formula_cached or self.allow_uncertainty_center) and self.value_type != "number":
            raise ValueError("Numeric opt-in requires number type")
        return self


class PageRequest(StrictModel):
    size: Annotated[int, Field(ge=1, le=200)] = 25
    cursor: Id | None = None


class Request(StrictModel):
    schema_version: Literal["materials-knowledge/v1"]
    request_id: Id


class QueryRequest(Request):
    domain_profile: Version
    collection_id: Id
    corpus_snapshot_id: Id | None = None
    use_policy: FormalPolicy = "source_reviewed"
    include_history: bool = False
    filters: Annotated[list[QueryFilter], Field(max_length=20)] = Field(default_factory=list)
    selection: list[SampleTestContextRef] | None = None
    page: PageRequest = Field(default_factory=PageRequest)
    allow_partial: bool = False

    @model_validator(mode="after")
    def selection_scope(self):
        if self.include_history != (self.use_policy == "history"):
            raise ValueError("History must be explicitly selected in policy and include_history")
        if self.selection:
            if self.corpus_snapshot_id is None or any(
                r.corpus_snapshot_id != self.corpus_snapshot_id for r in self.selection
            ):
                raise ValueError("SNAPSHOT_MISMATCH")
            if len({r.workspace_id for r in self.selection}) != 1:
                raise ValueError("WORKSPACE_MISMATCH")
        return self


class SearchRequest(Request):
    query: Annotated[str, Field(min_length=1, max_length=4096)]
    domain_profile: Version
    collection_id: Id
    corpus_snapshot_id: Id | None = None
    use_policy: Policy = "source_reviewed"
    source_kinds: list[Literal["record", "wiki", "source_passage"]] = Field(default_factory=lambda: ["record"])
    top_k: Annotated[int, Field(ge=1, le=100)] = 10
    allow_stale_index: bool = False


class GetRequest(Request):
    use_policy: Policy = "source_reviewed"
    target: KnowledgeRef
    view: Literal["summary", "full", "location"] = "summary"


class IndexLayerScope(StrictModel):
    workspace_id: Id
    layer: Literal["record", "source_passage", "wiki"]
    collection_id: Id
    use_policy: Policy
    corpus_snapshot_id: Id


class IndexBuildRequest(Request):
    scope: IndexLayerScope
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 30


class IndexStatusRequest(Request):
    scope: IndexLayerScope


class IndexBuildResult(Request):
    state: Literal["building", "ready", "failed"]
    generation_id: Id
    entry_count: Count
    source_manifest_hash: Hash
    error_code: str | None = None


class IndexStatusResult(Request):
    readiness: Literal["ready", "not_ready", "stale"]
    generation: Id | None = None
    eligibility: Literal["requires_read_validation"] = "requires_read_validation"
    mode: Literal["lexical"] = "lexical"
    reason: str | None = None


class CorpusRequest(Request):
    collection_id: Id
    kind: Literal["formal_records", "source_passages"]
    source_ids: Annotated[list[Id], Field(min_length=1, max_length=100)]
    use_policy: Policy

    @model_validator(mode="after")
    def selected_policy(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Duplicate source selection")
        if self.kind == "formal_records" and self.use_policy == "explore":
            raise ValueError("Formal policy required")
        if self.kind == "source_passages" and self.use_policy not in {"explore", "source_reviewed"}:
            raise ValueError("Source policy required")
        return self


class CorpusResult(Request):
    workspace_id: Id
    collection_id: Id
    kind: Literal["formal_records", "source_passages"]
    corpus_snapshot_id: Id
    use_policy: Policy
    source_ids: list[Id]
    record_refs: list[RecordRef] = Field(default_factory=list)
    source_snapshot_refs: list[SourceSnapshotRef] = Field(default_factory=list)


class PassageBrowseRequest(Request):
    corpus_snapshot_id: Id
    use_policy: Literal["explore", "source_reviewed"]
    source_id: Id
    asset_sha256: Hash | None = None
    physical_page: Annotated[int, Field(gt=0)] | None = None
    segment_id: Id | None = None
    page: PageRequest = Field(default_factory=PageRequest)


class RecordCatalogueRequest(Request):
    target: Annotated[Union[RecordRef, SampleTestContextRef], Field(discriminator="kind")]
    use_policy: FormalPolicy
    kind: Literal["contexts", "assertions", "evidence"]
    page: PageRequest = Field(default_factory=PageRequest)


class ProfileRef(StrictModel):
    id: Id
    version: Id
    sha256: Hash


class CompareRequest(Request):
    corpus_snapshot_id: Id
    use_policy: FormalPolicy = "source_reviewed"
    items: Annotated[list[SampleTestContextRef], Field(min_length=2, max_length=20)]
    comparison_profile: ProfileRef
    requested_operations: list[Operation]

    @model_validator(mode="after")
    def pinned(self):
        if any(r.corpus_snapshot_id != self.corpus_snapshot_id for r in self.items):
            raise ValueError("SNAPSHOT_MISMATCH")
        if len({r.workspace_id for r in self.items}) != 1:
            raise ValueError("WORKSPACE_MISMATCH")
        if len({r.model_dump_json() for r in self.items}) != len(self.items):
            raise ValueError("Distinct comparison items required")
        if len(set(self.requested_operations)) != len(self.requested_operations):
            raise ValueError("Duplicate operations")
        return self


ErrorCode = Literal[
    "INVALID_QUERY",
    "UNSUPPORTED_OPERATOR",
    "DOMAIN_FILTER_UNSUPPORTED",
    "COMPARISON_PROFILE_UNSUPPORTED",
    "COMPARISON_PROFILE_MISMATCH",
    "SOURCE_USE_POLICY_DENIED",
    "SOURCE_INDEX_MISMATCH",
    "REFERENCE_NOT_FOUND",
    "WORKSPACE_MISMATCH",
    "SNAPSHOT_MISMATCH",
    "SNAPSHOT_REVIEW_CHANGED",
    "STALE_CONTEXT",
    "CURSOR_MISMATCH",
    "RESULT_SET_EXPIRED",
    "TYPE_NOT_COMPARABLE",
    "ARCHIVE_UNAVAILABLE",
    "INTEGRITY_FAILURE",
    "INDEX_NOT_READY",
    "INDEX_STALE",
    "WIKI_NOT_READY",
    "QUERY_TIMEOUT",
    "REQUEST_CANCELED",
    "PREVIEW_UNSUPPORTED",
    "INDEX_TIMEOUT",
    "FTS5_UNAVAILABLE",
    "INDEX_UNAVAILABLE",
    "INDEX_BUILD_FAILED",
    "INDEX_REQUEST_CONFLICT",
]
SAFE_MESSAGES = {c: c.replace("_", " ").capitalize() + "." for c in ErrorCode.__args__}


class KnowledgeError(StrictModel):
    code: ErrorCode
    message: str
    retryable: bool
    request_id: Id
    affected_refs: list[KnowledgeRef] = Field(default_factory=list)
    remedy: Literal["correct_request", "refresh_context", "retry_later", "review_source", "stop"]
    diagnostic_id: Id | None = None

    @model_validator(mode="after")
    def safe(self):
        if self.message != SAFE_MESSAGES[self.code]:
            raise ValueError("Only fixed safe error messages are allowed")
        return self


def safe_error(
    code: ErrorCode, request_id: str, *, retryable=False, remedy="stop", affected_refs=None, diagnostic_id=None
):
    return KnowledgeError(
        code=code,
        message=SAFE_MESSAGES[code],
        request_id=request_id,
        retryable=retryable,
        remedy=remedy,
        affected_refs=affected_refs or [],
        diagnostic_id=diagnostic_id,
    )


class Scope(StrictModel):
    collection_id: Id
    source_selection: Literal["current_saved", "history", "pinned"]
    use_policy: Policy
    source_count: Count
    sources_read: Count
    sources_failed: Count
    failed_source_ids: list[Id] = Field(default_factory=list)
    version_freshness: Literal["pinned", "current", "stale", "unknown"]
    safety_policy_checked_at: str

    @model_validator(mode="after")
    def counts(self):
        if (
            self.sources_read + self.sources_failed > self.source_count
            or len(set(self.failed_source_ids)) != self.sources_failed
        ):
            raise ValueError("Inconsistent source counts")
        return self


class Completeness(StrictModel):
    matching_complete: bool
    exhaustive_within_scope: bool
    total_matches: Count | None
    returned: Count
    exclusion_summary_complete: bool = False
    has_more: bool
    next_cursor: Id | None = None
    result_set_id: Id | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.exhaustive_within_scope and not self.matching_complete:
            raise ValueError("Exhaustive requires completed matching")
        if not self.matching_complete and self.total_matches is not None:
            raise ValueError("Incomplete totals are unknown")
        if self.total_matches is not None and self.returned > self.total_matches:
            raise ValueError("Returned exceeds total")
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("Cursor and has_more mismatch")
        return self


class CatalogueEntry(StrictModel):
    ref: Annotated[Union[SampleTestContextRef, AssertionRef, EvidenceRef], Field(discriminator="kind")]
    label: str | None = None
    subject_ref: Id | None = None
    subject_kind: Id | None = None
    field_id: str | None = None
    qualifiers: dict[str, str] = Field(default_factory=dict)


class RecordCatalogueResult(Request):
    workspace_id: Id
    corpus_snapshot_id: Id
    kind: Literal["contexts", "assertions", "evidence"]
    scope: Scope
    completeness: Completeness
    entries: list[CatalogueEntry]


class Versions(StrictModel):
    query_policy: Version
    domain_vocabulary: Version
    projection: Version
    corpus_snapshot_id: Id
    corpus_manifest_sha256: Hash
    index_generation: Id | None = None


class ScientificStatus(StrictModel):
    source_review: Literal["unreviewed", "reviewed", "revoked", "unknown"] = "unknown"
    human_expert_review: bool = False
    independent_experiment_validation: bool = False
    scientific_validation: bool = False
    currently_eligible: bool = False
    record_currentness: Literal["current", "superseded", "unknown"] = "unknown"

    @model_validator(mode="after")
    def eligibility(self):
        if self.source_review == "revoked" and self.currently_eligible:
            raise ValueError("Revoked source is ineligible")
        return self


class ProcessRelationProjection(StrictModel):
    relation_id: Id
    predicate: Literal["has_formulation", "recipe_for", "process_applicability", "defines_step"]
    from_ref: Id
    to_ref: Id
    basis_kind: Id
    applicability: Id
    evidence_refs: list[EvidenceRef]


class ProcessContextPath(StrictModel):
    sample_ref: Id
    formulation_ref: Id | None = None
    process_ref: Id
    step_ref: Id | None = None
    relation_ids: Annotated[list[Id], Field(min_length=1, max_length=3)]
    relations: Annotated[list[ProcessRelationProjection], Field(min_length=1, max_length=3)]


# Additive preservation of formal document statement kinds; this is identity,
# not permission to treat claims/calculations as observed measurements.
StatementKind = Literal[
    "source_report",
    "measured",
    "source_reported_measurement",
    "declared_range",
    "scope_claim",
    "source_claim",
    "author_interpretation",
    "source_reported_calculation",
    "unknown",
]


class ValueProjection(StrictModel):
    assertion_id: Id
    field_id: str
    qualifiers: dict[str, str]
    raw_value: JsonValue
    value_type: Literal["number", "text", "category", "date", "time", "datetime", "duration"] | None = None
    typed_value_version: Literal["2"] | None = None
    typed_value: JsonValue = None
    typed_values: list[JsonValue] | None = None
    unit: str | None = None
    basis: str | None = None
    statement_kind: StatementKind = "unknown"
    subject_ref: Id | None = None
    subject_kind: Id | None = None
    subject_label: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    context_paths: list[ProcessContextPath] = Field(default_factory=list)


class EvidenceData(StrictModel):
    anchor: JsonValue = None
    content_kind: str | None = None
    effective_content_kind: Literal["verbatim", "parsed", "model_transcription", "unknown"] = "unknown"
    provenance: PassageProvenance | None = None
    quote: str | None = None
    reader_settings_status: str | None = None
    reader_options: JsonValue = None
    source_title: str | None = None
    resource_ref: Id | None = None


class RecordResult(StrictModel):
    kind: Literal["record_revision", "sample_test_context", "assertion", "evidence"]
    result_id: Id
    ref: Annotated[Union[RecordRef, SampleTestContextRef, AssertionRef, EvidenceRef], Field(discriminator="kind")]
    label: str | None = None
    status: ScientificStatus = Field(default_factory=ScientificStatus)
    storage: Literal["archive_verified", "local_verified", "unverified"]
    integrity: Literal["verified", "unverified"]
    values: list[ValueProjection] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    record_subject_ref: Id | None = None
    run_ref: Id | None = None
    series_ref: Id | None = None
    scientific_package: JsonValue = None
    evidence_data: EvidenceData | None = None
    relation_basis: Literal["source_explicit", "unknown"] = "unknown"
    retrieval_score: float | None = None

    @model_validator(mode="after")
    def kinds(self):
        if self.kind != self.ref.kind:
            raise ValueError("Result/ref branch mismatch")
        nested = [e for value in self.values for e in value.evidence_refs]
        nested.extend(
            e
            for value in self.values
            for path in value.context_paths
            for relation in path.relations
            for e in relation.evidence_refs
        )
        for e in [*self.evidence_refs, *nested]:
            if any(
                getattr(e, k) != getattr(self.ref, k)
                for k in (
                    "workspace_id",
                    "corpus_snapshot_id",
                    "task_id",
                    "source_id",
                    "source_revision_id",
                    "record_revision_id",
                    "record_snapshot_id",
                    "package_sha256",
                )
            ):
                raise ValueError("Evidence owner mismatch")
        return self


class PassageProvenance(StrictModel):
    reader: str = "unknown"
    task_id: Id | None = None
    processing_run_id: Id | None = None
    asset_id: Id | None = None
    block_id: Id | None = None
    ir_region: Id | None = None
    reading_sha256: Hash | None = None
    artifact_sha256: Hash | None = None
    verification_state: str | None = None
    requires_image_verification: bool = False
    quote_relation: Literal["exact_segment", "exact_substring", "unverified"] = "unverified"


class PassageResult(StrictModel):
    kind: Literal["source_passage"]
    result_id: Id
    ref: SourcePassageRef
    quote: str
    content_kind: Literal["verbatim", "parsed", "model_transcription", "unknown"] = "unknown"
    provenance: PassageProvenance = Field(default_factory=PassageProvenance)
    attribution: str
    source_review: Literal["unreviewed", "reviewed", "revoked", "unknown"]
    availability: Literal["available", "unavailable"]
    storage: Literal["local_verified", "archive_verified", "unverified"]
    integrity: Literal["verified", "unverified"]
    scientific_validation: Literal[False] = False
    eligible_for_publication: bool = False
    resource_ref: Id | None = None

    @model_validator(mode="after")
    def quote_and_review(self):
        from hashlib import sha256

        if sha256(self.quote.encode("utf-8")).hexdigest() != self.ref.quote_sha256:
            raise ValueError("SOURCE_INDEX_MISMATCH")
        if self.eligible_for_publication and (
            self.source_review != "reviewed"
            or self.storage != "archive_verified"
            or self.integrity != "verified"
            or self.availability != "available"
        ):
            raise ValueError("Publication support requires reviewed verified available source")
        return self


class WikiResult(StrictModel):
    kind: Literal["wiki_revision"]
    result_id: Id
    ref: WikiRevisionRef
    body: str
    title: str | None = None
    content: JsonValue = None
    claim_types: list[str] = Field(default_factory=list)
    source_review: Literal["reviewed"] = "reviewed"
    storage: Literal["archive_verified"] = "archive_verified"
    integrity: Literal["verified"] = "verified"
    dependency_refs: list[SupportRef]
    scientific_validation: Literal[False] = False


class AnalysisResult(StrictModel):
    kind: Literal["table_analysis_artifact"]
    result_id: Id
    ref: TableAnalysisArtifactRef
    config: JsonValue
    coverage: JsonValue
    computation: JsonValue
    storage: Literal["local_analysis"] = "local_analysis"
    scientific_publication: Literal[False] = False


class LexicalCandidate(StrictModel):
    kind: Literal["lexical_candidate"] = "lexical_candidate"
    result_id: Id
    ref: KnowledgeRef
    scope: IndexLayerScope
    content_kind: Literal["record_object", "record_assertion", "record_evidence", "source_passage", "wiki_revision"]
    source_content_kind: Literal["verbatim", "parsed", "model_transcription", "unknown"] = "unknown"
    provenance: PassageProvenance | None = None
    snippet: Annotated[str, Field(max_length=800)]
    snippet_truncated: bool
    indexed_text_truncated: bool = False
    source_label: Annotated[str, Field(max_length=512)] | None = None
    content_hash: Hash
    generation: Id
    readiness: Literal["ready"] = "ready"
    requires_authoritative_read: Literal[True] = True
    scientific_validation: Literal[False] = False
    score: float
    mode: Literal["lexical"] = "lexical"


ResultItem = Annotated[
    Union[RecordResult, PassageResult, WikiResult, AnalysisResult, LexicalCandidate], Field(discriminator="kind")
]


class Retrieval(StrictModel):
    mode: Literal["lexical", "vector", "hybrid"]
    candidate_count: Count
    index_generation: Id
    corpus_snapshot_id: Id
    readiness: Literal["ready", "not_ready"] = "ready"
    reason: str | None = None
    ranking_meaning: Literal["text_relevance_only"] = "text_relevance_only"


class Exclusion(StrictModel):
    ref: KnowledgeRef | None = None
    filter_index: Count | None = None
    reason: Id


class KnowledgeResult(Request):
    status: Literal["ok", "partial", "error", "canceled"]
    operation: Literal["query", "search", "get", "compare", "passages"]
    workspace_id: Id
    domain_profile: Version
    versions: Versions
    scope: Scope
    completeness: Completeness
    results: list[ResultItem] = Field(default_factory=list)
    comparisons: list["ComparabilityResult"] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    warnings: list[Id] = Field(default_factory=list)
    errors: list[KnowledgeError] = Field(default_factory=list)
    retrieval: Retrieval | None = None

    @model_validator(mode="after")
    def invariants(self):
        c, s = self.completeness, self.scope
        if c.returned != len(self.results) + len(self.comparisons):
            raise ValueError("Returned count mismatch")
        if self.status == "ok" and self.errors:
            raise ValueError("Successful result has errors")
        if self.status == "error" and not self.errors:
            raise ValueError("Error result requires errors")
        if s.sources_failed or s.sources_read < s.source_count:
            if c.matching_complete or c.exhaustive_within_scope or c.total_matches is not None or self.status == "ok":
                raise ValueError("Failed/incomplete source reads cannot assert complete success")
        if self.status in {"partial", "error", "canceled"} and (
            c.matching_complete or c.exhaustive_within_scope or c.total_matches is not None
        ):
            raise ValueError("Incomplete response cannot assert totals")
        if self.operation == "search" and (c.exhaustive_within_scope or c.total_matches is not None):
            raise ValueError("Ranked search cannot assert exhaustive totals")
        for error in self.errors:
            if error.request_id != self.request_id:
                raise ValueError("Error request mismatch")
            if any(
                r.workspace_id != self.workspace_id or r.corpus_snapshot_id != self.versions.corpus_snapshot_id
                for r in error.affected_refs
            ):
                raise ValueError("Error reference scope mismatch")
        for pair in self.comparisons:
            if (
                pair.left_ref.workspace_id != self.workspace_id
                or pair.left_ref.corpus_snapshot_id != self.versions.corpus_snapshot_id
            ):
                raise ValueError("Comparison result scope mismatch")
        for row in self.results:
            if (
                row.ref.workspace_id != self.workspace_id
                or row.ref.corpus_snapshot_id != self.versions.corpus_snapshot_id
            ):
                raise ValueError("Result scope mismatch")
            if isinstance(row, LexicalCandidate) and (
                row.scope.workspace_id != self.workspace_id
                or row.scope.corpus_snapshot_id != self.versions.corpus_snapshot_id
                or row.scope.collection_id != s.collection_id
                or row.scope.use_policy != s.use_policy
            ):
                raise ValueError("Candidate scope mismatch")
            if isinstance(row, PassageResult) and row.source_review != "reviewed" and s.use_policy != "explore":
                raise ValueError("SOURCE_USE_POLICY_DENIED")
        return self


class ConditionSelector(StrictModel):
    field_id: str
    qualifiers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known(self):
        QueryFilter(field_id=self.field_id, qualifiers=self.qualifiers, op="exists")
        return self


class ConditionValue(StrictModel):
    raw_value: JsonValue
    typed_decimal: str | None = None
    unit: str | None = None
    basis: str | None = None
    assertion_ref: AssertionRef | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class CheckedCondition(StrictModel):
    condition: ConditionSelector
    left: ConditionValue | None
    right: ConditionValue | None
    verdict: Literal["same", "allowed_difference", "different", "missing", "unknown"]
    reason: Id
    support_ref_indices: list[Count] = Field(default_factory=list)


class MissingCondition(StrictModel):
    condition: ConditionSelector
    side: Literal["left", "right", "both"]
    reason: Id


class BlockedOperation(StrictModel):
    operation: Operation
    reason: Id


class ComparabilityResult(StrictModel):
    comparison_id: Id
    left_ref: SampleTestContextRef
    right_ref: SampleTestContextRef
    profile: ProfileRef
    status: Literal["comparable", "not_comparable", "insufficient_conditions"]
    checked_conditions: list[CheckedCondition]
    differences: list[CheckedCondition]
    missing_conditions: list[MissingCondition]
    evidence_refs: list[EvidenceRef]
    allowed_operations: list[Operation]
    blocked_operations: list[BlockedOperation]
    policy_version: Version
    input_manifest_sha256: Hash

    @model_validator(mode="after")
    def coherence(self):
        if (
            self.left_ref.workspace_id != self.right_ref.workspace_id
            or self.left_ref.corpus_snapshot_id != self.right_ref.corpus_snapshot_id
        ):
            raise ValueError("Comparison scope mismatch")
        if self.left_ref == self.right_ref:
            raise ValueError("Distinct contexts required")
        if set(self.allowed_operations) & {o.operation for o in self.blocked_operations}:
            raise ValueError("Allowed and blocked operations overlap")
        if self.status != "comparable" and set(self.allowed_operations) - {"side_by_side", "condition_diff"}:
            raise ValueError("Ineligible comparison permits descriptive juxtaposition only")
        if self.status == "comparable" and (
            self.missing_conditions
            or any(c.verdict in {"different", "missing", "unknown"} for c in self.checked_conditions + self.differences)
        ):
            raise ValueError("Comparable requires known compatible conditions")
        if self.status == "not_comparable" and not any(c.verdict == "different" for c in self.differences):
            raise ValueError("Hard conflict must be explicit")
        if self.status == "insufficient_conditions" and not self.missing_conditions:
            raise ValueError("Missing conditions must be explicit")
        owners = (self.left_ref, self.right_ref)

        def owned(ref, owner):
            return all(
                getattr(ref, k) == getattr(owner, k)
                for k in (
                    "workspace_id",
                    "corpus_snapshot_id",
                    "task_id",
                    "source_id",
                    "source_revision_id",
                    "record_revision_id",
                    "record_snapshot_id",
                    "package_sha256",
                )
            )

        for ref in self.evidence_refs:
            if not any(owned(ref, o) for o in owners):
                raise ValueError("Comparison evidence owner mismatch")
        for c in self.checked_conditions + self.differences:
            for value, owner in ((c.left, self.left_ref), (c.right, self.right_ref)):
                if value is not None:
                    if value.assertion_ref is not None and not owned(value.assertion_ref, owner):
                        raise ValueError("Condition assertion owner mismatch")
                    if any(not owned(e, owner) for e in value.evidence_refs):
                        raise ValueError("Condition evidence owner mismatch")
            if any(i >= len(self.evidence_refs) for i in c.support_ref_indices):
                raise ValueError("Evidence index out of range")
        return self


class FieldCapability(StrictModel):
    field_id: str
    qualifier_keys: list[str]
    ops: list[Literal["eq", "contains", "exists", "gte", "gt", "lte", "lt"]]
    value_types: list[Literal["number", "text", "category", "date", "time", "datetime", "duration"]]


class ContextValidateRequest(Request):
    use_policy: Policy = "source_reviewed"
    refs: Annotated[list[KnowledgeRef], Field(min_length=1, max_length=200)]


class ContextValidateResult(Request):
    valid: bool
    errors: list[KnowledgeError] = Field(default_factory=list)


class Capabilities(StrictModel):
    schema_version: Literal["materials-knowledge/v1"]
    workspace_id: Id
    readiness: Literal["ready", "not_ready", "unsupported"]
    fields: list[FieldCapability]
    domain_profiles: list[Version]
    comparison_profiles: list[ProfileRef]
    collections: list[Id]
    policies: list[Policy]
    index_status: Literal["ready", "not_ready", "stale", "unsupported"]
    unsupported: list[Id] = Field(default_factory=list)
    exact_query: bool = False
    semantic_search: bool = False
    lexical_index: bool = False
    lexical_index_layers: list[Literal["record", "source_passage", "wiki"]] = Field(default_factory=list)
    source_passage: bool = False
    wiki: bool = False
    wiki_search: Literal["lexical_current_topics"] | None = None
    comparison_operations: list[Operation] = Field(default_factory=list)
    catalogue_vocabulary: JsonValue = None
    source_vocabulary: JsonValue = None
    mappings: JsonValue = None


class PreviewImage(StrictModel):
    data_uri: Annotated[str, Field(max_length=8388608, pattern=r"^data:image/png;base64,[A-Za-z0-9+/=]+$")]
    width: Annotated[int, Field(gt=0, le=1600)]
    height: Annotated[int, Field(gt=0, le=1600)]
    sha256: Hash


class PreviewBox(StrictModel):
    x0: float
    y0: float
    x1: float
    y1: float
    coordinate_space: Literal["normalized_page"] = "normalized_page"

    @model_validator(mode="after")
    def bounds(self):
        import math

        if (
            not all(math.isfinite(v) and 0 <= v <= 1 for v in (self.x0, self.y0, self.x1, self.y1))
            or self.x0 >= self.x1
            or self.y0 >= self.y1
        ):
            raise ValueError("Invalid normalized preview box")
        return self


class EvidencePreviewResult(StrictModel):
    schema_version: Literal["materials-knowledge/v1"]
    ref: Annotated[Union[EvidenceRef, SourcePassageRef], Field(discriminator="kind")]
    asset_sha256: Hash
    physical_page: Annotated[int, Field(gt=0)]
    image: PreviewImage
    location_status: Literal["located", "page_only", "unresolved"]
    boxes: list[PreviewBox] = Field(default_factory=list)
    quote: str | None = None
    status: ScientificStatus = Field(default_factory=ScientificStatus)
    scientific_validation: Literal[False] = False

    @model_validator(mode="after")
    def binding(self):
        expected = (
            self.ref.asset_sha256
            if isinstance(self.ref, EvidenceRef)
            else self.ref.source_snapshot_ref.source_asset_sha256
        )
        if self.asset_sha256 != expected:
            raise ValueError("Preview asset owner mismatch")
        if (self.location_status == "located") != bool(self.boxes):
            raise ValueError("Located preview requires reliable boxes")
        import base64
        import hashlib

        try:
            data = base64.b64decode(self.image.data_uri.split(",", 1)[1], validate=True)
        except Exception:
            raise ValueError("Invalid preview PNG") from None
        if hashlib.sha256(data).hexdigest() != self.image.sha256 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Preview image hash mismatch")
        return self


KnowledgeResult.model_rebuild()
SCHEMA_MODELS = (
    QueryRequest,
    SearchRequest,
    GetRequest,
    CompareRequest,
    KnowledgeResult,
    KnowledgeError,
    Capabilities,
    ContextValidateRequest,
    ContextValidateResult,
    SourceSnapshotRef,
    SourcePassageRef,
    ComparabilityResult,
    EvidencePreviewResult,
    CorpusRequest,
    CorpusResult,
    PassageBrowseRequest,
    RecordCatalogueRequest,
    RecordCatalogueResult,
    IndexBuildRequest,
    IndexStatusRequest,
    IndexBuildResult,
    IndexStatusResult,
)


def export_schemas() -> str:
    """Stable JSON; caller decides output location, no import-time writes."""
    return (
        json.dumps(
            {m.__name__: m.model_json_schema() for m in SCHEMA_MODELS},
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
