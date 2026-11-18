"""Version-bound source commands. No model calls or paid retries are made here."""

import json
import time
import uuid
from contextlib import ExitStack
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import StateConflict, fingerprint
from .document_derived_reset import _sources
from .knowledge_store import KnowledgeStore
from .storage import lifecycle_repository as rows
from .storage.asset_repository import get_asset
from .storage.models import (
    KnowledgeItem,
    KnowledgePublication,
    KnowledgeRevision,
    SourceOperation,
    StudyUnit,
    StudyWikiJob,
    Task,
)
from .storage.reading_repository import get_draft
from .storage.source_repository import get_source, require_registered
from .storage.upload_files import digest_locks

BUSY = {"queued", "reading", "extracting", "validating", "publishing", "write_unknown"}
MESSAGES = {
    "SOURCE_BUSY": "资料正在处理，请结束当前任务后再重新解析",
    "SOURCE_PUBLICATION_UNKNOWN": "存在正在保存或结果未知的写入，请先完成核对",
    "SOURCE_MODEL_UNKNOWN": "存在结果未知的模型请求，请先核对执行记录",
    "SOURCE_DEPENDENT_BUSY": "关联的知识生成任务仍在处理，请结束后重试",
    "SOURCE_ORIGINAL_UNAVAILABLE": "原件缺失或校验失败，无法重新解析",
}


def public_operation(row):
    return {k: getattr(row, k) for k in ("operation_id", "source_id", "kind", "state", "result", "error", "updated_at")}


def require_source(store, source_id, *, generation=None, session=None):
    def check(s):
        row = get_source(s, store.workspace_id, source_id)
        if row is not None and row.lifecycle != "active":
            raise StateConflict("来源已删除" if row.lifecycle in {"deleting", "deleted"} else "来源已撤下")
        if row is not None and generation is not None and generation < row.visibility_generation:
            raise StateConflict("来源正在重新解析，旧结果已撤下")

    if session is None:
        with Session(store.engine) as s:
            check(s)
    else:
        check(session)


def source_available(store, source_id, generation=None):
    try:
        require_source(store, source_id, generation=generation)
        return True
    except StateConflict:
        return False


class SourceLifecycle:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.workspace_id = self.store.workspace_id

    def _manifest(self, session, source_id):
        source = require_registered(session, self.workspace_id, source_id)
        ids = {source_id}
        drafts = list(session.scalars(rows.drafts(self.workspace_id)))
        while True:
            children = {
                d.source_id for d in drafts if d.recipe.get("standard_review", {}).get("parent_source_id") in ids
            }
            if children <= ids:
                break
            ids |= children
        tasks = list(
            session.scalars(
                rows.tasks(self.workspace_id).where(Task.source_id.in_(ids)).order_by(Task.created_at.desc())
            )
        )
        owned_drafts = [d for d in drafts if d.source_id in ids]
        asset_ids = {a for t in tasks for a in t.asset_ids} | {a for d in owned_drafts for a in d.asset_ids}
        other_assets = rows.other_asset_ids(session, self.workspace_id, ids)
        title = next((d.title for d in owned_drafts if d.source_id == source_id), None)
        current = next((t for t in tasks if t.source_id == source_id), None)
        if not title and current:
            names = [
                asset.original_name
                for aid in current.asset_ids
                if (asset := get_asset(session, self.workspace_id, aid))
            ]
            title = current.options.get("title") or (
                "资料组（%s 份文献）" % len(names) if len(names) > 1 else next(iter(names), source_id)
            )
        return {
            "workspace_id": self.workspace_id,
            "source_id": source_id,
            "source_ids": sorted(ids),
            "generation": source.generation,
            "title": title or source_id,
            "source_type": current.source_type if current else "experimental",
            "task_ids": sorted(t.task_id for t in tasks),
            "drafts": [
                {"source_id": d.source_id, "revision": d.revision, "hash": d.draft_sha256} for d in owned_drafts
            ],
            "assets": sorted(asset_ids),
            "exclusive_assets": sorted(asset_ids - other_assets),
            "wiki": self._affected_wiki(session, ids),
        }

    def _affected_wiki(self, session, ids):
        wiki = []
        from .knowledge_snapshot import KnowledgeSnapshotStore

        snapshots = KnowledgeSnapshotStore(self.store.root / "knowledge-snapshots")
        for item in session.scalars(select(KnowledgeItem).where(KnowledgeItem.workspace_id == self.workspace_id)):
            refs = set()
            for rev in session.scalars(
                select(KnowledgeRevision).where(
                    KnowledgeRevision.page_id == item.page_id, KnowledgeRevision.workspace_id == self.workspace_id
                )
            ):
                refs |= _sources(rev.dependency_manifest)
                corpus = rev.dependency_manifest.get("corpus_snapshot_id")
                if corpus:
                    from .knowledge_snapshot import KnowledgeIntegrityError

                    try:
                        # Only the immutable dependency manifest is needed, never its retired content blobs.
                        refs |= set(json.loads(snapshots._read("manifests", corpus)).get("sources", []))
                    except KnowledgeIntegrityError:
                        previous = [
                            w
                            for op in session.scalars(rows.operations(self.workspace_id))
                            for w in op.manifest.get("wiki", [])
                            if w["page_id"] == item.page_id and w.get("source_ids")
                        ]
                        if previous:
                            refs |= set().union(*(set(w["source_ids"]) for w in previous))
                        else:
                            refs.add("unavailable-corpus:" + corpus)  # Never infer exclusive ownership.
            if refs & ids:
                wiki.append(
                    {
                        "page_id": item.page_id,
                        "workspace_id": item.workspace_id,
                        "revision_id": item.current_revision_id,
                        "source_ids": sorted(refs),
                        "exclusive": refs <= ids,
                    }
                )
        return sorted(wiki, key=lambda w: w["page_id"])

    def _blocker(self, session, manifest, *, reparse):
        tasks = list(session.scalars(rows.tasks(self.workspace_id).where(Task.task_id.in_(manifest["task_ids"]))))
        if any(t.state in {"publishing", "write_unknown"} for t in tasks):
            return "SOURCE_PUBLICATION_UNKNOWN"
        if session.scalar(
            select(KnowledgePublication.publication_id)
            .where(
                KnowledgePublication.workspace_id == self.workspace_id,
                KnowledgePublication.page_id.in_([w["page_id"] for w in manifest["wiki"]]),
                KnowledgePublication.status.in_(["prepared", "publishing", "write_unknown"]),
            )
            .limit(1)
        ):
            return "SOURCE_PUBLICATION_UNKNOWN"
        units = select(StudyUnit.unit_id).where(
            StudyUnit.workspace_id == self.workspace_id, StudyUnit.source_id.in_(manifest["source_ids"])
        )
        if session.scalar(
            select(StudyWikiJob.job_id)
            .where(
                StudyWikiJob.unit_id.in_(units), StudyWikiJob.status.in_(["running", "publishing", "outcome_unknown"])
            )
            .limit(1)
        ):
            return "SOURCE_DEPENDENT_BUSY"
        from .source_purge import publication_blocked

        if publication_blocked(self.store, manifest):
            return "SOURCE_PUBLICATION_UNKNOWN"
        if reparse:
            if any(t.state in BUSY for t in tasks):
                return "SOURCE_BUSY"
            from .source_purge import has_unknown_requests

            if any(has_unknown_requests(self.store.root / "pending" / t.task_id) for t in tasks):
                return "SOURCE_MODEL_UNKNOWN"
            try:
                for aid in manifest["assets"]:
                    self.store.asset_bytes(aid)
            except (KeyError, OSError, StateConflict):
                return "SOURCE_ORIGINAL_UNAVAILABLE"
        return None

    def management(self, source_id):
        with Session(self.store.engine) as session:
            manifest = self._manifest(session, source_id)
            source = require_registered(session, self.workspace_id, source_id)
            operation = session.scalar(
                rows.operations(self.workspace_id)
                .where(SourceOperation.source_id == source_id, SourceOperation.kind == "delete")
                .order_by(SourceOperation.created_at.desc())
            )
            blocked = source.lifecycle in {"deleting", "deleted", "withdrawn"}
            deletion = self._blocker(session, manifest, reparse=False) if not blocked else "来源已删除或正在删除"
            reparse = self._blocker(session, manifest, reparse=True) if not blocked else "来源已删除或正在删除"
            if not reparse and manifest["source_type"] in {"paper", "patent"}:
                try:
                    self.service._require_native_document_readiness()
                    latest = session.scalar(
                        rows.tasks(self.workspace_id)
                        .where(Task.source_id == source_id)
                        .order_by(Task.created_at.desc())
                    )
                    # Validate current server configuration, not the old pipeline fingerprint.
                    self.service._require_document_configuration(
                        {**self.store._row(latest), "options": self.service.document_task_options()}
                    )
                except (ValueError, StateConflict):
                    reparse = "解析环境或模型配置尚未就绪"
            return {
                "source_id": source_id,
                "title": manifest["title"],
                "generation": manifest["generation"],
                "impact_token": fingerprint(manifest),
                "lifecycle": source.lifecycle,
                "impact": {
                    "sources": len(manifest["source_ids"]),
                    "tasks": len(manifest["task_ids"]),
                    "originals": len(manifest["exclusive_assets"]),
                    "shared_originals": len(manifest["assets"]) - len(manifest["exclusive_assets"]),
                    "wiki_deleted": sum(w["exclusive"] for w in manifest["wiki"]),
                    "wiki_invalidated": sum(not w["exclusive"] for w in manifest["wiki"]),
                },
                "can_delete": not deletion,
                "delete_reason": MESSAGES.get(deletion, deletion),
                "can_reparse": not reparse,
                "reparse_reason": MESSAGES.get(reparse, reparse),
                "operation": public_operation(operation) if operation else None,
            }

    def pending_rows(self):
        with Session(self.store.engine) as session:
            pending = session.scalars(
                rows.operations(self.workspace_id).where(
                    SourceOperation.kind == "delete", SourceOperation.state != "completed"
                )
            )
            return [
                {
                    "task_id": row.operation_id,
                    "source_id": row.source_id,
                    "source_type": row.manifest["source_type"],
                    "title": row.manifest["title"],
                    "state": "deleting",
                    "source_operation": public_operation(row),
                    "processing": {"stage": "deleting", "determinate": False},
                }
                for row in pending
            ]

    def operation(self, operation_id):
        with Session(self.store.engine) as session:
            return public_operation(rows.operation(session, self.workspace_id, operation_id))

    def _withdraw(self, session, manifest, *, deleting):
        now = time.time()
        for sid in manifest["source_ids"]:
            row = require_registered(session, self.workspace_id, sid)
            row.published_task_id = None
            row.active_task_id = None
            row.visibility_generation = row.generation + 1
            row.lifecycle = "deleting" if deleting else ("active" if sid == manifest["source_id"] else "withdrawn")
        for task in session.scalars(rows.tasks(self.workspace_id).where(Task.task_id.in_(manifest["task_ids"]))):
            if task.state in BUSY or task.state == "needs_review":
                task.state = "cancelled" if deleting else "superseded"
            task.attempt_token = None
            task.lease_until = None
            task.updated_at = now
        for unit in session.scalars(
            select(StudyUnit).where(
                StudyUnit.workspace_id == self.workspace_id, StudyUnit.source_id.in_(manifest["source_ids"])
            )
        ):
            unit.current_available = False
            unit.auto_generation_disabled = True
            unit.updated_at = now
            for job in session.scalars(select(StudyWikiJob).where(StudyWikiJob.unit_id == unit.unit_id)):
                if job.status not in {"outcome_unknown", "published"}:
                    job.status = "superseded"
        from .source_purge import withdraw_graph

        withdraw_graph(self.store, session, manifest["source_ids"])
        self._withdraw_wiki(session, manifest, deleting=deleting)

    def _withdraw_wiki(self, session, manifest, *, deleting):
        for w in manifest["wiki"]:
            item = session.get(KnowledgeItem, w["page_id"])
            if item.availability == "deleted":
                continue
            item.published_revision_id = None
            item.availability = "deleted" if deleting and w["exclusive"] else "stale"
            item.availability_reason = [
                {"code": "SOURCE_DELETED" if deleting else "SOURCE_REPARSING", "source_ids": manifest["source_ids"]}
            ]
            knowledge = KnowledgeStore(self.store, w["workspace_id"], initialize=False)
            knowledge._event(
                session,
                item,
                "deleted" if item.availability == "deleted" else "dependency_unavailable",
                {
                    "revision_id": item.current_revision_id,
                    "gaps": item.availability_reason,
                    "request_id": "source-lifecycle-" + uuid.uuid4().hex,
                    "request_hash": fingerprint(manifest),
                },
            )
            rows.retire_publications(session, self.workspace_id, item.page_id)

    def command(self, source_id, payload, *, kind):
        digest = fingerprint([source_id, kind, payload])
        # A stable lock serializes management commands; source locks fence existing publication paths.
        with self.store.lock("source-lifecycle"), ExitStack() as locks:
            with Session(self.store.engine) as session:
                old = rows.by_request(session, self.workspace_id, payload["request_id"])
                if old:
                    if old.request_hash != digest:
                        raise StateConflict("REQUEST_ID_CONFLICT")
                    return public_operation(old)
                manifest = self._manifest(session, source_id)
            for sid in manifest["source_ids"]:
                locks.enter_context(self.store.lock("source:" + sid))
            for page in sorted(w["page_id"] for w in manifest["wiki"]):
                locks.enter_context(self.store.lock("wiki-publication:" + page))
            knowledge = KnowledgeStore(self.store, self.workspace_id, initialize=False)
            with knowledge._write(require_ledger=bool(manifest["wiki"])) as session:
                fresh = self._manifest(session, source_id)
                require_source(self.store, source_id, session=session)
                if (
                    fresh != manifest
                    or payload["expected_generation"] != fresh["generation"]
                    or payload["impact_token"] != fingerprint(fresh)
                ):
                    raise StateConflict("资料或关联内容已变化，请刷新后重新确认")
                blocker = self._blocker(session, manifest, reparse=kind == "reparse")
                if blocker:
                    raise StateConflict(MESSAGES[blocker])
                if kind == "reparse" and manifest["source_type"] in {"paper", "patent"}:
                    self.service._require_native_document_readiness()
                    self.service._require_document_configuration(
                        {"source_type": manifest["source_type"], "options": self.service.document_task_options()}
                    )
                now = time.time()
                op = SourceOperation(
                    workspace_id=self.workspace_id,
                    operation_id="source-op-" + uuid.uuid4().hex,
                    source_id=source_id,
                    request_id=payload["request_id"],
                    request_hash=digest,
                    kind=kind,
                    state="stopping" if kind == "delete" else "completed",
                    manifest=deepcopy(manifest),
                    result={},
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(op)
                if kind == "reparse":
                    from .source_purge import read_json

                    raw_ids = []
                    for path in (self.store.root / "table_artifacts").glob("*/*/artifact.json"):
                        if _sources(read_json(path)) & set(manifest["source_ids"]):
                            raw_ids.append(path.parent.name)
                    manifest["withdrawn_artifacts"] = sorted(raw_ids)
                    op.manifest = deepcopy(manifest)
                self._withdraw(session, manifest, deleting=kind == "delete")
                if kind == "reparse":
                    draft = get_draft(session, self.workspace_id, source_id)
                    if draft:
                        draft.revision += 1
                        draft.recipe = {
                            "version": "1",
                            "entry_profile": "table_regions_v1",
                            "region_review": {"version": "1", "regions": []},
                        }
                        draft.updated_at = now
                        self.store._refresh_draft_hash(draft)
                        src = require_registered(session, self.workspace_id, source_id)
                        src.generation += 1
                        op.result = {
                            "source_id": source_id,
                            "draft_revision": draft.revision,
                            "route": "#regions/" + source_id,
                        }
                    else:
                        latest = rows.latest_task(session, self.workspace_id, source_id)
                        options = {
                            k: deepcopy(v)
                            for k, v in latest.options.items()
                            if not k.startswith("_") and k not in {"asset_bindings"}
                        }
                        options.update(self.service.document_task_options())
                        options["_full_reparse_run"] = op.operation_id
                        task = self.store._submit_in_session(
                            session,
                            source_id,
                            latest.source_type,
                            latest.asset_ids,
                            options,
                            fingerprint([op.operation_id, latest.asset_ids]),
                            expected_revision=manifest["generation"],
                        )
                        op.result = {
                            "source_id": source_id,
                            "task_id": task["task_id"],
                            "route": "#task/" + task["task_id"],
                        }
                session.flush()
                return public_operation(op)

    def recover(self):
        with Session(self.store.engine) as session:
            ids = list(
                session.scalars(
                    select(SourceOperation.operation_id).where(
                        SourceOperation.workspace_id == self.workspace_id,
                        SourceOperation.kind == "delete",
                        SourceOperation.state.in_(["stopping", "purging", "failed"]),
                    )
                )
            )
        for oid in ids:
            try:
                self._purge(oid)
            except Exception:
                # Never expose file paths, document text, or provider responses.
                with self.store._write() as session:
                    op = rows.operation(session, self.workspace_id, oid)
                    op.state = "failed"
                    op.error = "清理未完成，后台会继续重试"
                    op.updated_at = time.time()

    def _purge(self, operation_id):
        from .source_purge import build_file_plan, purge_files, purge_rows

        with self.store.lock("source-lifecycle"), ExitStack() as locks:
            with Session(self.store.engine) as session:
                op = rows.operation(session, self.workspace_id, operation_id)
                if op.state == "completed":
                    return
                manifest = deepcopy(op.manifest)
            for sid in manifest["source_ids"]:
                locks.enter_context(self.store.lock("source:" + sid))
            for page in sorted(w["page_id"] for w in manifest["wiki"]):
                locks.enter_context(self.store.lock("wiki-publication:" + page))
            for tid in manifest["task_ids"]:
                if tid in self.service._running:
                    return
                acquired = locks.enter_context(self.store.try_lock("task-worker:" + tid))
                if not acquired:
                    return
            # Registration and source deletion serialize ownership of the same original.
            digests = (aid.removeprefix("asset-") for aid in manifest["exclusive_assets"])
            locks.enter_context(digest_locks(self.store.data_root, self.store.root, digests))
            if "purge_paths" not in manifest:
                manifest["purge_paths"] = build_file_plan(self.store, manifest)
            with self.store._write() as session:
                op = rows.operation(session, self.workspace_id, operation_id)
                op.state = "purging"
                op.error = None
                op.manifest = manifest
            with self.store._write() as session:
                purge_files(self.store, session, manifest)
                purge_rows(self.store, session, manifest)
                for sid in manifest["source_ids"]:
                    row = require_registered(session, self.workspace_id, sid)
                    row.lifecycle = "deleted"
                    row.generation += 1
                op = rows.operation(session, self.workspace_id, operation_id)
                op.state = "completed"
                op.error = None
                op.result = {"deleted": True}
                op.updated_at = time.time()
