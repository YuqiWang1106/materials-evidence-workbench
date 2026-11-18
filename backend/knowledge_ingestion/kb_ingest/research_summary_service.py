"""Explicit research summary jobs and paid-result reuse, with Wiki revision CAS."""

import asyncio
import difflib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from .core import StateConflict, fingerprint
from .llm.project_model_dispatch import ProjectModelDispatcher
from .research_summary_contracts import SummaryRequest
from .research_summary_model import generate_summary
from .research_summary_store import ResearchSummaryFiles
from .research_summary_supports import freeze_window, no_self_reference, validate_content, validate_frozen

ACTIVE = {"queued", "running", "saving", "publishing"}
_DEFAULT_DISPATCHER = object()


def readable(content):
    return "\n".join(
        [content["title"]]
        + [
            section["heading"] + "\n" + "\n".join(c["text"] for c in section["claims"])
            for section in content["sections"]
        ]
    )


class ResearchWikiSummaryService:
    def __init__(self, research, wiki, *, pool=None, dispatcher=_DEFAULT_DISPATCHER):
        if research.files.store is not wiki.ingestion_service.store:
            raise ValueError("RESEARCH_STORE_MISMATCH")
        self.research = research
        self.wiki = wiki
        wiki.research_service = research
        self._configured_dispatcher = dispatcher
        self.files = ResearchSummaryFiles(research.files.store, wiki.workspace_id, wiki.collection_id)
        self._own_pool = pool is None
        self.pool = pool or ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-summary")
        self._closed = False

    @property
    def dispatcher(self):
        # Explicit None means Wiki is disabled, even if research has a key.
        return (
            self.research.dispatcher
            if self._configured_dispatcher is _DEFAULT_DISPATCHER
            else self._configured_dispatcher
        )

    def _ready(self):
        return isinstance(self.dispatcher, ProjectModelDispatcher)

    def _public(self, job):
        keys = (
            "job_id",
            "chat_id",
            "status",
            "request",
            "page_id",
            "error",
            "created_at",
            "updated_at",
            "events",
            "input_hash",
            "proposal",
        )
        result = {k: deepcopy(job.get(k)) for k in keys}
        result.update(
            {k: deepcopy(job[k]) for k in ("saved_revision", "apply_authorized", "publication_mode") if k in job}
        )
        result["can_save_retry"] = job["status"] in {"save_failed", "publish_failed"}
        result["automatic_retries"] = False
        result["message"] = {
            "save_failed": "模型结果已保存。可重试存稿，不会再次调用模型。",
            "publish_failed": "总结修订已保存，发布未完成。可重试发布，不会再次调用模型或创建修订。",
            "failed": "整理未完成；模型请求可能已发送，未自动重发。请先检查已有结果。",
            "interrupted": "整理被中断，未自动重发；请先查看既有结果再主动提交新请求。",
            "cancelled": "整理已取消；在途模型请求的结果可能未知，未自动重发。",
            "proposal_ready": "提案已保存，等待显式应用；目标页尚未更改。",
        }.get(job["status"], "")
        return result

    def get(self, cid, jid):
        return self._public(self.files.job(cid, jid)[0])

    def list(self, cid, offset=0, limit=25):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("INVALID_SUMMARY_PAGE")
        self.research.files.read_chat(cid)
        jobs = [
            j for jid in self.files.ids() if (j := self.files.read(self.files.path(jid) / "job.json"))["chat_id"] == cid
        ]
        jobs.sort(key=lambda j: (-j["created_at"], j["job_id"]))
        return {"jobs": [self._public(j) for j in jobs[offset : offset + limit]]}

    def enqueue(self, cid, payload):
        request = SummaryRequest.model_validate(payload).model_dump(mode="json")
        jid = "summary-" + fingerprint([cid, request["request_id"]])
        with self.files.job_lock(jid):
            if (self.files.path(jid) / "job.json").exists():
                old, _ = self.files.job(cid, jid)
                if old["request"] != request:
                    raise StateConflict("REQUEST_ID_CONFLICT")
                return self._public(old)
            if self._closed or not self._ready():
                raise ValueError("RESEARCH_MODEL_NOT_READY")
            snapshot = freeze_window(
                self.research, self.wiki, cid, request["end_turn_id"], max_turns=request["max_turns"]
            )
            no_self_reference(self.wiki, snapshot["manifest"]["research_summary"], request["target_page_id"])
            if request["target_page_id"]:
                page = self.wiki.get_page(request["target_page_id"])
                self._base(page, request)
                snapshot["base_content"] = page["revision"]["content"]
            now = time.time()
            job = {
                "job_id": jid,
                "chat_id": cid,
                "request": request,
                "input_hash": fingerprint(snapshot),
                "status": "queued",
                "attempt": uuid.uuid4().hex,
                "page_id": None,
                "proposal": None,
                "error": None,
                "events": [],
                "created_at": now,
                "updated_at": now,
            }
            from .research_summary_unified import is_unified

            if is_unified(snapshot["manifest"]):
                job.update(publication_mode="unified_chat_summary_auto", saved_revision=None, apply_authorized=False)
            self.files.write(self.files.path(jid) / "input.json", snapshot)
            self.files.save(job)
        self.pool.submit(self._run, cid, jid)
        return self.get(cid, jid)

    @staticmethod
    def _base(page, request):
        if (
            page["current_revision"] != request["base_revision"]
            or page["revision"]["content_hash"] != request["base_content_hash"]
        ):
            raise StateConflict("BASE_REVISION_CONFLICT")

    def _run(self, cid, jid):
        lease = self.files.lease(jid)
        if lease is None:
            return
        try:
            with self.files.job_lock(jid):
                job, snapshot = self.files.job(cid, jid)
                if job["status"] != "queued":
                    return
                job["status"] = "running"
                self.files.save(job)
                token = job["attempt"]

            def cancelled():
                current, _ = self.files.job(cid, jid)
                return current["status"] != "running" or current["attempt"] != token

            def event(value):
                with self.files.job_lock(jid):
                    current, _ = self.files.job(cid, jid)
                    if current["status"] != "running" or current["attempt"] != token:
                        raise ValueError("SUMMARY_CANCELLED")
                    current["events"].append(value)
                    self.files.save(current)

            try:
                result = asyncio.run(
                    generate_summary(
                        dispatcher=self.dispatcher,
                        research=self.research,
                        wiki=self.wiki,
                        snapshot=snapshot,
                        on_event=event,
                        cancel_check=cancelled,
                    )
                )
                with self.files.job_lock(jid):
                    current, _ = self.files.job(cid, jid)
                    if current["status"] != "running" or current["attempt"] != token:
                        return
                    validate_content(snapshot["manifest"], result)
                    self.files.write(self.files.path(jid) / "result.json", result)
                    current.update(status="saving", error=None)
                    self.files.save(current)
                    self._save(current, snapshot)
            except Exception:
                with self.files.job_lock(jid):
                    current, _ = self.files.job(cid, jid)
                    if current["status"] in {"running", "saving"} and current["attempt"] == token:
                        saved = (self.files.path(jid) / "result.json").exists()
                        current.update(
                            status="save_failed" if saved else "failed",
                            attempt=None,
                            error="SAVE_RETRY_REQUIRED" if saved else "SUMMARY_FAILED_NO_AUTOMATIC_RETRY",
                        )
                        self.files.save(current)
        finally:
            self.files.release_worker(lease)

    def _save(self, job, snapshot, *, apply=False):
        from .research_summary_unified import is_unified

        if is_unified(snapshot["manifest"]):
            return self._save_unified(job, snapshot, apply=apply)
        result = self.files.read(self.files.path(job["job_id"]) / "result.json")
        validate_content(snapshot["manifest"], result)
        validate_frozen(self.research, self.wiki, snapshot)
        request = job["request"]
        page_id = request["target_page_id"] or "wiki-" + fingerprint([self.wiki.workspace_id, job["job_id"]])
        if request["target_page_id"] and not apply:
            job.update(
                status="proposal_ready",
                error=None,
                proposal={
                    "content": result,
                    "base_revision": request["base_revision"],
                    "base_content_hash": request["base_content_hash"],
                    "diff": "\n".join(
                        difflib.unified_diff(
                            readable(snapshot["base_content"]).splitlines(),
                            readable(result).splitlines(),
                            fromfile="current",
                            tofile="proposed",
                        )
                    ),
                },
            )
            self.files.save(job)
            return self._public(job)
        rid = "research-summary-save-" + job["job_id"]
        digest = fingerprint([job["input_hash"], result, request])
        old = self.wiki.store.find_revision_request(rid, digest)
        if old is None:
            if apply:
                self._base(self.wiki.get_page(page_id), request)
            self.wiki.store.save_revision(
                page_id=page_id,
                expected_revision=request["base_revision"] if apply else 0,
                request_id=rid,
                request_hash=digest,
                content=result,
                dependency_manifest=snapshot["manifest"],
            )
        job.update(status="completed", attempt=None, page_id=page_id, error=None, updated_at=time.time())
        self.files.save(job)
        return self._public(job)

    def _save_unified(self, job, snapshot, *, apply=False):
        result = self.files.read(self.files.path(job["job_id"]) / "result.json")
        validate_content(snapshot["manifest"], result)
        request = job["request"]
        target = request["target_page_id"]
        page_id = target or "wiki-" + fingerprint([self.wiki.workspace_id, job["job_id"]])
        rid = "research-summary-save-" + job["job_id"]
        digest = fingerprint([job["input_hash"], result, request])
        old = self.wiki.store.find_revision_request(rid, digest)
        # No pre-existing revision is accepted without this job's persisted apply grant.
        if target and not (apply or job.get("apply_authorized")):
            if old is not None:
                raise StateConflict("SUMMARY_APPLY_AUTHORIZATION_REQUIRED")
            validate_frozen(self.research, self.wiki, snapshot)
            job.update(
                status="proposal_ready",
                error=None,
                publication_mode="unified_chat_summary_auto",
                proposal={
                    "content": result,
                    "base_revision": request["base_revision"],
                    "base_content_hash": request["base_content_hash"],
                    "diff": "\n".join(
                        difflib.unified_diff(
                            readable(snapshot["base_content"]).splitlines(),
                            readable(result).splitlines(),
                            fromfile="current",
                            tofile="proposed",
                        )
                    ),
                },
            )
            self.files.save(job)
            return self._public(job)
        if apply and old is None:
            # A rejected CAS leaves the reviewable proposal unchanged.
            self._base(self.wiki.get_page(page_id), request)
            job["apply_authorized"] = True
            self.files.save(job)
        try:
            if old is None:
                validate_frozen(self.research, self.wiki, snapshot)
                no_self_reference(self.wiki, snapshot["manifest"]["research_summary"], target)
                self.wiki.store.save_revision(
                    page_id=page_id,
                    expected_revision=request["base_revision"] if target else 0,
                    request_id=rid,
                    request_hash=digest,
                    content=result,
                    dependency_manifest=snapshot["manifest"],
                )
                old = self.wiki.store.find_revision_request(rid, digest)
            revision = old["revision"]
            if old["page_id"] != page_id or (
                job.get("saved_revision") is not None and job["saved_revision"] != revision
            ):
                raise StateConflict("SUMMARY_SAVED_REVISION_CONFLICT")
            job.update(
                status="publishing",
                page_id=page_id,
                saved_revision=revision,
                error=None,
                publication_mode="unified_chat_summary_auto",
            )
            self.files.save(job)
            self.wiki.ensure_unified_published(page_id, revision)
            job.update(status="completed", attempt=None, error=None, updated_at=time.time())
            self.files.save(job)
        except Exception:
            # Recover a save whose DB commit succeeded before its response was lost.
            saved = self.wiki.store.find_revision_request(rid, digest)
            if saved:
                job.update(page_id=page_id, saved_revision=saved["revision"])
            job.update(
                status="publish_failed" if saved else "save_failed",
                attempt=None,
                error="PUBLISH_RETRY_REQUIRED" if saved else "SAVE_RETRY_REQUIRED",
            )
            self.files.save(job)
        return self._public(job)

    def save_retry(self, cid, jid):
        with self.files.job_lock(jid):
            job, snapshot = self.files.job(cid, jid)
            if job["status"] == "completed":
                return self._public(job)
            if job["status"] not in {"save_failed", "publish_failed"}:
                raise StateConflict("SAVE_RETRY_NOT_AVAILABLE")
            return self._save(job, snapshot)

    def apply(self, cid, jid):
        with self.files.job_lock(jid):
            job, snapshot = self.files.job(cid, jid)
            if job["status"] == "completed":
                return self._public(job)
            if job["status"] != "proposal_ready":
                raise StateConflict("PROPOSAL_NOT_AVAILABLE")
            return self._save(job, snapshot, apply=True)

    def cancel(self, cid, jid):
        with self.files.job_lock(jid):
            job, _ = self.files.job(cid, jid)
            if job["status"] in ACTIVE:
                saved = (self.files.path(jid) / "result.json").exists()
                status = ("publish_failed" if job.get("saved_revision") else "save_failed") if saved else "cancelled"
                job.update(
                    status=status, attempt=None, error="EXPLICIT_RETRY_REQUIRED" if saved else "SUMMARY_CANCELLED"
                )
                self.files.save(job)
            return self._public(job)

    def recover(self):
        for jid in self.files.ids():
            lease = self.files.lease(jid)
            if lease is None:
                continue
            try:
                with self.files.job_lock(jid):
                    job = self.files.read(self.files.path(jid) / "job.json")
                    if job["status"] in ACTIVE:
                        job.update(
                            status=("publish_failed" if job.get("saved_revision") else "save_failed")
                            if (self.files.path(jid) / "result.json").exists()
                            else "interrupted",
                            attempt=None,
                            error="EXPLICIT_RETRY_REQUIRED",
                        )
                        self.files.save(job)
            finally:
                self.files.release_worker(lease)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._own_pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
