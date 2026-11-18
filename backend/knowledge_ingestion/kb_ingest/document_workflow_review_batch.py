"""Validate and commit one group of document candidate reviews under the source lock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Never, TypedDict

from .core import atomic_json, fingerprint
from .core.errors import raise_state_conflict
from .document_candidate_review import review_row, review_scopes, scope_members
from .storage.task_files import checked_child

if TYPE_CHECKING:
    from .document_workflow_candidates import AvailableGroup, CandidateReview, CandidateSnapshot
    from .ingest.documents.document_quality_types import JsonObject
    from .store import Store
    from .study_units import StudyUnitService


class Decision(TypedDict, total=False):
    candidate_ref: str
    decision: str
    note: str
    correction: JsonObject | None
    ownership_target_ref: str | None


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    candidate_id: str
    candidate_digest: str
    review_digest: str | None
    operator: str
    items: list[Decision]
    scope: dict[str, str] | None = None


class BatchResult(TypedDict):
    candidate_id: str
    candidate_sha256: str | None
    review_sha256: str | None
    reviews: list[CandidateReview]
    review_scopes: list[JsonObject]


def _invalid(message: str) -> Never:
    raise ValueError(message)


def require_review_digest(digest: str | None) -> None:
    if not isinstance(digest, str) or not digest:
        raise_state_conflict("review version is required; reload before reviewing")


def _pointers(batch: CandidateBatch) -> list[str]:
    if not isinstance(batch.items, list) or not batch.items or (batch.scope is None and len(batch.items) > 100):
        _invalid("a batch must contain 1 to 100 explicit decisions")
    if not isinstance(batch.operator, str) or not batch.operator.strip() or len(batch.operator) > 200:
        _invalid("review operator is required")
    pointers: list[str] = []
    for item in batch.items:
        pointer = item.get("candidate_ref") if isinstance(item, dict) else None
        if not isinstance(pointer, str):
            _invalid("batch candidate pointers must be unique")
        pointers.append(pointer)
    if len(set(pointers)) != len(pointers):
        _invalid("batch candidate pointers must be unique")
    return pointers


def _require_version(view: CandidateSnapshot, batch: CandidateBatch) -> None:
    if view.get("status") != "available" or view["candidate_sha256"] != batch.candidate_digest:
        raise_state_conflict("candidate version changed; reload before reviewing")
    if batch.review_digest is not None and view["review_sha256"] != batch.review_digest:
        raise_state_conflict("review version changed; reload before reviewing")


def _available_group(view: CandidateSnapshot, candidate_id: str) -> AvailableGroup:
    group = next((row for row in view["groups"] if row["candidate_id"] == candidate_id), None)
    if group is None or group["raw_status"] != "available":
        _invalid("candidate group not found")
    return group


def _saved_scopes(view: CandidateSnapshot, candidate_id: str) -> list[JsonObject]:
    group = next(row for row in view["groups"] if row["candidate_id"] == candidate_id)
    if group["raw_status"] != "available":
        raise KeyError("review_scopes")
    return group["review_scopes"]


def _category_decisions(batch: CandidateBatch, prior: dict[str, CandidateReview]) -> None:
    if any(item.get("decision") not in ("confirmed", "corrected", "excluded") for item in batch.items):
        _invalid("category review requires confirmed, corrected or excluded decisions")
    for item in batch.items:
        owner = item.get("ownership_target_ref")
        if owner is not None:
            previous = prior.get(item["candidate_ref"])
            if owner != (previous.get("ownership_target_ref") if previous is not None else None):
                _invalid("category review cannot change fact ownership")


def _validate_scope(group: AvailableGroup, batch: CandidateBatch, pointers: set[str]) -> bool:
    prior = {row["candidate_ref"]: row for row in group.get("reviews", [])}
    locked = {pointer for pointer, row in prior.items() if row.get("decision") == "excluded"}
    if pointers & locked:
        raise_state_conflict("excluded candidate records are locked in this review version")
    if batch.scope is None:
        return False
    members = scope_members(group["review_scopes"], batch.scope)
    if not pointers <= members:
        _invalid("batch contains records outside review scope")
    if "category" not in batch.scope:
        return False
    if pointers != members - locked:
        _invalid("category review must cover every nonlocked category member")
    _category_decisions(batch, prior)
    return True


def _source_hashes(store: Store, asset_ids: list[str]) -> dict[str, str]:
    hashes = {asset_id: store.get_asset(asset_id)["sha256"] for asset_id in asset_ids}
    for asset_id, digest in hashes.items():
        if sha256(store.asset_path(asset_id).read_bytes()).hexdigest() != digest:
            raise_state_conflict("document original integrity failure")
    return hashes


@dataclass(frozen=True, slots=True)
class ReviewWorkspace:
    store: Store
    snapshot: Callable[[], CandidateSnapshot]
    artifact_root: Callable[[JsonObject], Path]
    study_units: Callable[[], StudyUnitService]

    def review(self, task_id: str, batch: CandidateBatch) -> BatchResult:
        task = self.store.get_task(task_id)
        with self.store.lock("source:" + task["source_id"]):
            return self._review_locked(task_id, batch)

    def _review_locked(self, task_id: str, batch: CandidateBatch) -> BatchResult:
        pointers = _pointers(batch)
        view = self.snapshot()
        _require_version(view, batch)
        task = self.store.get_task(task_id)
        if task["state"] not in ("needs_review", "failed"):
            raise_state_conflict("已发布或历史候选只读；请创建新的审核版本")
        group = _available_group(view, batch.candidate_id)
        category = _validate_scope(group, batch, set(pointers))
        hashes = _source_hashes(self.store, task["asset_ids"])
        rows: list[CandidateReview] = [
            review_row(group, item, batch.operator, hashes, allow_excluded=category) for item in batch.items
        ]
        reviews = [row for row in group.get("reviews", []) if row["candidate_ref"] not in pointers] + rows
        if batch.scope is not None and not set(pointers) <= scope_members(
            review_scopes(group["raw"], reviews), batch.scope
        ):
            _invalid("ownership edit moves records outside review scope")
        path = checked_child(
            self.artifact_root(task), "candidate-reviews", fingerprint(batch.candidate_id)[:24] + ".json"
        )
        # All validation precedes invalidation and the single atomic replacement.
        units = self.study_units()
        affected = pointers + [owner for row in rows if (owner := row.get("ownership_target_ref"))]
        units.invalidate_review(task_id, batch.candidate_id, affected)
        atomic_json(
            path,
            {"candidate_id": batch.candidate_id, "candidate_sha256": fingerprint(group["raw"]), "reviews": reviews},
        )
        units.sync_task(task_id)
        current = self.snapshot()
        return {
            "candidate_id": batch.candidate_id,
            "candidate_sha256": current["candidate_sha256"],
            "review_sha256": current["review_sha256"],
            "reviews": rows,
            "review_scopes": _saved_scopes(current, batch.candidate_id),
        }
