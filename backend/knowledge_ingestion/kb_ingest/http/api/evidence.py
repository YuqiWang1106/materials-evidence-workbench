"""Read task evidence and its source location or physical page image."""

from typing import Annotated, Protocol

from fastapi import APIRouter, Query
from fastapi.responses import Response

from .. import website_fields as _times


class _Evidence(Protocol):
    def evidence(self, task_id: str, evidence_id: str, *, location_only: bool = False) -> dict[str, object]: ...

    def document_evidence_page_image(self, task_id: str, evidence_id: str, page: int) -> bytes: ...


def routes(service: _Evidence) -> APIRouter:
    router = APIRouter()

    @_times.asset_evidence.route(router.get, "/api/tasks/{task_id}/evidence/{evidence_id}")
    def evidence(task_id: str, evidence_id: str) -> dict[str, object]:
        return service.evidence(task_id, evidence_id)

    @_times.asset_evidence.route(router.get, "/api/tasks/{task_id}/evidence/{evidence_id}/location")
    def evidence_location(task_id: str, evidence_id: str) -> dict[str, object]:
        return service.evidence(task_id, evidence_id, location_only=True)

    @router.get("/api/tasks/{task_id}/evidence/{evidence_id}/page-image", response_model=None)
    def evidence_page_image(task_id: str, evidence_id: str, page: Annotated[int, Query(ge=1)]) -> Response:
        return Response(service.document_evidence_page_image(task_id, evidence_id, page), media_type="image/png")

    return router
