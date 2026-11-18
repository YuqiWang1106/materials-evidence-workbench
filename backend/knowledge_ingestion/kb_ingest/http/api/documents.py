"""Document artifacts, display and review endpoints."""

from collections.abc import Callable
from typing import Annotated, NotRequired, Protocol, TypedDict, Unpack

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kb_ingest.http.task_view import _TaskRecord, public_task
from kb_ingest.ingest.service import IngestionService

from .. import website_fields as _times


class DocumentDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DocumentReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_refs: list[str] = Field(min_length=1, max_length=100)
    decision: str
    note: str = Field(min_length=1, max_length=2000)
    operator: str = Field(min_length=1, max_length=200)


class DocumentCandidateReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(min_length=1, max_length=200)
    candidate_ref: str = Field(min_length=2, max_length=1000)
    decision: str
    operator: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=2000)
    correction: dict[object, object] | None = None
    expected_review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ownership_target_ref: str | None = Field(default=None, min_length=2, max_length=1000)


class DocumentCandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_ref: str = Field(min_length=2, max_length=1000)
    decision: str
    note: str = Field(default="", max_length=2000)
    correction: dict[object, object] | None = None
    ownership_target_ref: str | None = Field(default=None, min_length=2, max_length=1000)


class DocumentReviewRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentReviewScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: str = Field(pattern=r"^(sample|shared_method|cross_sample_statements|unclassified)$")
    source_ref: str = Field(min_length=1, max_length=1000)
    category: str | None = Field(default=None, pattern=r"^(composition|preparation|tests|statements)$")

    @field_validator("category")
    @classmethod
    def category_must_be_explicit_when_supplied(cls, value: str | None) -> str:
        if value is None:
            message = "category must name a review category when supplied"
            raise ValueError(message)
        return value


class DocumentCandidateBatch(DocumentReviewRevision):
    candidate_id: str = Field(min_length=1, max_length=200)
    operator: str = Field(min_length=1, max_length=200)
    items: list[DocumentCandidateDecision] = Field(min_length=1)
    review_scope: DocumentReviewScope | None = None


class _ArtifactQuery(BaseModel):
    asset_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class _ArtifactParams(TypedDict):
    asset_id: str | None
    page: int | None
    offset: int
    limit: int


class _DecisionParams(TypedDict):
    expected_candidate_sha256: str
    candidate_id: str
    candidate_ref: str
    decision: str
    operator: str
    note: str
    correction: dict[object, object] | None
    expected_review_sha256: str | None
    ownership_target_ref: str | None


class _BatchParams(TypedDict):
    expected_candidate_sha256: str
    expected_review_sha256: str
    candidate_id: str
    operator: str
    items: list[dict[str, object]]
    review_scope: NotRequired[dict[str, str] | None]


class _ReviewParams(TypedDict):
    expected_package_sha256: str
    target_refs: list[str]
    decision: str
    note: str
    operator: str


class _Documents(Protocol):
    def document_artifact(self, task_id: str, kind: str, **query: Unpack[_ArtifactParams]) -> dict[str, object]: ...

    def document_candidates(self, task_id: str, *, offset: int, limit: int) -> dict[str, object]: ...

    def document_candidate_page_image(
        self, task_id: str, candidate_id: str, evidence_ref: str, *, expected_candidate_sha256: str
    ) -> bytes: ...

    def document_candidate_review(self, task_id: str, **body: Unpack[_DecisionParams]) -> dict[str, object]: ...

    def document_candidate_reviews_batch(self, task_id: str, **body: Unpack[_BatchParams]) -> dict[str, object]: ...

    def document_review_revision(
        self, task_id: str, *, expected_candidate_sha256: str, expected_review_sha256: str
    ) -> _TaskRecord: ...

    def document_review(self, task_id: str, **body: Unpack[_ReviewParams]) -> dict[str, object]: ...

    def document_reviews(
        self, task_id: str, *, target_ref: str | None, offset: int, limit: int
    ) -> dict[str, object]: ...


def routes(service: IngestionService) -> APIRouter:
    router = APIRouter()
    documents: _Documents = service

    @router.get("/api/tasks/{task_id}/document/{kind}", response_model=None)
    def document_artifact(task_id: str, kind: str, query: Annotated[_ArtifactQuery, Query()]) -> dict[str, object]:
        return documents.document_artifact(
            task_id, kind, asset_id=query.asset_id, page=query.page, offset=query.offset, limit=query.limit
        )

    @_times.candidates.route(router.get, "/api/tasks/{task_id}/document-candidates")
    def document_candidates(
        task_id: str, offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> dict[str, object]:
        from kb_ingest.document_display import attach_display

        attach: Callable[[IngestionService, str, dict[str, object]], dict[str, object]] = attach_display
        return attach(service, task_id, documents.document_candidates(task_id, offset=offset, limit=limit))

    @_times.candidates.route(router.post, "/api/tasks/{task_id}/document-display")
    def document_display(task_id: str, body: DocumentDisplay) -> dict[str, object]:
        from kb_ingest.document_display import generate_display

        generate: Callable[[IngestionService, str, str], dict[str, object]] = generate_display
        return generate(service, task_id, body.expected_candidate_sha256)

    @router.get(
        "/api/tasks/{task_id}/document-candidates/{candidate_id}/evidence/{evidence_ref}/page-image",
        response_model=None,
    )
    def document_candidate_page_image(
        task_id: str,
        candidate_id: str,
        evidence_ref: str,
        candidate_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    ) -> Response:
        return Response(
            documents.document_candidate_page_image(
                task_id, candidate_id, evidence_ref, expected_candidate_sha256=candidate_sha256
            ),
            media_type="image/png",
        )

    _review_routes(router, documents)
    return router


def _review_routes(router: APIRouter, service: _Documents) -> None:
    @_times.updated.route(router.post, "/api/tasks/{task_id}/document-candidate-reviews")
    def document_candidate_review(task_id: str, body: DocumentCandidateReview) -> dict[str, object]:
        return service.document_candidate_review(task_id, **body.model_dump())

    @_times.candidate_reviews.route(router.post, "/api/tasks/{task_id}/document-candidate-reviews/batch")
    def document_candidate_reviews_batch(task_id: str, body: DocumentCandidateBatch) -> dict[str, object]:
        return service.document_candidate_reviews_batch(task_id, **body.model_dump(exclude_unset=True))

    @router.post("/api/tasks/{task_id}/document-review-revision", response_model=None)
    def document_review_revision(task_id: str, body: DocumentReviewRevision) -> dict[str, object]:
        return public_task(service.document_review_revision(task_id, **body.model_dump()))

    @_times.created.route(router.post, "/api/tasks/{task_id}/document-reviews")
    def document_review(task_id: str, body: DocumentReview) -> dict[str, object]:
        return service.document_review(task_id, **body.model_dump())

    @_times.source_reviews.route(router.get, "/api/tasks/{task_id}/document-reviews")
    def document_reviews(
        task_id: str,
        target_ref: str | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> dict[str, object]:
        return service.document_reviews(task_id, target_ref=target_ref, offset=offset, limit=limit)
