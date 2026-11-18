"""Bind source-level review, revocation and PDF previews to the displayed record package."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Never, NotRequired, TypedDict

from .core import StateConflict, fingerprint
from .core.errors import raise_state_conflict
from .ingest.documents.document_quality_assess import check_publication
from .ingest.documents.document_quality_types import JsonObject
from .storage.task_files import checked_child, checked_files
from .store import Store


def _invalid(message: str) -> Never:
    raise ValueError(message)


class OriginalAsset(TypedDict):
    asset_id: str
    sha256: str


class EvidenceAnchor(TypedDict, total=False):
    kind: str
    page_numbers: list[int]


class Evidence(TypedDict):
    evidence_id: str
    asset_ref: str
    anchor: NotRequired[EvidenceAnchor | None]


@dataclass(frozen=True, slots=True)
class SourceReview:
    package_sha256: str
    target_refs: list[str]
    decision: str
    note: str
    operator: str


class SourceAudit(TypedDict):
    review_id: str
    task_id: str
    package_sha256: str
    source_sha256: dict[str, str]
    record_revision_id: str
    target_refs: list[str]
    target_fingerprint: str
    decision: str
    note: str
    operator: str
    created_at: float
    scientific_validation: bool
    effect: str


TARGET_KEYS = (
    ("assertions", "assertion_id"),
    ("relations", "relation_id"),
    ("objects", "object_id"),
    ("issues", "issue_id"),
)


def review_targets(review: SourceReview, sections: list[tuple[list[JsonObject], str]]) -> str:
    allowed = {row[key] for records, key in sections for row in records}
    if not review.target_refs or not set(review.target_refs) <= allowed:
        _invalid("unknown review target")
    if (
        review.decision not in ("confirmed_source", "rejected", "uncertain")
        or not review.note.strip()
        or not review.operator.strip()
    ):
        _invalid("review requires decision, explanation and operator")
    return fingerprint(
        [
            row
            for records, _ in sections
            for row in records
            if any(v in review.target_refs for k, v in row.items() if k.endswith("_id") and isinstance(v, str))
        ]
    )


def audit_entry(
    task_id: str, review: SourceReview, assets: list[OriginalAsset], revision_id: str, digest: str
) -> SourceAudit:
    return {
        "review_id": uuid.uuid4().hex,
        "task_id": task_id,
        "package_sha256": review.package_sha256,
        "source_sha256": {a["asset_id"]: a["sha256"] for a in assets},
        "record_revision_id": revision_id,
        "target_refs": review.target_refs,
        "target_fingerprint": digest,
        "decision": review.decision,
        "note": review.note,
        "operator": review.operator,
        "created_at": time.time(),
        "scientific_validation": False,
        "effect": "source_review_no_scientific_elevation_reprocess_required",
    }


@dataclass(frozen=True, slots=True)
class ReviewQuery:
    target_ref: str | None
    offset: int
    limit: int

    def validate(self) -> None:
        if (
            (self.target_ref is not None and not self.target_ref)
            or type(self.offset) is not int
            or self.offset < 0
            or type(self.limit) is not int
            or not 1 <= self.limit <= 100
        ):
            _invalid("invalid document review query")


def _read_audits(paths: Iterable[Path]) -> Iterable[SourceAudit]:
    for path in paths:
        yield json.loads(path.read_text())


def current_audits(root: Path, digest: str, target_ref: str | None) -> list[SourceAudit]:
    rows = [
        row
        for row in _read_audits(checked_files(root, "*.json"))
        if row.get("package_sha256") == digest and (target_ref is None or target_ref in row.get("target_refs", []))
    ]
    return sorted(rows, key=lambda row: (row["created_at"], row["review_id"]))


def revoked_targets(root: Path, digest: str) -> set[str]:
    blocked: set[str] = set()
    for row in _read_audits(sorted(checked_files(root, "*.json"))):
        if row["package_sha256"] == digest and row["decision"] in ("rejected", "uncertain"):
            blocked.update(row["target_refs"])
    return blocked


class ReviewPage(TypedDict):
    package_sha256: str
    target_ref: str | None
    total: int
    offset: int
    limit: int
    has_more: bool
    reviews: list[SourceAudit]
    blocked: bool
    blocked_target_refs: list[str]
    reviewable: bool
    scientific_validation: bool


def review_page(
    query: ReviewQuery, digest: str, rows: list[SourceAudit], blocked: set[str], reviewable: bool
) -> ReviewPage:
    return {
        "package_sha256": digest,
        "target_ref": query.target_ref,
        "total": len(rows),
        "offset": query.offset,
        "limit": query.limit,
        "has_more": query.offset + query.limit < len(rows),
        "reviews": rows[query.offset : query.offset + query.limit],
        "blocked": query.target_ref in blocked if query.target_ref else bool(blocked),
        "blocked_target_refs": sorted(blocked),
        "reviewable": reviewable,
        "scientific_validation": False,
    }


def require_physical_page(page: int) -> None:
    if type(page) is not int or page < 1:
        _invalid("invalid physical page")


def require_document_preview(source_type: str) -> None:
    if source_type not in ("paper", "patent"):
        _invalid("PDF page preview is only available for documents")


def evidence_asset_ref(evidence: list[Evidence], evidence_id: str, page: int) -> str:
    item = next((e for e in evidence if e["evidence_id"] == evidence_id), None)
    if item is None:
        message = "Evidence not found"
        raise KeyError(message)
    anchor: EvidenceAnchor = item.get("anchor") or {}
    if anchor.get("kind") != "pdf" or page not in anchor.get("page_numbers", []):
        _invalid("physical page is not supported by this evidence")
    return item["asset_ref"]


def evidence_original(assets: list[OriginalAsset], asset_id: str) -> OriginalAsset:
    source_asset = next((a for a in assets if a["asset_id"] == asset_id), None)
    if source_asset is None:
        raise_state_conflict("evidence source is not in the current record package")
    return source_asset


def render_evidence(store: Store, original: OriginalAsset, registered: str, asset_ids: list[str], page: int) -> bytes:
    if registered not in asset_ids:
        raise_state_conflict("evidence source is not registered to this task")
    asset = store.get_asset(registered)
    if asset["sha256"] != original["sha256"]:
        raise_state_conflict("PDF evidence source does not match the current original")
    from .ingest.documents.readers.documents import render_page

    _, uri = render_page(
        {"asset_id": registered, "local_path": str(store.asset_path(registered)), "sha256": asset["sha256"]}, page
    )
    return base64.b64decode(uri.split(",", 1)[1])


def check_package(package: JsonObject, expected_hash: str | None) -> None:
    try:
        check_publication(package, expected_hash)
    except ValueError as exc:
        raise StateConflict(str(exc)) from None


def check_report(artifact_root: Path, header: JsonObject) -> None:
    report_path = checked_child(artifact_root, "quality-report.json")
    if not report_path.is_file() or json.loads(report_path.read_text()) != header["document_quality"]:
        raise_state_conflict("stored document quality report is absent or changed")


def check_originals(store: Store, assets: list[OriginalAsset], bindings: Callable[[str], str]) -> None:
    for asset in assets:
        registered = bindings(asset["asset_id"])
        if sha256(store.asset_path(registered).read_bytes()).hexdigest() != asset["sha256"]:
            raise_state_conflict("document original integrity failure")
