"""HTTP routes for ingestion task submission, inspection and operations."""

from typing import Literal, Protocol

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from kb_ingest.ingest.service import IngestionService

from ...core.errors import StateConflict
from ...ingest.task_reads import list_task_rows
from ...observation import task_view as observation
from .. import website_fields as _times
from ..task_view import _TaskRecord, public_task


class SubmittedTask(_TaskRecord, Protocol):
    def __getitem__(self, key: Literal["task_id"], /) -> str: ...


class _TaskService(Protocol):
    def submit_source(
        self,
        source_id: str,
        source_type: str,
        asset_ids: list[str],
        options: dict[object, object],
        expected_revision: int | None,
    ) -> SubmittedTask: ...
    def task_status(self, task_id: str) -> _TaskRecord: ...
    def reconcile_publication(self, task_id: str) -> _TaskRecord: ...
    def retry(self, task_id: str) -> _TaskRecord: ...


class _ResultService(Protocol):
    def result_view(self, task_id: str, *, offset: int, limit: int, subject_ref: str | None) -> dict[str, object]: ...


ALLOWED_OPTIONS = {
    "table_profiles",
    "encoding",
    "delimiter",
    "header_row",
    "sheets",
    "scope",
    "language",
    "force_ocr",
    "supplement_links",
    "auto_detect",
    "reader_options",
    "table_mode",
}


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    source_type: str
    asset_ids: list[str] = Field(min_length=1, max_length=50)
    options: dict[object, object] = Field(default_factory=dict)
    expected_revision: int | None = None


class Prepared(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: dict[object, object]
    asset_bindings: dict[str, str]
    expected_revision: int | None = None


def routes(service: IngestionService) -> APIRouter:
    router = APIRouter()
    commands: _TaskService = service

    @router.post("/api/tasks", response_model=None)
    def submit(body: Submit) -> dict[str, object]:
        if set(body.options) - ALLOWED_OPTIONS:
            raise HTTPException(422, "Options may describe extraction scope/table layout only")
        if body.source_type == "prepared":
            raise HTTPException(422, "Use the explicit prepared-record import endpoint")
        task = commands.submit_source(
            body.source_id, body.source_type, body.asset_ids, body.options, body.expected_revision
        )
        service.start(task["task_id"])
        return public_task(task)

    @router.post("/api/prepared", response_model=None)
    def prepared(body: Prepared) -> dict[str, object]:
        # A prepared record can only reference previously uploaded assets.
        return public_task(service.import_prepared(body.package, body.asset_bindings, body.expected_revision))

    @_times.task_list.route(router.get, "/api/tasks")
    def tasks() -> list[dict[str, object]]:
        return list_task_rows(service, task)

    @router.get("/api/tasks/{task_id}", response_model=None)
    def task(task_id: str) -> dict[str, object]:
        return public_task(commands.task_status(task_id))

    @_times.document_observation.route(router.get, "/api/tasks/{task_id}/document-observation")
    def document_observation(task_id: str) -> dict[str, object]:
        return observation.for_task(service, task_id)

    @router.get("/api/tasks/{task_id}/document-observation/stream", response_model=None)
    async def document_observation_stream(task_id: str, request: Request) -> StreamingResponse:
        commands.task_status(task_id)  # an unknown task is refused before the stream's headers are sent
        events = observation.stream(
            request, lambda: observation.for_task(service, task_id), public=_times.document_observation.project
        )
        headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
        return StreamingResponse(events, media_type="text/event-stream", headers=headers)

    return router


def result_routes(service: _ResultService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks/{task_id}/result-view", response_model=None)
    def result_view(
        task_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
        subject_ref: str | None = None,
    ) -> dict[str, object]:
        return service.result_view(task_id, offset=offset, limit=limit, subject_ref=subject_ref)

    return router


def operation_routes(service: IngestionService) -> APIRouter:
    router = APIRouter()
    commands: _TaskService = service

    @router.post("/api/tasks/{task_id}/cancel", response_model=None)
    def cancel(task_id: str) -> dict[str, object]:
        return public_task(service.resources.cancel(task_id))

    @router.post("/api/tasks/{task_id}/reconcile", response_model=None)
    def reconcile(task_id: str) -> dict[str, object]:
        try:
            return public_task(commands.reconcile_publication(task_id))
        except StateConflict:
            raise
        except RuntimeError as exc:
            raise HTTPException(503, "写入状态尚未确定，已保留原结果且没有重复上传") from exc

    @router.post("/api/tasks/{task_id}/retry", response_model=None)
    def retry(task_id: str) -> dict[str, object]:
        commands.retry(task_id)
        service.start(task_id)
        return public_task(service.resources.task(task_id))

    @router.get("/api/tasks/{task_id}/records", response_model=None)
    def records(task_id: str) -> dict[str, object]:
        return service.records(task_id)

    return router
