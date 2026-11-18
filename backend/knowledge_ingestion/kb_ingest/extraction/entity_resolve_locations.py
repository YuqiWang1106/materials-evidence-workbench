"""Source locations and rendered-page highlights for resolved graph records."""

from collections.abc import Buffer, Mapping, Sequence
from dataclasses import dataclass
from typing import SupportsFloat, SupportsIndex, TypedDict

type Row = dict[str, object]
type Record = Mapping[str, object]
type Registry = Mapping[str, Record]


def _record(value: object) -> Record:
    if isinstance(value, Mapping):
        return value
    msg = "Expected a graph record"
    raise TypeError(msg)


def _number(value: object) -> float:
    if isinstance(value, (str, Buffer, SupportsFloat, SupportsIndex)):
        return float(value)
    msg = f"float() argument must be a string or a real number, not '{type(value).__name__}'"
    raise TypeError(msg)


class _Location(TypedDict, total=False):
    asset_ref: object
    page: object
    block_id: str
    quote: str
    block_ids: list[str]
    source_id: str
    highlight_regions: list[Row]


def highlight_regions(block: Record) -> list[Row]:
    """Rendered-page box for one block, in the shape the original-page overlay already draws."""
    region = _record(_record(block.get("anchor") or {}).get("coordinate_region") or {})
    bbox = region.get("bbox")
    width = region.get("width")
    height = region.get("height")
    if not (isinstance(bbox, list) and len(bbox) == 4 and width and height):
        return []
    return [
        {
            "page": region.get("page", block.get("page")),
            "bbox": [float(value) for value in bbox],
            "width": _number(width),
            "height": _number(height),
            "unit": region.get("unit") or "pt",
            "origin": region.get("origin") or "top_left",
            "coordinate_space": region.get("coordinate_space") or "rendered_page",
        }
    ]


@dataclass(frozen=True, slots=True)
class _Passage:
    block_id: str
    quote: str
    block_ids: Sequence[str] | None

    def attach(self, row: _Location, registry: Registry, source_id: str | None) -> None:
        block_id = self.block_id
        block = registry[block_id]
        row["asset_ref"] = block.get("asset_ref")
        row["page"] = block.get("page")
        row["block_id"] = block_id
        row["quote"] = self.quote
        covered = [item for item in (self.block_ids or [block_id]) if item in registry]
        if covered and covered != [block_id]:
            row["block_ids"] = covered
        if source_id:
            row["source_id"] = source_id
        regions = []
        for item in covered or [block_id]:
            if registry[item].get("page") == block.get("page"):
                regions.extend(highlight_regions(registry[item]))
        if regions:
            row["highlight_regions"] = regions


def location(
    registry: Registry, block_id: str, quote: str, block_ids: Sequence[str] | None = None, source_id: str | None = None
) -> _Location:
    row: _Location = {}
    _Passage(block_id, quote, block_ids).attach(row, registry, source_id)
    return row
