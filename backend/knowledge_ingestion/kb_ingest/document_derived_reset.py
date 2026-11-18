"""Offline, source-scoped retirement of document derivatives with a recoverable backup.

No files or cost rows are deleted. Historical conversations keep their evidence
bytes; current source pointers and Wiki availability decide reuse eligibility.
"""

import shutil
import time
from contextlib import ExitStack
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import StateConflict, atomic_json, fingerprint
from .knowledge_snapshot import KnowledgeSnapshotStore
from .knowledge_store import KnowledgeStore
from .storage import lifecycle_repository as rows
from .storage.models import (
    KnowledgeItem,
    KnowledgeRevision,
    StudyUnit,
    StudyWikiJob,
    Task,
)
from .storage.source_repository import require_registered
from .storage.sqlite import online_backup
from .storage.task_repository import require_task

ACTIVE_TASKS = {"reading", "extracting", "validating", "publishing", "write_unknown"}
ACTIVE_JOBS = {"running", "publishing"}


def _sources(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_id" and isinstance(item, str):
                result.add(item)
            elif key in {"source_ids", "sources"} and isinstance(item, list):
                result.update(x for x in item if isinstance(x, str))
            else:
                result.update(_sources(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_sources(item))
    return result


class DocumentDerivedReset:
    def __init__(self, store):
        self.store = store
        self.workspace_id = store.workspace_id

    def plan(self, source_ids):
        ids = sorted(set(source_ids))
        if not ids:
            raise ValueError("DOCUMENT_SOURCES_REQUIRED")
        with Session(self.store.engine) as session:
            tasks = list(
                session.scalars(rows.tasks(self.workspace_id).where(Task.source_id.in_(ids)).order_by(Task.task_id))
            )
            if (
                {t.source_id for t in tasks} != set(ids)
                or any(t.source_type not in {"paper", "patent"} for t in tasks)
                or rows.draft_source_ids(session, self.workspace_id).intersection(ids)
            ):
                raise ValueError("DOCUMENT_SOURCES_ONLY")
            if any(t.state in ACTIVE_TASKS for t in tasks):
                raise StateConflict("DOCUMENT_TASKS_ACTIVE")
            units = list(
                session.scalars(
                    select(StudyUnit)
                    .where(StudyUnit.workspace_id == self.workspace_id, StudyUnit.source_id.in_(ids))
                    .order_by(StudyUnit.unit_id)
                )
            )
            jobs = list(
                session.scalars(
                    select(StudyWikiJob)
                    .where(StudyWikiJob.unit_id.in_([u.unit_id for u in units]))
                    .order_by(StudyWikiJob.job_id)
                )
            )
            if any(j.status in ACTIVE_JOBS for j in jobs):
                raise StateConflict("DOCUMENT_WIKI_JOBS_ACTIVE")
            assets = [self.store.get_asset(a) for a in sorted({a for t in tasks for a in t.asset_ids})]
            payload = {
                "version": "document-derived-reset-v1",
                "workspace_id": self.workspace_id,
                "source_ids": ids,
                "sources": [self.store.source_state(s) for s in ids],
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "source_id": t.source_id,
                        "source_type": t.source_type,
                        "state": t.state,
                        "binding": fingerprint(self.store._row(t)),
                    }
                    for t in tasks
                ],
                "assets": [{k: a[k] for k in ("asset_id", "sha256", "original_name", "size_bytes")} for a in assets],
                "units": [self.store._row(u) for u in units],
                "jobs": [
                    {"job_id": j.job_id, "status": j.status, "binding": fingerprint(self.store._row(j))} for j in jobs
                ],
                "wiki_pages": self._affected_wiki(session, ids),
            }
            return {**payload, "manifest_hash": fingerprint(payload)}

    def _affected_wiki(self, session, ids):
        pages = []
        snapshots = KnowledgeSnapshotStore(self.store.root / "knowledge-snapshots")
        for item in session.scalars(
            select(KnowledgeItem).where(KnowledgeItem.workspace_id == self.workspace_id).order_by(KnowledgeItem.page_id)
        ):
            if item.availability == "deleted":
                continue
            revision = session.get(KnowledgeRevision, item.current_revision_id)
            refs = _sources(revision.dependency_manifest)
            corpus = revision.dependency_manifest.get("corpus_snapshot_id")
            if corpus:
                refs.update(snapshots.read_corpus(corpus).get("sources", []))
            if not refs.intersection(ids):
                continue
            pages.append(
                {
                    "page_id": item.page_id,
                    "workspace_id": item.workspace_id,
                    "revision_id": item.current_revision_id,
                    "availability": item.availability,
                    "source_ids": sorted(refs),
                    "action": "delete" if refs <= set(ids) else "invalidate",
                }
            )
        return pages

    def _require_single_workspace(self):
        partitions = self.store.data_root / "workspaces"
        has_other_files = partitions.is_symlink() or (
            partitions.is_dir() and any(p != self.store.root for p in partitions.iterdir())
        )
        with Session(self.store.engine) as session:
            if has_other_files or rows.has_other_workspace_rows(session, self.workspace_id):
                message = "BACKUP_REQUIRES_SINGLE_WORKSPACE"
                raise StateConflict(message)

    def _backup(self, destination, manifest):
        destination = Path(destination).resolve()
        if destination.is_relative_to(self.store.data_root):
            raise ValueError("BACKUP_MUST_BE_OUTSIDE_DATA_ROOT")
        if destination.exists():
            raise ValueError("BACKUP_DESTINATION_EXISTS")
        self._require_single_workspace()
        destination.mkdir(parents=True, mode=0o700)
        # The maintenance caller stops the service. SQLite backup also handles WAL.
        online_backup(self.store.data_root / "coordination.sqlite3", destination / "coordination.sqlite3")
        for entry in self.store.root.iterdir():
            if entry.name in {
                "workspaces",
                "locks",
                "cache",
                "coordination.sqlite3",
                "coordination.sqlite3-wal",
                "coordination.sqlite3-shm",
            }:
                continue
            if entry.is_symlink():
                raise ValueError("BACKUP_SYMLINK_NOT_SUPPORTED")
            if entry.is_dir():
                shutil.copytree(entry, destination / entry.name, symlinks=True)
            elif entry.is_file():
                shutil.copy2(entry, destination / entry.name)
        for asset in manifest["assets"]:
            original = self.store.asset_path(asset["asset_id"])
            copied = destination / original.relative_to(self.store.root)
            if sha256(copied.read_bytes()).hexdigest() != asset["sha256"]:
                raise StateConflict("BACKUP_ASSET_HASH_MISMATCH")
        atomic_json(destination / "reset-manifest.json", manifest)
        return destination

    def apply(self, manifest, backup_dir):
        ids = manifest.get("source_ids", [])
        with ExitStack() as locks:
            locks.enter_context(self.store.lock("document-derived-reset"))
            for source_id in sorted(ids):
                locks.enter_context(self.store.lock("source:" + source_id))
            if self.plan(ids) != manifest:
                raise StateConflict("RESET_PLAN_CHANGED")
            backup = self._backup(backup_dir, manifest)
            stores = {
                p["workspace_id"]: KnowledgeStore(self.store, p["workspace_id"], initialize=False)
                for p in manifest["wiki_pages"]
            }
            writer = next(iter(stores.values()))._write() if stores else self.store._write()
            with writer as session:
                for source_id in ids:
                    source = require_registered(session, self.workspace_id, source_id)
                    source.active_task_id = None
                    source.published_task_id = None
                    source.generation += 1
                for t in manifest["tasks"]:
                    task = require_task(session, self.workspace_id, t["task_id"])
                    task.options = {**deepcopy(task.options), "_document_retired": True}
                    task.state = "cancelled"
                    task.progress = "Historical document extraction"
                    task.attempt_token = None
                    task.lease_until = None
                    task.updated_at = time.time()
                for u in manifest["units"]:
                    unit = session.get(StudyUnit, u["unit_id"])
                    unit.current_available = False
                    unit.auto_generation_disabled = True
                    unit.updated_at = time.time()
                for j in manifest["jobs"]:
                    job = session.get(StudyWikiJob, j["job_id"])
                    # Keep sent/unknown receipts intact; retirement never releases money.
                    if job.status not in {"outcome_unknown", "published"}:
                        job.status = "superseded"
                    job.updated_at = time.time()
                for p in manifest["wiki_pages"]:
                    item = session.get(KnowledgeItem, p["page_id"])
                    store = stores[p["workspace_id"]]
                    item.published_revision_id = None
                    gap = {
                        "code": "DOCUMENT_SOURCE_RETIRED",
                        "source_ids": sorted(set(p["source_ids"]).intersection(ids)),
                    }
                    if p["action"] == "delete":
                        item.availability = "deleted"
                        item.availability_reason = [gap]
                        request = "document-reset-" + manifest["manifest_hash"] + "-" + p["page_id"]
                        store._event(
                            session,
                            item,
                            "deleted",
                            {
                                "request_id": request,
                                "request_hash": fingerprint(request),
                                "revision_id": item.current_revision_id,
                                "reason": "document-derived-reset",
                            },
                        )
                    else:
                        item.availability = "stale"
                        item.availability_reason = [gap]
                        store._event(
                            session,
                            item,
                            "dependency_unavailable",
                            {"gaps": [gap], "revision_id": item.current_revision_id},
                        )
                    rows.retire_publications(session, self.workspace_id, item.page_id)
            receipt = {
                "source_ids": ids,
                "manifest_hash": manifest["manifest_hash"],
                "backup": str(backup),
                "wiki_pages": manifest["wiki_pages"],
                "retired_at": time.time(),
            }
            atomic_json(backup / "reset-receipt.json", receipt)
            return receipt
