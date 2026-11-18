"""Table inventory, preview and catalogue routes."""

from typing import Annotated, Protocol

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field


class TablePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet: str | None = None
    row_start: int = Field(default=1, ge=1)
    row_end: int | None = Field(default=None, ge=1)
    column_start: int = Field(default=1, ge=1)
    column_end: int | None = Field(default=None, ge=1)
    reader_options: dict[object, object] = Field(default_factory=dict)


class _CatalogueService(Protocol):
    def table_inventory(
        self, asset_id: str, reader_options: dict[object, object] | None = None, /
    ) -> dict[str, object]: ...

    def table_preview(self, asset_id: str, **options: object) -> dict[str, object]: ...


def routes(service: _CatalogueService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/assets/{asset_id}/tables", response_model=None)
    def tables(asset_id: str) -> dict[str, object]:
        return service.table_inventory(asset_id)

    @router.post("/api/assets/{asset_id}/tables", response_model=None)
    def selected_tables(
        asset_id: str, reader_options: Annotated[dict[object, object], Body(default_factory=dict)]
    ) -> dict[str, object]:
        return service.table_inventory(asset_id, reader_options)

    @router.post("/api/assets/{asset_id}/table-preview", response_model=None)
    def table_preview(asset_id: str, body: TablePreview) -> dict[str, object]:
        return service.table_preview(asset_id, **body.model_dump(exclude_none=True))

    @router.get("/api/table-catalogue", response_model=None)
    def table_catalogue() -> dict[str, object]:
        from kb_ingest.contract import CATALOGUE, RELATIONS

        return {
            "fields": CATALOGUE["fields"],
            "categories": CATALOGUE["categories"],
            "relations": list(RELATIONS.values()),
        }

    return router
