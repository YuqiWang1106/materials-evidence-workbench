"""Persistent per-table model suggestions bound to researcher-approved payloads."""

from __future__ import annotations

import fcntl
import json
import time
from hashlib import sha256

from .core import StateConflict, atomic_json, fingerprint


class StandardModelJobsMixin:
    def _standard_jobs_root(self):
        return self.store.root / "standard_jobs"

    def _standard_job_path(self, job_id):
        if not isinstance(job_id, str) or not job_id.startswith("stj-") or len(job_id) != 36:
            raise ValueError("无效的表格识别任务编号")
        path = self._standard_jobs_root() / job_id / "job.json"
        if not path.is_file():
            raise KeyError("表格识别任务不存在")
        job = json.loads(path.read_text())
        if job.get("job_id") != job_id:
            raise StateConflict("表格识别任务文件与编号不匹配")
        return path, job

    @staticmethod
    def _omitted_row_ranges(start, end, selected):
        chosen = set(selected)
        ranges, first, previous = [], None, None
        for row in range(start, end + 1):
            if row in chosen:
                if first is not None:
                    ranges.append([first, previous])
                    first = previous = None
            elif first is None:
                first = previous = row
            else:
                previous = row
        if first is not None:
            ranges.append([first, previous])
        return ranges

    def standard_payload_preview(self, source_id, payload):
        from openpyxl.utils.cell import range_boundaries

        from .standard_table_meaning import build_context

        draft, asset = self._standard_input(source_id, payload["expected_revision"])
        state = self._standard_state(draft)
        if payload["expected_version"] != state["version"]:
            raise StateConflict("含义版本已变化，请刷新后重新预览发送内容")
        region = next((r for r in draft["recipe"]["region_review"]["regions"] if r["id"] == payload["region_id"]), None)
        if region is None:
            raise ValueError("未知表格")
        context = build_context(
            asset,
            region,
            draft["reader_options"],
            sample_rows=payload.get("sample_rows"),
            sample_range=payload.get("sample_range"),
        )
        _, start, _, end = range_boundaries(region["data_ranges"][0])
        return {
            "source_id": source_id,
            "revision": draft["revision"],
            "draft_sha256": draft["draft_sha256"],
            "version": state["version"],
            "region_id": region["id"],
            "payload": context,
            "payload_sha256": context["payload_sha256"],
            "included_rows": context["sample_rows"],
            "omitted_row_ranges": self._omitted_row_ranges(start, end, context["sample_rows"]),
            "cell_count": sum(len(r["cells"]) for r in context["headers"] + context["examples"]),
        }

    def standard_model_jobs(self, source_id):
        self._standard_input(source_id)
        root = self._standard_jobs_root()
        jobs = []
        if root.exists():
            for path in root.glob("stj-*/job.json"):
                try:
                    job = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                if job.get("source_id") == source_id:
                    jobs.append(job)
        jobs.sort(key=lambda item: (item.get("created_at", 0), item.get("job_id", "")))
        return {"source_id": source_id, "jobs": jobs}

    def enqueue_standard_suggestion(self, source_id, payload):
        if payload.get("confirmed") is not True:
            raise StateConflict("请先查看并确认将发送给模型的表格内容")
        if not self.table_options.get("api_key"):
            raise ValueError("尚未配置表格模型；可以手动确认同一份读法")
        preview = self.standard_payload_preview(source_id, payload)
        if payload.get("payload_sha256") != preview["payload_sha256"]:
            raise StateConflict("发送内容与预览不一致，请重新查看后确认")
        job = self._persist_standard_job(preview)
        if job.pop("_created", False):
            self.start_standard_model_job(source_id, job["job_id"])
        return job

    @staticmethod
    def _standard_job_id(preview, model, prompt_version):
        identity = fingerprint(
            {
                "source_id": preview["source_id"],
                "draft_sha256": preview["draft_sha256"],
                "region_id": preview["region_id"],
                "payload_sha256": preview["payload_sha256"],
                "expected_version": preview["version"],
                "model": model,
                "prompt": prompt_version,
            }
        )
        return "stj-" + identity[:32]

    def _persist_standard_job(self, preview, *, model=None, prompt_version=None):
        from .standard_workflow import MODEL, PROMPT_VERSION

        model, prompt_version = model or MODEL, prompt_version or PROMPT_VERSION
        source_id = preview["source_id"]
        job_id = self._standard_job_id(preview, model, prompt_version)
        job_dir = self._standard_jobs_root() / job_id
        path = job_dir / "job.json"
        # Atomic JSON readers can reuse a running job without waiting for its
        # provider-duration lock. Existing payloads are never overwritten.
        if path.exists():
            return json.loads(path.read_text())
        created = False
        # Publication is a separate short lock. Workers hold the execution
        # lock across provider calls and take standard/source locks only at
        # the final request-start boundary; publication must never wait for
        # that execution lock while holding a source lock.
        with self.store.lock("standard-model-publish:" + job_id):
            if path.exists():
                job = json.loads(path.read_text())
            else:
                job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_json(job_dir / "payload.json", preview["payload"])
                now = time.time()
                job = {
                    "job_id": job_id,
                    "source_id": source_id,
                    "revision": preview["revision"],
                    "draft_sha256": preview["draft_sha256"],
                    "region_id": preview["region_id"],
                    "payload_sha256": preview["payload_sha256"],
                    "expected_version": preview["version"],
                    "sample_rows": preview["included_rows"],
                    "model": model,
                    "prompt_version": prompt_version,
                    "status": "queued",
                    "attempt": 1,
                    "attempts": [{"attempt": 1, "status": "queued", "request_started": False}],
                    "request_started": False,
                    "created_at": now,
                    "started_at": None,
                    "completed_at": None,
                    "updated_at": now,
                    "form": None,
                    "diagnostics": None,
                    "error": None,
                }
                if "sample_range" in preview["payload"]:
                    job["sample_range"] = preview["payload"]["sample_range"]
                atomic_json(path, job)
                created = True
        return {**job, "_created": created}

    def retry_standard_suggestion(self, source_id, job_id, payload):
        from .standard_workflow import MODEL, PROMPT_VERSION

        if payload.get("confirmed") is not True:
            raise StateConflict("显式重试前请再次确认原表发送内容")
        path, job = self._standard_job_path(job_id)
        if job.get("source_id") != source_id:
            raise StateConflict("任务不属于当前资料")
        if job["status"] not in ("failed", "outcome_unknown", "needs_researcher"):
            raise StateConflict("当前任务状态不允许重试")
        if job.get("model") != MODEL or job.get("prompt_version") != PROMPT_VERSION:
            raise StateConflict("模型或提示词规则版本已变化；请按当前预览创建新任务")
        if payload.get("expected_version") != job.get("expected_version"):
            raise StateConflict("读法版本已变化；请按当前预览重新确认并创建新任务")
        preview = self.standard_payload_preview(source_id, payload)
        if (
            payload.get("payload_sha256") != job.get("payload_sha256")
            or preview["payload_sha256"] != job.get("payload_sha256")
            or preview["draft_sha256"] != job.get("draft_sha256")
            or preview["region_id"] != job.get("region_id")
        ):
            raise StateConflict("发送内容已变化，请重新预览并创建新任务")
        with self.store.lock("standard-model-job:" + job_id):
            job = json.loads(path.read_text())
            if job["status"] not in ("failed", "outcome_unknown", "needs_researcher"):
                raise StateConflict("任务状态已变化，请刷新后重试")
            job["attempt"] += 1
            job.update(
                status="queued",
                request_started=False,
                started_at=None,
                completed_at=None,
                error=None,
                updated_at=time.time(),
            )
            job["attempts"].append({"attempt": job["attempt"], "status": "queued", "request_started": False})
            atomic_json(path, job)
        self.start_standard_model_job(source_id, job_id)
        return job

    def start_standard_model_job(self, source_id, job_id):
        key = "standard-model:" + job_id
        with self._guard:
            if key in self._running:
                return
            self._running.add(key)

        def run():
            try:
                self._run_standard_model_job(job_id)
            finally:
                with self._guard:
                    self._running.discard(key)

        try:
            self.pool.submit(run)
        except RuntimeError:
            with self._guard:
                self._running.discard(key)
            raise

    def recover_standard_model_jobs(self):
        root = self._standard_jobs_root()
        if not root.exists():
            return
        for path in root.glob("stj-*/job.json"):
            try:
                job = json.loads(path.read_text())
                if job.get("status") == "running":
                    lock_name = "standard-model-job:" + job["job_id"]
                    lock_path = self.store.root / "locks" / (sha256(lock_name.encode()).hexdigest() + ".lock")
                    with lock_path.open("a+b") as stream:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        job = json.loads(path.read_text())
                        if job.get("status") == "running":
                            attempt_dir = path.parent / "attempts" / str(job["attempt"])
                            responses = sorted(attempt_dir.glob("provider-*.json")) if attempt_dir.exists() else []
                            if responses:
                                job["recovered_provider_response"] = responses[-1].name
                                self._restore_standard_model_response(path, job, responses[-1], attempt_dir)
                            elif job.get("request_started"):
                                job["status"] = "outcome_unknown"
                                job["error"] = "服务中断时模型请求结果无法确认；不会自动重发"
                            else:
                                job["status"] = "queued"
                                job["error"] = None
                            recovered_at = time.time()
                            job.update(
                                request_started=False,
                                updated_at=recovered_at,
                                completed_at=recovered_at if job.get("status") != "queued" else None,
                            )
                            atomic_json(path, job)
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                if job.get("status") == "queued":
                    self.start_standard_model_job(job["source_id"], job["job_id"])
            except (OSError, ValueError, KeyError, StateConflict):
                continue

    def _restore_standard_model_response(self, job_path, job, response_path, attempt_dir):
        """Restore a durably received candidate after a crash without another API call."""
        from .standard_model_form import to_review_form
        from .standard_table_meaning import build_context, inspect_standard_table

        try:
            draft, asset = self._standard_input(job["source_id"], job["revision"])
            region = next(
                (item for item in draft["recipe"]["region_review"]["regions"] if item["id"] == job["region_id"]), None
            )
            if region is None:
                raise StateConflict("已确认表区不存在，无法恢复候选")
            if draft["draft_sha256"] != job["draft_sha256"]:
                raise StateConflict("原件或已确认范围已变化，无法恢复候选")
            if self._standard_state(draft)["version"] != job["expected_version"]:
                raise StateConflict("读法版本已变化，无法恢复候选")
            context = json.loads((job_path.parent / "payload.json").read_text())
            rebuilt = build_context(
                asset,
                region,
                draft["reader_options"],
                sample_rows=context["sample_rows"],
                sample_range=context.get("sample_range"),
            )
            if rebuilt != context or rebuilt["payload_sha256"] != job["payload_sha256"]:
                raise StateConflict("原表发送内容已变化，无法恢复候选")
            provider_raw = json.loads(response_path.read_text())
            form = to_review_form(provider_raw, context)
            try:
                inspected = inspect_standard_table(form, asset, region, draft["reader_options"])
                diagnostics = inspected["diagnostics"]
            except ValueError as exc:
                diagnostics = {"ready": False, "issues": [{"code": "meaning_incomplete", "message": str(exc)}]}
            # This state means a schema-valid proposal is available for people
            # to review; diagnostics.ready separately reports executability.
            status = "ready_for_review"
            result = {
                "status": status,
                "form": form,
                "diagnostics": diagnostics,
                "model": job["model"],
                "human_confirmed": False,
                "region_id": job["region_id"],
                "payload_sha256": job["payload_sha256"],
                "attempt": job["attempt"],
                "recovered": True,
            }
            atomic_json(attempt_dir / "result.json", result)
            completed = time.time()
            job.update(
                status=status,
                form=form,
                diagnostics=diagnostics,
                error=None,
                request_started=False,
                started_at=job.get("started_at") or completed,
                completed_at=completed,
                updated_at=completed,
                recovered=True,
            )
            job["attempts"][-1].update(
                status=status,
                finished_at=time.time(),
                request_started=False,
                recovered_provider_response=response_path.name,
            )
        except Exception as exc:
            completed = time.time()
            job.update(
                status="failed",
                form=None,
                diagnostics=None,
                error="已收到模型回复，但无法解析为可核对的结构化建议；可手动填写或显式重试",
                request_started=False,
                started_at=job.get("started_at") or completed,
                completed_at=completed,
                updated_at=completed,
                recovered=True,
            )
            job["attempts"][-1].update(
                status="failed", finished_at=time.time(), request_started=False, recovery_error=str(exc)
            )
        atomic_json(job_path, job)

    def _run_standard_model_job(self, job_id):
        from .standard_model_form import request_schema, to_review_form
        from .standard_table_meaning import build_context, inspect_standard_table

        path, _ = self._standard_job_path(job_id)
        with self.store.lock("standard-model-job:" + job_id):
            job = json.loads(path.read_text())
            if job.get("status") != "queued":
                return job
            attempt = job["attempt"]
            attempt_dir = path.parent / "attempts" / str(attempt)
            attempt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            started = time.time()
            job.update(
                status="running",
                request_started=False,
                started_at=started,
                completed_at=None,
                updated_at=started,
                error=None,
            )
            job["attempts"][-1].update(status="running", started_at=started)
            atomic_json(path, job)
            raw = None
            try:
                draft, asset = self._standard_input(job["source_id"], job["revision"])
                if draft["draft_sha256"] != job["draft_sha256"]:
                    raise StateConflict("原件或已确认范围已变化，请重新预览")
                if self._standard_state(draft)["version"] != job["expected_version"]:
                    raise StateConflict("读法版本已变化，请重新核对")
                region = next(
                    (r for r in draft["recipe"]["region_review"]["regions"] if r["id"] == job["region_id"]), None
                )
                if region is None:
                    raise StateConflict("已确认表区不存在，请重新确认范围")
                context = json.loads((path.parent / "payload.json").read_text())
                rebuilt = build_context(
                    asset,
                    region,
                    draft["reader_options"],
                    sample_rows=context["sample_rows"],
                    sample_range=context.get("sample_range"),
                )
                if rebuilt != context or rebuilt["payload_sha256"] != job["payload_sha256"]:
                    raise StateConflict("原表发送内容与已确认预览不一致，请重新预览")
                from .standard_workflow import INSTRUCTIONS, MODEL, PROMPT_VERSION, _make_client

                if job["model"] != MODEL or job["prompt_version"] != PROMPT_VERSION:
                    raise StateConflict("模型或读法规则版本已变化，请重新预览发送内容")
                client = _make_client(self, attempt_dir)
                request = {"source": context}
                encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode()) > 90000:
                    raise ValueError("已确认表格内容超过模型请求上限")
                # Rebuilding an XLSX payload can take time. A researcher may
                # update the draft/meaning in that interval. Serialize this
                # final check and the durable request-start publication with
                # both writers; release their locks before calling a provider.
                with self.store.lock("standard:" + job["source_id"]), self.store.lock("source:" + job["source_id"]):
                    current, _ = self._standard_input(job["source_id"], job["revision"])
                    if (
                        current["draft_sha256"] != job["draft_sha256"]
                        or self._standard_state(current)["version"] != job["expected_version"]
                    ):
                        raise StateConflict("原件、范围或读法版本已变化；未发送模型请求")
                    job.update(request_started=True, updated_at=time.time())
                    job["attempts"][-1].update(request_started=True, request_count=1)
                    atomic_json(path, job)
                provider_raw = client.call(
                    INSTRUCTIONS,
                    encoded,
                    request_schema(context),
                    phase="standard_table_meaning",
                    context={
                        "source_id": job["source_id"],
                        "region_id": job["region_id"],
                        "draft_sha256": job["draft_sha256"],
                        "payload_sha256": job["payload_sha256"],
                        "job_id": job_id,
                    },
                )
                atomic_json(attempt_dir / "provider-1.json", provider_raw)
                job.update(request_started=False, updated_at=time.time())
                job["attempts"][-1].update(request_started=False, completed_calls=1)
                atomic_json(path, job)
                # Schema/shape failures are failed attempts with a visible
                # manual-entry or explicit-retry path. They are not silently
                # converted into a fabricated partial form.
                raw = to_review_form(provider_raw, context)
                try:
                    inspected = inspect_standard_table(raw, asset, region, draft["reader_options"])
                    diagnostics = inspected["diagnostics"]
                except ValueError as exc:
                    diagnostics = {"ready": False, "issues": [{"code": "meaning_incomplete", "message": str(exc)}]}
                atomic_json(attempt_dir / "proposal-1.json", raw)
                status = "ready_for_review"
                result = {
                    "status": status,
                    "form": raw,
                    "diagnostics": diagnostics,
                    "model": MODEL,
                    "human_confirmed": False,
                    "region_id": job["region_id"],
                    "payload_sha256": job["payload_sha256"],
                    "attempt": attempt,
                }
                atomic_json(attempt_dir / "result.json", result)
                completed = time.time()
                job.update(
                    status=status,
                    form=raw,
                    diagnostics=diagnostics,
                    error=None,
                    completed_at=completed,
                    updated_at=completed,
                    request_started=False,
                )
                job["attempts"][-1].update(status=status, finished_at=time.time(), request_started=False)
                atomic_json(path, job)
                return job
            except Exception as _exc:
                status = (
                    "outcome_unknown"
                    if job.get("request_started")
                    else ("needs_researcher" if raw is not None else "failed")
                )
                completed = time.time()
                job.update(
                    status=status,
                    form=raw,
                    error=(
                        "模型请求结果无法确认；不会自动重发"
                        if status == "outcome_unknown"
                        else (
                            "模型已返回内容，但没有可核对的结构化建议；可手动填写或显式重试"
                            if raw is not None
                            else "模型请求失败或回复格式无效；可手动填写或显式重试"
                        )
                    ),
                    completed_at=completed,
                    updated_at=completed,
                )
                job["attempts"][-1].update(
                    status=status, finished_at=time.time(), request_started=job.get("request_started", False)
                )
                atomic_json(path, job)
                return job
