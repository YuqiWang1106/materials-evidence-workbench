"""Read-only projection of one attempt: its event journal, model request ledger, progress and result records.

Nothing here writes, starts a reader or calls a model. Missing, damaged or contradictory evidence is reported as
a partial history or an unknown result; it is never turned into a success.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from kb_ingest.core import clock

from .journal import available, journal_path, read_lines, session
from .schema import ERRORS, STAGES, Event, is_count, is_seconds, is_transport_status, safe_event, safe_id, safe_model

ACTIVE = frozenset({"queued", "reading", "extracting", "validating", "publishing"})
MAX_EVENTS = 5000
MAX_ATTEMPTS = 1000
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
LEDGER = "model-attempts.jsonl"
PROGRESS, TABLES, WINDOWS, GRAPH = "progress.json", "entity-tables.json", "entity-windows.json", "entity-graph.json"
# Files whose change makes a new snapshot.
RECORDS = (LEDGER, PROGRESS, TABLES, WINDOWS, GRAPH)
_UNITS = ("张表格", "个窗口", "份原件")
_TABLE_OUTCOMES = ("mapped", "partial", "failed", "empty")
# A result in one of these states has no terminal observation yet; a result record may still settle it.
_UNSETTLED = ("started", "unknown", "interrupted")
_PHASE_PREFIXES = ("table-", "visual-", "entities-", "attributes-")
_PHASES = ("synonyms", "identity", "resolve", "claims")
_STAGE_FIELDS = ("status", "elapsed_seconds", "completed", "total", "failed", "error_code")

type Results = dict[str, Event]


@dataclass
class _History:
    """Whether any evidence of this attempt was missing, damaged or contradictory."""

    incomplete: bool = False

    def gap(self, found: bool = True) -> None:
        self.incomplete = self.incomplete or found


def snapshot(
    root: Path, task: Mapping[str, object], *, now: float | None = None, forbidden: Iterable[object] = ()
) -> dict[str, object]:
    """The timeline of the task's current attempt, built from the files under `root`.

    Args:
        root: The attempt's artifact directory.
        task: The task record (state, attempt, generation, created_at, updated_at).
        now: Epoch seconds of the observation; the wall clock when omitted.
        forbidden: Secrets that must not appear in a shown model name.
    """
    root = Path(root)
    observed_at = clock.now().timestamp() if now is None else now
    secrets = tuple(forbidden)
    rows, cut = read_lines(journal_path(root))
    history = _History(cut or not available(root))
    events, last = _events(rows, secrets, history)
    attempts, ledger_incomplete = _model_attempts(root, events, task, secrets)
    progress = _progress(root, task, history)
    stages, stage_history, tables, windows = _fold(events)
    _recover_tables(root, tables, bool(rows), history)
    _recover_windows(root, windows, bool(rows), history)
    active = task.get("state") in ACTIVE
    if not active:
        _close_unfinished(stages, tables, windows)
    body: dict[str, object] = {
        "state": task.get("state"),
        "generation": task.get("generation"),
        "history": "partial" if history.incomplete else "recorded" if rows else "legacy_incomplete",
        "ledger_incomplete": ledger_incomplete,
        "progress": progress,
        "events": events,
        "model_attempts": attempts,
        "table_results": list(tables.values()),
        "window_results": list(windows.values()),
        "summary": {"tables": _status_counts(tables), "windows": _status_counts(windows)},
    }
    revision = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    return {
        "version": 1,
        "task_id": task["task_id"],
        "attempt": task["attempt"],
        "created_at": task.get("created_at"),
        "observed_at": observed_at,
        "elapsed_seconds": elapsed_seconds(task, observed_at),
        **body,
        "stages": list(stages.values()),
        "stage_history": stage_history,
        "embedding_index": {"status": "not_observed", "reason": "separate_background_index"},
        "cursor": f"{task['attempt']}:{last}:{revision}",
    }


def elapsed_seconds(task: Mapping[str, object], now: float) -> float | None:
    """Seconds since submission: still running for an active task, up to its last update otherwise."""
    created = task.get("created_at")
    ended = now if task.get("state") in ACTIVE else task.get("updated_at", now)
    return max(0.0, ended - created) if is_seconds(created) and is_seconds(ended) else None


def _status_counts(results: Results) -> dict[str, int]:
    return dict(Counter(str(item["status"]) for item in results.values()))


def _events(rows: list[dict[str, object]], forbidden: tuple[object, ...], history: _History) -> tuple[list[Event], int]:
    """(events in order, last sequence number). A dropped, repeated or foreign-writer row is a gap."""
    events: list[Event] = []
    previous = 0
    for row in rows:
        event, sequence = safe_event(row), row.get("seq")
        if event is None or type(sequence) is not int or sequence <= previous:
            history.gap()
            continue
        history.gap(sequence != previous + 1 or row.get("_session") != session())
        if "requested_model" in event and safe_model(event["requested_model"], forbidden) is None:
            event.pop("requested_model")
            event.pop("returned_model", None)
        event["seq"] = previous = sequence
        events.append(event)
    history.gap(len(events) > MAX_EVENTS)
    return events[-MAX_EVENTS:], previous


def _model_attempts(
    root: Path, events: list[Event], task: Mapping[str, object], forbidden: tuple[object, ...]
) -> tuple[list[dict[str, object]], bool]:
    """(the latest state of every transport attempt in the request ledger, whether the ledger was cut or damaged)."""
    ledger, incomplete = read_lines(root / LEDGER)
    latest: dict[str, dict[str, object]] = {}
    for row in ledger:
        attempt_id = safe_id(row.get("attempt_id"))
        if attempt_id:
            latest.setdefault(attempt_id, {}).update(row)
    if len(latest) > MAX_ATTEMPTS:
        latest = dict(list(latest.items())[-MAX_ATTEMPTS:])
        incomplete = True
    links = {
        event["attempt_id"]: event for event in events if event["kind"] == "model_attempt" and "attempt_id" in event
    }
    active = task.get("state") in ACTIVE
    return [_attempt(key, row, links.get(key, {}), active, forbidden) for key, row in latest.items()], incomplete


def _attempt(
    attempt_id: str, row: Mapping[str, object], link: Event, active: bool, forbidden: tuple[object, ...]
) -> dict[str, object]:
    raw = row.get("status")
    status = raw if is_transport_status(raw) else "unknown"
    if status == "started" and not active:
        status = "unknown"  # nothing is running any more, and the ledger never recorded how the request ended
    number, started, wall = row.get("attempt"), row.get("started_at_epoch"), row.get("wall_seconds")
    requested = safe_model(row.get("requested_model"), forbidden)
    item: dict[str, object] = {
        "attempt_id": attempt_id,
        "phase": _phase(row.get("phase")),
        "attempt": number if type(number) is int else None,
        "status": status,
        "activity": "unconfirmed" if status == "started" else None,
        "will_retry": row.get("will_retry") is True,
        "possible_duplicate_charge": row.get("possible_duplicate_charge") is True,
        "requested_model": requested,
        # A provider-controlled name is untrusted: only the name that was requested is repeated.
        "returned_model": requested if requested and row.get("returned_model") == requested else None,
        "started_at": started if is_seconds(started) else None,
        "wall_seconds": wall if is_seconds(wall) else None,
    }
    if status.startswith("http_") or status in ERRORS:
        item["error_code"] = status
    item.update({key: link[key] for key in ("logical_id", "parent_kind", "parent_id") if key in link})
    return item


def _phase(value: object) -> str:
    phase = safe_id(value)
    return phase if phase and (phase.startswith(_PHASE_PREFIXES) or phase in _PHASES) else "other"


def _progress(root: Path, task: Mapping[str, object], history: _History) -> dict[str, object]:
    """The current-stage counter of this attempt, with every unexpected value left out."""
    progress, damaged = read_record(root / PROGRESS)
    history.gap(damaged)
    if progress.get("processing_run_id") != f"{task['task_id']}:attempt{task['attempt']}":
        return {}
    checks: dict[str, Callable[[object], bool]] = {
        "stage": lambda value: value in STAGES,
        "unit": lambda value: value in _UNITS,
        "completed": is_count,
        "total": is_count,
        "failed": is_count,
        "updated_at": is_seconds,
    }
    return {key: progress[key] for key, valid in checks.items() if key in progress and valid(progress[key])}


def _fold(events: list[Event]) -> tuple[Results, list[Event], Results, Results]:
    """(latest state per stage, every stage run in order, latest state per table, latest state per window)."""
    stages: Results = {}
    stage_history: list[Event] = []
    tables: Results = {}
    windows: Results = {}
    for event in events:
        if event["kind"] == "stage":
            _fold_stage(event, stages, stage_history)
        elif event["kind"] == "table" and "item_id" in event:
            tables[str(event["item_id"])] = dict(event)
        elif event["kind"] == "window" and "item_id" in event:
            windows[str(event["item_id"])] = dict(event)
    return stages, stage_history, tables, windows


def _fold_stage(event: Event, stages: Results, stage_history: list[Event]) -> None:
    name, started = str(event["stage"]), event["status"] == "started"
    state = stages.get(name)
    if state is None or (started and state["status"] != "started"):
        # A stage that starts again is a new run: its times never mix with the previous run's.
        state = {"stage": name, "started_at": event.get("at") if started else None, "status": "unknown"}
        stages[name] = state
        stage_history.append(state)
    state.update({key: event[key] for key in _STAGE_FIELDS if key in event})
    if not started:
        state["ended_at"] = event.get("at")


def _close_unfinished(*collections: Results) -> None:
    """The task is no longer running: whatever still says started never reported how it ended."""
    for results in collections:
        for item in results.values():
            if item.get("status") == "started":
                item["status"] = "unknown"


def _conflict(item_id: str, observed: object, recorded: object) -> Event:
    """Contradictory terminal evidence is not shown as a definite result."""
    return {
        "item_id": item_id,
        "status": "unknown",
        "source": "artifact_conflict",
        "observed_status": observed,
        "artifact_status": recorded,
        "error_code": "observation_evidence_conflict",
        "elapsed_seconds": None,
    }


def _settle(results: Results, item_id: str, outcome: str, recorded: bool, history: _History) -> bool:
    """Whether `outcome` from a result record should be added for `item_id`; reports conflicts and lost events."""
    observed = results.get(item_id)
    status = None if observed is None else observed.get("status")
    if observed is not None and status not in _UNSETTLED:
        # An observed not_processed is more specific than the table log's generic failed outcome.
        if status != outcome and not (status == "not_processed" and outcome == "failed"):
            history.gap()
            results[item_id] = _conflict(item_id, status, outcome)
        return False
    history.gap(observed is not None or recorded)  # the terminal observation is missing
    return True


def _recover_tables(root: Path, tables: Results, recorded: bool, history: _History) -> None:
    """Restore table outcomes whose terminal event was lost from the table log written when mapping ended."""
    record, damaged = read_record(root / TABLES)
    logged = record.get("tables", [])
    history.gap(damaged or not isinstance(logged, list))
    for row in logged if isinstance(logged, list) else []:
        item_id = safe_id(row.get("table_id")) if isinstance(row, dict) else None
        outcome = row.get("outcome") if isinstance(row, dict) else None
        if not item_id or outcome not in _TABLE_OUTCOMES:
            history.gap()
        elif _settle(tables, item_id, str(outcome), recorded, history):
            counts = {key: row[key] for key in ("requests", "unread_cells", "attributes") if is_count(row.get(key))}
            tables[item_id] = {
                "item_id": item_id,
                "status": outcome,
                "elapsed_seconds": None,
                "source": "artifact_recovered",
                **counts,
            }


def _recover_windows(root: Path, windows: Results, recorded: bool, history: _History) -> None:
    """Restore failed and unprocessed windows from the graph summary written when extraction ended.

    Only the summary's explicit lists are used: a window missing from them is never taken as extracted. A listed
    window must also belong to this attempt, by an event or by the window manifest.
    """
    manifest, manifest_damaged = read_record(root / WINDOWS)
    graph, graph_damaged = read_record(root / GRAPH)
    history.gap(manifest_damaged or graph_damaged)
    known = set(windows) | _listed(manifest.get("windows", []), "window_id", history)
    summary = graph.get("summary", {})
    history.gap(not isinstance(summary, dict))
    lists = summary if isinstance(summary, dict) else {}
    failed = _listed(lists.get("failed_windows"), None, history)
    skipped = _listed(lists.get("not_processed_windows"), None, history)
    history.gap(bool((failed | skipped) - known))
    for item_id in sorted(failed & skipped & known):
        history.gap()
        windows[item_id] = _conflict(item_id, windows.get(item_id, {}).get("status", "unknown"), "unknown")
    outcomes = [(item_id, "failed") for item_id in sorted(failed - skipped)]
    outcomes += [(item_id, "not_processed") for item_id in sorted(skipped - failed)]
    for item_id, outcome in outcomes:
        if item_id in known and _settle(windows, item_id, outcome, recorded, history):
            windows[item_id] = {
                "item_id": item_id,
                "status": outcome,
                "elapsed_seconds": None,
                "source": "artifact_recovered",
            }


def _listed(value: object, key: str | None, history: _History) -> set[str]:
    """The safe identifiers in a list of identifiers (or of rows holding one under `key`); None is an empty list."""
    if value is None:
        return set()
    if not isinstance(value, list):
        history.gap()
        return set()
    found = {safe_id(item if key is None else item.get(key) if isinstance(item, dict) else None) for item in value}
    history.gap(None in found)
    return {item for item in found if item is not None}


def read_record(path: Path, limit: int = MAX_ARTIFACT_BYTES) -> tuple[dict[str, object], bool]:
    """(the JSON object in a regular file, damaged) without following a final link.

    A record that does not exist yet is normal before its phase ends. One that exists but is a link, oversized,
    unreadable or not a JSON object is damaged.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}, False
    except OSError:
        return {}, True
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        return {}, True
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, stat.S_ISREG(opened.st_mode)) != (info.st_dev, info.st_ino, True):
                return {}, True
            data = stream.read(limit + 1)
        value = json.loads(data) if len(data) <= limit else None
    except (OSError, ValueError):
        return {}, True
    return (value, False) if isinstance(value, dict) else ({}, True)
