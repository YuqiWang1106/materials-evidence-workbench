"""Table region review and confirmation routes."""

from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import website_fields as _times


class ConfirmRegions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    operator: str = Field(default="本地使用者", min_length=1, max_length=200)


class PrepareRegionSheet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    asset_id: str = Field(min_length=1, max_length=100)
    sheet: str | None


class ConfirmRegionSheet(PrepareRegionSheet):
    operator: str = Field(default="本地使用者", min_length=1, max_length=200)


class RegionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    region_id: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class _RegionService(Protocol):
    def region_review(self, source_id: str, /) -> dict[str, object]: ...

    def confirm_regions(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def prepare_region_sheet(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def confirm_region_sheet(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def region_snapshot(self, source_id: str, /) -> dict[str, object]: ...

    def region_context(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...


def routes(service: _RegionService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/table-drafts/{source_id}/region-review", response_model=None)
    def region_review(source_id: str) -> dict[str, object]:
        return service.region_review(source_id)

    @_times.confirmation.route(router.post, "/api/table-drafts/{source_id}/confirm-regions")
    def confirm_regions(source_id: str, body: ConfirmRegions) -> dict[str, object]:
        return service.confirm_regions(source_id, body.model_dump())

    @_times.timestamps.route(router.post, "/api/table-drafts/{source_id}/prepare-region-sheet")
    def prepare_region_sheet(source_id: str, body: PrepareRegionSheet) -> dict[str, object]:
        return service.prepare_region_sheet(source_id, body.model_dump())

    @router.post("/api/table-drafts/{source_id}/confirm-region-sheet", response_model=None)
    def confirm_region_sheet(source_id: str, body: ConfirmRegionSheet) -> dict[str, object]:
        return service.confirm_region_sheet(source_id, body.model_dump())

    @_times.confirmation.route(router.get, "/api/table-drafts/{source_id}/region-snapshot")
    def region_snapshot(source_id: str) -> dict[str, object]:
        return service.region_snapshot(source_id)

    @router.post("/api/table-drafts/{source_id}/region-context", response_model=None)
    def region_context(source_id: str, body: RegionContext) -> dict[str, object]:
        return service.region_context(source_id, body.model_dump())

    return router
