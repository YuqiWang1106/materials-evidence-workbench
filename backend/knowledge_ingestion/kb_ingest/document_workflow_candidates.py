"""Read candidate groups, version their reviews and locate evidence on registered originals."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from .core import StateConflict, fingerprint
from .document_candidate_review import review_scopes
from .document_issue_scope import annotation_sha256, load_issue_annotations
from .document_source_binding import canonical_location_evidence
from .document_source_index import load_source_index
from .ingest.documents.document_quality_types import JsonObject, JsonValue
from .storage.task_files import checked_child, checked_files
from .store import Store

if TYPE_CHECKING:
    from .document_evidence_location import EvidenceLocator


class CandidateReview(TypedDict):
    candidate_id: str
    candidate_ref: str
    decision: str
    issue_scope_sha256: str
    note: str
    operator: str
    correction: JsonObject
    original: JsonObject
    original_present: dict[str, bool]
    model_ref: JsonValue
    ownership_target_ref: str | None
    original_owner_ref: str | None
    candidate_sha256: str
    source_sha256: dict[str, str]
    updated_at: float
    scientific_validation: bool


class AvailableGroup(TypedDict):
    candidate_id: str
    raw_status: Literal["available"]
    raw: JsonObject
    group_id: str | None
    target_block_ids: JsonValue
    fact_refs: JsonValue
    source_locations: dict[str, JsonObject]
    reviews: list[CandidateReview]
    review_scopes: NotRequired[list[JsonObject]]
    issue_annotations: NotRequired[JsonObject]


class _UnavailableGroup(TypedDict):
    candidate_id: str
    raw_status: Literal["unreadable", "not_an_object"]
    raw: JsonValue
    source_locations: dict[str, JsonObject]


type CandidateGroup = AvailableGroup | _UnavailableGroup


class CandidateSnapshot(TypedDict):
    status: Literal["available", "not_available"]
    candidate_sha256: str | None
    review_sha256: str | None
    total_groups: int
    offset: int
    limit: int
    groups: list[CandidateGroup]
    reason: NotRequired[str]
    has_more: NotRequired[bool]
    review_state: NotRequired[str]
    scientific_validation: NotRequired[bool]


@dataclass(frozen=True, slots=True)
class CandidatePage:
    offset: int
    limit: int

    def __post_init__(self) -> None:
        if (
            type(self.offset) is not int
            or self.offset < 0
            or type(self.limit) is not int
            or not 1 <= self.limit <= 10000
        ):
            msg = "invalid candidate pagination"
            raise ValueError(msg)

    def empty(self) -> CandidateSnapshot:
        return {
            "status": "not_available",
            "reason": "model_candidates_not_saved",
            "candidate_sha256": None,
            "review_sha256": None,
            "total_groups": 0,
            "offset": self.offset,
            "limit": self.limit,
            "groups": [],
        }


def _group_metadata(path: Path) -> dict[str, JsonObject]:
    if not path.is_file():
        return {}
    try:
        return {
            row["group_id"]: row
            for row in json.loads(path.read_text())
            if isinstance(row, dict) and isinstance(row.get("group_id"), str)
        }
    except (OSError, ValueError, TypeError):
        return {}


@dataclass(frozen=True, slots=True)
class _CandidateIndex:
    traces: dict[str, JsonObject]
    summaries: dict[str, JsonObject]
    group_ids: dict[str, str]

    @classmethod
    def load(cls, root: Path) -> _CandidateIndex:
        traces = _group_metadata(checked_child(root, "conversion-trace.json"))
        summaries = _group_metadata(checked_child(root, "input-summary.json"))
        ids = {"raw-" + fingerprint(group_id)[:24]: group_id for group_id in traces.keys() | summaries.keys()}
        return cls(traces, summaries, ids)


def _locations(
    raw: JsonObject, locator: EvidenceLocator | None, summary: JsonObject, assets: dict[str, JsonObject]
) -> dict[str, JsonObject]:
    items = raw.get("evidence", [])
    if locator is None or not isinstance(items, list):
        return {}
    locations = {}
    for item in items:
        if isinstance(item, dict) and isinstance(ref := item.get("ref"), str):
            location_item = canonical_location_evidence(item, summary, assets)
            locations[ref] = locator.locate(location_item)
    return locations


@dataclass(frozen=True, slots=True)
class CandidateFiles:
    root: Path
    source_hashes: Callable[[], dict[str, str]]
    location_assets: dict[str, JsonObject] | None

    def _reviews(self, review_root: Path, candidate_id: str, raw: JsonObject) -> list[CandidateReview]:
        path = checked_child(review_root, fingerprint(candidate_id)[:24] + ".json")
        if not path.is_file():
            return []
        try:
            saved = json.loads(path.read_text())
            source_hashes = self.source_hashes()
            if saved.get("candidate_sha256") == fingerprint(raw) and all(
                row.get("source_sha256") == source_hashes for row in saved.get("reviews", [])
            ):
                reviews: list[CandidateReview] = saved.get("reviews", [])
                return reviews
        except (OSError, ValueError, TypeError):
            return []
        return []

    def _row(
        self, path: Path, index: _CandidateIndex, review_root: Path, locator: EvidenceLocator | None
    ) -> tuple[CandidateGroup, JsonObject]:
        candidate_id = path.stem
        try:
            raw: JsonValue = json.loads(path.read_text())
        except (OSError, ValueError):
            return (
                {"candidate_id": candidate_id, "raw": None, "raw_status": "unreadable", "source_locations": {}},
                {"candidate_id": candidate_id, "raw_status": "unreadable"},
            )
        digest_row: JsonObject = {"candidate_id": candidate_id, "raw": raw}
        if not isinstance(raw, dict):
            return (
                {"candidate_id": candidate_id, "raw": raw, "raw_status": "not_an_object", "source_locations": {}},
                digest_row,
            )
        group_id = index.group_ids.get(candidate_id)
        summary = index.summaries.get(group_id, {}) if group_id is not None else {}
        trace = index.traces.get(group_id, {}) if group_id is not None else {}
        locations = _locations(raw, locator, summary, self.location_assets or {})
        reviews = self._reviews(review_root, candidate_id, raw)
        row: AvailableGroup = {
            "candidate_id": candidate_id,
            "raw": raw,
            "raw_status": "available",
            "group_id": group_id,
            "target_block_ids": summary.get("target_block_ids", []),
            "fact_refs": trace.get("fact_refs", {}),
            "source_locations": locations,
            "reviews": reviews,
        }
        return row, digest_row

    def read(self, paths: list[Path]) -> CandidateRows:
        assets = self.location_assets
        index = load_source_index(self.root, assets) if assets is not None else None
        review_root = checked_child(self.root, "candidate-reviews")
        groups = _CandidateIndex.load(self.root)
        from .document_evidence_location import EvidenceLocator

        session: AbstractContextManager[EvidenceLocator | None] = (
            EvidenceLocator(assets=assets, registry=index.registry, page_inventory=index.page_inventory)
            if index is not None
            else nullcontext(None)
        )
        rows: list[CandidateGroup] = []
        identities: list[JsonObject] = []
        with session as locator:
            for path in paths:
                row, identity = self._row(path, groups, review_root, locator)
                rows.append(row)
                identities.append(identity)
        return CandidateRows(rows, identities)


@dataclass(frozen=True, slots=True)
class CandidateRows:
    groups: list[CandidateGroup]
    identities: list[JsonObject]

    def attach_scopes(self, root: Callable[[], Path], hashes: dict[str, str]) -> None:
        for row in self.groups:
            if row["raw_status"] == "available":
                row["review_scopes"] = review_scopes(row["raw"], row["reviews"])
                row["issue_annotations"] = load_issue_annotations(root(), row["raw"], row["candidate_id"], hashes)

    def snapshot(
        self, page: CandidatePage, package_hash: str | None, review_dir: Callable[[], Path]
    ) -> CandidateSnapshot:
        digest = fingerprint(self.identities)
        review_digest = fingerprint(
            {
                "candidate_sha256": digest,
                "package_sha256": package_hash,
                "source_reviews": [json.loads(p.read_text()) for p in sorted(checked_files(review_dir(), "*.json"))],
                "groups": [_review_digest_row(row) for row in self.groups],
            }
        )
        return {
            "status": "available",
            "candidate_sha256": digest,
            "review_sha256": review_digest,
            "total_groups": len(self.groups),
            "offset": page.offset,
            "limit": page.limit,
            "has_more": page.offset + page.limit < len(self.groups),
            "groups": self.groups[page.offset : page.offset + page.limit],
            "review_state": "not_reviewed",
            "scientific_validation": False,
        }


def _review_digest_row(row: CandidateGroup) -> dict[str, object]:
    if row["raw_status"] != "available":
        return {"candidate_id": row["candidate_id"], "reviews": [], "issue_scope_sha256": None}
    return {
        "candidate_id": row["candidate_id"],
        "reviews": sorted(row["reviews"], key=lambda review: review["candidate_ref"]),
        "issue_scope_sha256": annotation_sha256(row["raw"], row["issue_annotations"]),
    }


def candidate_page_target(
    snapshot: CandidateSnapshot, expected_digest: str, candidate_id: str, evidence_ref: str
) -> tuple[str, int]:
    if snapshot["candidate_sha256"] != expected_digest:
        msg = "候选版本已变化，请刷新后重新打开原文"
        raise StateConflict(msg)
    group = next((row for row in snapshot["groups"] if row["candidate_id"] == candidate_id), None)
    if group is None or group["raw_status"] != "available":
        msg = "Candidate not found"
        raise KeyError(msg)
    items = group["raw"].get("evidence", [])
    if not isinstance(items, Iterable):
        msg = f"'{type(items).__name__}' object is not iterable"
        raise TypeError(msg)
    if not any(isinstance(item, dict) and item.get("ref") == evidence_ref for item in items):
        msg = "Candidate evidence not found"
        raise KeyError(msg)
    location = group["source_locations"].get(evidence_ref, {})
    asset_ref, page = location.get("asset_ref"), location.get("page")
    if location.get("status") == "not_found" or not isinstance(asset_ref, str) or type(page) is not int:
        msg = "没有找到原文位置"
        raise ValueError(msg)
    return asset_ref, page


def candidate_page_png(store: Store, registered: str, page: int) -> bytes:
    asset = store.get_asset(registered)
    path = store.asset_path(registered)
    if sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
        msg = "document original integrity failure"
        raise StateConflict(msg)
    import pymupdf

    with pymupdf.open(path) as pdf:
        if page < 1 or page > pdf.page_count:
            msg = "候选页码超出原件范围"
            raise ValueError(msg)
        png: bytes = pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes("png")
        return png
