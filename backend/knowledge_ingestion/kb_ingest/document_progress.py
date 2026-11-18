"""Read-only, bounded progress projection; never starts a parser or model."""

import json
import math
import time
from collections import Counter

from .core import StateConflict

ACTIVE = {"queued", "reading", "extracting", "validating", "publishing"}


def _originals(store, task):
    originals = []
    for asset_id in task.get("asset_ids", []):
        try:
            name = store.get_asset(asset_id)["original_name"]
        except KeyError:
            continue  # Legacy asset metadata may be missing; keep other files visible.
        originals.append({"asset_id": asset_id, "name": name})
    return originals


def document_title(store, task):
    assets = [a["name"] for a in _originals(store, task)]
    names = [name for name in assets if name.lower().endswith(".pdf")] or assets
    if len(names) > 1:
        return f"资料组（{len(names)} 份文献）"
    return task.get("options", {}).get("title") or (names[0] if names else task.get("title") or task["source_id"])


def _json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        return {}
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}


def extracted_message(graph: dict) -> str:
    """The task's progress line once entities are extracted. It says so when windows were left out, so an
    interrupted or partly failed extraction does not read like a complete one."""
    merged = graph.get("merged") or {}
    counts = merged.get("entities") or {}
    text = (
        f"已提取：配方 {counts.get('formulation', 0)} 个，实体共 {sum(counts.values())} 个，"
        f"属性 {merged.get('attributes', 0)} 段"
    )
    missing = len(graph.get("failed_windows") or []) + len(graph.get("not_processed_windows") or [])
    if missing:
        return f"{text}；本次抽取不完整，{missing} 个窗口未完成，保存前请先核对缺口"
    return f"{text}；请核对后写入知识库"


def _retry_attempts(latest: dict) -> list[dict]:
    # Keep a first uncertain generation visible without exposing incomplete token-count rows.
    fields = ("attempt", "attempt_id", "phase", "status", "possible_duplicate_charge")
    rows = [row for row in latest.values() if all(key in row for key in (*fields, "request_hash"))]
    retried = {
        row["request_hash"]
        for row in rows
        if row.get("attempt", 0) > 1 or row.get("status") in {"transport_unknown", "transport_write_failed"}
    }
    return [{key: row[key] for key in fields} for row in rows if row["request_hash"] in retried]


def document_progress(service, task):
    now = time.time()
    elapsed = max(0, int((now if task["state"] in ACTIVE else task.get("updated_at", now)) - task["created_at"]))
    result = {
        "elapsed_seconds": elapsed,
        "elapsed_scope": "since_submission",
        "determinate": False,
        "progress_scope": "current_stage",
        "documents": [a for a in _originals(service.store, task) if a["name"].lower().endswith(".pdf")],
        "stage": task["state"],
    }
    try:
        root = service._document_artifact_root(task)
        progress = _json(root / "progress.json")
        run = f"{task['task_id']}:attempt{task['attempt']}"
        if progress.get("processing_run_id", run) != run:
            progress = {}
        result.update(
            {k: progress[k] for k in ("stage", "document_index", "documents_total", "asset_id") if k in progress}
        )
        completed, total = progress.get("completed"), progress.get("total")
        if (
            task["state"] in ACTIVE
            and progress.get("processing_run_id") == run
            and type(total) is int
            and total > 0
            and type(completed) is int
            and 0 <= completed <= total
        ):
            result.update(
                determinate=True,
                completed=completed,
                total=total,
                percent=int(completed * 100 / total),
                unit=progress.get("unit", "项"),
                failed=max(0, progress.get("failed", 0)) if type(progress.get("failed", 0)) is int else 0,
            )
        if task["state"] not in ACTIVE:
            result["stage"] = task["state"]
        timing = _json(root / "timing.json")
        if (
            isinstance(timing.get("reading_seconds"), (int, float))
            and math.isfinite(timing["reading_seconds"])
            and timing["reading_seconds"] >= 0
        ):
            result["reading_seconds"] = round(timing["reading_seconds"], 1)
        summary = _json(root / "entity-graph.json").get("summary") or {}
        if (
            isinstance(summary, dict)
            and isinstance(summary.get("windows"), int)
            and isinstance(summary.get("failed_windows"), list)
        ):
            result["extraction_coverage"] = {
                "windows": summary["windows"],
                "failed_windows": len(summary["failed_windows"]),
            }
            if isinstance(summary.get("skipped_pages"), list):
                result["extraction_coverage"]["skipped_pages"] = len(summary["skipped_pages"])
        performance = _json(root / "performance.json")
        seconds = performance.get("extraction_review_seconds")
        if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0:
            result["extraction_review_seconds"] = round(seconds, 1)
        path = root / "model-attempts.jsonl"
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 4 * 1024 * 1024:
            latest = {}
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # A live writer may not have finished its last line.
                if isinstance(row, dict):
                    latest[row.get("attempt_id", row.get("phase"))] = row
            counts = Counter(row.get("status") for row in latest.values())
            attempts = _retry_attempts(latest)
            if attempts:
                result["model_attempts"] = attempts
            result["model_batches"] = {
                k: counts[k] for k in ("started", "completed", "time_budget_exhausted", "failed")
            }
            result["model_batches"]["failed"] += sum(v for k, v in counts.items() if str(k).startswith("http_"))
            if latest and task["state"] == "extracting" and not progress.get("stage"):
                phase = str(next(reversed(latest.values())).get("phase", ""))
                result["stage"] = (
                    "table_mapping"
                    if phase.startswith("table-")
                    else "visual_reading"
                    if phase.startswith("visual-regions")
                    else "entity_extraction"
                )
    except (OSError, ValueError, KeyError, TypeError, StateConflict):
        pass  # Partial artifacts never prevent polling or expose their contents.
    return result
