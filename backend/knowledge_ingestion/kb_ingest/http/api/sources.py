"""HTTP routes for source lifecycle commands and operation status."""

from typing import Protocol, TypedDict

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import website_fields as _times


class _OperationResult(TypedDict, total=False):
    task_id: str


class _Operation(TypedDict):
    result: _OperationResult


class _SourceService(Protocol):
    def source_management(self, source_id: str) -> dict[str, object]: ...
    def source_command(self, source_id: str, payload: dict[str, object], kind: str) -> _Operation: ...
    def source_operation(self, operation_id: str) -> dict[str, object]: ...
    def start(self, task_id: str) -> None: ...


class SourceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_generation: int = Field(ge=0)
    impact_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


def routes(service: _SourceService) -> APIRouter:
    router = APIRouter()

    @_times.source_management.route(router.get, "/api/sources/{source_id}/management")
    def source_management(source_id: str) -> dict[str, object]:
        return service.source_management(source_id)

    @_times.updated.route(router.post, "/api/sources/{source_id}/delete", status_code=202)
    def source_delete(source_id: str, body: SourceCommand) -> _Operation:
        return service.source_command(source_id, body.model_dump(), "delete")

    @_times.updated.route(router.post, "/api/sources/{source_id}/reparse", status_code=202)
    def source_reparse(source_id: str, body: SourceCommand) -> _Operation:
        operation = service.source_command(source_id, body.model_dump(), "reparse")
        if operation["result"].get("task_id"):
            service.start(operation["result"]["task_id"])
        return operation

    @_times.updated.route(router.get, "/api/source-operations/{operation_id}")
    def source_operation(operation_id: str) -> dict[str, object]:
        return service.source_operation(operation_id)

    return router
