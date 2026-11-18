"""Standard table review, model suggestions and confirmation."""

from typing import Literal, Protocol, Self, TypedDict

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import website_fields as _times


class StandardReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    expected_version: int = Field(ge=0)


class StandardForms(StandardReviewInput):
    forms: dict[object, object]
    review_flow: Literal["per_table"] | None = None
    confirm_region_id: str | None = Field(default=None, min_length=1, max_length=100)


class StandardMeaningCandidate(StandardReviewInput):
    region_id: str = Field(min_length=1, max_length=100)
    form: dict[object, object]


class StandardPayloadPreview(StandardReviewInput):
    region_id: str = Field(min_length=1, max_length=100)
    sample_rows: list[int] | None = Field(default=None, min_length=1, max_length=24)
    sample_range: str | None = Field(default=None, min_length=1, max_length=32)


class StandardSuggestion(StandardPayloadPreview):
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool

    @model_validator(mode="after")
    def require_sample_selection(self) -> Self:
        if self.sample_rows is None and self.sample_range is None:
            message = "Provide sample_rows or sample_range for the confirmed model payload"
            raise ValueError(message)
        return self


class StandardBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    region_id: str = Field(min_length=1, max_length=100)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_rows: list[int] | None = Field(default=None, min_length=1, max_length=24)
    sample_range: str | None = Field(default=None, min_length=1, max_length=32)
    confirmed: bool

    @model_validator(mode="after")
    def require_sample_selection(self) -> Self:
        if self.sample_rows is None and self.sample_range is None:
            message = "Provide sample_rows or sample_range for every confirmed table"
            raise ValueError(message)
        return self


class StandardModelBatch(StandardReviewInput):
    confirmed: bool
    items: list[StandardBatchItem] = Field(min_length=1)


class StandardSuggestionRetry(StandardSuggestion):
    pass


class StandardPreview(StandardReviewInput):
    scope_id: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=100)


class StandardConfirm(StandardReviewInput):
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed: bool
    operator: str = Field(default="本地使用者", min_length=1, max_length=200)


class _ConfirmedTask(TypedDict):
    task_id: str


class _StandardReviewService(Protocol):
    def standard_review(self, source_id: str, /) -> dict[str, object]: ...

    def update_standard_review(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def standard_meaning_candidate(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def standard_payload_preview(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def enqueue_standard_suggestion(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def standard_model_jobs(self, source_id: str, /) -> dict[str, object]: ...

    def enqueue_standard_model_batch(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def standard_model_batches(self, source_id: str, /) -> dict[str, object]: ...

    def standard_model_batch(self, source_id: str, batch_id: str, /) -> dict[str, object]: ...

    def retry_standard_suggestion(
        self, source_id: str, job_id: str, payload: dict[str, object], /
    ) -> dict[str, object]: ...

    def preview_standard_review(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def confirm_standard_review(self, source_id: str, payload: dict[str, object], /) -> _ConfirmedTask: ...

    def start(self, task_id: str, /) -> None: ...


def routes(service: _StandardReviewService) -> APIRouter:
    router = APIRouter()

    @_times.standard_review.route(router.get, "/api/table-drafts/{source_id}/standard-review")
    def standard_review(source_id: str) -> dict[str, object]:
        return service.standard_review(source_id)

    @_times.standard_review.route(router.put, "/api/table-drafts/{source_id}/standard-review")
    def update_standard_review(source_id: str, value: StandardForms) -> dict[str, object]:
        return service.update_standard_review(source_id, value.model_dump())

    @router.post("/api/table-drafts/{source_id}/standard-meaning-candidate", response_model=None)
    def standard_meaning_candidate(source_id: str, value: StandardMeaningCandidate) -> dict[str, object]:
        return service.standard_meaning_candidate(source_id, value.model_dump())

    @router.post("/api/table-drafts/{source_id}/standard-payload-preview", response_model=None)
    def standard_payload_preview(source_id: str, value: StandardPayloadPreview) -> dict[str, object]:
        return service.standard_payload_preview(source_id, value.model_dump())

    _register_suggestions(router, service)

    @_times.standard_preview.route(router.post, "/api/table-drafts/{source_id}/standard-preview")
    def standard_preview(source_id: str, value: StandardPreview) -> dict[str, object]:
        return service.preview_standard_review(source_id, value.model_dump())

    @router.post("/api/table-drafts/{source_id}/standard-confirm", response_model=None)
    def standard_confirm(source_id: str, value: StandardConfirm) -> _ConfirmedTask:
        task = service.confirm_standard_review(source_id, value.model_dump())
        service.start(task["task_id"])
        return task

    return router


def _register_suggestions(router: APIRouter, service: _StandardReviewService) -> None:
    @_times.suggestion.route(router.post, "/api/table-drafts/{source_id}/standard-suggestion")
    def standard_suggestion(source_id: str, value: StandardSuggestion) -> dict[str, object]:
        return service.enqueue_standard_suggestion(source_id, value.model_dump())

    @_times.suggestions.route(router.get, "/api/table-drafts/{source_id}/standard-suggestions")
    def standard_suggestions(source_id: str) -> dict[str, object]:
        return service.standard_model_jobs(source_id)

    @_times.created.route(router.post, "/api/table-drafts/{source_id}/standard-model-batches")
    def enqueue_standard_model_batch(source_id: str, value: StandardModelBatch) -> dict[str, object]:
        return service.enqueue_standard_model_batch(source_id, value.model_dump())

    @_times.batches.route(router.get, "/api/table-drafts/{source_id}/standard-model-batches")
    def standard_model_batches(source_id: str) -> dict[str, object]:
        return service.standard_model_batches(source_id)

    @_times.created.route(router.get, "/api/table-drafts/{source_id}/standard-model-batches/{batch_id}")
    def standard_model_batch(source_id: str, batch_id: str) -> dict[str, object]:
        return service.standard_model_batch(source_id, batch_id)

    @_times.suggestion.route(router.post, "/api/table-drafts/{source_id}/standard-suggestions/{job_id}/retry")
    def retry_standard_suggestion(source_id: str, job_id: str, value: StandardSuggestionRetry) -> dict[str, object]:
        return service.retry_standard_suggestion(source_id, job_id, value.model_dump())
