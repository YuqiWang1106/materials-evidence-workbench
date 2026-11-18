"""Stable entity work units derived from registered sources and original locators."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from kb_ingest.core import fingerprint
from kb_ingest.llm.errors import ModelRequestBindingError
from kb_ingest.llm.logical_request import LogicalRequestBinding, LogicalRequestFamily


class _Asset(BaseModel):
    model_config = ConfigDict(strict=True)
    asset_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _Location(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    asset_ref: str = Field(min_length=1)
    page: int = Field(gt=0)
    kind: str = "text"
    ir_region: str | None = None
    anchor: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EntityRequestIdentity:
    workspace_id: str
    source_revision_id: str
    asset_ids: frozenset[str]

    @classmethod
    def from_package(cls, workspace_id: str, package: Mapping[str, object]) -> EntityRequestIdentity:
        try:
            header, assets = package["header"], package["assets"]
            if not isinstance(header, dict) or not isinstance(assets, list) or not assets or not workspace_id:
                raise ModelRequestBindingError()
            registered = tuple(_Asset.model_validate(asset) for asset in assets)
            keys = tuple(asset.asset_id for asset in registered)
            source_id = header["source_id"]
            if not isinstance(source_id, str) or not source_id or len(keys) != len(set(keys)):
                raise ModelRequestBindingError()
            digest = fingerprint(sorted((asset.asset_id, asset.sha256) for asset in registered))[:20]
            revision = source_id + ":source:" + digest
            if header.get("source_revision_id") != revision:
                raise ModelRequestBindingError()
            return cls(workspace_id, revision, frozenset(keys))
        except (KeyError, ValidationError, TypeError):
            raise ModelRequestBindingError() from None

    def _location(self, block: Mapping[str, object]) -> str:
        try:
            row = _Location.model_validate(block)
        except ValidationError:
            raise ModelRequestBindingError() from None
        if row.asset_ref not in self.asset_ids:
            raise ModelRequestBindingError()
        region = row.anchor.get("coordinate_region")
        cell = row.anchor.get("table_cell")
        coordinates = (
            {key: region[key] for key in ("bbox", "width", "height", "unit", "origin") if key in region}
            if isinstance(region, dict)
            else {}
        )
        cell_position = (
            {key: cell[key] for key in ("row", "column", "row_span", "col_span") if key in cell}
            if isinstance(cell, dict)
            else {}
        )
        if not row.ir_region and not coordinates.get("bbox"):
            raise ModelRequestBindingError()
        # Captions and footnotes share their table's measured region but are distinct structural roles.
        role = row.kind if row.kind in ("caption", "footnote", "table_cell") else "region"
        return fingerprint([row.asset_ref, row.page, row.ir_region, coordinates, cell_position, role])

    def plan(
        self,
        family: LogicalRequestFamily,
        units: Sequence[Sequence[Mapping[str, object]]],
        *,
        unit_keys: Sequence[str] | None = None,
    ) -> tuple[LogicalRequestBinding, ...]:
        locations = tuple(tuple(self._location(block) for block in unit) for unit in units)
        if any(not unit or len(unit) != len(set(unit)) for unit in locations):
            raise ModelRequestBindingError()
        if len(locations) != len(set(locations)):
            raise ModelRequestBindingError()
        keys = tuple(unit_keys) if unit_keys is not None else tuple(str(index) for index in range(len(units)))
        if len(keys) != len(locations) or len(keys) != len(set(keys)):
            raise ModelRequestBindingError()
        structure = tuple(zip(keys, locations, strict=True))
        digest = fingerprint(structure)
        return tuple(
            LogicalRequestBinding(self.workspace_id, self.source_revision_id, family, digest, fingerprint(item))
            for item in structure
        )

    def singleton(self, family: LogicalRequestFamily) -> LogicalRequestBinding:
        digest = fingerprint([self.source_revision_id, family.value])
        return LogicalRequestBinding(self.workspace_id, self.source_revision_id, family, digest, "source")


def visual_region_units(blocks: Sequence[Mapping[str, object]]) -> list[list[Mapping[str, object]]]:
    pages: dict[tuple[object, object], list[Mapping[str, object]]] = {}
    for block in blocks:
        if block.get("kind") in ("image", "chart", "equation", "formula", "figure"):
            pages.setdefault((block.get("asset_ref"), block.get("page")), []).append(block)
    keys = list(pages)
    return [[block for key in keys[start : start + 2] for block in pages[key]] for start in range(0, len(keys), 2)]
