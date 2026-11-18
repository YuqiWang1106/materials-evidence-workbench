"""Verify, publish and reconcile an ingestion task under its source lock."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import TYPE_CHECKING

from .contract import strict_equal, validate_package
from .core import atomic_json, fingerprint
from .core.errors import raise_state_conflict

if TYPE_CHECKING:
    from kb_ingest.ingest.service import IngestionService


def _require_archive(service: IngestionService) -> None:
    if service.archive is None:
        message = "Archive is not configured"
        raise RuntimeError(message)


def reconcile(service: IngestionService, task_id: str) -> dict:
    _require_archive(service)
    store = service.store
    initial = store.get_task(task_id)
    with store.lock("source:" + initial["source_id"]):
        task = store.get_task(task_id)
        if task["state"] == "saved":
            return task
        if task["state"] not in ("publishing", "write_unknown"):
            raise_state_conflict("该任务没有待核对的写入")
        path = store.pending_path(task_id, "publication-package.json")
        package = json.loads(path.read_text()) if path.is_file() else service.candidate(task_id)
        receipt = service.archive.reconcile_pending(package, store.pending_path(task_id, "publication"))
        store.begin_publish(task_id)
        if receipt is None:
            store.publication_failed(task_id, "已确认旧写入未提交，可重新确认读法", unknown=False)
        else:
            store.finish_publish(task_id, receipt, make_current=False)
        return store.get_task(task_id)


def _candidate(service: IngestionService, task_id: str, digest: str | None, review_digest: str | None) -> dict:
    package = service.candidate(task_id)
    if package["header"].get("candidate_review_protocol") == "human_reviewed_sol_candidate_v1":
        if not review_digest:
            raise_state_conflict("review version is required; reload before publishing")
        package = service.document_publication_package(
            task_id, expected_candidate_sha256=digest, expected_review_sha256=review_digest
        )
    validate_package(package)
    return package


def _check_table(service: IngestionService, task: dict, package: dict, digest: str | None) -> None:
    service._require_table_confirmation(task)
    from .ingest.tables.row_measurements import check_package, is_profile

    draft = service.store.get_table_draft(task["source_id"])
    if is_profile(draft["recipe"]):
        check_package(service, draft, package)
    if not digest:
        raise_state_conflict("请先预览并确认当前结果 / preview required")
    if digest != fingerprint(package):
        raise_state_conflict("预览结果版本已变化，请重新核对")
    from .ingest.tables.table_quality import verify_original_tables

    verify_original_tables(package, {aid: service.store.asset_path(aid) for aid in task["asset_ids"]})


def published_task_id(state: dict | None) -> str | None:
    # Absence is distinct from a source with no published task. Preserve the original lookup failure.
    if state is None:
        message = "'NoneType' object is not subscriptable"
        raise TypeError(message)
    return state["published_task_id"]


def _make_current(service: IngestionService, task: dict, task_id: str, package: dict) -> bool:
    if package["header"].get("processing_summary", {}).get("outcome") != "partial":
        return True
    previous = published_task_id(service.store.source_state(task["source_id"]))
    if not previous or previous == task_id:
        return True
    header = service.candidate(previous)["header"]
    return header.get("processing_summary", header.get("automation", {})).get("outcome") == "partial"


def _write(service: IngestionService, task_id: str, task: dict, package: dict, make_current: bool) -> None:
    store = service.store
    store.begin_publish(task_id)
    atomic_json(store.pending_path(task_id, "publication-package.json"), package)
    bindings = task["options"].get("asset_bindings", {a["asset_id"]: a["asset_id"] for a in package["assets"]})
    paths = {aid: store.asset_path(registered) for aid, registered in bindings.items()}
    try:
        receipt = service.archive.publish(package, paths, store.pending_path(task_id, "publication"))
        if not strict_equal(package, service.archive.read(receipt)):
            raise_state_conflict("Archive readback differs from the submitted scientific package")
        store.finish_publish(task_id, receipt, make_current=make_current)
    except Exception as exc:
        # An unreadable classification cannot authorize a resend.
        unknown = True
        with suppress(Exception):
            state = service.archive.publication_state(store.pending_path(task_id, "publication"))
            unknown = state["outcome"] != "not_written"
        message = "publication requires reconciliation" if unknown else "publication was not accepted; retry available"
        store.publication_failed(task_id, f"{type(exc).__name__}: {message}", unknown=unknown)
        raise


def publish(service: IngestionService, task_id: str, digest: str | None, review_digest: str | None) -> dict:
    _require_archive(service)
    store = service.store
    initial = store.get_task(task_id)
    # Revision submission and publication use the same cross-process lock.
    with store.lock("source:" + initial["source_id"]):
        task = store.get_task(task_id)
        if task["state"] == "saved":
            return task
        package = _candidate(service, task_id, digest, review_digest)
        if service._retired_table(task):
            raise_state_conflict("旧表格候选只读，请重新确认读法 / retired")
        if task["source_type"] == "experimental":
            _check_table(service, task, package, digest)
        service._document_publish_check(task, package, digest)
        _write(service, task_id, task, package, _make_current(service, task, task_id, package))
    return store.get_task(task_id)
