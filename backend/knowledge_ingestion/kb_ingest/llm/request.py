"""Provider request and logical bindings shared by execution and recovery."""

from dataclasses import dataclass
from pathlib import Path
from typing import Required, TypedDict

from .identity import Json
from .logical_request import LogicalRequestBinding


class CallOptions(TypedDict, total=False):
    phase: Required[str]
    images: list[str] | None
    context: Json | None
    files: list[Json] | None
    input_token_ceiling: int | None
    bypass_cache: bool
    reasoning_effort: str | None
    cost_capacity_hold: str | None
    logical_binding: LogicalRequestBinding | None


@dataclass(kw_only=True)
class Call:
    phase: str
    images: list[str] | None = None
    context: Json | None = None
    files: list[Json] | None = None
    input_token_ceiling: int | None = None
    bypass_cache: bool = False
    reasoning_effort: str | None = None
    cost_capacity_hold: str | None = None
    logical_binding: LogicalRequestBinding | None = None


@dataclass
class Request:
    options: Call
    payload: Json
    request_hash: str
    legacy_hash: str
    stage_input_id: str | None
    cache: Path | None
    input_bound: int
    image_count: int
    file_count: int
    has_pdf: bool
    input_source: str = "byte_upper_bound"
