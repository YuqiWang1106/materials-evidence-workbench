"""HTTP routes for table draft editing and reading confirmation."""

from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ...ingest.tables.draft_reads import DraftReads
from .. import website_fields as _times
from ..task_view import public_task
from .tasks import SubmittedTask


class _DraftService(Protocol):
    table_workflow_mode: str

    def create_table_draft(self, payload: dict[str, object]) -> dict[str, object]: ...
    def update_table_draft(self, source_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def require_legacy_table_workflow(self) -> None: ...
    def confirm_table_draft(self, source_id: str, payload: dict[str, object]) -> SubmittedTask: ...
    def start(self, task_id: str) -> None: ...
    def reading_preview(self, source_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def upgrade_table_draft(self, source_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def reuse_table_recipe(self, source_id: str, payload: dict[str, object]) -> dict[str, object]: ...


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    asset_ids: list[str] = Field(min_length=1, max_length=50)
    entry_profile: str | None = None


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    asset_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    recipe: dict[object, object] | None = None
    reader_options: dict[object, object] | None = None


class ConfirmReading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_generation: int = Field(ge=0)
    operator: str = Field(default="本地使用者", min_length=1, max_length=200)
    expected_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_scope_hashes: dict[str, str] | None = None


class ReadingPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    profile_index: int = Field(default=0, ge=0)
    scope_id: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=100)


class RecipeReuse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    recipe_version_id: str


class DraftUpgrade(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


def routes(service: _DraftService, reads: DraftReads) -> APIRouter:
    router = APIRouter()

    @_times.timestamps.route(router.post, "/api/table-drafts")
    def create_draft(body: DraftCreate) -> dict[str, object]:
        return service.create_table_draft(body.model_dump(exclude_none=True))

    @_times.drafts.route(router.get, "/api/table-drafts")
    def drafts() -> list[dict[str, object]]:
        return reads.drafts(service.table_workflow_mode)

    @_times.timestamps.route(router.get, "/api/table-drafts/{source_id}")
    def draft(source_id: str) -> dict[str, object]:
        return reads.draft(source_id)

    @_times.timestamps.route(router.put, "/api/table-drafts/{source_id}")
    def update_draft(source_id: str, body: DraftUpdate) -> dict[str, object]:
        return service.update_table_draft(source_id, body.model_dump(exclude_unset=True))

    @router.post("/api/table-drafts/{source_id}/confirm", response_model=None)
    def confirm_draft(source_id: str, body: ConfirmReading) -> dict[str, object]:
        service.require_legacy_table_workflow()
        task = service.confirm_table_draft(source_id, body.model_dump())
        service.start(task["task_id"])
        return public_task(task)

    return router


def reading_routes(service: _DraftService) -> APIRouter:
    router = APIRouter()

    @router.post("/api/table-drafts/{source_id}/reading-preview", response_model=None)
    def reading_preview(source_id: str, body: ReadingPreview) -> dict[str, object]:
        service.require_legacy_table_workflow()
        return service.reading_preview(source_id, body.model_dump())

    @_times.timestamps.route(router.post, "/api/table-drafts/{source_id}/upgrade")
    def upgrade_reading(source_id: str, body: DraftUpgrade) -> dict[str, object]:
        return service.upgrade_table_draft(source_id, body.model_dump())

    @_times.reused_draft.route(router.post, "/api/table-drafts/{source_id}/reuse")
    def reuse_recipe(source_id: str, body: RecipeReuse) -> dict[str, object]:
        return service.reuse_table_recipe(source_id, body.model_dump())

    return router
