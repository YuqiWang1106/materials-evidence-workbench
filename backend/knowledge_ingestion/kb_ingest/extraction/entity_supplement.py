"""Explicit, bounded repair of failed windows. Original task artifacts are immutable.

Each subwindow has its own ledger. Never redispatch an unknown call. Saved candidates can be applied idempotently.
"""

import json
import re
import time
from collections.abc import Iterable, Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar

from kb_ingest.core import StateConflict, atomic_json, fingerprint
from kb_ingest.core.contract_entity import normalize_name
from kb_ingest.extraction.entity_review import load, task_root

from .entity_windows import structure_units

VERSION = "failed-window-supplement-v1"
FILES = (
    "entity-graph.json",
    "entity-blocks.json",
    "entity-windows.json",
    "entity-reviews.json",
    "entity-graph-resolved.json",
)


class SupplementIdError(ValueError):
    """A supplement run or request id is not 32 lowercase hexadecimal characters."""

    def __init__(self) -> None:
        super().__init__("invalid supplement id")


class SupplementBlockedError(StateConflict):
    """The task cannot be supplemented now; each subclass fixes its public 409 reason."""

    reason: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.reason)


class TaskNotSavedError(SupplementBlockedError):
    reason = "supplement requires a saved entity task"


class TaskNotPublishedError(SupplementBlockedError):
    reason = "only the current published task can be supplemented"


class ReviewedDocumentError(SupplementBlockedError):
    reason = "reviewed documents require manual supplement review; automatic merge disabled"


class MissingArtifactsError(SupplementBlockedError):
    reason = "missing extraction artifacts"


def split_failed_windows(windows, blocks, selected, max_chars=2400):
    by_id = {w["window_id"]: w for w in windows}
    if not selected or len(set(selected)) != len(selected) or any(w not in by_id for w in selected):
        raise ValueError("unknown or duplicate failed window")
    output = []
    for wid in selected:
        source = [{**blocks[key], "block_id": key} for key in by_id[wid]["block_ids"]]
        packed, current, size = [], [], 0
        for unit in structure_units(source):  # Keep tables, captions and headings together; no quote is rewritten.
            length = sum(len(b.get("text") or "") for b in unit)
            if current and size + length > max_chars:
                packed.append(current)
                current, size = [], 0
            current.extend(unit)
            size += length
        if current:
            packed.append(current)
        for i, group in enumerate(packed):
            output.append(
                {
                    "unit_id": f"{wid}-{i + 1}",
                    "parent_window": wid,
                    "block_ids": [b["block_id"] for b in group],
                    "pages": sorted({b["page"] for b in group if b.get("page")}),
                    "characters": sum(len(b.get("text") or "") for b in group),
                }
            )
    return output


def hydrate_known_entities(checked, original, raw=None):
    # merge_windows normally obtains these entities from earlier successful windows.
    # A supplement does not rerun those windows, so use their immutable mentions only.
    raw_by_id = {e["local_id"]: e for e in (raw or {}).get("entities", [])}
    for entity in checked["entities"]:
        if not entity.get("known"):
            continue
        name = raw_by_id.get(entity["local_id"], entity)["name"]
        matches = [
            old
            for old in original.get("entities", [])
            if old["type"] == entity["type"] and normalize_name(old["name"]) == normalize_name(name)
        ]
        if len(matches) != 1:
            raise ValueError("ambiguous_known_entity")
        entity.update(
            name=matches[0]["name"],
            aliases=list(matches[0].get("aliases", [])),
            code=matches[0].get("code") or matches[0]["name"],
            mentions=deepcopy(matches[0].get("mentions", [])),
        )
        entity.pop("known", None)
    return checked


def restore_formulation_codes(graph, original):
    """Names are display text; an existing formulation's code is its persistent identity."""
    lookup = {}
    for entity in original.get("entities", []):
        if entity["type"] != "formulation":
            continue
        code = entity.get("code") or entity["name"]
        for name in [entity["name"], code, *entity.get("aliases", [])]:
            lookup.setdefault(normalize_name(name), set()).add(code)
    for entity in graph["entities"]:
        if entity["type"] != "formulation":
            continue
        if entity.get("code") in {
            e.get("code") or e["name"] for e in original.get("entities", []) if e["type"] == "formulation"
        }:
            continue
        exact = {
            e.get("code") or e["name"]
            for e in original.get("entities", [])
            if e["type"] == "formulation" and normalize_name(e["name"]) == normalize_name(entity["name"])
        }
        codes = exact or set().union(
            *(lookup.get(normalize_name(name), set()) for name in [entity["name"], *entity.get("aliases", [])])
        )
        if len(codes) > 1:
            raise ValueError("ambiguous_existing_formulation")
        if codes:
            entity["code"] = next(iter(codes))
    return graph


def published_snapshot(service: Any, task_id: str) -> tuple[dict[str, Any], Path, dict[str, Any], str]:
    """Bind a supplement to the current published, unreviewed extraction artifacts.

    Returns:
        The task, its immutable artifact root, the artifact payloads and their binding.
    """
    task, root = task_root(service, task_id)
    if task["state"] != "saved":
        raise TaskNotSavedError()
    if service.store.source_state(task["source_id"])["published_task_id"] != task_id:
        raise TaskNotPublishedError()
    draft, reviews, blocks = load(root)
    if reviews:
        raise ReviewedDocumentError()
    files = {name: json.loads((root / name).read_text()) if (root / name).exists() else None for name in FILES}
    if not draft or not blocks or files["entity-windows.json"] is None:
        raise MissingArtifactsError()
    binding = fingerprint({"task_id": task_id, "package": task["package_sha256"], "files": files})
    return task, root, files, binding


def _runs_dir(service, task_id):
    return service.store.pending_dir(task_id) / "entity-supplements"


def run_path(service: Any, task_id: str, run_id: str) -> Path:
    """The run's mirror folder; the id is validated before any path is built."""
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise SupplementIdError()
    return _runs_dir(service, task_id) / run_id


def read_run(path: Path, *, task: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The committed task receipt is authoritative; run.json is only a mirror of it."""
    committed = ((task or {}).get("receipt") or {}).get("entity_supplements") or {}
    if path.name in committed:
        return deepcopy(committed[path.name])
    return json.loads((path / "run.json").read_text())


def load_run(service: Any, task_id: str, run_id: str) -> dict[str, Any]:
    """Read one run against the task's current receipt without dispatching anything."""
    return read_run(run_path(service, task_id, run_id), task=service.store.get_task(task_id))


def read_runs(store, task):
    """The transaction receipt is authoritative; older applied sidecars remain readable."""
    root = store.pending_dir(task["task_id"]) / "entity-supplements"
    committed = (task.get("receipt") or {}).get("entity_supplements") or {}
    run_ids = {p.parent.name for p in root.glob("*/run.json")} | set(committed)
    if any(not re.fullmatch(r"[a-f0-9]{32}", rid) for rid in run_ids):
        raise SupplementIdError()
    return [(root / rid, read_run(root / rid, task=task)) for rid in sorted(run_ids)]


def save_run(path: Path, run: dict[str, Any]) -> None:
    """Stamp and mirror an uncommitted run. The applied mirror keeps its committed timestamp instead."""
    run["updated_at"] = time.time()
    atomic_json(path / "run.json", run)


def view(service, task_id):
    task, root = task_root(service, task_id)
    draft, reviews, blocks = load(root)
    runs = [run for _, run in read_runs(service.store, task)]
    summary = (draft or {}).get("summary", {})
    # Windows left unprocessed by the document's allowance are resumed like failed ones.
    failed = summary.get("failed_windows", []) + summary.get("not_processed_windows", [])
    binding, blocked = None, None
    try:
        _, _, _, binding = published_snapshot(service, task_id)
    except StateConflict as exc:
        blocked = str(exc)
    applied = recovered_windows(
        runs,
        (
            json.loads((root / "entity-windows.json").read_text()).get("windows", [])
            if (root / "entity-windows.json").exists()
            else []
        ),
    )
    return {
        "binding": binding,
        "failed_windows": failed,
        "remaining_windows": [w for w in failed if w not in applied],
        "blocked": blocked,
        "runs": runs,
        "scientific_validation": False,
    }


def create(service, task_id, *, request_id, expected_binding, window_ids, operator):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id) or not operator.strip():
        raise ValueError("invalid request identity")
    task, _ = task_root(service, task_id)
    with service.store.lock("source:" + task["source_id"]):
        task, root, files, binding = published_snapshot(service, task_id)
        path = run_path(service, task_id, request_id)
        intent = {"binding": expected_binding, "window_ids": window_ids, "operator": operator}
        if (path / "run.json").exists() or request_id in ((task.get("receipt") or {}).get("entity_supplements") or {}):
            run = read_run(path, task=task)
            if run["intent"] != intent:
                raise StateConflict("request id already used")
            return run
        if binding != expected_binding:
            raise StateConflict("source changed; refresh plan")
        prior = view(service, task_id)["runs"]
        if any(set(window_ids) & set(r["intent"]["window_ids"]) for r in prior):
            raise StateConflict(
                "selected windows already have a supplement; inspect or resume it instead of redispatching"
            )
        summary = files["entity-graph.json"]["summary"]
        failed = summary["failed_windows"] + summary.get("not_processed_windows", [])
        if not window_ids or any(w not in failed for w in window_ids):
            raise ValueError("only failed or unprocessed windows may be selected")
        units = split_failed_windows(files["entity-windows.json"]["windows"], files["entity-blocks.json"], window_ids)
        if len(units) > 100:
            raise ValueError("supplement exceeds 100 bounded subwindows")
        run = {
            "run_id": request_id,
            "intent": intent,
            "state": "queued",
            "units": [{**u, "state": "pending"} for u in units],
            "created_at": time.time(),
            "scientific_validation": False,
            "version": VERSION,
        }
        path.mkdir(parents=True, exist_ok=True)
        atomic_json(path / "source-snapshot.json", files)
        save_run(path, run)
        return run


def recovered_windows(runs, windows):
    covered = {}
    for run in runs:
        if run["state"] != "applied":
            continue
        for unit in run["units"]:
            if unit["state"] == "ready":
                covered.setdefault(unit["parent_window"], set()).update(unit["block_ids"])
    return {
        w["window_id"] for w in windows if w["block_ids"] and set(w["block_ids"]) <= covered.get(w["window_id"], set())
    }


def parent_binding(path: Path, run: Mapping[str, Any], unit_ids: Iterable[str]) -> str:
    # Bind the immutable parent plus every persisted response/execution artifact.
    artifacts = {}
    for uid in unit_ids:
        for file in sorted((path / uid).rglob("*")):
            if file.is_file() and file.suffix != ".lock":
                artifacts[str(file.relative_to(path))] = sha256(file.read_bytes()).hexdigest()
    return fingerprint({"run": run, "artifacts": artifacts})
