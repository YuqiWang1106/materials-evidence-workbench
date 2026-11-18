"""Application boundary shared by HTTP, CLI, experimental and document inputs."""

from __future__ import annotations

import json
import threading
import time as time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager as contextmanager
from copy import deepcopy
from hashlib import sha256 as sha256
from pathlib import Path
from typing import TYPE_CHECKING as _TYPE_CHECKING

from kb_ingest import pipeline_versions
from kb_ingest import service_publication as _service_publication
from kb_ingest import service_scheduler as _service_scheduler
from kb_ingest import service_search as _service_search
from kb_ingest.contract import strict_equal as strict_equal
from kb_ingest.contract import validate_package
from kb_ingest.core import NotRetryableError, StateConflict, atomic_json, fingerprint
from kb_ingest.core.workspace import Workspace
from kb_ingest.core.workspace import graph_workspace as graph_workspace
from kb_ingest.document_report_jobs import DocumentReportJobsMixin
from kb_ingest.document_workflow import DocumentWorkflowMixin
from kb_ingest.document_workflow import document_query_metadata as document_query_metadata
from kb_ingest.ingest import service_attempts as _service_attempts
from kb_ingest.ingest import service_error_messages as _service_errors
from kb_ingest.ingest.resources import IngestionResources
from kb_ingest.ingest.tables.raw_table_workflow import RawTableWorkflowMixin
from kb_ingest.ingest.tables.region_workflow import RegionWorkflowMixin
from kb_ingest.ingest.tables.table_workflow import TableWorkflowMixin
from kb_ingest.llm.errors import is_model_outcome_unknown
from kb_ingest.query import query_package as query_package
from kb_ingest.standard_model_batches import StandardModelBatchesMixin
from kb_ingest.standard_model_jobs import StandardModelJobsMixin
from kb_ingest.standard_workflow import StandardWorkflowMixin
from kb_ingest.storage.task_files import checked_child as checked_child
from kb_ingest.table_chat_workflow import TableChatWorkflowMixin

if _TYPE_CHECKING:
    from kb_ingest.document_report_revision_service import DocumentReportRevisionService
    from kb_ingest.llm.model_requests import ModelRequestContext
    from kb_ingest.research_summary_service import ResearchWikiSummaryService


class RetiredTableWorkflow(StateConflict):
    """A former public table entrypoint is unavailable in the delivered service."""


_DEFAULT_ARCHIVE = object()


class IngestionService(
    TableChatWorkflowMixin,
    RawTableWorkflowMixin,
    TableWorkflowMixin,
    RegionWorkflowMixin,
    StandardWorkflowMixin,
    StandardModelJobsMixin,
    StandardModelBatchesMixin,
    DocumentWorkflowMixin,
    DocumentReportJobsMixin,
):
    def __init__(
        self,
        root: Path | Workspace,
        archive=_DEFAULT_ARCHIVE,
        document_options=None,
        concurrency: int = 2,
        table_options=None,
        table_workflow_mode: str = "compatibility",
    ):
        if table_workflow_mode not in ("latest-only", "compatibility"):
            raise ValueError(_service_errors.TABLE_WORKFLOW_MODE)
        self.table_workflow_mode = table_workflow_mode
        if "model_client" in (document_options or {}):
            raise ValueError(_service_errors.INJECTED_MODEL_CLIENT)
        # Host-only ephemeral capability; never persisted in task options.
        self.project_budget_context: ModelRequestContext | None = None
        self.document_options = deepcopy(document_options or {})
        self.table_options = deepcopy(table_options or {})
        if "model_client" in self.table_options:
            raise ValueError(_service_errors.INJECTED_TABLE_CLIENT)
        self.document_mode()  # Reject bad server routing before creating a store.
        strategy = self.document_options.get("document_input_strategy", "ir_batches")
        if (
            strategy not in ("ir_batches", "mineru_text_full_pdf", "grouped_source_pdf")
            or (strategy == "mineru_text_full_pdf" and self.document_mode() != "direct-six-v1")
            or (strategy == "grouped_source_pdf" and self.document_mode() != "program-only-v1")
            or (self.document_mode() == "program-only-v1" and strategy != "grouped_source_pdf")
        ):
            raise ValueError(_service_errors.INPUT_STRATEGY)
        self.resources = IngestionResources(root)
        self.store = self.resources.store
        self.workspace_id = self.study_unit_workspace_id = self.store.workspace_id
        from kb_ingest.archive import LocalArchiveStore

        self.archive = LocalArchiveStore(self.store.root / "archive") if archive is _DEFAULT_ARCHIVE else archive
        self.pipeline_fingerprint = pipeline_versions.PIPELINE_VERSION
        self.pool = ThreadPoolExecutor(max_workers=max(1, min(concurrency, 8)), thread_name_prefix="kb-ingestion")
        self._running: set[str] = set()
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self.scheduler_health = _service_scheduler.SchedulerHealth()
        self._legacy_table_cache: dict[tuple[str, str, int, int], bool] = {}
        self.document_report_revision_service: DocumentReportRevisionService | None = None
        self.research_wiki_summary_services: list[ResearchWikiSummaryService] = []

    def knowledge_record_port(self, registry, workspace_id, collection_id, snapshot_store, **options):
        """Explicit server-owned read adapter; no default real-data/cache root."""
        from kb_ingest.knowledge_source import IngestionPublishedRecordAdapter

        return IngestionPublishedRecordAdapter(self, registry, workspace_id, collection_id, snapshot_store, **options)

    def knowledge_table_artifact_port(self, registry, workspace_id, collection_id, corpus_manifest):
        from kb_ingest.knowledge_table_artifact import KnowledgeTableArtifactAdapter

        def turn_scope(chat_id, turn_id):
            files = self._table_chats()
            with self.store.lock("table-chat:" + chat_id):
                chat = files.read_chat(chat_id)
                if turn_id not in chat["turn_ids"]:
                    raise KeyError("Turn not found")
                turn, snapshot = files.read_turn(chat_id, turn_id)
                artifacts = self._raw_artifacts()
                sources = []
                for tid in snapshot["table_version_ids"]:
                    raw = artifacts.load("raw", tid)["payload"]
                    if fingerprint(raw) != snapshot["table_hashes"][tid]:
                        raise ValueError("Selected raw table integrity failure")
                    sources.append(raw["source_id"])
                return {
                    "table_version_ids": list(snapshot["table_version_ids"]),
                    "source_ids": sorted(set(sources)),
                    "status": turn["status"],
                }

        return KnowledgeTableArtifactAdapter(self, registry, workspace_id, collection_id, turn_scope, corpus_manifest)

    def capabilities(self):
        from kb_ingest.ingest.tables.table_reading import LIMITS

        table_status = self._table_model_status()
        return {
            "archive_configured": self.archive is not None,
            "native_document_readiness": deepcopy(self.document_options.get("native_document_readiness")),
            "formats": {"experimental": [".csv", ".xlsx"], "paper": [".pdf"], "patent": [".pdf"]},
            "table_layouts": ["records", "objects", "key_value", "curves", "channels"],
            "reading_plan_version": "2",
            "table_plan_layouts": ["row", "column", "matrix", "block", "curve"],
            "model_configured": bool(self.document_options),
            "table_mode": "confirmed",
            "table_model_calls": 0,
            "table_model_calls_scope": "range_confirmation",
            "table_workflow_mode": self.table_workflow_mode,
            "table_analysis_workflow": "basic-table-analysis-v1",
            "table_chat_workflow": "table-chat-v1",
            "table_chat_available": table_status["ready"],
            "table_chat_readiness": table_status,
            "table_chat_model": self.table_options.get("model") or "gpt-6-sol",
            "table_entry_profiles": (
                ["table_regions_v1"]
                if self.table_workflow_mode == "latest-only"
                else ["table_regions_v1", "row_measurements_v1", "legacy_reading"]
            ),
            "standard_table_model": self.table_options.get("model") or "gpt-6-sol",
            "standard_table_model_available": table_status["ready"],
            "document_pipeline": "2" if self.document_enabled() else "legacy",
            "document_extraction_mode": self.document_mode(),
            "document_semantic_model": self.document_options.get("model_config", {}).get("model", "gpt-6-luna")
            if self.document_enabled()
            else None,
            "document_input_strategy": self.document_options.get("document_input_strategy", "ir_batches")
            if self.document_enabled()
            else None,
            "table_limits": dict(LIMITS),
            "scientific_validation": "source-grounded candidate; not independent laboratory validation",
            "source_types": ["paper", "patent", "experimental", "prepared"],
        }

    def _require_native_document_readiness(self):
        readiness = self.document_options.get("native_document_readiness")
        if readiness is not None and readiness.get("status") != "ready":
            raise StateConflict("native_documents_not_ready")

    def require_legacy_table_workflow(self):
        if self.table_workflow_mode == "latest-only":
            raise RetiredTableWorkflow("旧表格入口已退役；请从表格范围核对进入标准审核 / retired")

    def start_scheduler(self):
        if self._scheduler:
            return

        self._scheduler = threading.Thread(
            target=_service_scheduler.poll, args=(self,), daemon=True, name="kb-recovery"
        )
        self._scheduler.start()

    def health(self) -> dict[str, object]:
        return _service_scheduler.health_report(self)

    def source_management(self, source_id):
        from kb_ingest.source_lifecycle import SourceLifecycle

        return SourceLifecycle(self).management(source_id)

    def source_command(self, source_id, payload, kind):
        from kb_ingest.source_lifecycle import SourceLifecycle

        return SourceLifecycle(self).command(source_id, payload, kind=kind)

    def source_operation(self, operation_id):
        from kb_ingest.source_lifecycle import SourceLifecycle

        return SourceLifecycle(self).operation(operation_id)

    def recover_source_operations(self):
        from kb_ingest.source_lifecycle import SourceLifecycle

        SourceLifecycle(self).recover()

    def document_task_options(self):
        options = {"_pipeline_fingerprint": self.pipeline_fingerprint}
        if self.document_enabled():
            options.update(
                _document_pipeline_version="2",
                _document_extraction_mode=self.document_mode(),
                _document_input_strategy=self.document_options.get("document_input_strategy", "ir_batches"),
                _pipeline_fingerprint=self.document_fingerprint(),
            )
            if self.document_mode() == "program-only-v1":
                options.update(
                    _review_policy="program_only_v1", _candidate_review_protocol="human_reviewed_sol_candidate_v1"
                )
        return options

    def submit_source(self, source_id, source_type, asset_ids, options=None, expected_revision=None):
        if source_type in ("paper", "patent"):
            self._require_native_document_readiness()
        if set(options or {}) & {
            "document_extraction_mode",
            "_document_extraction_mode",
            "_document_pipeline_version",
            "review_policy",
            "_review_policy",
            "_candidate_review_protocol",
        }:
            raise ValueError("Document extraction mode is server configuration only")
        options = {**(options or {}), "_pipeline_fingerprint": self.pipeline_fingerprint}
        if source_type in ("paper", "patent") and self.document_enabled():
            if self.document_options.get("document_input_strategy") == "mineru_text_full_pdf":
                if (
                    len(asset_ids) != 1
                    or self.store.get_asset(asset_ids[0])["original_name"].lower().endswith(".pdf") is False
                ):
                    raise ValueError("Full-PDF self-test accepts exactly one PDF original / 仅支持一份 PDF")
            options["_document_pipeline_version"] = "2"
            options["_document_extraction_mode"] = self.document_mode()
            if self.document_mode() == "program-only-v1":
                options["_review_policy"] = "program_only_v1"
                options["_candidate_review_protocol"] = "human_reviewed_sol_candidate_v1"
            options["_document_input_strategy"] = self.document_options.get("document_input_strategy", "ir_batches")
            options["_pipeline_fingerprint"] = self.document_fingerprint()
        if source_type == "experimental":
            self.require_legacy_table_workflow()
            raise StateConflict("请先确认表格读法，再创建提取任务 / confirmation required")
        return self.store.submit(source_id, source_type, asset_ids, options, expected_revision)

    @staticmethod
    def _is_table_package(package):
        header = package.get("header", {})
        return bool(
            header.get("reading_confirmation")
            or header.get("table_quality_version")
            or any(s.get("source_type") == "experimental" for s in package.get("sources", []))
        )

    def _retired_table(self, task):
        if task["source_type"] == "experimental":
            if self.table_workflow_mode == "latest-only":
                if not task.get("confirmation_version_id"):
                    return True
                try:
                    version = self.store.get_confirmation(task["confirmation_version_id"])
                except KeyError:
                    return True
                if not version["recipe"].get("standard_review"):
                    return True
            return not task.get("confirmation_version_id") or (
                task["state"] != "saved" and task["options"].get("_pipeline_fingerprint") != self.pipeline_fingerprint
            )
        if task["source_type"] != "prepared":
            return False
        # Older importers persisted table packages as `prepared`. They must not
        # bypass confirmation via restart, retry, or direct publication.
        name = "candidate.json" if task.get("package_sha256") else "prepared_input.json"
        path = self.store.pending_path(task["task_id"], name)
        try:
            stat = path.stat()
            key = (task["task_id"], str(path), stat.st_mtime_ns, stat.st_size)
            if key not in self._legacy_table_cache:
                self._legacy_table_cache[key] = self._is_table_package(json.loads(path.read_text()))
            return self._legacy_table_cache[key]
        except (OSError, ValueError):
            return True  # Unreadable historical input is never resumed implicitly.

    def retry(self, task_id):
        task = self.store.get_task(task_id)
        if self._retired_table(task):
            raise StateConflict(_service_errors.RETIRED_TABLE)
        if task["state"] == "failed" and is_model_outcome_unknown(task["error"]):
            # Preserve the unresolved attempt; a retry must not send it again.
            return task
        if task["state"] in ("needs_review", "publishing", "write_unknown", "saved"):
            raise NotRetryableError  # answering success here read as "retried" although nothing ran
        self._require_document_configuration(task)
        if task["source_type"] == "experimental":
            self._require_table_confirmation(task)
        self.store.retry(task_id)
        return self.store.get_task(task_id)

    def task_status(self, task_id):
        task = self.store.get_task(task_id)
        task["read_only"] = self._retired_table(task)
        task["title"] = task["options"].get("title") or task["source_id"]
        if task["source_type"] in ("paper", "patent"):
            self._document_task_progress(task_id, task)
        # Historical summaries remain readable; they can never restart a model.
        path = self.store.pending_path(task_id, "automation-summary.json")
        try:
            data = json.loads(path.read_text())
            if data.get("package_sha256") == task["package_sha256"]:
                task["automation"] = data["automation"]
        except (OSError, ValueError, KeyError):
            pass
        if task["source_type"] in ("paper", "patent"):
            if task["options"].get("_document_pipeline_version") == "2":
                report = self.document_artifact(task_id, "quality")
                task["document_quality"] = report.get("data", report)
            else:
                task["document_quality"] = {"status": "legacy_not_rechecked"}
        return task

    def _document_task_progress(self, task_id: str, task: dict) -> None:
        # Resolve legacy tasks through their existing registered originals.
        task["original_names"] = []
        for asset_id in task["asset_ids"]:
            try:
                name = self.store.get_asset(asset_id)["original_name"]
            except KeyError:
                continue
            if name and name not in task["original_names"]:
                task["original_names"].append(name)
        title = task["options"].get("title") or ""
        task["document_title"] = title if title != task["source_id"] else ""
        try:
            package = self.candidate(task_id)
        except (StateConflict, OSError, ValueError):
            package = {}
        for assertion in package.get("assertions", []):
            if assertion.get("field_id") == "source.title":
                title = assertion.get("value", {}).get("raw_value")
                if isinstance(title, str) and title.strip() and title != task["source_id"]:
                    task["document_title"] = title.strip()
                    break
        from kb_ingest.document_progress import document_progress, document_title

        task["title"] = document_title(self.store, task)
        task["processing"] = document_progress(self, task)

    def close(self):
        self._stop.set()
        if self._scheduler:
            self._scheduler.join()
        study_jobs = getattr(self, "study_wiki_jobs", None)
        if study_jobs is not None:
            study_jobs.close()
        for summary in getattr(self, "research_wiki_summary_services", []):
            summary.close()
        if self.document_report_revision_service is not None:
            self.document_report_revision_service.close()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def start(self, task_id: str):
        _service_scheduler.start_worker(self, task_id)

    def _heartbeat(self, task):
        return _service_scheduler.heartbeat(self, task)

    def process(self, task_id: str) -> dict:
        with self.store.lock("task-worker:" + task_id):
            return self._process_owned(task_id)

    def _process_owned(self, task_id: str) -> dict:
        return _service_attempts.process_owned(self, task_id)

    def import_prepared(self, package: dict, bindings: dict[str, str], expected_revision=None) -> dict:
        validate_package(package)
        if package["header"].get("document_pipeline_version") == "2":
            raise StateConflict(_service_errors.PREPARED_V2)
        if set(bindings) != {a["asset_id"] for a in package["assets"]}:
            raise ValueError(_service_errors.REQUIRED_ASSET_BINDING)
        for asset in package["assets"]:
            registered = self.store.get_asset(bindings[asset["asset_id"]])
            if registered["sha256"] != asset["sha256"]:
                raise ValueError(_service_errors.ASSET_BYTES_MISMATCH)
        if package["header"].get("table_quality_version"):
            from kb_ingest.ingest.tables.table_quality import verify_original_tables

            verify_original_tables(
                package, {aid: self.store.asset_path(registered) for aid, registered in bindings.items()}
            )
        if self._is_table_package(package):
            raise StateConflict(_service_errors.PREPARED_TABLE)
        self.store.reserve_revision(package)
        # Prepared JSON is durable staging; the ledger holds only its hash/bindings.
        options = {"prepared_sha256": fingerprint(package), "asset_bindings": bindings}
        task = self.store.submit(
            package["header"]["source_id"], "prepared", sorted(set(bindings.values())), options, expected_revision
        )
        if task["state"] != "queued":
            return task
        with self.store.lock("prepare:" + task["task_id"]):
            atomic_json(self.store.pending_path(task["task_id"], "prepared_input.json"), package)
            return self.process(task["task_id"])

    def candidate(self, task_id: str) -> dict:
        if self.store.get_task(task_id)["state"] in ("queued", "reading", "extracting", "validating"):
            raise StateConflict("Current extraction is not ready; previous attempts remain in audit history")
        path = self.store.pending_path(task_id, "candidate.json")
        if not path.is_file():
            raise StateConflict("No candidate is ready")
        package = json.loads(path.read_text())
        if fingerprint(package) != self.store.get_task(task_id)["package_sha256"]:
            raise StateConflict("Candidate package integrity failure")
        return package

    def records(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        # A paper confirmed into the entity knowledge base is saved there, not in Archive: its record is the candidate.
        if task["state"] == "saved" and "entity_graph" in (task.get("receipt") or {}):
            return self.candidate(task_id)
        if task["state"] == "saved":
            if self.archive is None:
                raise RuntimeError("Archive is not configured; formal record cannot be read")
            return self.archive.read(task["receipt"])
        return self.candidate(task_id)

    def reconcile_publication(self, task_id):
        """Read an existing archive commit without authorizing another write."""
        return _service_publication.reconcile(self, task_id)

    def publish(self, task_id: str, *, expected_package_sha256=None, expected_review_sha256=None) -> dict:
        return _service_publication.publish(self, task_id, expected_package_sha256, expected_review_sha256)

    def search(
        self,
        source_id: str | None = None,
        filters: list[dict] | None = None,
        include_history=False,
        query_scope="source_reviewed",
    ):
        scope = _service_search.SearchScope(source_id, include_history, query_scope)
        return _service_search.search(self, scope, filters)

    def search_detailed(self, source_id=None, filters=None, include_history=False, query_scope="source_reviewed"):
        """Same published-version boundary, with explicit typed comparison gaps."""
        scope = _service_search.SearchScope(source_id, include_history, query_scope)
        return _service_search.search_detailed(self, scope, filters)

    def evidence(self, task_id: str, evidence_id: str, *, location_only=False):
        package = self.records(task_id)
        item = next((e for e in package["evidence"] if e["evidence_id"] == evidence_id), None)
        if item is None:
            raise KeyError("Evidence not found")
        task = self.store.get_task(task_id)
        binding = task["options"].get("asset_bindings", {}).get(item["asset_ref"], item["asset_ref"])
        payload = {k: item.get(k) for k in ("evidence_id", "anchor", "content_kind")} if location_only else item
        if location_only:
            # A mutable draft cannot describe how an older result was read.
            # Return only the exact reader settings pinned at its confirmation.
            payload.update(reader_options=None, reader_settings_status="unavailable")
            if task.get("confirmation_version_id"):
                confirmation = self.store.get_confirmation(task["confirmation_version_id"])
                if binding in confirmation["asset_ids"]:
                    payload.update(
                        reader_options=deepcopy(confirmation["reader_options"].get(binding, {})),
                        reader_settings_status="confirmed",
                    )
            elif Path(self.store.get_asset(binding)["original_name"]).suffix.lower() == ".xlsx":
                payload.update(reader_options={}, reader_settings_status="format_default")
        return {
            **payload,
            "registered_asset_id": binding,
            "asset": self.store.get_asset(binding),
            "download_path": f"/api/assets/{binding}/download",
        }
