"""Execute one claimed task with attempt-scoped outputs and fenced state changes."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from kb_ingest.contract import validate_package
from kb_ingest.core import StateConflict, atomic_json, fingerprint
from kb_ingest.core.errors import raise_state_conflict
from kb_ingest.core.workspace import graph_workspace
from kb_ingest.llm.errors import ModelOutcomeUnknownError, ModelRequestBindingError
from kb_ingest.llm.logical_request import BudgetCreationPermit, BudgetOpenEvidence, BudgetOpenMode
from kb_ingest.observation import hooks as _observe
from kb_ingest.storage.task_files import checked_child

if TYPE_CHECKING:
    from kb_ingest.ingest.service import IngestionService


def _document_extractor(service: IngestionService, pinned: dict) -> Callable[[list[dict], str, dict, str], dict]:
    from kb_ingest.ingest.documents.document_direct_pipeline import extract_document_direct
    from kb_ingest.ingest.documents.extractors.documents_extract import extract_document

    if pinned.get("_document_pipeline_version") != "2":
        return extract_document
    mode = pinned.get("_document_extraction_mode", "legacy")
    if mode == "program-only-v1":
        from kb_ingest.document_program_pipeline import extract_program_document

        if service.project_budget_context is not None:
            return partial(extract_program_document, project_budget_context=service.project_budget_context)
        return extract_program_document
    if mode == "direct-six-v1":
        if service.document_options.get("document_input_strategy") == "mineru_text_full_pdf":
            from kb_ingest.document_sol_selftest import extract_sol_full_pdf

            return extract_sol_full_pdf
        return extract_document_direct
    if mode == "entity-graph-v1":
        from kb_ingest.extraction.entity_pipeline import extract_entity_document

        return extract_entity_document
    from kb_ingest.ingest.documents.document_pipeline import extract_document_v2

    return extract_document_v2


def _check_protocol(pinned: dict, package: dict) -> None:
    if pinned.get("_document_pipeline_version") != "2":
        return
    header = package["header"]
    if (
        header.get("document_pipeline_version") != "2"
        or header.get("document_extraction_mode", "legacy") != pinned.get("_document_extraction_mode", "legacy")
        or (
            pinned.get("_document_extraction_mode") == "program-only-v1"
            and header.get("review_policy") != "program_only_v1"
        )
        or (
            pinned.get("_candidate_review_protocol")
            and header.get("candidate_review_protocol") != pinned["_candidate_review_protocol"]
        )
    ):
        raise_state_conflict("Document output protocol or extraction mode differs from the pinned task")


@dataclass(frozen=True, slots=True)
class _Attempt:
    service: IngestionService
    task_id: str
    task: dict
    token: str
    directory: Path

    def timeline(self) -> Path | None:
        """Where the entity pipeline keeps this attempt's timeline; None for every other kind of task."""
        task = self.task
        entity = task["options"].get("_document_extraction_mode") == "entity-graph-v1"
        if task["source_type"] not in ("paper", "patent") or not entity:
            return None
        run = f"{self.task_id}:attempt{task['attempt']}"
        path: Path = self.service._document_workspace(self.task_id, "artifacts", fingerprint(run))
        return path

    def cancelled(self) -> bool:
        current = self.service.store.get_task(self.task_id, include_unavailable=True)
        if current["attempt_token"] != self.token or current["state"] != "extracting" or self.service._stop.is_set():
            return True
        # While this process renews the lease, a wall-clock jump such as a wake from sleep is not expiry.
        own = self.service.scheduler_health.own_lease_expired(self.task_id, self.token)
        return (current["lease_until"] or 0) <= time.time() if own is None else own

    def _configure_document(self, options: dict) -> None:
        service, task_id = self.service, self.task_id
        pinned = self.task["options"]
        options.update(service.document_options, _model_workspace=service.resources.model_workspace)
        options["document_extraction_mode"] = pinned.get("_document_extraction_mode", "legacy")
        options["source_type"] = self.task["source_type"]
        v2 = pinned.get("_document_pipeline_version") == "2"
        options["work_dir"] = str(
            service._document_workspace(task_id) if v2 else checked_child(self.directory, "document")
        )
        options["workspace_id"] = graph_workspace(service)
        if v2:
            options["cost_run_id"] = task_id
            options.setdefault("reader_cache_dir", str(service.store.root / "cache" / "document-reading"))
        options["cancel_check"] = self.cancelled
        if options.get("model_config"):
            config = options["model_config"] = dict(options["model_config"])
            config["ledger_path"] = checked_child(self.directory, "model-attempts.jsonl")
            config.setdefault("cache_dir", service.store.root / "cache" / "model")
            config["budget"] = {
                **config.get("budget", {}),
                "budget_state_path": service.store.pending_path(task_id, "model-budget.json"),
            }
            config["cancel_check"] = self.cancelled
        if pinned.get("_full_reparse_run"):
            options["reader_cache_dir"] = str(service.store.pending_path(task_id, "fresh-reading"))
            options["_full_reparse_run"] = pinned["_full_reparse_run"]
            if options.get("model_config"):
                options["model_config"]["cache_dir"] = service.store.pending_path(task_id, "fresh-model")
                options["model_config"]["cache_namespace"] = pinned["_full_reparse_run"]
        if pinned.get("_document_extraction_mode") == "entity-graph-v1":
            options["entity_store"] = service.store
            self._configure_model_owner(options)
        elif v2 and options["document_extraction_mode"] == "legacy":
            self._configure_model_owner(options)

    def _configure_model_owner(self, options: dict) -> None:
        path = self.service._document_workspace(self.task_id, "model-budget.json")
        mode = BudgetOpenMode.OPEN_EXISTING
        if not path.exists() and self.task["attempt"] == 1 and not self.task["options"].get("_document_review_parent"):
            mode = BudgetOpenMode.INIT_NEW
        evidence = BudgetOpenEvidence(mode, path, self.service.store.workspace_id)
        options["_model_budget_evidence"] = evidence
        if mode == BudgetOpenMode.INIT_NEW:
            originals = self._asset_digests()
            options["_model_budget_permit"] = BudgetCreationPermit(
                evidence, lambda: self._model_owner_is_current(originals)
            )

    def _model_owner_is_current(self, originals: tuple[tuple[str, str], ...]) -> bool:
        store = self.service.store
        current = store.get_task(self.task_id, include_unavailable=True)
        source = store.source_state(self.task["source_id"])
        if not source or self.cancelled():
            return False
        expected = (self.task_id, self.task["source_id"], self.task["generation"], self.token)
        actual = tuple(current[key] for key in ("task_id", "source_id", "generation", "attempt_token"))
        if actual != expected or current["attempt"] != 1 or current["options"].get("_document_review_parent"):
            return False
        if source["active_task_id"] != self.task_id or source["generation"] != current["generation"]:
            return False
        if current["asset_ids"] != self.task["asset_ids"]:
            return False
        return all(store.get_asset(asset_id)["sha256"] == digest for asset_id, digest in originals)

    def _asset_digests(self) -> tuple[tuple[str, str], ...]:
        # Registered asset hashes are immutable; _originals has already checked the actual file bytes.
        return tuple(
            (asset_id, self.service.store.get_asset(asset_id)["sha256"]) for asset_id in self.task["asset_ids"]
        )

    def _originals(self) -> list[dict]:
        task, service = self.task, self.service
        service._require_document_configuration(task)
        if task["source_type"] == "experimental":
            service._require_table_confirmation(task)
        pinned = task["options"].get("_pipeline_fingerprint")
        expected = (
            service.document_fingerprint()
            if task["options"].get("_document_pipeline_version") == "2"
            else service.pipeline_fingerprint
        )
        if pinned and pinned != expected:
            raise_state_conflict("Pipeline configuration changed; submit a new source revision")
        assets = [
            {**service.store.get_asset(aid), "local_path": str(service.store.asset_path(aid)), "role": "original"}
            for aid in task["asset_ids"]
        ]
        for asset in assets:
            if sha256(Path(asset["local_path"]).read_bytes()).hexdigest() != asset["sha256"]:
                raise_state_conflict("Original file integrity failure")
        return assets

    def _extract(self, assets: list[dict]) -> dict:
        task, service = self.task, self.service
        options = deepcopy(task["options"])
        if task["source_type"] == "experimental":
            from kb_ingest.ingest.tables.extractors.experimental import extract_experimental

            package = extract_experimental(
                assets, task["source_id"], options, f"{self.task_id}:attempt{task['attempt']}"
            )
            service._annotate_confirmed_package(task, package)
            return package
        if task["source_type"] in ("paper", "patent"):
            extract = _document_extractor(service, task["options"])
            self._configure_document(options)
            package = extract(assets, task["source_id"], options, f"{self.task_id}:attempt{task['attempt']}")
            _check_protocol(task["options"], package)
            return package
        if task["source_type"] == "prepared":
            return json.loads(service.store.pending_path(self.task_id, "prepared_input.json").read_text())
        message = "Unsupported extraction type"
        raise ValueError(message)

    def run(self) -> None:
        stages = _observe.tracker(self.timeline())
        stages.enter("task_preflight")
        assets = self._originals()
        store = self.service.store
        store.transition(self.task_id, self.token, "extracting", "读取原件并关联对象与证据")
        package = self._extract(assets)
        # Late responses remain audit-only until the fenced candidate save.
        atomic_json(
            checked_child(self.directory, "extraction-output.json"),
            {"status": "unvalidated_attempt_output", "package": package},
        )
        store.transition(self.task_id, self.token, "validating", "检查记录结构、对象归属与证据引用")
        stages.enter("candidate_validation")
        check = validate_package(package)
        if package["header"].get("table_quality_version"):
            from kb_ingest.ingest.tables.table_quality import verify_original_tables

            paths = {a["asset_id"]: a["local_path"] for a in assets}
            if self.task["source_type"] == "prepared":
                paths = {
                    original: paths[registered]
                    for original, registered in self.task["options"]["asset_bindings"].items()
                }
            check["original_table_check"] = verify_original_tables(package, paths)
        atomic_json(checked_child(self.directory, "validation.json"), check)
        stages.enter("candidate_save")
        store.save_candidate(self.task_id, self.token, package)
        stages.finish()
        if self.task["source_type"] in ("paper", "patent"):
            from kb_ingest.study_units import StudyUnitService

            StudyUnitService(self.service).sync_task(self.task_id)


def _conflict_code(error: StateConflict) -> str:
    """Which integrity check stopped the attempt, as a fixed timeline code."""
    text = str(error).lower()
    if "configuration changed" in text or "extraction mode" in text:
        return "pipeline_configuration_changed"
    return "original_integrity_failure" if "integrity" in text else "task_processing_error"


def process_owned(service: IngestionService, task_id: str) -> dict:
    store = service.store
    if service._retired_table(store.get_task(task_id)):
        raise_state_conflict("旧表格流程已停止，请重新确认读法 / retired")
    task = store.claim(task_id)
    if task is None:
        return store.get_task(task_id)
    token = task["attempt_token"]
    directory = store.pending_path(task_id, "attempts", token)
    directory.mkdir(parents=True, exist_ok=True)
    attempt = _Attempt(service, task_id, task, token, directory)
    timeline = attempt.timeline()
    try:
        with service._heartbeat(task):
            attempt.run()
    except StateConflict as exc:
        _observe.tracker(timeline).fail(_conflict_code(exc))
        # Only the current attempt may mark a failed integrity check.
        with suppress(StateConflict):
            store.transition(task_id, token, "failed", "原件或版本完整性检查失败", str(exc))
        raise
    except Exception as exc:
        _observe.tracker(timeline).fail(exc)
        # Provider exceptions may contain secrets. Preserve only their safe classification.
        error = f"{type(exc).__name__}: extraction failed; inspect scoped validation/model report"
        progress = "处理失败，可查看问题并重试"
        if isinstance(exc, (ModelOutcomeUnknownError, ModelRequestBindingError)):
            error = str(exc)
        if isinstance(exc, ModelOutcomeUnknownError):
            progress = "模型请求结果尚未确认，原件与请求记录已保留；不会自动重发"
        with suppress(StateConflict):
            store.transition(task_id, token, "failed", progress, error)
        raise
    finally:
        _observe.release(timeline)
    return store.get_task(task_id)
