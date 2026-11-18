"""Durable, deduplicated summaries of a frozen server-owned conversation window."""

import re
import time
from copy import deepcopy

from .core import StateConflict, canonical, fingerprint
from .knowledge_claim_contracts import WikiConflict
from .knowledge_summary_context import INPUT_POLICY_VERSION, project_turn_evidence
from .knowledge_summary_contracts import TEMPLATE_VERSION, SummaryContent
from .knowledge_summary_model import MAX_CONTEXT_BYTES, generate_summary
from .knowledge_wiki import fields
from .llm import BudgetExceeded, ModelCallError
from .table_chat_store import TableChatStore, checked_id, identity
from .table_chat_workflow import _historical_output

LEGACY_INPUT_POLICY_VERSION = "table-summary-input-v1"

PUBLIC = (
    "job_id",
    "chat_id",
    "end_turn_id",
    "status",
    "message_count",
    "turn_count",
    "page_id",
    "error",
    "template_version",
    "attempt",
    "retry_of_job_id",
    "created_at",
    "updated_at",
)
SAFE_ERRORS = {
    "SUMMARY_INTERRUPTED": "服务执行已中断，未自动重发模型请求；可主动重试",
    "SUMMARY_INPUT_CHANGED": "冻结窗口的对话或来源已变化；请核对资料后重新选择截止轮",
    "SUMMARY_SAVE_FAILED": "模型结果已保留，草稿保存失败；主动重试将复用结果",
    "SUMMARY_PUBLICATION_FAILED": "知识内容已保存，发布未完成；重试将继续发布，不会重新请求模型。",
    "INVALID_SUMMARY_OUTPUT": "模型输出不符合固定栏目或许可证据要求，未保存草稿",
    "SUMMARY_EXECUTION_FAILED": "总结未完成，请检查服务后主动重试",
    "model_not_configured": "尚未配置总结模型，请完成服务端配置后主动重试",
    "provider_http": "模型服务请求失败，未自动重发；请检查服务后主动重试",
    "provider_schema": "整理程序的输出格式定义被模型服务拒绝；请修复程序后重试，无需修改对话内容。",
    "provider_protocol": "模型服务响应格式无效，未保存草稿",
    "timeout": "模型请求超时，计费状态未知；未自动重发",
    "context_limit": "总结上下文超过上限，未静默截断内容",
}


def _error(code):
    return {"code": code, "message": SAFE_ERRORS[code]}


def _artifact_projection(payload):
    """Deterministic view of existing calculations, never recomputation or downsampling."""
    artifact = payload["artifact"]

    def project(value):
        result = {
            key: deepcopy(value[key])
            for key in ("kind", "table_version_id", "config", "comparison_mode", "joined", "unit_conversion")
            if key in value
        }
        if isinstance(value.get("result"), dict):
            original = value["result"]
            result["result"] = _historical_output(original)
            for target, column in zip(result["result"].get("columns", []), original.get("columns", [])):
                for operation in ("line", "scatter", "histogram", "group_statistics"):
                    raw = column.get(operation, {})
                    if "n" in raw:
                        target[operation]["point_count"] = raw["n"]
        if isinstance(value.get("columns"), list):
            result.update(_historical_output(value))
        if isinstance(value.get("tables"), list):
            result["tables"] = [project(table) for table in value["tables"]]
        return result

    return {
        **{key: payload[key] for key in ("artifact_id", "chat_id", "turn_id", "call_id")},
        "artifact": project(artifact),
        "coverage": {
            "projection": "statistics-and-operation-status-v1",
            "full_artifact_stored": True,
            "curve_coordinates_included": False,
            "groups_and_bins_included": False,
            "limitations": "未包含完整曲线坐标、分段、逐行排除和分组/直方图细节；不能据此新增峰位等结论",
        },
    }


class SummaryFiles(TableChatStore):
    def __init__(self, store, namespace):
        super().__init__(store)
        self.root = store.root / "wiki_summaries"
        self.namespace = namespace

    def path(self, job_id, name="job.json"):
        checked_id("summary", job_id)
        return self._safe(self.root / self.namespace / "jobs" / job_id / name)

    def index(self, kind, key):
        return self._safe(self.root / self.namespace / kind / (fingerprint(key) + ".json"))

    def acquire(self, job_id):
        return self.acquire_worker(identity("turn", ["wiki-summary-worker", job_id]))

    def ids(self):
        path = self._safe(self.root / self.namespace / "jobs")
        if not path.exists():
            return []
        return sorted(
            p.name
            for p in path.iterdir()
            if re.fullmatch(r"summary-[0-9a-f]{64}", p.name) and self._safe(p / "job.json").exists()
        )


class WikiSummaryService:
    def __init__(self, wiki, *, generate=None):
        self.wiki = wiki
        self.service = wiki.ingestion_service
        self.namespace = fingerprint([wiki.workspace_id, wiki.collection_id])
        self.files = SummaryFiles(self.service.store, self.namespace)
        self.generate = generate or generate_summary
        self.lock_name = "wiki-summaries:" + self.namespace

    def capabilities(self):
        options = self.service.table_options
        return {
            "available": bool(isinstance(options.get("api_key"), str) and options["api_key"].strip()),
            "model": "gpt-6-sol",
            "template_version": TEMPLATE_VERSION,
            "max_messages": 40,
            "max_turns": 20,
            "input_policy_version": INPUT_POLICY_VERSION,
            "max_context_bytes": MAX_CONTEXT_BYTES,
        }

    def _public(self, job):
        return deepcopy(
            {
                **{key: job[key] for key in PUBLIC},
                "page_deleted": bool(job.get("page_id") and self.wiki.store.is_deleted(job["page_id"])),
                "input_policy_version": job.get("input_policy_version", LEGACY_INPUT_POLICY_VERSION),
            }
        )

    def _read(self, job_id):
        job = self.files.read(self.files.path(job_id))
        snapshot = self.files.read(self.files.path(job_id, "input.json"))
        if (
            job.get("job_id") != job_id
            or job.get("namespace") != self.namespace
            or fingerprint(snapshot) != job.get("input_sha256")
            or job.get("status") not in {"queued", "running", "completed", "failed", "interrupted"}
            or len(
                {
                    record.get("input_policy_version", LEGACY_INPUT_POLICY_VERSION)
                    for record in (job, snapshot, snapshot["context"], snapshot["dependency_manifest"]["summary"])
                }
            )
            != 1
            or any(
                job.get(k) != snapshot.get(k)
                for k in ("chat_id", "end_turn_id", "window_key", "message_count", "turn_count")
            )
        ):
            raise StateConflict("SUMMARY_INTEGRITY_FAILED")
        return job, snapshot

    def _freeze(self, chat_id, end_turn_id, window_key):
        files = self.service._table_chats()
        with self.service.store.lock("table-chat:" + chat_id):
            chat = files.read_chat(chat_id)
            if end_turn_id not in chat["turn_ids"]:
                raise StateConflict("SUMMARY_END_TURN_NOT_FOUND")
            stop = chat["turn_ids"].index(end_turn_id) + 1
            tids = chat["turn_ids"][max(0, stop - 20) : stop]
            for tid in tids:
                turn, _ = files.read_turn(chat_id, tid)
                if turn["status"] != "completed" or not turn["assistant_text"].strip():
                    raise StateConflict("SUMMARY_WINDOW_NOT_COMPLETED")
        turns = [self.wiki.resolver.completed_turn(chat_id, tid) for tid in tids]
        corpus_id = self.wiki._corpus([r["source_id"] for _, _, raws, _ in turns for r in raws])
        supports = []
        deps = []
        messages = []
        headers = {}
        for turn, _, raws, artifacts in turns:
            available, dependencies, usable = self.wiki.resolver.supports(turn, raws, artifacts, corpus_id)
            supports.extend(available)
            deps.extend(dependencies)
            for raw in raws:
                headers[raw["table_version_id"]] = {
                    "table_version_id": raw["table_version_id"],
                    "columns": deepcopy(raw["columns"]),
                    "default_header_row_count": raw["default_header_row_count"],
                    "rows": deepcopy(raw["rows"][: raw["default_header_row_count"]]),
                }
            # Preserve observed evidence, not unseen source matrices or extra history.
            tables = [
                {
                    key: deepcopy(raw.get(key))
                    for key in (
                        "table_version_id",
                        "table_id",
                        "source_id",
                        "asset_id",
                        "asset_sha256",
                        "source_region_id",
                        "name",
                        "original_name",
                        "sheet",
                        "table_range",
                        "row_count",
                        "column_count",
                    )
                }
                for raw in raws
            ]
            messages.append(
                {
                    "turn_id": turn["turn_id"],
                    "status": turn["status"],
                    "user_text": turn["message"],
                    "assistant_text": turn["assistant_text"],
                    "tables": tables,
                    "tool_calls": deepcopy(turn["tool_calls"]),
                    "artifacts": [_artifact_projection(a) for a in artifacts],
                    "usable_support_ids": usable,
                    "unavailable_support_ids": [s["support_id"] for s in available if s["support_id"] not in usable],
                }
            )
        supports = list({s["support_id"]: s for s in supports}.values())
        dependencies = list({d["dependency_id"]: d for d in deps}.values())
        context = {
            "template_version": TEMPLATE_VERSION,
            "chat_id": chat_id,
            "end_turn_id": end_turn_id,
            **project_turn_evidence(messages, table_headers=headers),
            "supports": supports,
            "window_only": True,
            "scientific_validation": False,
        }
        context["input_hash"] = fingerprint(context)
        if len(canonical(context).encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise WikiConflict("SUMMARY_CONTEXT_LIMIT")
        summary = {
            "template_version": TEMPLATE_VERSION,
            "input_policy_version": INPUT_POLICY_VERSION,
            "chat_id": chat_id,
            "end_turn_id": end_turn_id,
            "turn_ids": tids,
            "message_count": len(tids) * 2,
            "turn_count": len(tids),
            "input_hash": context["input_hash"],
        }
        manifest = {
            "version": "knowledge-dependencies-v1",
            "corpus_snapshot_id": corpus_id,
            "supports": supports,
            "dependencies": dependencies,
            "summary": summary,
        }
        snapshot = {**summary, "window_key": window_key, "context": context, "dependency_manifest": manifest}
        self._validate_frozen(snapshot)
        return snapshot

    def _validate_frozen(self, snapshot, content=None):
        files = self.service._table_chats()
        chat = files.read_chat(snapshot["chat_id"])
        try:
            stop = chat["turn_ids"].index(snapshot["end_turn_id"]) + 1
            if chat["turn_ids"][max(0, stop - 20) : stop] != snapshot["turn_ids"]:
                raise ValueError("window")
            revision = {
                "corpus_snapshot_id": snapshot["dependency_manifest"]["corpus_snapshot_id"],
                "dependency_manifest": snapshot["dependency_manifest"],
                "supports": snapshot["dependency_manifest"]["supports"],
                "content": content or {"sections": []},
            }
            gaps = self.wiki.validate_current(revision)
            if any(g["code"] not in {"EMPTY_KNOWLEDGE", "CLAIM_SUPPORT_MISSING"} for g in gaps):
                raise ValueError("dependency")
        except Exception:
            raise StateConflict("SUMMARY_INPUT_CHANGED") from None

    def _request(self, rid, digest):
        path = self.files.index("requests", rid)
        if path.exists():
            old = self.files.read(path)
            if old["request_hash"] != digest:
                raise StateConflict("REQUEST_ID_CONFLICT")
            return old["job_id"]
        return None

    def _bind(self, rid, digest, job):
        self.files.write(self.files.index("requests", rid), {"request_hash": digest, "job_id": job["job_id"]})
        self.files.write(self.files.index("windows", job["window_key"]), {"job_id": job["job_id"]})

    def _latest(self, window_key):
        # Window pointers are derived indexes. A crash between job/request/index
        # writes must not make a durable attempt unreachable or revive an older one.
        candidates = []
        for jid in self.files.ids():
            job, snapshot = self._read(jid)
            if job["window_key"] == window_key:
                candidates.append((job, snapshot))
        if not candidates:
            return None
        job, snapshot = max(
            candidates,
            key=lambda pair: (
                pair[0]["status"] == "completed",
                pair[0]["attempt"],
                pair[0]["created_at"],
                pair[0]["job_id"],
            ),
        )
        committed = self._published_saved(snapshot)
        if committed and job["status"] != "completed":
            job.update(status="completed", page_id=committed["page_id"], error=None, updated_at=time.time())
            self.files.write(self.files.path(job["job_id"]), job)
        self.files.write(self.files.index("windows", window_key), {"job_id": job["job_id"]})
        return job

    def enqueue(self, payload):
        fields(payload, {"request_id", "chat_id", "end_turn_id"}, {"request_id", "chat_id", "end_turn_id"})
        checked_id("chat", payload["chat_id"])
        checked_id("turn", payload["end_turn_id"])
        digest = fingerprint(["enqueue", payload])
        lease = None
        with self.service.store.lock(self.lock_name):
            old = self._request(payload["request_id"], digest)
            if old:
                return self._public(self._read(old)[0])
            key = fingerprint(
                [self.namespace, payload["chat_id"], payload["end_turn_id"], TEMPLATE_VERSION, INPUT_POLICY_VERSION]
            )
            latest = self._latest(key)
            if latest:
                self._bind(payload["request_id"], digest, latest)
                return self._public(latest)
            jid = identity("summary", key)
            snapshot = self._freeze(payload["chat_id"], payload["end_turn_id"], key)
            job, lease = self._new(jid, snapshot, payload["request_id"], digest)
        self._start(job["job_id"], lease)
        return self._public(job)

    def _new(self, jid, snapshot, rid, digest, previous=None, result=None):
        lease = self.files.acquire(jid)
        if lease is None:
            raise StateConflict("SUMMARY_ALREADY_RUNNING")
        now = time.time()
        job = {key: snapshot[key] for key in ("chat_id", "end_turn_id", "window_key", "message_count", "turn_count")}
        job.update(
            job_id=jid,
            namespace=self.namespace,
            status="queued",
            page_id=None,
            error=None,
            input_policy_version=snapshot.get("input_policy_version", LEGACY_INPUT_POLICY_VERSION),
            template_version=TEMPLATE_VERSION,
            attempt=previous["attempt"] + 1 if previous else 1,
            retry_of_job_id=previous["job_id"] if previous else None,
            created_at=now,
            updated_at=now,
            input_sha256=fingerprint(snapshot),
            model_started=False,
        )
        try:
            self.files.write(self.files.path(jid, "input.json"), snapshot)
            if result is not None:
                self.files.write(self.files.path(jid, "result.json"), result)
            self.files.write(self.files.path(jid), job)
            self._bind(rid, digest, job)
        except BaseException:
            self.files.release_worker(lease)
            raise
        return job, lease

    def retry(self, job_id, payload):
        fields(payload, {"request_id"}, {"request_id"})
        checked_id("summary", job_id)
        self.get(job_id)  # Recover a worker lost to restart before choosing retry behavior.
        digest = fingerprint(["retry", job_id, payload])
        lease = None
        with self.service.store.lock(self.lock_name):
            old = self._request(payload["request_id"], digest)
            if old:
                return self._public(self._read(old)[0])
            previous, snapshot = self._read(job_id)
            latest = self._latest(previous["window_key"])
            if latest["status"] in {"queued", "running", "completed"} or self._public(latest)["page_deleted"]:
                self._bind(payload["request_id"], digest, latest)
                return self._public(latest)
            self._validate_frozen(snapshot)
            jid = identity("summary", [previous["window_key"], payload["request_id"]])
            if self.files.path(jid).exists():
                existing, _ = self._read(jid)
                self._bind(payload["request_id"], digest, existing)
                return self._public(existing)
            path = self.files.path(latest["job_id"], "result.json")
            result = self.files.read(path) if path.exists() else None
            job, lease = self._new(jid, snapshot, payload["request_id"], digest, previous=latest, result=result)
        self._start(job["job_id"], lease)
        return self._public(job)

    def _saved(self, snapshot):
        key = snapshot["window_key"]
        return self.wiki.store.find_revision_request("summary-save-" + key, fingerprint(["summary-save", snapshot]))

    def _published_saved(self, snapshot):
        saved = self._saved(snapshot)
        if not saved or self.wiki.store.is_deleted(saved["page_id"]):
            return None
        try:
            page = self.wiki.store.get_page(saved["page_id"])
            if (
                page["availability"] == "current"
                and page.get("published_revision_id") == page["current_revision_id"]
                and self.wiki.store.published_publication(page["page_id"], page["current_revision_id"])
            ):
                return saved
        except KeyError:
            if self.wiki.store.is_deleted(saved["page_id"]):
                return None
            raise
        return None

    def get(self, job_id):
        with self.service.store.lock(self.lock_name):
            job, snapshot = self._read(job_id)
            if not job.get("page_id"):
                saved = self._saved(snapshot)
                if saved:
                    job.update(page_id=saved["page_id"], updated_at=time.time())
                    self.files.write(self.files.path(job_id), job)
            if job["status"] == "completed":
                return self._public(job)
            committed = self._published_saved(snapshot)
            if committed:
                job.update(status="completed", page_id=committed["page_id"], error=None, updated_at=time.time())
                self.files.write(self.files.path(job_id), job)
                return self._public(job)
            if job["status"] not in {"queued", "running"}:
                return self._public(job)
            lease = self.files.acquire(job_id)
            if lease is None:
                return self._public(job)
            try:
                old = self._published_saved(snapshot)
                job.update(
                    status="completed" if old else "interrupted",
                    page_id=old["page_id"] if old else job.get("page_id"),
                    error=None if old else _error("SUMMARY_INTERRUPTED"),
                    updated_at=time.time(),
                )
                self.files.write(self.files.path(job_id), job)
            finally:
                self.files.release_worker(lease)
            return self._public(job)

    def list(self, chat_id):
        checked_id("chat", chat_id)
        self.service._table_chats().read_chat(chat_id)
        return {
            "jobs": sorted(
                [self.get(jid) for jid in self.files.ids() if self._read(jid)[0]["chat_id"] == chat_id],
                key=lambda j: (j["created_at"], j["job_id"]),
                reverse=True,
            )
        }

    def recover(self):
        for jid in self.files.ids():
            self.get(jid)

    def _start(self, job_id, lease):
        def run():
            try:
                self._run(job_id)
            finally:
                self.files.release_worker(lease)

        try:
            future = self.service.pool.submit(run)
            future.add_done_callback(lambda f: self.files.release_worker(lease) if f.cancelled() else None)
        except RuntimeError:
            self.files.release_worker(lease)

    def _validated_result(self, result, snapshot):
        try:
            if not isinstance(result, dict) or set(result) != {"content", "usage"}:
                raise ValueError("result")
            content = SummaryContent.model_validate(result["content"]).model_dump(mode="json")
            if len(canonical(content).encode("utf-8")) > 128_000:
                raise ValueError("output limit")
            allowed = {sid for t in snapshot["context"]["turns"] for sid in t["usable_support_ids"]}
            supports = {s["support_id"]: s for s in snapshot["dependency_manifest"]["supports"]}
            for section in content["sections"]:
                for index, claim in enumerate(section["claims"]):
                    ids = claim["support_ids"]
                    if not set(ids) <= allowed or claim["claim_type"] == "reported_measurement":
                        raise ValueError("support")
                    if claim["claim_type"] == "computed_observation" and not any(
                        supports[s]["support_type"] == "calculation" for s in ids
                    ):
                        raise ValueError("calculation")
                    claim["claim_id"] = "claim-" + fingerprint([snapshot["window_key"], section["section_id"], index])
                    claim["scientific_validation"] = False
            if not isinstance(result["usage"], dict) or len(canonical(result["usage"]).encode("utf-8")) > 32_000:
                raise ValueError("usage")
            return {"content": content, "usage": deepcopy(result["usage"]), "input_sha256": fingerprint(snapshot)}
        except (ValueError, TypeError, KeyError):
            raise ValueError("INVALID_SUMMARY_OUTPUT") from None

    def _run(self, job_id):
        result_saved = False
        publishing = False
        try:
            with self.service.store.lock(self.lock_name):
                job, snapshot = self._read(job_id)
                if job["status"] != "queued":
                    return
                job.update(status="running", updated_at=time.time())
                self.files.write(self.files.path(job_id), job)
            old = self._saved(snapshot)
            if old:
                result_saved = True
                publishing = True
                self.wiki.ensure_table_published(old["page_id"])
                self._finish(job_id, "completed", page_id=old["page_id"])
                return
            self._validate_frozen(snapshot)
            path = self.files.path(job_id, "result.json")
            if path.exists():
                saved = self.files.read(path)
                if saved.get("input_sha256") != fingerprint(snapshot):
                    raise StateConflict("SUMMARY_INPUT_CHANGED")
                result = self._validated_result({k: saved[k] for k in ("content", "usage")}, snapshot)
            else:
                if self.service._stop.is_set():
                    raise InterruptedError()
                with self.service.store.lock(self.lock_name):
                    live, _ = self._read(job_id)
                    if live["model_started"]:
                        raise InterruptedError()
                    live.update(model_started=True, updated_at=time.time())
                    self.files.write(self.files.path(job_id), live)  # Durable intent before any provider I/O.
                result = self._validated_result(
                    self.generate(
                        options=self.service.resources.model_options(self.service.table_options),
                        context=deepcopy(snapshot["context"]),
                        run_dir=self.files.path(job_id, "execution"),
                        cancel_check=self.service._stop.is_set,
                    ),
                    snapshot,
                )
                self.files.write(path, result)  # Save before any Wiki transaction; never regenerate to retry a save.
            result_saved = True
            if self.service._stop.is_set():
                raise InterruptedError()
            revision = {"dependency_manifest": snapshot["dependency_manifest"]}
            with self.wiki.dependency_guard(revision):
                self._validate_frozen(snapshot, result["content"])
                key = snapshot["window_key"]
                page_id = "wiki-" + fingerprint(["summary", self.namespace, key])
                row = self.wiki.store.save_revision(
                    page_id=page_id,
                    expected_revision=0,
                    request_id="summary-save-" + key,
                    request_hash=fingerprint(["summary-save", snapshot]),
                    content=result["content"],
                    dependency_manifest=snapshot["dependency_manifest"],
                )
            publishing = True
            self.wiki.ensure_table_published(row["page_id"])
            self._finish(job_id, "completed", page_id=row["page_id"])
        except Exception as exc:
            if isinstance(exc, InterruptedError) or self.service._stop.is_set():
                code = "SUMMARY_INTERRUPTED"
            elif publishing:
                code = "SUMMARY_PUBLICATION_FAILED"
            elif isinstance(exc, StateConflict):
                code = "SUMMARY_INPUT_CHANGED"
            elif str(exc) == "INVALID_SUMMARY_OUTPUT":
                code = "INVALID_SUMMARY_OUTPUT"
            elif isinstance(exc, BudgetExceeded):
                code = "timeout"
            elif isinstance(exc, ModelCallError):
                code = exc.reason if exc.reason in SAFE_ERRORS else "provider_protocol"
            else:
                code = "SUMMARY_SAVE_FAILED" if result_saved else "SUMMARY_EXECUTION_FAILED"
            self._finish(job_id, "interrupted" if code == "SUMMARY_INTERRUPTED" else "failed", error=_error(code))

    def _finish(self, job_id, status, *, page_id=None, error=None):
        with self.service.store.lock(self.lock_name):
            job, snapshot = self._read(job_id)
            # Polling may recover a committed publication before this worker exits.
            # A concurrent edit/deletion cannot undo that completed audit result.
            if job["status"] == "completed":
                return
            if page_id is None:
                saved = self._saved(snapshot)
                page_id = saved["page_id"] if saved else job.get("page_id")
            job.update(status=status, page_id=page_id, error=error, updated_at=time.time())
            self.files.write(self.files.path(job_id), job)
