"""Durable database and file receipts accepted by the offline bundle format."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import PurePosixPath

from ..core.workspace import workspace_files
from ..storage import sqlite
from ..storage.engine import file_connection
from .database import _receipted_rows, _require_stopped_tasks
from .files import MigrationError, _canonical, _fail, _file_hash, _hash, _read, _safe


def _is_digest(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value)


def _require_supplement(valid):
    if not valid:
        _fail("Invalid entity-supplement publication receipt")


def _entity_supplements(receipt, task):
    """Recognize only committed, hash-bound supplement publication snapshots."""
    snapshots = receipt["entity_supplements"]
    _require_supplement(isinstance(snapshots, dict) and snapshots and isinstance(task, dict))
    _require_supplement(_is_digest(task.get("package_sha256")))
    for run_id, run in snapshots.items():
        _check_supplement_run(run_id, run, task)


def _check_supplement_run(run_id, run, task):
    _require_supplement(_is_digest(run_id, 32) and isinstance(run, dict))
    _require_supplement(
        run.get("run_id") == run_id
        and run.get("state") == "applied"
        and run.get("version") == "failed-window-supplement-v1"
        and run.get("scientific_validation") is False
    )
    intent, units, publication = run.get("intent"), run.get("units"), run.get("publication")
    _require_supplement(
        isinstance(intent, dict)
        and _is_digest(intent.get("binding"))
        and isinstance(intent.get("operator"), str)
        and bool(intent["operator"].strip())
    )
    windows = intent.get("window_ids")
    _require_supplement(
        isinstance(windows, list)
        and bool(windows)
        and all(isinstance(w, str) and bool(w) for w in windows)
        and len(set(windows)) == len(windows)
    )
    _require_supplement(isinstance(units, list) and bool(units) and isinstance(publication, dict))
    _require_supplement(
        publication.get("package_sha256") == task["package_sha256"]
        and _is_digest(publication.get("base_graph_sha256"))
        and _is_digest(publication.get("base_blocks_sha256"))
    )
    parts = _supplement_parts(units, windows)
    _require_supplement(parts and publication.get("parts") == parts)
    _require_supplement(
        run.get("candidate_sha256")
        == _hash(_canonical([(unit["unit_id"], unit.get("candidate_sha256")) for unit in units]))
    )


def _supplement_parts(units, windows):
    parts = []
    identities = []
    for unit in units:
        _require_supplement(isinstance(unit, dict))
        uid = unit.get("unit_id")
        _require_supplement(
            isinstance(uid, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", uid)
            and uid not in identities
            and unit.get("state") in ("ready", "failed", "unknown")
        )
        _require_supplement(
            unit.get("parent_window") in windows
            and isinstance(unit.get("block_ids"), list)
            and bool(unit["block_ids"])
            and all(isinstance(block, str) and bool(block) for block in unit["block_ids"])
        )
        identities.append(uid)
        if unit["state"] == "ready":
            _require_supplement(_is_digest(unit.get("candidate_sha256")))
            counts = unit.get("added")
            _require_supplement(
                isinstance(counts, dict)
                and set(counts) == {"entities", "attributes", "locations"}
                and all(type(count) is int and count >= 0 for count in counts.values())
            )
            parts.append({"unit_id": uid, "candidate_sha256": unit["candidate_sha256"]})
    return parts


def _supplement_artifacts(root, task, receipt):
    """Validate source and candidate bindings without relying on the optional run mirror."""
    try:
        task_id = task["task_id"]
        root = workspace_files(root, task["workspace_id"]) if "workspace_id" in task else root
        pending = _safe(root, "pending/" + task_id)
        package = _read(_safe(pending, "candidate.json"))
        header = package.get("header") or {}
        if (
            _hash(_canonical(package)) != task["package_sha256"]
            or header.get("source_id") != task["source_id"]
            or header.get("processing_run_id") != f"{task_id}:attempt{task['attempt']}"
        ):
            _fail("Supplement task source binding changed")
        work = _safe(pending, "document-v2")
        if header.get("document_artifact_layout") == "attempt-v1":
            work = _safe(work, "artifacts/" + _hash(_canonical(header["processing_run_id"])))
        elif header.get("document_artifact_layout") is not None:
            _fail("Unknown supplement artifact layout")
        files = _supplement_source_files(work)
        binding = _hash(_canonical({"task_id": task_id, "package": task["package_sha256"], "files": files}))
        for run_id, snapshot in receipt["entity_supplements"].items():
            folder = _safe(pending, "entity-supplements/" + run_id)
            publication = snapshot["publication"]
            if (
                _read(_safe(folder, "source-snapshot.json")) != files
                or snapshot["intent"]["binding"] != binding
                or publication["base_graph_sha256"] != _file_hash(_safe(work, "entity-graph-resolved.json"))
                or publication["base_blocks_sha256"] != _file_hash(_safe(work, "entity-blocks.json"))
            ):
                _fail("Supplement publication source binding changed")
            for part in publication["parts"]:
                candidate = _read(_safe(folder, part["unit_id"] + "/candidate.json"))
                if _hash(_canonical(candidate)) != part["candidate_sha256"]:
                    _fail("Supplement publication candidate changed")
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        message = "Invalid entity-supplement publication artifacts"
        raise MigrationError(message) from None


def _supplement_source_files(work):
    names = (
        "entity-graph.json",
        "entity-blocks.json",
        "entity-windows.json",
        "entity-reviews.json",
        "entity-graph-resolved.json",
    )
    return {name: _read(_safe(work, name)) if _safe(work, name).exists() else None for name in names}


def _receipt_variant(receipt, *, table=None, state=None, task=None):
    if not receipt:
        return "empty"
    if not isinstance(receipt, dict):
        _fail("Unknown durable receipt type")
    if table == "tasks" and set(receipt) in (
        {"entity_graph", "operator"},
        {"entity_graph", "operator", "entity_supplements"},
    ):
        if (
            state != "saved"
            or not isinstance(receipt["entity_graph"], dict)
            or not isinstance(receipt["operator"], str)
            or not receipt["operator"].strip()
        ):
            _fail("Invalid entity-confirmation receipt")
        if "entity_supplements" in receipt:
            _entity_supplements(receipt, task)
        return "entity"
    if table == "wiki_embedding_cache" and set(receipt) == {"receipt_id", "prompt_tokens", "total_tokens"}:
        if (
            not isinstance(receipt["receipt_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", receipt["receipt_id"])
            or type(receipt["prompt_tokens"]) is not int
            or type(receipt["total_tokens"]) is not int
            or not 0 <= receipt["prompt_tokens"] <= 8192
            or receipt["total_tokens"] != receipt["prompt_tokens"]
        ):
            _fail("Invalid embedding provider receipt")
        return "provider"
    if (
        isinstance(receipt.get("snapshot_id"), str)
        and isinstance(receipt.get("package_sha256"), str)
        and isinstance(receipt.get("assets"), dict)
        and isinstance(receipt.get("status"), str)
    ):
        return "archive"
    _fail("Unknown durable receipt type")


def _receipts(path, *, data_root=None) -> list[dict[str, object]]:
    result = []
    with file_connection(path, readonly=True) as db:
        for table in sqlite.table_names(db):
            columns = sqlite.column_names(db, table)
            if "receipt" in columns:
                selected = columns if table == "tasks" else ["receipt"]
                for row in _receipted_rows(db, table, selected):
                    task = dict(zip(selected, row, strict=False)) if table == "tasks" else None
                    try:
                        receipt = json.loads(row[selected.index("receipt")])
                    except ValueError:
                        message = "Invalid durable receipt JSON"
                        raise MigrationError(message) from None
                    variant = _receipt_variant(
                        receipt, table=table, state=task.get("state") if task else None, task=task
                    )
                    if variant == "archive":
                        result.append(receipt)
                    elif variant == "entity" and "entity_supplements" in receipt:
                        _supplement_artifacts(data_root or path.parent, task, receipt)
            if table == "tasks" and "state" in columns:
                _require_stopped_tasks(db, columns)
            if (
                table == "knowledge_publications"
                and "status" in columns
                and db.exec_driver_sql(
                    "SELECT 1 FROM knowledge_publications WHERE status IN ('publishing','write_unknown') LIMIT 1"
                ).fetchone()
            ):
                _fail("Source has unresolved Wiki publication writes")
    return result


def _file_receipt(relative, value, main_directory):
    if PurePosixPath(relative).name == "receipt.json":
        return ("receipt", value) if _receipt_variant(value) == "archive" else None
    prefix = re.escape(str(main_directory)) + r"(?:/workspaces/[0-9a-f]{64})?"
    pattern = prefix + r"/table_artifacts/(raw/rawtable|analyses/analysis)-[0-9a-f]{64}/publication\.json"
    if not re.fullmatch(pattern, relative):
        return None
    if not isinstance(value, dict) or value.get("status") not in {
        "local_saved",
        "oasis_verified",
        "archive_verified",
        "sync_failed",
        "write_unknown",
    }:
        _fail("Unknown table artifact publication envelope")
    if value["status"] == "write_unknown":
        _fail("Unresolved table artifact publication blocks migration")
    if value["status"] in {"oasis_verified", "archive_verified"}:
        if _receipt_variant(value.get("receipt")) != "archive":
            _fail("Verified table publication has no archive receipt")
        return "table-publication", value["receipt"]
    if value.get("receipt") is not None:
        _fail("Unverified table publication contains unexpected receipt")
    return None


def _migrated_file(value, reference, receipts):
    snapshot = reference["snapshot_id"]
    if snapshot not in receipts:
        _fail("File archive receipt was not imported")
    if reference["kind"] == "receipt":
        return receipts[snapshot]
    if reference["kind"] == "table-publication":
        result = deepcopy(value)
        result["receipt"] = receipts[snapshot]
        result["status"] = "archive_verified"
        return result
    _fail("Unknown migration file receipt variant")
