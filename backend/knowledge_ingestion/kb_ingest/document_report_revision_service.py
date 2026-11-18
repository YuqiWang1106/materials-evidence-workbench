"""Explicit website reports: one shared-budget dispatch, durable response, no replay."""

import asyncio
import fcntl
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Lock

from .core import StateConflict
from .document_report_revisions import ReportRevisionStore
from .document_reports import INSTRUCTIONS, MODEL, encode_report_input, report_schema
from .llm.model_requests import ModelRequestError
from .llm.project_model_dispatch import ProjectModelDispatcher
from .research_chat_engine import strict_json


class ReportRevisionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DocumentReportRevisionService:
    def __init__(self, ingestion, *, dispatcher=None):
        self.ingestion = ingestion
        self.dispatcher = dispatcher
        self.files = ReportRevisionStore(
            ingestion._report_root(),
            snapshot_loader=lambda u: ingestion._report_current(u["task_id"], u["candidate_id"], u["study_ref"])[1],
            source_lock=lambda u: ingestion.store.lock("source:" + ingestion.store.get_task(u["task_id"])["source_id"]),
            legacy_reader=ingestion._report_read,
            model=dispatcher.context.policy.model if isinstance(dispatcher, ProjectModelDispatcher) else MODEL,
        )
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="report-revision")
        self._stop = Event()
        self._guard = Lock()
        self._futures = set()

    def capabilities(self):
        ready = isinstance(self.dispatcher, ProjectModelDispatcher)
        return {
            "model": self.dispatcher.context.policy.model if ready else MODEL,
            "model_available": ready,
            "model_unavailable_reason": None if ready else "model_service_not_configured",
            "automatic_generation": False,
            "automatic_retries": False,
            "max_output_tokens": 6000,
        }

    def _public(self, row):
        return {
            **row,
            **self.capabilities(),
            "job_id": row.get("legacy_job", {}).get("job_id", row.get("revision_id")),
            "current_input_version": row["current_input"]["identity"],
            "current_prompt_version": row["current_input"]["prompt_version"],
        }

    def current(self, unit):
        row = self.files.current(unit)
        if row["status"] == "not_generated" and not row["current_input"]["available"]:
            raise ValueError("report unit unavailable")
        return self._public(row)

    def get(self, unit, rid):
        return self._public(self.files.get(unit, rid))

    def history(self, unit, *, offset=0, limit=20):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid report page")
        rows = self.files.history(unit)
        page = rows[offset : offset + limit]
        return {
            "revisions": [self._public(row) for row in page],
            "has_more": offset + len(page) < len(rows),
            "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
        }

    def _lease(self, unit, rid):
        # Validate complete owner/hash before constructing the worker path.
        self.files.get(unit, rid)
        path = self.files._safe(self.files._directory(unit) / "revisions" / rid / "worker.lock")
        stream = path.open("a+b")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        return stream

    @staticmethod
    def _release(lease):
        if lease is not None:
            fcntl.flock(lease, fcntl.LOCK_UN)
            lease.close()

    def request(self, unit, *, request_id, expected_input_sha256, retry_of=None):
        if self._stop.is_set() or not self.capabilities()["model_available"]:
            raise ReportRevisionError("model_unavailable")
        row = self.files.request(
            unit, request_id=request_id, expected_input_sha256=expected_input_sha256, retry_of=retry_of
        )
        if row["storage_status"] == "queued":
            rid = row["revision_id"]
            lease = self._lease(unit, rid)
            if lease is not None:

                def run():
                    try:
                        self._run(unit, rid)
                    finally:
                        self._release(lease)

                try:
                    future = self.pool.submit(run)
                except RuntimeError:
                    self._release(lease)
                    raise ReportRevisionError("model_unavailable") from None
                with self._guard:
                    self._futures.add(future)

                def finished(f):
                    if f.cancelled():
                        try:
                            self.files.cancel(unit, rid)
                        finally:
                            self._release(lease)
                    with self._guard:
                        self._futures.discard(f)

                future.add_done_callback(finished)
        return self._public(row)

    def _receipt(self, receipt):
        if not receipt or not receipt.get("call_id") or not isinstance(self.dispatcher, ProjectModelDispatcher):
            return receipt
        context = self.dispatcher.context
        if receipt.get("project_id") != context.project_id:
            raise StateConflict("report request owner mismatch")
        try:
            row = context.service.read_call(context.project_id, receipt["call_id"])
        except ModelRequestError as error:
            if error.code == "call_not_found":
                return {**receipt, "state": "unknown", "missing_legacy_request": True}
            raise
        if row is None:
            return {**receipt, "state": "unknown", "missing_legacy_request": True}
        if row["request_sha256"] != receipt["request_sha256"] or row["model"] != receipt.get(
            "model", context.policy.model
        ):
            raise StateConflict("report request binding mismatch")
        return {**receipt, **row, "ledger_state": row["state"]}

    def _audit(self, unit, rid):
        with self.files._locked(unit):
            state = self.files._state(unit)
            _, _, row, response, _ = self.files._revision(unit, rid, state)
            if response is None:
                row["budget_receipt"] = self._receipt(row["budget_receipt"])
                self.files._write(unit, state)
            return deepcopy(row)

    def _running(self, unit, rid):
        if self._stop.is_set():
            return False
        # The held worker lease owns this immutable revision. Poll only the
        # hash-checked lifecycle index; source input is checked at dispatch/CAS.
        with self.files.source_lock(unit):
            row = self.files._state(unit)["revisions"].get(rid)
            return row is not None and row["status"] == "running"

    async def _dispatch(self, unit, rid, request):
        def correlate(event):
            if not self._running(unit, rid):
                raise StateConflict("report execution cancelled")
            receipt = {**event, "project_id": self.dispatcher.context.project_id, "state": "dispatch_pending"}
            if not self.files.mark_dispatched(unit, rid, budget_receipt=receipt):
                raise StateConflict("report dispatch already claimed")

        task = asyncio.create_task(self.dispatcher.dispatch(request, on_dispatch=correlate))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.02)
                if not task.done() and not self._running(unit, rid):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise ReportRevisionError("cancelled")
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _output(payload):
        # Keep report text/parsed output only; reasoning and provider request are never stored.
        texts = []
        output = payload.get("output")
        if type(output) is not list:
            return {"invalid_provider_output": True}
        for item in output:
            if type(item) is not dict:
                return {"invalid_provider_output": True}
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") not in (None, "completed")
            ):
                return {"invalid_provider_output": True}
            if type(item.get("content")) is not list:
                return {"invalid_provider_output": True}
            for part in item["content"]:
                if type(part) is not dict or part.get("type") != "output_text" or type(part.get("text")) is not str:
                    return {"invalid_provider_output": True}
                texts.append(part["text"])
        raw = "\n".join(texts)
        try:
            return strict_json(raw)
        except (ValueError, TypeError, RecursionError):
            return {"invalid_output_text": raw}

    def _run(self, unit, rid):
        try:
            if not self.files.claim(unit, rid):
                return
            with self.files.source_lock(unit):
                _, snapshot, _, _, _ = self.files._revision(unit, rid, self.files._state(unit))
            request = {
                "model": self.dispatcher.context.policy.model,
                "instructions": INSTRUCTIONS
                + (
                    "\n"
                    "Do not merge shared process parameters into a complete ordered preparation procedure; "
                    "only preserve explicitly recorded applicability and order."
                ),
                "input": encode_report_input(snapshot),
                "max_output_tokens": 6000,
                "reasoning": {"effort": "medium"},
                "store": False,
                "stream": False,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "study_report",
                        "schema": report_schema(snapshot),
                        "strict": False,
                    }
                },
            }
            payload = asyncio.run(self._dispatch(unit, rid, request))
            receipt = self._receipt(self.files.get(unit, rid)["budget_receipt"])
            self.files.save_response(unit, rid, self._output(payload), budget_receipt=receipt)
            if self._running(unit, rid):
                try:
                    self.files.finalize(unit, rid)
                except ValueError:
                    self.files.fail(unit, rid, code="invalid_report")
        except OSError:
            # Durable response/report may exist. Leave it recoverable; never resend.
            return
        except Exception as error:
            try:
                row = self._audit(unit, rid)
                if row["status"] == "running":
                    if self._stop.is_set() or isinstance(error, ReportRevisionError) and error.code == "cancelled":
                        self.files.cancel(unit, rid)
                    else:
                        self.files.fail(unit, rid, code="provider_failed", budget_receipt=row["budget_receipt"])
            except (OSError, ValueError, StateConflict):
                pass

    def cancel(self, unit, rid):
        self.files.cancel(unit, rid)
        return self.get(unit, rid)

    def persist(self, unit, rid):
        lease = self._lease(unit, rid)
        if lease is None:
            return self.get(unit, rid)
        try:
            return self._public(self.files.finalize(unit, rid))
        finally:
            self._release(lease)

    def recover(self):
        root = self.files._safe(self.files.root / "units")
        completed = 0
        if not root.exists():
            return completed
        for directory in root.iterdir():
            try:
                envelope = self.files._read(directory / "index.json")
                unit = envelope["data"]["unit"]
                if directory != self.files._directory(unit):
                    raise StateConflict("report unit directory differs")
                state = self.files._state(unit)
                for rid in state["order"]:
                    lease = self._lease(unit, rid)
                    if lease is None:
                        continue
                    try:
                        row = self._audit(unit, rid)
                        if row["status"] == "queued":
                            self.files.cancel(unit, rid)
                            continue
                        receipt = row["budget_receipt"] or {}
                        if (
                            row["status"] == "running"
                            and not row.get("response_sha256")
                            and receipt.get("state") == "sent"
                        ):
                            self.dispatcher.context.service.mark_call_unknown(
                                receipt["project_id"], receipt["call_id"], "process_recovery"
                            )
                            self._audit(unit, rid)
                        for action in self.files.recover(unit, revision_ids=[rid]):
                            try:
                                self.files.finalize(unit, action["revision_id"])
                                completed += 1
                            except ValueError:
                                self.files.fail(unit, rid, code="invalid_report")
                    finally:
                        self._release(lease)
            except (OSError, ValueError, KeyError, StateConflict, ModelRequestError):
                continue
        return completed

    def close(self):
        self._stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
