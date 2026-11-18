"""Prepare a human review revision inside the store's existing transaction."""

import json
import shutil
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import NotRequired, TypedDict

from .contract import validate_package
from .core import atomic_json, fingerprint
from .core.errors import raise_state_conflict as conflict
from .ingest.documents.document_quality_assess import package_content_hash
from .ingest.documents.document_quality_types import JsonObject
from .storage.task_files import checked_child, checked_files
from .store import Store


class _RevisionHeader(TypedDict):
    record_revision_id: str
    document_quality: JsonObject
    processing_run_id: str
    document_artifact_layout: NotRequired[str]
    review_parent_task_id: NotRequired[str]
    review_parent_record_revision_id: NotRequired[str]
    candidate_review_protocol: NotRequired[str]


class RevisionPackage(TypedDict):
    header: _RevisionHeader


class _ReviewTask(TypedDict):
    task_id: str


def require_review_version(
    candidate_digest: str | None, review_digest: str | None, expected_candidate: str, expected_review: str
) -> None:
    if candidate_digest != expected_candidate or not expected_review or review_digest != expected_review:
        conflict("candidate or review version changed; reload before continuing")


def require_saved_originals(store: Store, state: str, asset_ids: list[str], package: RevisionPackage) -> None:
    if state != "saved" or package["header"].get("candidate_review_protocol") != "human_reviewed_sol_candidate_v1":
        conflict("only saved human-reviewed Sol candidates can continue review")
    for asset_id in asset_ids:
        if sha256(store.asset_path(asset_id).read_bytes()).hexdigest() != store.get_asset(asset_id)["sha256"]:
            conflict("document original integrity failure")


@dataclass(frozen=True, slots=True)
class ReviewHistory:
    rows: list[JsonObject]
    revoked: bool

    @classmethod
    def load(
        cls, review_dir: Path, locate_publication: Callable[[], Path], has_revocations: Callable[[JsonObject], bool]
    ) -> "ReviewHistory":
        rows = [json.loads(path.read_text()) for path in sorted(checked_files(review_dir, "*.json"))]
        publication_path = locate_publication()
        publication = json.loads(publication_path.read_text()) if publication_path.is_file() else None
        if rows and publication is None:
            conflict("saved publication is required to preserve source-review decisions")
        return cls(rows, bool(publication and has_revocations(publication)))

    def copy_to(self, pending: Path, root: Path, parent_id: str) -> None:
        if self.rows:
            archive = checked_child(pending, "document-v2", "prior-source-reviews", parent_id)
            for row in self.rows:
                atomic_json(checked_child(archive, fingerprint(row) + ".json"), row)
        if self.revoked:
            # Historical publications lack pointer-to-assertion mappings; retain edits
            # but require fresh confirmation for every previously accepted decision.
            for path in checked_files(checked_child(root, "candidate-reviews"), "*.json"):
                saved = json.loads(path.read_text())
                for row in saved.get("reviews", []):
                    if row.get("decision") in ("confirmed", "corrected"):
                        row.update(
                            previous_decision=row["decision"],
                            decision="uncertain",
                            source_review_invalidation={
                                "parent_task_id": parent_id,
                                "reason": "prior_source_review_requires_fresh_confirmation",
                            },
                        )
                atomic_json(path, saved)


def _rekey(value: object, previous: str, current: str) -> None:
    if isinstance(value, dict):
        if value.get("record_revision_id") == previous:
            value["record_revision_id"] = current
        for child in value.values():
            _rekey(child, previous, current)
    elif isinstance(value, list):
        for child in value:
            _rekey(child, previous, current)


@dataclass(frozen=True, slots=True)
class ReviewRevision:
    data_root: Path
    source_root: Path
    parent_id: str
    source_id: str
    package: RevisionPackage
    history: ReviewHistory

    def __post_init__(self) -> None:
        # Never copy extraction artifacts through an arbitrary symbolic link.
        if self.source_root.is_symlink() or any(path.is_symlink() for path in self.source_root.rglob("*")):
            conflict("review artifacts contain an unsafe symbolic link")

    def prepare(self, new_task: _ReviewTask) -> RevisionPackage:
        run = new_task["task_id"] + ":attempt1"
        # The store has not committed this task, so its public pending_dir guard cannot see it yet.
        pending = checked_child(self.data_root, "pending", new_task["task_id"])
        root = checked_child(pending, "document-v2", "artifacts", fingerprint(run))
        shutil.copytree(self.source_root, root)
        self.history.copy_to(pending, root, self.parent_id)
        copied = deepcopy(self.package)
        header = copied["header"]
        previous = header["record_revision_id"]
        _rekey(copied, previous, self.source_id + ":record:" + run)
        header["processing_run_id"] = run
        header["document_artifact_layout"] = "attempt-v1"
        header["review_parent_task_id"] = self.parent_id
        header["review_parent_record_revision_id"] = previous
        # Preserve reading gaps and quality findings while rebinding their content hash.
        header["document_quality"]["package_content_sha256"] = package_content_hash(dict(copied))
        validate_package(dict(copied))
        atomic_json(checked_child(root, "quality-report.json"), header["document_quality"])
        atomic_json(checked_child(pending, "candidate.json"), copied)
        return copied
