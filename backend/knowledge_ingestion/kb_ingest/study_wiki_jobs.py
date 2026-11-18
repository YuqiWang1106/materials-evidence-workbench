"""One durable generation attempt per fixed study input, separate publication retries."""

import asyncio
import fcntl
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import StateConflict, atomic_json, fingerprint
from .document_report_revision_service import DocumentReportRevisionService
from .knowledge_claim_contracts import KnowledgeSupport, WikiContent
from .llm.model_requests import ModelRequestError
from .llm.project_model_dispatch import ModelDispatchError, ProjectModelDispatcher
from .storage.models import StudyUnit, StudyWikiJob
from .study_units import StudyUnitService
from .study_wiki_policy import POLICY_VERSION, RULE_VERSION


def _row(row):
    return {c.name: deepcopy(getattr(row, c.name)) for c in row.__table__.columns}


class StudyWikiJobs:
    def __init__(self, ingestion, wiki, *, dispatchers=None, generation=None, auto_start=True):
        self.ingestion = ingestion
        self.store = ingestion.store
        self.wiki = wiki
        self.units = StudyUnitService(ingestion, workspace_id=wiki.workspace_id)
        self.dispatchers = dict(dispatchers or {})
        self.generation = generation
        self.auto_start = auto_start
        self._stop = Event()
        self._guard = Lock()
        self._pending = set()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="study-wiki")
        self.selection_path = self.store.root / "study-wiki-model-selection.json"
        ingestion.study_wiki_jobs = self

    def _generation(self):
        if self.generation is None:
            from . import study_unit_wiki_generation

            return study_unit_wiki_generation
        return self.generation

    def configure_selection(self, model, *, comparison_id, evidence_hash):
        if model not in self.dispatchers:
            raise StateConflict("STUDY_WIKI_MODEL_UNAVAILABLE")
        if not isinstance(comparison_id, str) or not comparison_id or not re.fullmatch("[0-9a-f]{64}", evidence_hash):
            raise ValueError("MODEL_COMPARISON_REQUIRED")
        dispatcher = self.dispatchers[model]
        if not isinstance(dispatcher, ProjectModelDispatcher) or dispatcher.context.policy.model != model:
            raise StateConflict("STUDY_WIKI_MODEL_UNAVAILABLE")
        atomic_json(
            self.selection_path,
            {
                "version": "study-wiki-model-selection-v1",
                "model": model,
                "comparison_id": comparison_id,
                "evidence_hash": evidence_hash,
                "rule_version": RULE_VERSION,
            },
        )
        return self.capabilities()

    def _selection(self):
        try:
            selected = json.loads(self.selection_path.read_text())
            if not isinstance(selected, dict):
                return None
            if (
                selected.get("version") != "study-wiki-model-selection-v1"
                or selected.get("rule_version") != RULE_VERSION
            ):
                return None
            if (
                not selected.get("model")
                or not selected.get("comparison_id")
                or not re.fullmatch("[0-9a-f]{64}", selected.get("evidence_hash", ""))
            ):
                return None
            return selected
        except FileNotFoundError:
            if len(self.dispatchers) == 1:
                model = next(iter(self.dispatchers))
                return {
                    "version": "study-wiki-model-selection-v1",
                    "model": model,
                    "selection_source": "private_configuration",
                    "rule_version": RULE_VERSION,
                }
            return None
        except (OSError, ValueError, TypeError):
            return None

    def capabilities(self):
        selection = self._selection()
        model = selection["model"] if selection else None
        dispatcher = self.dispatchers.get(model)
        ready = isinstance(dispatcher, ProjectModelDispatcher) and dispatcher.context.policy.model == model
        return {
            "ready": bool(ready),
            "model": model,
            "unavailable_reason": None
            if ready
            else "model_comparison_required"
            if selection is None
            else "trusted_dispatcher_required",
            "rule_version": RULE_VERSION,
            "concurrency": 1,
            "max_output_tokens": 8000,
            "automatic_retries": False,
        }

    def _load(self, job_id):
        with Session(self.store.engine) as session:
            job = session.get(StudyWikiJob, job_id)
            if job is None:
                raise KeyError("STUDY_WIKI_JOB_NOT_FOUND")
            row = _row(job)
        self.units.get(row["unit_id"])
        if fingerprint(row["input_payload"]) != row["input_payload_hash"]:
            raise StateConflict("STUDY_WIKI_INPUT_INTEGRITY")
        if row["result"] and fingerprint(row["result"].get("compiled")) != row["result"].get("compiled_hash"):
            raise StateConflict("STUDY_WIKI_RESULT_INTEGRITY")
        return row

    def get(self, job_id):
        row = self._load(job_id)
        return {k: v for k, v in row.items() if k not in {"input_payload", "input_payload_hash", "provider_response"}}

    def for_unit(self, unit_id):
        self.units.get(unit_id)
        with Session(self.store.engine) as session:
            ids = list(
                session.scalars(
                    select(StudyWikiJob.job_id)
                    .where(StudyWikiJob.unit_id == unit_id)
                    .order_by(StudyWikiJob.created_at.desc(), StudyWikiJob.job_id.desc())
                )
            )
        return [self.get(jid) for jid in ids]

    def proposal(self, unit_id):
        with Session(self.store.engine) as session:
            row = session.scalar(
                select(StudyWikiJob)
                .where(StudyWikiJob.unit_id == unit_id, StudyWikiJob.status == "update_available")
                .order_by(StudyWikiJob.created_at.desc())
            )
            if row is None:
                return None
            result = _row(row)
        unit = self.units.get(unit_id)
        return (
            self.get(result["job_id"])
            if unit["current_available"]
            and not unit["auto_generation_disabled"]
            and unit["current_snapshot_id"] == result["snapshot_id"]
            else None
        )

    def _update(self, job_id, **values):
        with self.store._write() as session:
            row = session.get(StudyWikiJob, job_id)
            if row is None:
                raise KeyError("STUDY_WIKI_JOB_NOT_FOUND")
            for key, value in values.items():
                setattr(row, key, deepcopy(value))
            row.updated_at = time.time()

    def _current(self, row):
        unit = self.units.get(row["unit_id"])
        if not unit["current_available"] or unit["current_snapshot_id"] != row["snapshot_id"]:
            raise StateConflict("STUDY_WIKI_SUPERSEDED")
        if unit["auto_generation_disabled"] or unit["page_id"] and self.wiki.store.is_deleted(unit["page_id"]):
            raise StateConflict("STUDY_WIKI_DISABLED")
        self.units.resolve(self.units.reference(unit["unit_id"]))
        return unit

    def request(self, unit_id, *, expected_snapshot_id, request_id, schedule=True, allow_retry=True):
        from .table_chat_store import request_id as validate_request_id

        validate_request_id(request_id)
        unit = self.units.get(unit_id)
        if not unit["current_available"] or unit["current_snapshot_id"] != expected_snapshot_id:
            raise StateConflict("STUDY_SNAPSHOT_CHANGED")
        if unit["auto_generation_disabled"] or unit["page_id"] and self.wiki.store.is_deleted(unit["page_id"]):
            raise StateConflict("STUDY_WIKI_DISABLED")
        if not self.capabilities()["ready"]:
            raise StateConflict("STUDY_WIKI_MODEL_NOT_READY")
        selection = self._selection()
        try:
            fixed = self._generation().build_input(self.units, unit_id, snapshot_id=expected_snapshot_id)
        except (ValueError, StateConflict, KeyError) as error:
            return self._preparation_failed(unit, selection, error)
        if fixed["snapshot_id"] != expected_snapshot_id or fixed["content_hash"] != unit["snapshot"]["content_hash"]:
            raise StateConflict("STUDY_SNAPSHOT_CHANGED")
        job_id = "study-wiki-job-" + fingerprint([unit_id, fixed["input_hash"], RULE_VERSION])[:40]
        base = (
            self.wiki.store.get_page(unit["page_id"])["current_revision_id"]
            if unit["page_id"] and not self.wiki.store.is_deleted(unit["page_id"])
            else None
        )
        with self.store._write() as session:
            stored = session.get(StudyUnit, unit_id)
            if (
                stored.current_snapshot_id != expected_snapshot_id
                or not stored.current_available
                or stored.auto_generation_disabled
            ):
                raise StateConflict("STUDY_SNAPSHOT_CHANGED")
            job = session.get(StudyWikiJob, job_id)
            if job is None:
                now = time.time()
                job = StudyWikiJob(
                    job_id=job_id,
                    unit_id=unit_id,
                    snapshot_id=expected_snapshot_id,
                    input_hash=fixed["input_hash"],
                    rule_version=RULE_VERSION,
                    model={
                        "model": selection["model"],
                        "selection": selection,
                        "reasoning_effort": "medium",
                        "max_output_tokens": 8000,
                    },
                    input_payload=fixed,
                    input_payload_hash=fingerprint(fixed),
                    status="queued",
                    base_revision=base,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
            for prior in session.scalars(
                select(StudyWikiJob).where(
                    StudyWikiJob.unit_id == unit_id,
                    StudyWikiJob.job_id != job_id,
                    StudyWikiJob.status.in_(["queued", "update_available"]),
                )
            ):
                if prior.snapshot_id != expected_snapshot_id:
                    prior.status = "superseded"
        existing = self._load(job_id)
        if allow_retry and existing["status"] == "failed":
            if existing["provider_response"] is not None:
                self._update(job_id, status="publishing" if existing["result"] else "received", error=None)
            else:
                receipt = self._receipt(existing)
                if receipt is None or receipt.get("ledger_state") in {"not_reserved", "canceled"}:
                    self._update(job_id, status="queued", provider_receipt=None, error=None)
                else:
                    self._update(job_id, status="outcome_unknown", error={"code": "MODEL_OUTCOME_UNKNOWN"})
        if self.auto_start and schedule:
            self._schedule(job_id)
        return self.get(job_id)

    def _preparation_failed(self, unit, selection, error):
        payload = {
            "version": "study-wiki-input-unavailable",
            "unit_id": unit["unit_id"],
            "snapshot_id": unit["current_snapshot_id"],
            "content_hash": unit["snapshot"]["content_hash"],
        }
        input_hash = fingerprint(payload)
        jid = "study-wiki-job-" + fingerprint([unit["unit_id"], input_hash, RULE_VERSION])[:40]
        code = (
            "STUDY_WIKI_INPUT_TOO_LARGE"
            if "exceed" in str(error).lower() or "limit" in str(error).lower()
            else "STUDY_WIKI_INPUT_PREPARATION_FAILED"
        )
        with self.store._write() as session:
            row = session.get(StudyWikiJob, jid)
            if row is None:
                now = time.time()
                row = StudyWikiJob(
                    job_id=jid,
                    unit_id=unit["unit_id"],
                    snapshot_id=unit["current_snapshot_id"],
                    input_hash=input_hash,
                    rule_version=RULE_VERSION,
                    model={"model": selection["model"], "selection": selection},
                    input_payload=payload,
                    input_payload_hash=fingerprint(payload),
                    status="failed",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            row.error = {"code": code, "phase": "input_preparation"}
        return self.get(jid)

    def on_snapshots(self, rows):
        for row in rows:
            if row.get("auto_generation_disabled"):
                continue
            if row.get("page_id"):
                try:
                    page = self.wiki.store.get_page(row["page_id"])
                    binding = page["revision"]["dependency_manifest"].get("study_unit", {})
                    if binding.get("snapshot_id") != row["current_snapshot_id"]:
                        self.wiki.store.record_availability(row["page_id"], [{"code": "STUDY_SNAPSHOT_CHANGED"}])
                except (KeyError, StateConflict):
                    pass
            if not self.capabilities()["ready"]:
                continue
            try:
                self.request(
                    row["unit_id"],
                    expected_snapshot_id=row["current_snapshot_id"],
                    request_id="auto-" + row["current_snapshot_id"],
                    allow_retry=False,
                )
            except (ValueError, StateConflict, KeyError):
                continue

    def _lease(self):
        stream = (self.store.root / "locks" / (fingerprint("study-wiki-single-worker") + ".lock")).open("a+b")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        return stream

    def _schedule(self, job_id):
        with self._guard:
            if self._stop.is_set() or job_id in self._pending:
                return
            self._pending.add(job_id)

        def run():
            try:
                self.process(job_id)
            finally:
                with self._guard:
                    self._pending.discard(job_id)

        try:
            self.pool.submit(run)
        except RuntimeError:
            with self._guard:
                self._pending.discard(job_id)

    def _receipt(self, row):
        receipt = row.get("provider_receipt")
        if not receipt:
            return None
        dispatcher = self.dispatchers.get(row["model"]["model"])
        if not isinstance(dispatcher, ProjectModelDispatcher):
            return receipt
        try:
            execution = dispatcher.context.service.read_call(receipt["project_id"], receipt["call_id"])
        except ModelRequestError as error:
            if error.code == "call_not_found":
                return {**receipt, "ledger_state": "unknown", "missing_legacy_request": True}
            raise
        if execution is None:
            return {**receipt, "ledger_state": "unknown", "missing_legacy_request": True}
        if execution["request_sha256"] != receipt["request_sha256"] or execution["model"] != row["model"]["model"]:
            raise StateConflict("STUDY_WIKI_REQUEST_BINDING")
        return {
            **receipt,
            "ledger_state": execution["state"],
            "receipt_id": execution["receipt_id"],
            "result_sha256": execution["result_sha256"],
            "sent_at": execution["sent_at"],
        }

    def _responses_request(self, fixed, model):
        spec = self._generation().request(fixed)
        content = [{"type": "input_text", "text": spec["input_text"]}] + [
            {"type": "input_file", **deepcopy(file)} for file in spec.get("files", [])
        ]
        return {
            "model": model,
            "instructions": spec["instructions"],
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": 8000,
            "reasoning": {"effort": "medium"},
            "store": False,
            "stream": False,
            "text": {
                "format": {"type": "json_schema", "name": "study_unit_wiki", "schema": spec["schema"], "strict": False}
            },
        }

    def process(self, job_id):
        lease = self._lease()
        if lease is None:
            return self.get(job_id)
        try:
            row = self._load(job_id)
            if row["status"] in {"published", "update_available", "failed", "outcome_unknown", "superseded"}:
                return self.get(job_id)
            self._current(row)
            if row["status"] == "running" and row.get("provider_receipt") and row.get("provider_response") is None:
                receipt = self._receipt(row)
                self._update(
                    job_id, status="outcome_unknown", provider_receipt=receipt, error={"code": "MODEL_OUTCOME_UNKNOWN"}
                )
                return self.get(job_id)
            if row["provider_response"] is None:
                dispatcher = self.dispatchers.get(row["model"]["model"])
                if (
                    not isinstance(dispatcher, ProjectModelDispatcher)
                    or dispatcher.context.policy.model != row["model"]["model"]
                ):
                    self._update(job_id, status="failed", error={"code": "STUDY_WIKI_MODEL_UNAVAILABLE"})
                    return self.get(job_id)
                request = self._responses_request(row["input_payload"], row["model"]["model"])
                dispatcher.validate(request)
                self._update(job_id, status="running")

                def on_dispatch(event):
                    self._current(self._load(job_id))
                    self._update(
                        job_id,
                        provider_receipt={
                            **event,
                            "project_id": dispatcher.context.project_id,
                            "state": "dispatch_pending",
                        },
                    )

                response = asyncio.run(dispatcher.dispatch(request, on_dispatch=on_dispatch))
                parsed = DocumentReportRevisionService._output(response)
                self._update(
                    job_id,
                    status="received",
                    provider_response=parsed,
                    provider_receipt=self._receipt(self._load(job_id)),
                )
                row = self._load(job_id)
            if row["result"] is None:
                compiled = self._generation().compile(row["provider_response"], row["input_payload"])
                WikiContent.model_validate(compiled["content"])
                supports = [
                    KnowledgeSupport.model_validate(s).model_dump(mode="json") for s in compiled["support_refs"]
                ]
                compiled = deepcopy(compiled)
                compiled["support_refs"] = supports
                self._update(
                    job_id, status="received", result={"compiled": compiled, "compiled_hash": fingerprint(compiled)}
                )
            self._apply(job_id)
        except StateConflict as exc:
            code = str(exc)
            status = (
                "superseded"
                if code
                in {"STUDY_WIKI_SUPERSEDED", "STUDY_WIKI_DISABLED", "STUDY_SNAPSHOT_CHANGED", "STUDY_REFERENCE_STALE"}
                else "failed"
            )
            self._update(job_id, status=status, error={"code": code})
        except Exception as exc:
            row = self._load(job_id)
            receipt = self._receipt(row)
            dispatched = bool(receipt and receipt.get("ledger_state") not in {"not_reserved", "canceled", "failed"})
            unknown = row["provider_response"] is None and dispatched
            code = (
                exc.code.upper()
                if isinstance(exc, ModelDispatchError)
                else "PUBLICATION_FAILED"
                if row["result"]
                else "INVALID_GENERATION"
                if row["provider_response"] is not None
                else "MODEL_FAILED"
            )
            self._update(
                job_id,
                status="outcome_unknown" if unknown else "failed",
                provider_receipt=receipt,
                error={"code": code},
            )
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)
            lease.close()
        return self.get(job_id)

    def _manifest(self, row, compiled):
        unit = self.units.get(row["unit_id"])
        corpus = self.wiki._corpus([unit["source_id"]], kind="study-unit-editorial-corpus")
        dependencies = [
            {"dependency_id": "study-" + fingerprint(ref), "kind": "study_unit_snapshot", "ref": deepcopy(ref)}
            for ref in compiled["dependency_refs"]
        ]
        return {
            "version": "knowledge-dependencies-v1",
            "corpus_snapshot_id": corpus,
            "supports": deepcopy(compiled["support_refs"]),
            "dependencies": dependencies,
            "study_unit": {
                "unit_id": row["unit_id"],
                "snapshot_id": row["snapshot_id"],
                "content_hash": row["input_payload"]["content_hash"],
                "input_hash": row["input_hash"],
                "rule_version": RULE_VERSION,
                "publication_policy": POLICY_VERSION,
                "manual_edited": False,
                "generation_manifest": deepcopy(compiled["generation_manifest"]),
            },
        }

    def _apply(self, job_id, *, adopt=False, expected_page_revision=None, request_id=None):
        row = self._load(job_id)
        compiled = row["result"]["compiled"]
        with self.store.lock("study-wiki-unit:" + row["unit_id"]):
            unit = self._current(row)
            page_id = unit["page_id"] or "study-wiki-" + fingerprint([self.wiki.workspace_id, row["unit_id"]])[:40]
            with self.store.lock("source:" + unit["source_id"]):
                self._current(row)
                try:
                    page = self.wiki.store.get_page(page_id)
                except KeyError:
                    if self.wiki.store.is_deleted(page_id):
                        raise StateConflict("STUDY_WIKI_DISABLED")
                    page = None
                rid = request_id or "study-generate-" + job_id
                digest = fingerprint([rid, row["input_hash"], compiled])
                known = self.wiki.store.find_revision_request(rid, digest)
                applied = row["result"].get("applied_revision_id") or (known["revision_id"] if known else None)
                if applied and page and page["current_revision_id"] == applied:
                    saved = known or page["revision"]
                else:
                    if (
                        page
                        and not adopt
                        and (
                            page["revision"]["dependency_manifest"].get("study_unit", {}).get("manual_edited")
                            or page["current_revision_id"] != row["base_revision"]
                        )
                    ):
                        self._update(job_id, status="update_available")
                        return
                    if adopt and (page is None or page["current_revision"] != expected_page_revision):
                        raise StateConflict("REVISION_CONFLICT")
                    manifest = self._manifest(row, compiled)
                    saved = self.wiki.store.save_revision(
                        page_id=page_id,
                        expected_revision=page["current_revision"] if page else 0,
                        request_id=rid,
                        request_hash=digest,
                        content=compiled["content"],
                        dependency_manifest=manifest,
                    )
                # The page link and job apply receipt share one local commit.
                # A preceding Wiki commit is recoverable via its exact request ID.
                with self.store._write() as session:
                    stored = session.get(StudyUnit, row["unit_id"])
                    stored.page_id = page_id
                    job = session.get(StudyWikiJob, job_id)
                    job.result = {**row["result"], "applied_revision_id": saved["revision_id"]}
                    job.status = "publishing"
                    job.error = None
                    job.updated_at = time.time()
            page = self.wiki.ensure_study_published(page_id)
            result = self._load(job_id)["result"]
            result["published_revision_id"] = page["published_revision_id"]
            self._update(job_id, status="published", result=result, error=None)

    def prepare_evaluation(self, unit_id, *, snapshot_id, model):
        unit = self.units.get(unit_id)
        if not unit["current_available"] or unit["current_snapshot_id"] != snapshot_id:
            raise StateConflict("STUDY_SNAPSHOT_CHANGED")
        dispatcher = self.dispatchers.get(model)
        if not isinstance(dispatcher, ProjectModelDispatcher) or dispatcher.context.policy.model != model:
            raise StateConflict("STUDY_WIKI_MODEL_UNAVAILABLE")
        fixed = self._generation().build_input(self.units, unit_id, snapshot_id=snapshot_id)
        return {"fixed_input": fixed, "request": dispatcher.validate(self._responses_request(fixed, model))}

    def import_evaluation_result(self, unit_id, *, snapshot_id, input_hash, model, response, provider_receipt):
        """Import only a completed response bound to this exact production request."""
        prepared = self.prepare_evaluation(unit_id, snapshot_id=snapshot_id, model=model)
        if (
            prepared["fixed_input"]["input_hash"] != input_hash
            or not self._selection()
            or self._selection()["model"] != model
        ):
            raise StateConflict("EVALUATION_INPUT_MISMATCH")
        dispatcher = self.dispatchers[model]
        digest = dispatcher.context.request_hash(dispatcher.context.policy.base_url, "/responses", prepared["request"])
        call = dispatcher.context.service.read_call(dispatcher.context.project_id, provider_receipt.get("call_id"))
        if (
            call is None
            or call["request_sha256"] != digest
            or call["model"] != model
            or call["state"] != "completed"
            or call["result_sha256"] != dispatcher.context.service.result_hash(response)
        ):
            raise StateConflict("EVALUATION_RECEIPT_MISMATCH")
        lease = self._lease()
        if lease is None:
            raise StateConflict("STUDY_WIKI_WORKER_BUSY")
        try:
            job = self.request(
                unit_id,
                expected_snapshot_id=snapshot_id,
                request_id="evaluated-" + fingerprint([input_hash, model]),
                schedule=False,
            )
            existing = self._load(job["job_id"])
            if existing["provider_receipt"]:
                if existing["provider_receipt"]["call_id"] != provider_receipt["call_id"]:
                    raise StateConflict("STUDY_WIKI_ALREADY_DISPATCHED")
            else:
                receipt = {
                    **provider_receipt,
                    "project_id": dispatcher.context.project_id,
                    "request_sha256": digest,
                    "model": model,
                    "ledger_state": "completed",
                    "receipt_id": response.get("id"),
                    "result_sha256": call["result_sha256"],
                }
                self._update(
                    job["job_id"],
                    status="received",
                    provider_receipt=receipt,
                    provider_response=DocumentReportRevisionService._output(response),
                )
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)
            lease.close()
        if self.auto_start:
            self._schedule(job["job_id"])
        return self.get(job["job_id"])

    def retry_publication(self, job_id):
        row = self._load(job_id)
        if row["result"] is None or row["status"] not in {"failed", "publishing"}:
            raise StateConflict("PUBLICATION_RETRY_ONLY")
        self._current(row)
        self._update(job_id, status="publishing", error=None)
        if self.auto_start:
            self._schedule(job_id)
        return self.get(job_id)

    def adopt(self, job_id, *, expected_snapshot_id, expected_page_revision, request_id):
        from .table_chat_store import request_id as validate_request_id

        validate_request_id(request_id)
        row = self._load(job_id)
        if row["status"] != "update_available" or row["snapshot_id"] != expected_snapshot_id:
            raise StateConflict("UPDATE_PROPOSAL_CHANGED")
        self._current(row)
        self._apply(job_id, adopt=True, expected_page_revision=expected_page_revision, request_id=request_id)
        return self.get(job_id)

    def delete(self, page_id, payload):
        with Session(self.store.engine) as session:
            stored = session.scalar(
                select(StudyUnit).where(StudyUnit.page_id == page_id, StudyUnit.workspace_id == self.wiki.workspace_id)
            )
            if stored is None:
                raise KeyError("WIKI_NOT_FOUND")
            uid = stored.unit_id
        with self.store.lock("study-wiki-unit:" + uid):
            if not self.wiki.store.is_deleted(page_id):
                page = self.wiki.store.get_page(page_id)
                if page["current_revision"] != payload["expected_revision"]:
                    raise StateConflict("REVISION_CONFLICT")
            with self.store._write() as session:
                unit = session.get(StudyUnit, uid)
                unit.auto_generation_disabled = True
                for job in session.scalars(
                    select(StudyWikiJob).where(
                        StudyWikiJob.unit_id == uid, StudyWikiJob.status.in_(["queued", "update_available"])
                    )
                ):
                    job.status = "superseded"
            return self.wiki.store.delete(page_id, payload)

    def recover(self):
        with Session(self.store.engine) as session:
            ids = list(
                session.scalars(
                    select(StudyWikiJob.job_id)
                    .join(StudyUnit, StudyUnit.unit_id == StudyWikiJob.unit_id)
                    .where(
                        StudyUnit.workspace_id == self.wiki.workspace_id,
                        StudyWikiJob.status.in_(["queued", "running", "received", "publishing"]),
                    )
                    .order_by(StudyWikiJob.created_at)
                )
            )
        if self.auto_start:
            for jid in ids:
                self._schedule(jid)
        return {"recoverable": len(ids)}

    def close(self):
        self._stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
