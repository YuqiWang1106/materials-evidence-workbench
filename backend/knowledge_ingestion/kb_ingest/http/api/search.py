"""Search published records with the requested scope and typed filters."""

from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str | None = None
    filters: list[dict[object, object]] = Field(default_factory=list, max_length=20)
    include_history: bool = False
    query_scope: str = "source_reviewed"


class _SearchService(Protocol):
    def search(
        self, source_id: str | None, filters: list[dict[object, object]], include_history: bool, query_scope: str
    ) -> list[dict[str, object]]: ...

    def search_detailed(
        self, source_id: str | None, filters: list[dict[object, object]], include_history: bool, query_scope: str
    ) -> dict[str, object]: ...


def routes(service: _SearchService) -> APIRouter:
    router = APIRouter()

    @router.post("/api/search", response_model=None)
    def search(body: Search) -> list[dict[str, object]]:
        return service.search(body.source_id, body.filters, body.include_history, body.query_scope)

    @router.post("/api/search/detailed", response_model=None)
    def search_detailed(body: Search) -> dict[str, object]:
        return service.search_detailed(body.source_id, body.filters, body.include_history, body.query_scope)

    return router
