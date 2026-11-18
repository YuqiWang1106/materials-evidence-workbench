"""Bounded, source-owned physical cleanup. Shared objects and execution history survive."""

import json
import re
import shutil
from pathlib import Path

from sqlalchemy import delete, select

from .core import StateConflict, atomic_json, fingerprint
from .document_derived_reset import _sources
from .storage import lifecycle_repository as rows
from .storage.asset_repository import get_asset
from .storage.models import (
    KnowledgeDependency,
    KnowledgeIndexCurrent,
    KnowledgeIndexEntry,
    KnowledgeIndexGeneration,
    KnowledgePublication,
    KnowledgeRevision,
    KnowledgeSupportRow,
    StudyUnit,
    StudyUnitRevision,
    StudyWikiJob,
)


def read_json(path):
    if path.is_symlink():
        raise StateConflict("Unsafe cleanup path")
    if not path.exists():
        return {}
    if path.stat().st_size > 64 * 1024 * 1024:
        raise StateConflict("Cleanup artifact too large")
    return json.loads(path.read_text())


def has_unknown_requests(root):
    for path in root.rglob("*budget*.json") if root.exists() else []:
        data = read_json(path)
        if any(
            row.get("state") in {"sent", "unknown"} or row.get("state") == "pending" and row.get("dispatched")
            for row in data.get("requests", {}).values()
        ):
            return True
        if data.get("pending"):
            return True
    return False


def task_refs(value):
    if isinstance(value, dict):
        return {v for k, v in value.items() if k in {"task_id", "source_task_id"} and isinstance(v, str)} | set().union(
            *(task_refs(v) for v in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(task_refs(v) for v in value), set())
    return set()


def owned_tree(root, path):
    parts = path.relative_to(root).parts
    if parts[0] == "table_artifacts":
        count = 3
    elif parts[:3] == ("document_reports", "revisions-v1", "units"):
        count = 4
    elif parts[0] in {"standard_reviews", "document_reports", "standard_jobs", "standard_batches"}:
        count = 2
    else:
        return path
    return root / Path(*parts[:count])


def states(value):
    if isinstance(value, dict):
        return {v for k, v in value.items() if k in {"state", "status"} and isinstance(v, str)} | set().union(
            *(states(v) for v in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(states(v) for v in value), set())
    return set()


def publication_blocked(store, manifest):
    ids = set(manifest["source_ids"])
    for path in (store.root / "table_artifacts").glob("*/*/artifact.json"):
        if _sources(read_json(path)) & ids:
            status = read_json(path.parent / "publication.json").get("status")
            if status == "write_unknown":
                return True
    trees = set()
    for folder in ("document_reports", "standard_reviews", "standard_jobs", "standard_batches"):
        for path in (store.root / folder).rglob("*.json"):
            data = read_json(path)
            if _sources(data) & ids or task_refs(data) & set(manifest["task_ids"]):
                trees.add(owned_tree(store.root, path))
    for tree in trees:
        for path in tree.rglob("*.json"):
            if path.name not in {"state.json", "job.json", "batch.json", "index.json", "model-budget.json"}:
                continue
            if states(read_json(path)) & {
                "queued",
                "running",
                "publishing",
                "prepared",
                "sent",
                "unknown",
                "write_unknown",
                "outcome_unknown",
            }:
                return True
    return False


def hashes(value):
    if isinstance(value, str):
        return {value} if re.fullmatch("[a-f0-9]{64}", value) else set()
    if isinstance(value, dict):
        return set().union(*(hashes(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(hashes(v) for v in value), set())
    return set()


def safe_path(root, relative):
    path = root / relative
    requested = Path(relative)
    if path == root or ".." in requested.parts or requested.is_absolute() or requested.parts[0] == "workspaces":
        raise StateConflict("Unsafe cleanup path")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise StateConflict("Unsafe cleanup link")
        if part == root:
            break
    if not path.resolve().is_relative_to(root):
        raise StateConflict("Unsafe cleanup path")
    return path


def build_file_plan(store, manifest):
    ids = set(manifest["source_ids"])
    pages = {w["page_id"] for w in manifest["wiki"] if w["exclusive"]}
    tasks = set(manifest["task_ids"])
    paths = set()
    # Whole task trees own screenshots and binaries as well as JSON artifacts.
    for tid in tasks:
        paths.add(str(store.pending_dir(tid, include_unavailable=True).relative_to(store.root)))
    for aid in manifest["exclusive_assets"]:
        asset = store.get_asset(aid)
        paths.add("originals/" + asset["storage_name"])
        paths.add("cache/document-reading/" + asset["sha256"])
    # Source-qualified review and table trees, never history conversations or private config.
    for folder in (
        "table_artifacts",
        "region_reviews",
        "region_sheet_reviews",
        "standard_reviews",
        "document_reports",
        "standard_jobs",
        "standard_batches",
    ):
        base = store.root / folder
        for path in base.rglob("*.json") if base.exists() else []:
            data = read_json(path)
            refs = _sources(data)
            if (refs and refs <= ids) or task_refs(data) & tasks:
                paths.add(str(owned_tree(store.root, path).relative_to(store.root)))
    # Cached model answers may be shared only when another task actually records that request.
    owned_requests, retained_requests = set(), set()
    for path in (store.root / "pending").glob("*/**/*budget*.json"):
        data = read_json(path)
        requests = set(data.get("requests", {}))
        if path.relative_to(store.root / "pending").parts[0] in tasks:
            owned_requests |= requests
        else:
            retained_requests |= requests
    for request_hash in owned_requests - retained_requests:
        if re.fullmatch("[a-f0-9]{64}", request_hash):
            paths.add("cache/model/" + request_hash + ".json")
    # Content-addressed archives: remove owned manifests/packages and only unshared blobs.
    for namespace in ("archive", "knowledge-snapshots"):
        base = store.root / namespace
        objects = base / "objects"
        data_by_hash = {}
        for path in objects.iterdir() if objects.exists() else []:
            if path.is_symlink():
                raise StateConflict("Unsafe snapshot link")
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                prefix = stream.read(64).lstrip()
            if not prefix.startswith((b"{", b"[")):
                continue
            try:
                data_by_hash[path.name] = read_json(path)
            except (ValueError, UnicodeDecodeError):
                continue  # Binary original, reached through a manifest.
        owned = set()
        for key, data in data_by_hash.items():
            refs = _sources(data)
            page = data.get("page_id") if isinstance(data, dict) else None
            if (refs and (refs <= ids | pages or namespace == "knowledge-snapshots" and refs & ids)) or page in pages:
                owned.add(key)
        # Commit receipts are authoritative roots and include source identity.
        for path in (base / "commits").iterdir() if (base / "commits").exists() else []:
            data = read_json(path)
            refs = _sources(data)
            if refs and refs <= ids | pages:
                paths.add(str(path.relative_to(store.root)))
                owned |= hashes(data)
        closure = set(owned)
        while True:
            next_set = closure | set().union(*(hashes(data_by_hash.get(k, {})) for k in closure), set())
            if next_set == closure:
                break
            closure = next_set
        protected = set()
        for key, data in data_by_hash.items():
            if key not in closure:
                protected |= hashes(data)
        # References reachable from remaining objects protect their entire subtree.
        while True:
            more = protected | set().union(*(hashes(data_by_hash.get(k, {})) for k in protected), set())
            if more == protected:
                break
            protected = more
        for key in closure - protected:
            if (objects / key).exists():
                paths.add(str((objects / key).relative_to(store.root)))
        for path in (base / "manifests").iterdir() if (base / "manifests").exists() else []:
            refs = _sources(read_json(path))
            if refs & ids:
                paths.add(str(path.relative_to(store.root)))
    return sorted(paths)


def audit_tree(store, root, audit_id):
    audit = store.root / "source-audit" / audit_id
    # Retain exact monetary/execution ledgers, but not cached model responses.
    for path in root.rglob("*") if root.exists() else []:
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in {"state.json", "job.json", "batch.json", "revision.json"}:
            allowed = {
                "job_id",
                "batch_id",
                "task_id",
                "source_id",
                "revision_id",
                "request_id",
                "call_id",
                "attempt_id",
                "status",
                "state",
                "attempt",
                "generation",
                "request_started",
                "request_hash",
                "request_sha256",
                "input_sha256",
                "output_sha256",
                "result_sha256",
                "response_sha256",
                "report_sha256",
                "revision_sha256",
                "service_identity",
                "model",
                "provider",
                "prompt_version",
                "created_at",
                "updated_at",
                "completed_at",
                "started_at",
                "sent_at",
                "finished_at",
                "elapsed_seconds",
                "duration_seconds",
                "request_state",
                "receipt_id",
                "id",
                "usage",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "amount_micro_usd",
                "reserved_micro_usd",
                "settled_micro_usd",
                "actual_micro_usd",
                "cost_micro_usd",
                "cost_usd",
                "budget_receipt",
                "requests",
                "revisions",
                "unit",
                "input_version",
                "candidate_id",
                "study_ref",
            }

            def metadata(value, dynamic=False):
                if isinstance(value, dict):
                    return {
                        k: metadata(v, k in {"requests", "revisions"})
                        for k, v in value.items()
                        if dynamic or k in allowed
                    }
                if isinstance(value, list):
                    return [metadata(v) for v in value if isinstance(v, dict)]
                return value

            atomic_json(audit / path.relative_to(root), metadata(read_json(path)))
            continue
        if path.name not in {
            "model-budget.json",
            "model-attempts.jsonl",
            "program-model-attempts.jsonl",
            "cost-ledger.jsonl",
        }:
            continue
        relative = path.relative_to(root)
        dest = audit / relative
        if path.name == "cost-ledger.jsonl":
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        elif path.suffix == ".jsonl":
            rows = []
            for line in path.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            # Allowlisted technical metadata, never raw requests/responses.
            allowed = {
                "attempt_id",
                "phase",
                "status",
                "call_id",
                "request_hash",
                "request_sha256",
                "result_sha256",
                "started_at",
                "ended_at",
                "elapsed_seconds",
                "duration_seconds",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost",
                "cost_usd",
                "model",
                "timestamp",
                "created_at",
                "updated_at",
            }
            atomic_json(dest.with_suffix(".json"), [{k: v for k, v in row.items() if k in allowed} for row in rows])
        else:
            data = read_json(path)
            for row in data.get("requests", {}).values():
                row.pop("result", None)
            atomic_json(dest, data)


def purge_files(store, session, manifest):
    # Recheck references in the same SQLite write transaction as asset-row cleanup.
    ids = set(manifest["source_ids"])
    other = rows.other_asset_ids(session, store.workspace_id, ids)
    protected = set()
    for aid in other & set(manifest["assets"]):
        asset = get_asset(session, store.workspace_id, aid)
        if asset:
            protected |= {
                "originals/" + asset.storage_name,
                "cache/document-reading/" + asset.sha256,
                "archive/objects/" + asset.sha256,
                "knowledge-snapshots/objects/" + asset.sha256,
            }
    for relative in manifest["purge_paths"]:
        path = safe_path(store.root, relative)
        if path.is_dir() and relative.split("/")[0] in {
            "pending",
            "standard_jobs",
            "standard_batches",
            "document_reports",
        }:
            audit_tree(store, path, fingerprint(relative))
    for relative in manifest["purge_paths"]:
        if relative in protected:
            continue
        path = safe_path(store.root, relative)
        if path.is_dir():
            # Refuse directory symlinks, even deep inside owned trees.
            if any(p.is_symlink() for p in path.rglob("*")):
                raise StateConflict("Unsafe cleanup link")
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def purge_rows(store, session, manifest):
    ids = manifest["source_ids"]
    units = list(
        session.scalars(
            select(StudyUnit).where(StudyUnit.workspace_id == store.workspace_id, StudyUnit.source_id.in_(ids))
        )
    )
    for unit in units:
        for job in session.scalars(select(StudyWikiJob).where(StudyWikiJob.unit_id == unit.unit_id)):
            job.input_payload = {}
            job.provider_response = None
            job.result = None
        for rev in session.scalars(select(StudyUnitRevision).where(StudyUnitRevision.unit_id == unit.unit_id)):
            rev.payload = {}
    pages = [w["page_id"] for w in manifest["wiki"] if w["exclusive"]]
    for rev in session.scalars(
        select(KnowledgeRevision).where(
            KnowledgeRevision.workspace_id == store.workspace_id, KnowledgeRevision.page_id.in_(pages)
        )
    ):
        rev.content = {"deleted": True}
        session.execute(delete(KnowledgeSupportRow).where(KnowledgeSupportRow.revision_id == rev.revision_id))
        session.execute(delete(KnowledgeDependency).where(KnowledgeDependency.revision_id == rev.revision_id))
        rev.dependency_manifest = {"deleted": True}
    for pub in session.scalars(
        select(KnowledgePublication).where(
            KnowledgePublication.workspace_id == store.workspace_id, KnowledgePublication.page_id.in_(pages)
        )
    ):
        pub.package = {}
        pub.asset_hashes = {}
    # Remove index text for affected sources, keeping unrelated generation data intact.
    affected = set()
    for entry in session.scalars(
        select(KnowledgeIndexEntry).where(KnowledgeIndexEntry.workspace_id == store.workspace_id)
    ):
        if (
            _sources(entry.source_identity) & set(ids)
            or _sources(entry.ref) & set(ids)
            or entry.ref.get("page_id") in pages
        ):
            affected.add(entry.generation_id)
            session.delete(entry)
    session.execute(delete(KnowledgeIndexCurrent).where(KnowledgeIndexCurrent.generation_id.in_(affected)))
    for gid in affected:
        generation = session.get(KnowledgeIndexGeneration, gid)
        generation.state = "stale"
    rows.purge_ingestion_rows(session, store.workspace_id, ids, manifest["exclusive_assets"])


def withdraw_graph(store, session, source_ids):
    from .storage.entity_store import EntityStore
    from .storage.models import KbAttribute, KbEntity, KbRelated

    workspace = store.workspace_id
    for sid in source_ids:
        touched = {
            a.entity_id
            for a in session.scalars(select(KbAttribute).join(KbEntity).where(KbEntity.workspace_id == workspace))
            if a.source_id == sid or any(c.get("source_id") == sid for c in a.citations or [])
        }
        EntityStore(store).write(workspace, sid, {"entities": [], "attributes": [], "related": []}, session=session)
        for eid in touched:
            entity = session.get(KbEntity, eid)
            if entity is None or entity.workspace_id != workspace:
                continue
            used = session.scalar(
                select(KbAttribute.attribute_id)
                .where(KbAttribute.workspace_id == workspace, KbAttribute.entity_id == eid)
                .limit(1)
            )
            used = used or session.scalar(
                select(KbRelated.related_id)
                .where(
                    KbRelated.workspace_id == workspace,
                    (KbRelated.entity_a == eid) | (KbRelated.entity_b == eid),
                )
                .limit(1)
            )
            if not used:
                session.delete(entity)
