"""Saved raw table versions and their analyses."""

from typing import Literal, Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import website_fields as _times


class RawTableSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    region_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    reviewed: Literal[True]
    operator: str = Field(default="本地使用者", min_length=1, max_length=200)

    @field_validator("reviewed", mode="before")
    @classmethod
    def explicit_review(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("请先确认当前表格")
        return value


class BasicAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    y_columns: list[str] = Field(min_length=1, max_length=32)
    x_column: str | None = None
    group_columns: list[str] = Field(default_factory=list, max_length=3)
    units: dict[str, str] = Field(default_factory=dict)
    header_row_count: int = Field(default=1, ge=0)
    line_plot: bool = False
    number_format: Literal["source", "decimal_dot", "decimal_comma"] = "source"


class RawTableAnalyze(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reviewed: Literal[True]
    config: BasicAnalysisConfig

    @field_validator("reviewed", mode="before")
    @classmethod
    def explicit_review(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("请先确认分析设置")
        return value


class _RawTableService(Protocol):
    def list_raw_tables(self, source_id: str, /) -> dict[str, object]: ...

    def save_raw_table(self, source_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def list_saved_table_versions(self) -> dict[str, object]: ...

    def get_raw_table(self, table_version_id: str, /) -> dict[str, object]: ...

    def analyze_raw_table(self, table_version_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def get_raw_table_analysis(self, analysis_id: str, /) -> dict[str, object]: ...

    def retry_raw_table_publication(self, table_version_id: str, /) -> dict[str, object]: ...

    def retry_analysis_publication(self, analysis_id: str, /) -> dict[str, object]: ...


def routes(service: _RawTableService) -> APIRouter:
    router = APIRouter()

    @_times.source_tables.route(router.get, "/api/table-drafts/{source_id}/raw-tables")
    def list_raw_tables(source_id: str) -> dict[str, object]:
        return service.list_raw_tables(source_id)

    @_times.raw_table.route(router.post, "/api/table-drafts/{source_id}/raw-tables")
    def save_raw_table(source_id: str, body: RawTableSave) -> dict[str, object]:
        return service.save_raw_table(source_id, body.model_dump())

    @_times.table_versions.route(router.get, "/api/raw-tables")
    def saved_table_versions() -> dict[str, object]:
        return service.list_saved_table_versions()

    return router


def analysis_routes(service: _RawTableService) -> APIRouter:
    router = APIRouter()

    @_times.raw_table.route(router.get, "/api/raw-tables/{table_version_id}")
    def get_raw_table(table_version_id: str) -> dict[str, object]:
        return service.get_raw_table(table_version_id)

    @_times.analysis.route(router.post, "/api/raw-tables/{table_version_id}/analyses")
    def analyze_raw_table(table_version_id: str, body: RawTableAnalyze) -> dict[str, object]:
        return service.analyze_raw_table(table_version_id, body.model_dump(exclude_unset=True))

    @_times.analysis.route(router.get, "/api/table-analyses/{analysis_id}")
    def get_raw_table_analysis(analysis_id: str) -> dict[str, object]:
        return service.get_raw_table_analysis(analysis_id)

    @_times.raw_table.route(router.post, "/api/raw-tables/{table_version_id}/sync")
    def retry_raw_table_publication(table_version_id: str) -> dict[str, object]:
        return service.retry_raw_table_publication(table_version_id)

    @_times.analysis.route(router.post, "/api/table-analyses/{analysis_id}/sync")
    def retry_analysis_publication(analysis_id: str) -> dict[str, object]:
        return service.retry_analysis_publication(analysis_id)

    return router
