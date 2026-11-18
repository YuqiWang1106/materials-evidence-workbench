"""HTTP routes for stored table recipes."""

from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ...ingest.tables.draft_reads import DraftReads
from .. import website_fields as _times


class _RecipeService(Protocol):
    def require_legacy_table_workflow(self) -> None: ...
    def create_table_recipe(self, payload: dict[str, object]) -> dict[str, object]: ...


class RecipeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    operator: str = Field(default="本地使用者", min_length=1, max_length=200)


def routes(service: _RecipeService, reads: DraftReads) -> APIRouter:
    router = APIRouter()

    @_times.recipes.route(router.get, "/api/table-recipes")
    def recipes() -> list[dict[str, object]]:
        service.require_legacy_table_workflow()
        return reads.recipes()

    @_times.created.route(router.post, "/api/table-recipes")
    def create_recipe(body: RecipeCreate) -> dict[str, object]:
        return service.create_table_recipe(body.model_dump())

    return router
