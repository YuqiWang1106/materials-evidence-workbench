"""Durable first-generation study reports, independent of extraction state."""

import fcntl
import json
from collections import Counter
from copy import deepcopy
from hashlib import sha256

from .core import StateConflict, atomic_json, clock, fingerprint
from .document_report_generation import compile_report_output, generation_audit
from .document_report_time import report_times
from .document_reports import (
    INSTRUCTIONS,
    MODEL,
    PROMPT_VERSION,
    SUPPORTED_PROMPT_VERSIONS,
    build_report_input,
    encode_report_input,
    report_schema,
    validate_report_output,
)
from .extraction.entity_evidence_service import EntityEvidenceMixin
from .ingest.resources import IngestionResources
from .llm import BudgetExceeded, ModelBudget, ModelCallError, ModelClient
from .storage.file_time import epoch_seconds, utc_text


class DocumentReportJobsMixin(EntityEvidenceMixin):
    resources: IngestionResources

    @staticmethod
    def _report_job_id(task_id, candidate_id, study_ref):
        if any(not isinstance(v, str) or not v or len(v) > 300 for v in (task_id, candidate_id, study_ref)):
            raise ValueError("invalid report identity")
        return "drj-" + fingerprint([task_id, candidate_id, study_ref])[:32]

    def _report_root(self):
        return self.store.root / "document_reports"

    def _report_current(self, task_id, candidate_id, study_ref, previous=None):
        task = self.store.get_task(task_id)
        if task["source_type"] not in ("paper", "patent"):
            raise ValueError("reports require paper/patent tasks")
        self.candidate(task_id)  # Reject active extraction and damaged packages.
        for asset_id in task["asset_ids"]:
            self.store.asset_bytes(asset_id)
        from .document_display import attach_display

        view = attach_display(self, task_id, self.document_candidates(task_id, offset=0, limit=10000))
        identity = dict(
            task_id=task_id,
            candidate_id=candidate_id,
            study_ref=study_ref,
            candidate_sha256=view.get("candidate_sha256"),
            review_sha256=view.get("review_sha256"),
        )
        if previous is not None and (identity != previous["identity"] or previous["version"] != PROMPT_VERSION):
            return task, {"identity": identity}
        snapshot = build_report_input(
            view, task_id=task_id, candidate_id=candidate_id, study_ref=study_ref, source_type=task["source_type"]
        )
        return task, snapshot

    def _report_availability(self):
        config = self.document_options.get("model_config") or {}
        available = bool(config.get("api_key")) and config.get("protocol", "responses") in {
            "responses",
            "chat_completions",
        }
        return {
            "model": config.get("model") or MODEL,
            "model_available": available,
            "model_unavailable_reason": None if available else "model_service_not_configured",
        }

    def _report_read(self, path):
        try:
            job = json.loads(path.read_text())
            identity = job["input_version"]
            if (
                job["job_id"] != path.parent.name
                or job["job_id"]
                != self._report_job_id(identity["task_id"], identity["candidate_id"], identity["study_ref"])
                or not isinstance(job["model"], str)
                or not job["model"]
                or job["prompt_version"] not in SUPPORTED_PROMPT_VERSIONS
            ):
                raise ValueError("identity")
            snapshot = json.loads((path.parent / "input.json").read_text())
            if (
                snapshot["identity"] != identity
                or fingerprint(snapshot) != job["input_sha256"]
                or snapshot["version"] != job["prompt_version"]
                or snapshot["scientific_validation"] is not False
            ):
                raise ValueError("input binding")
            report = None
            if job["status"] == "ready" or job.get("report_sha256") is not None:
                report = json.loads((path.parent / "report.json").read_text())
                if (
                    fingerprint(report) != job["report_sha256"]
                    or report["input_version"] != identity
                    or report["input_sha256"] != job["input_sha256"]
                    or report["model"] != job["model"]
                    or report["prompt_version"] != job["prompt_version"]
                    or report["status"] != "draft"
                    or report["scientific_validation"] is not False
                    or report["report_id"] != "report-" + job["job_id"][4:]
                ):
                    raise ValueError("report binding")
                validate_report_output(report["output"], snapshot)
                if snapshot["version"] == "study-report-v6":
                    raw = json.loads((path.parent / "attempts" / str(job["attempt"]) / "provider.json").read_text())
                    if report["output"] != compile_report_output(raw, snapshot) or report.get(
                        "generation"
                    ) != generation_audit(raw, snapshot):
                        raise ValueError("generation binding")
            return job, snapshot, report
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise StateConflict("report artifact integrity check failed; no automatic regeneration") from exc

    @staticmethod
    def _report_expected(snapshot, payload):
        if (
            payload.get("expected_candidate_sha256") != snapshot["identity"]["candidate_sha256"]
            or payload.get("expected_review_sha256") != snapshot["identity"]["review_sha256"]
        ):
            raise StateConflict("candidate/review version changed; reload before opening report")

    def _report_response(self, job, snapshot, report, current):
        rules_changed = job["prompt_version"] != PROMPT_VERSION
        stale = rules_changed or current["identity"] != snapshot["identity"] or job["status"] == "stale"
        return {
            **report_times(deepcopy(job), epoch_seconds),
            **self._report_availability(),
            "status": "stale" if stale else job["status"],
            "stale": stale,
            "stale_reason": "report_rules_changed" if rules_changed else "input_changed" if stale else None,
            "current_prompt_version": PROMPT_VERSION,
            "current_input_version": deepcopy(current["identity"]),
            "report": report_times(report, epoch_seconds) if not stale and job["status"] == "ready" else None,
        }

    def document_report(self, task_id, *, candidate_id, study_ref):
        job_id = self._report_job_id(task_id, candidate_id, study_ref)
        path = self._report_root() / job_id / "job.json"
        if not path.exists():
            _, current = self._report_current(task_id, candidate_id, study_ref)
            return {
                "status": "not_generated",
                "job_id": None,
                "report": None,
                "error": None,
                "stale": False,
                "input_version": current["identity"],
                "current_prompt_version": PROMPT_VERSION,
                **self._report_availability(),
            }
        job, snapshot, report = self._report_read(path)
        _, current = self._report_current(task_id, candidate_id, study_ref, previous=snapshot)
        return self._report_response(job, snapshot, report, current)

    def enqueue_document_report(self, task_id, payload):
        if self.document_report_revision_service is not None:
            raise StateConflict("explicit report revision required")
        cid, study = payload.get("candidate_id"), payload.get("study_ref")
        job_id = self._report_job_id(task_id, cid, study)
        task = self.store.get_task(task_id)
        path = self._report_root() / job_id / "job.json"
        created = False
        with self.store.lock("source:" + task["source_id"]):
            prior = self._report_read(path)[1] if path.exists() else None
            _, snapshot = self._report_current(task_id, cid, study, previous=prior)
            self._report_expected(snapshot, payload)
            with self.store.lock("document-report-publish:" + job_id):
                if path.exists():
                    job, saved, report = self._report_read(path)
                    return self._report_response(job, saved, report, snapshot)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_json(path.parent / "input.json", snapshot)
                available = self._report_availability()["model_available"]
                now = clock.now()
                job = {
                    "job_id": job_id,
                    "input_version": snapshot["identity"],
                    "input_sha256": fingerprint(snapshot),
                    "model": self._report_availability()["model"],
                    "prompt_version": PROMPT_VERSION,
                    "status": "queued" if available else "failed",
                    "attempt": 1,
                    "request_started": False,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "report_sha256": None,
                    "error": None
                    if available
                    else {"code": "model_unavailable", "message": "报告模型尚未配置可计价的官方端点与凭据"},
                    "attempts": [
                        {"attempt": 1, "status": "queued" if available else "failed", "request_started": False}
                    ],
                }
                atomic_json(path, report_times(job, utc_text))
                created = available
        if created:
            self.start_document_report_job(job_id)
        return self._report_response(job, snapshot, None, snapshot)

    def retry_document_report(self, task_id, job_id, payload):
        if self.document_report_revision_service is not None:
            raise StateConflict("explicit report revision required")
        if payload.get("retry") is not True:
            raise StateConflict("explicit retry:true required")
        if job_id != self._report_job_id(task_id, payload.get("candidate_id"), payload.get("study_ref")):
            raise StateConflict("retry report belongs to another unit")
        path = self._report_root() / job_id / "job.json"
        task = self.store.get_task(task_id)
        with self.store.lock("document-report-job:" + job_id), self.store.lock("source:" + task["source_id"]):
            job, snapshot, _ = self._report_read(path)
            if job["prompt_version"] != PROMPT_VERSION:
                raise StateConflict("report rules changed; archived reports cannot retry or regenerate")
            _, current = self._report_current(task_id, payload["candidate_id"], payload["study_ref"], previous=snapshot)
            self._report_expected(current, payload)
            if snapshot["identity"] != current["identity"]:
                raise StateConflict("stale reports cannot regenerate in first-generation workflow")
            if job["status"] not in ("failed", "outcome_unknown"):
                raise StateConflict("report state does not allow retry")
            if not self._report_availability()["model_available"]:
                raise StateConflict("report model unavailable")
            job.update(
                attempt=job["attempt"] + 1,
                status="queued",
                request_started=False,
                error=None,
                completed_at=None,
                updated_at=clock.now(),
            )
            job["attempts"].append({"attempt": job["attempt"], "status": "queued", "request_started": False})
            atomic_json(path, report_times(job, utc_text))
        self.start_document_report_job(job_id)
        return self._report_response(job, snapshot, None, current)

    def start_document_report_job(self, job_id):
        if self.document_report_revision_service is not None:
            raise StateConflict("legacy report generation retired on website")
        key = "document-report:" + job_id
        with self._guard:
            if key in self._running:
                return
            self._running.add(key)

        def run():
            try:
                self._run_document_report_job(job_id)
            finally:
                with self._guard:
                    self._running.discard(key)

        try:
            self.pool.submit(run)
        except RuntimeError:
            with self._guard:
                self._running.discard(key)
            # Durable queued state is recovered by the next service instance.

    def _report_client(self, directory, job):
        config = self.document_options.get("model_config") or {}
        if not self._report_availability()["model_available"]:
            raise ModelCallError("model_unavailable")
        return ModelClient(
            api_key=config["api_key"],
            base_url=config.get("base_url", "https://api.openai.com/v1"),
            model=job["model"],
            protocol=config.get("protocol", "responses"),
            reasoning_effort="medium",
            max_attempts=1,
            max_output_tokens=6000,
            timeout=180,
            concurrency=1,
            count_input_tokens=False,
            budget=ModelBudget(
                max_calls=1,
                max_output_tokens=6000,
                max_total_tokens=150000,
                max_wall_seconds=180,
                active_time=True,
                **self.resources.model_workspace,
                budget_state_path=directory / "budget.json",
            ),
            ledger_path=directory / "attempts.jsonl",
            cache_dir=self._report_root() / "cache",
        )

    @staticmethod
    def _report_ledger(directory):
        path = directory / "attempts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _run_document_report_job(self, job_id):
        if self.document_report_revision_service is not None:
            return
        path = self._report_root() / job_id / "job.json"
        with self.store.lock("document-report-job:" + job_id):
            job, snapshot, _ = self._report_read(path)
            if job["prompt_version"] != PROMPT_VERSION or job["status"] != "queued":
                return
            directory = path.parent / "attempts" / str(job["attempt"])
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            identity = job["input_version"]
            received = False
            job.update(status="running", updated_at=clock.now())
            job["attempts"][-1]["status"] = "running"
            atomic_json(path, report_times(job, utc_text))
            try:
                task, _ = self._report_current(
                    identity["task_id"], identity["candidate_id"], identity["study_ref"], previous=snapshot
                )
                client = self._report_client(directory, job)

                def version_guard():
                    # ModelClient repeats this after waiting for its provider
                    # slot and just before reservations/dispatch.
                    with self.store.lock("source:" + task["source_id"]):
                        _, current = self._report_current(
                            identity["task_id"], identity["candidate_id"], identity["study_ref"], previous=snapshot
                        )
                        if current["identity"] != identity:
                            job["status"] = "stale"
                            return True
                        job.update(request_started=True, updated_at=clock.now())
                        job["attempts"][-1]["request_started"] = True
                        atomic_json(path, report_times(job, utc_text))
                    return False

                client.cancel_check = version_guard
                result = client.call(
                    INSTRUCTIONS,
                    encode_report_input(snapshot),
                    report_schema(snapshot),
                    phase="document_study_report",
                    context={
                        "job_id": job_id,
                        "input_sha256": job["input_sha256"],
                        "attempt": job["attempt"],
                        "prompt_version": PROMPT_VERSION,
                    },
                    bypass_cache=True,
                )
                atomic_json(directory / "provider.json", result)
                received = True
                audit = generation_audit(result, snapshot)
                result = compile_report_output(result, snapshot)
                rows = self._report_ledger(directory)
                final = next((r for r in reversed(rows) if r.get("status") != "started"), {})
                report = {
                    "report_id": "report-" + job_id[4:],
                    "input_version": identity,
                    "input_sha256": job["input_sha256"],
                    "model": job["model"],
                    "prompt_version": PROMPT_VERSION,
                    "generated_at": utc_text(clock.now()),
                    "status": "draft",
                    "scientific_validation": False,
                    "usage": final.get("usage"),
                    "output": result,
                    "structured_table": snapshot["structured_table"],
                    "study_label": snapshot["study_label"],
                    "context": snapshot["context"],
                    "input_summary": {
                        "fact_count": len(snapshot["facts"]),
                        "excluded_count": snapshot["excluded_count"],
                        "issue_count": len(snapshot["issues"]),
                        "review_counts": dict(Counter(f["review_status"] for f in snapshot["facts"])),
                    },
                    "citations": snapshot["citations"],
                    "gaps": snapshot["gaps"],
                }
                if audit is not None:
                    report["generation"] = audit
                atomic_json(path.parent / "report.json", report)
                with self.store.lock("source:" + task["source_id"]):
                    _, current = self._report_current(
                        identity["task_id"], identity["candidate_id"], identity["study_ref"], previous=snapshot
                    )
                    stale = current["identity"] != identity
                    job.update(
                        status="stale" if stale else "ready",
                        report_sha256=fingerprint(report),
                        request_started=False,
                        error={"code": "input_changed", "message": "生成期间候选或审核版本变化，报告不作为当前结果"}
                        if stale
                        else None,
                    )
                    self._finish_report_job(path, job)
            except Exception as exc:
                rows = self._report_ledger(directory)
                final = rows[-1] if rows else {}
                unknown = (
                    job.get("request_started")
                    and not received
                    and final.get("status") in ("started", "transport_unknown", "cancelled_after_dispatch")
                )
                if job["status"] == "stale" or isinstance(exc, StateConflict):
                    status, code, message = "stale", "input_changed", "候选、审核或原件已变化，未产生当前报告"
                elif isinstance(exc, BudgetExceeded):
                    status, code, message = "failed", "execution_limit", "报告调用次数或执行时间达到上限，未自动重发"
                elif unknown:
                    status, code, message = (
                        "outcome_unknown",
                        "outcome_unknown",
                        "模型请求结果未知，保留请求记录且不会自动重发",
                    )
                else:
                    code = (
                        "model_unavailable"
                        if str(exc) == "model_unavailable"
                        else "invalid_report"
                        if received
                        else "provider_failed"
                    )
                    status, message = "failed", "模型请求或报告校验失败；仅可显式重试"
                job.update(status=status, error={"code": code, "message": message}, request_started=bool(unknown))
                self._finish_report_job(path, job)

    @staticmethod
    def _finish_report_job(path, job):
        finished = clock.now()
        job.update(updated_at=finished, completed_at=finished)
        job["attempts"][-1].update(status=job["status"], request_started=job["request_started"], finished_at=finished)
        atomic_json(path, report_times(job, utc_text))

    def recover_document_report_jobs(self):
        if self.document_report_revision_service is not None:
            return
        for path in self._report_root().glob("drj-*/job.json"):
            try:
                job, _, _ = self._report_read(path)
                if job["prompt_version"] != PROMPT_VERSION:
                    continue
                if job["status"] == "running":
                    lock_path = (
                        self.store.root
                        / "locks"
                        / (sha256(("document-report-job:" + job["job_id"]).encode()).hexdigest() + ".lock")
                    )
                    with lock_path.open("a+b") as stream:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        job, _, _ = self._report_read(path)
                        if job["prompt_version"] != PROMPT_VERSION:
                            continue
                        if job["status"] == "running":
                            unknown = bool(job.get("request_started"))
                            job.update(
                                status="outcome_unknown" if unknown else "queued",
                                updated_at=clock.now(),
                                error={"code": "outcome_unknown", "message": "执行进程中断，模型结果未知；不会自动重发"}
                                if unknown
                                else None,
                            )
                            job["attempts"][-1]["status"] = job["status"]
                            atomic_json(path, report_times(job, utc_text))
                if job["status"] == "queued":
                    self.start_document_report_job(job["job_id"])
            except (OSError, ValueError, KeyError, StateConflict):
                # Corruption remains fail-closed; reads surface the integrity error.
                continue
