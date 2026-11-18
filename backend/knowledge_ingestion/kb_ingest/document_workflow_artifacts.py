"""Resolve attempt-scoped document artifacts and project a bounded reading page."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from .core import fingerprint
from .core.errors import raise_state_conflict
from .ingest.documents.document_quality_types import JsonObject, JsonValue
from .storage.task_files import checked_child

_IN_PROGRESS = ("queued", "reading", "extracting", "validating")
_NAMES = {
    "reading": "document-ir.json",
    "dependencies": "object-dependencies.json",
    "quality": "quality-report.json",
    "performance": "performance.json",
}


class _AttemptTask(TypedDict):
    task_id: str
    attempt: int
    state: str


class _AttemptHeader(TypedDict, total=False):
    document_artifact_layout: str
    processing_run_id: str


class _Candidate(TypedDict):
    header: _AttemptHeader


def has_current_candidate(task: Mapping[str, object]) -> bool:
    return bool(task.get("package_sha256")) and task["state"] not in _IN_PROGRESS


def artifact_root(root: Path, task: _AttemptTask, package: _Candidate | None) -> Path:
    if package is None:
        # A queued retry has not claimed its next attempt yet.
        attempt = task["attempt"] + (1 if task["state"] == "queued" else 0)
        run = f"{task['task_id']}:attempt{attempt}"
    else:
        header = package["header"]
        layout = header.get("document_artifact_layout")
        if layout is None:
            return root  # Historical v2 artifacts remain readable.
        if layout != "attempt-v1":
            raise_state_conflict("unknown document artifact layout")
        run = f"{task['task_id']}:attempt{task['attempt']}"
        if header.get("processing_run_id") != run:
            raise_state_conflict("document artifact attempt does not match current candidate")
    return checked_child(root, "artifacts", fingerprint(run))


class _DocumentReading(TypedDict):
    asset_ref: str
    pages: list[JsonObject]
    regions: list[JsonObject]


class _Reading(TypedDict):
    documents: list[_DocumentReading]
    visual_readings: NotRequired[list[JsonObject]]


class _ReadingPage(TypedDict):
    status: Literal["available"]
    pages: list[JsonObject]
    total_regions: int
    offset: int
    regions: list[JsonObject]
    total_visual_readings: int
    visual_readings: list[JsonObject]


class _Payload(TypedDict):
    status: Literal["available"]
    data: JsonValue


@dataclass(frozen=True, slots=True)
class ArtifactRequest:
    kind: str
    asset_id: str | None
    page: int | None
    offset: int
    limit: int

    def __post_init__(self) -> None:
        if self.kind not in tuple(_NAMES):
            message = "unsupported document artifact"
            raise ValueError(message)
        if type(self.offset) is not int or self.offset < 0 or type(self.limit) is not int or not 1 <= self.limit <= 100:
            message = "invalid pagination"
            raise ValueError(message)
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            message = "invalid page"
            raise ValueError(message)

    def path(self, root: Path) -> Path:
        return checked_child(root, _NAMES[self.kind])

    def read(self, path: Path) -> _ReadingPage | _Payload:
        data = json.loads(path.read_text())
        if self.kind == "reading":
            return self._reading(data)
        return {"status": "available", "data": data}

    def _reading(self, value: _Reading) -> _ReadingPage:
        pages: list[JsonObject] = []
        regions: list[JsonObject] = []
        for doc in value["documents"]:
            if self.asset_id and doc["asset_ref"] != self.asset_id:
                continue
            pages.extend(
                {"asset_ref": doc["asset_ref"], **p}
                for p in doc["pages"]
                if self.page is None or p["page"] == self.page
            )
            regions.extend(
                {"asset_ref": doc["asset_ref"], **r}
                for r in doc["regions"]
                if self.page is None or r.get("page") == self.page
            )
        visual = [
            row
            for row in value.get("visual_readings", [])
            if (not self.asset_id or row.get("asset_ref") == self.asset_id)
            and (self.page is None or row.get("page") == self.page)
        ]
        return {
            "status": "available",
            "pages": pages,
            "total_regions": len(regions),
            "offset": self.offset,
            "regions": regions[self.offset : self.offset + self.limit],
            "total_visual_readings": len(visual),
            "visual_readings": visual[self.offset : self.offset + self.limit],
        }
