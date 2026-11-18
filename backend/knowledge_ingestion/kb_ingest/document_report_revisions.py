"""Report revisions lock the source before the unit and verify immutable inputs on read."""

import fcntl
import json
import re
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from .core import StateConflict, atomic_json, canonical, fingerprint
from .core.clock import now
from .document_report_generation import compile_report_output, generation_audit
from .document_report_time import report_generated_at, report_times
from .document_reports import MODEL, PROMPT_VERSION, encode_report_input, validate_report_output
from .storage.file_time import utc_text

VERSION = "document-report-revisions/v1"
STATUSES = {"queued", "running", "ready", "failed", "cancelled", "outcome_unknown"}
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_DEFAULT_CLOCK = time.time  # The existing constructor default can be distinguished from an injected clock.


class ReportRevisionStore:
    def __init__(self, report_root, *, snapshot_loader, source_lock, legacy_reader=None, clock=time.time, model=MODEL):
        self.legacy_root = Path(report_root)
        self.root = self.legacy_root / "revisions-v1"
        self.snapshot_loader = snapshot_loader
        self.source_lock = source_lock
        self.legacy_reader = legacy_reader
        self.clock = now if clock is _DEFAULT_CLOCK else clock
        self.model = model

    @staticmethod
    def _unit(unit):
        if (
            type(unit) is not dict
            or set(unit) != {"task_id", "candidate_id", "study_ref"}
            or any(type(v) is not str or not v or len(v) > 300 for v in unit.values())
        ):
            raise StateConflict("invalid report unit")
        return "drj-" + fingerprint([unit[k] for k in ("task_id", "candidate_id", "study_ref")])[:32]

    def _safe(self, path):
        path = Path(path)
        try:
            parts = path.relative_to(self.legacy_root).parts
        except ValueError:
            raise StateConflict("report path outside storage root") from None
        current = self.legacy_root
        if current.is_symlink():
            raise StateConflict("report storage symlink denied")
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise StateConflict("report storage symlink denied")
        return path

    def _directory(self, unit):
        return self._safe(self.root / "units" / self._unit(unit))

    @contextmanager
    def _locked(self, unit):
        self._unit(unit)
        with self.source_lock(unit):
            directory = self._directory(unit)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._safe(directory / "unit.lock").open("a+b") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _read(self, path):
        path = self._safe(path)
        try:
            if path.is_symlink() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact")
            return json.loads(path.read_text())
        except (OSError, ValueError, TypeError) as exc:
            raise StateConflict("report revision integrity check failed") from exc

    def _once(self, path, value):
        path = self._safe(path)
        if len(canonical(value).encode()) > MAX_ARTIFACT_BYTES:
            raise StateConflict("report artifact too large")
        if path.exists():
            if self._read(path) != value:
                raise StateConflict("immutable report artifact already differs")
            return
        atomic_json(path, value)

    def _state(self, unit):
        path = self._directory(unit) / "index.json"
        if not path.exists():
            return {
                "version": VERSION,
                "unit": deepcopy(unit),
                "generation": 0,
                "selected_revision_id": None,
                "current_revision_id": None,
                "order": [],
                "revisions": {},
                "requests": {},
            }
        envelope = self._read(path)
        try:
            state = envelope["data"]
            if (
                envelope["sha256"] != fingerprint(state)
                or state["version"] != VERSION
                or state["unit"] != unit
                or type(state["generation"]) is not int
                or state["generation"] < 0
                or set(state["order"]) != set(state["revisions"])
                or len(state["order"]) != len(state["revisions"])
                or any(r["status"] not in STATUSES for r in state["revisions"].values())
            ):
                raise ValueError("index")
            for field in ("selected_revision_id", "current_revision_id"):
                if state[field] is not None and state[field] not in state["revisions"]:
                    raise ValueError("pointer")
            return state
        except (KeyError, ValueError, TypeError) as exc:
            raise StateConflict("report revision index integrity failed") from exc

    def _write(self, unit, state):
        state["revisions"] = {rid: report_times(row, utc_text) for rid, row in state["revisions"].items()}
        envelope = {"data": state, "sha256": fingerprint(state)}
        if len(canonical(envelope).encode()) + 1 > MAX_ARTIFACT_BYTES:
            raise StateConflict("report revision index too large")
        atomic_json(self._safe(self._directory(unit) / "index.json"), envelope)

    def _snapshot(self, unit):
        self._unit(unit)
        try:
            snapshot = deepcopy(self.snapshot_loader(deepcopy(unit)))
            if (
                snapshot["scientific_validation"] is not False
                or snapshot["version"] != PROMPT_VERSION
                or any(snapshot["identity"][k] != v for k, v in unit.items())
            ):
                raise ValueError("input")
            for key in ("candidate_sha256", "review_sha256"):
                if not re.fullmatch("[a-f0-9]{64}", snapshot["identity"][key]):
                    raise ValueError("hash")
            encode_report_input(snapshot)
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise StateConflict("invalid current report input") from exc
        return snapshot

    def _read_current(self, unit):
        try:
            return self._snapshot(unit)
        except StateConflict:
            return None

    @staticmethod
    def _input_state(current):
        if current is None:
            return {
                "available": False,
                "sha256": None,
                "identity": None,
                "prompt_version": PROMPT_VERSION,
                "reason": "current_input_unavailable",
            }
        return {
            "available": True,
            "sha256": fingerprint(current),
            "identity": deepcopy(current["identity"]),
            "prompt_version": current["version"],
            "reason": None,
        }

    def _revision(self, unit, rid, state):
        if type(rid) is not str or not re.fullmatch("drr-[a-f0-9]{32}", rid) or rid not in state["revisions"]:
            raise StateConflict("revision does not belong to report unit")
        directory = self._directory(unit) / "revisions" / rid
        meta = self._read(directory / "revision.json")
        snapshot = self._read(directory / "input.json")
        row = state["revisions"][rid]
        try:
            if (
                fingerprint(meta) != row["revision_sha256"]
                or meta["revision_id"] != rid
                or meta["unit"] != unit
                or not isinstance(meta["model"], str)
                or not meta["model"]
                or meta["input_sha256"] != fingerprint(snapshot)
                or meta["input_version"] != snapshot["identity"]
                or meta["prompt_version"] != snapshot["version"]
                or snapshot["scientific_validation"] is not False
                or any(snapshot["identity"][k] != v for k, v in unit.items())
            ):
                raise ValueError("revision")
            response = None
            report = None
            if (directory / "response.json").exists():
                response = self._read(directory / "response.json")
                if (
                    response["revision_id"] != rid
                    or response["input_sha256"] != meta["input_sha256"]
                    or fingerprint(response["output"]) != response["output_sha256"]
                    or type(response["budget_receipt"]) is not dict
                    or not response["budget_receipt"]
                    or row.get("response_sha256") not in (None, fingerprint(response))
                ):
                    raise ValueError("response")
            if (directory / "report.json").exists():
                report = self._read(directory / "report.json")
                if (
                    report["revision_id"] != rid
                    or report["input_sha256"] != meta["input_sha256"]
                    or report["input_version"] != meta["input_version"]
                    or report["model"] != meta["model"]
                    or report["prompt_version"] != meta["prompt_version"]
                    or report["scientific_validation"] is not False
                    or report["status"] != "draft"
                    or response is None
                    or report["output"] != compile_report_output(response["output"], snapshot)
                    or report.get("generation") != generation_audit(response["output"], snapshot)
                    or report["budget_receipt"] != response["budget_receipt"]
                    or row.get("report_sha256") not in (None, fingerprint(report))
                ):
                    raise ValueError("report")
                validate_report_output(report["output"], snapshot)
            if row["status"] == "ready" and (report is None or row.get("report_sha256") is None):
                raise ValueError("missing report")
            if response is not None:
                # A durable response can precede an interrupted index replacement.
                # Reads derive its verified audit fields; mutations persist the repair.
                row.update(response_sha256=fingerprint(response), budget_receipt=deepcopy(response["budget_receipt"]))
            return meta, snapshot, row, response, report
        except (KeyError, ValueError, TypeError) as exc:
            raise StateConflict("report revision binding failed") from exc

    def _legacy(self, unit):
        path = self._safe(self.legacy_root / self._unit(unit) / "job.json")
        if not path.exists():
            return None
        if self.legacy_reader is None:
            raise StateConflict("legacy integrity reader unavailable")
        job, snapshot, report = self.legacy_reader(path)
        if any(snapshot["identity"][k] != v for k, v in unit.items()):
            raise StateConflict("legacy report owner mismatch")
        return {
            "revision_id": "legacy-" + job["job_id"],
            "legacy": True,
            "unit": deepcopy(unit),
            "input_sha256": job["input_sha256"],
            "input_version": job["input_version"],
            "prompt_version": job["prompt_version"],
            "status": job["status"],
            "report": report,
            "created_at": job["created_at"],
            "attempt": job["attempt"],
            "budget_receipt": None,
            "legacy_job": deepcopy(job),
            "previous_revision_id": None,
        }

    def _view(self, unit, rid, state, current):
        if type(rid) is str and rid.startswith("legacy-"):
            result = self._legacy(unit)
            if result is None or result["revision_id"] != rid:
                raise StateConflict("unknown legacy revision")
            selected = not state["order"]
        else:
            meta, _, row, _, report = self._revision(unit, rid, state)
            result = {**deepcopy(meta), **deepcopy(row), "legacy": False, "report": deepcopy(report)}
            selected = state["current_revision_id"] == rid
        rules = current is not None and result["prompt_version"] != current["version"]
        changed = current is None or result["input_sha256"] != fingerprint(current)
        historical = not selected
        stale = rules or changed or result["status"] == "stale" or (historical and result["report"] is not None)
        return {
            **report_times(result),
            "storage_status": result["status"],
            "status": "stale" if stale else result["status"],
            "stale": stale,
            "stale_reason": "current_input_unavailable"
            if current is None
            else "report_rules_changed"
            if rules
            else "input_changed"
            if changed
            else "superseded"
            if stale
            else None,
            "historical": historical,
            "is_current": selected and not stale and result["status"] == "ready",
            "current_input": self._input_state(current),
        }

    def current(self, unit):
        with self.source_lock(unit):
            current = self._read_current(unit)
            state = self._state(unit)
            rid = state["current_revision_id"] or state["selected_revision_id"]
            legacy = self._legacy(unit) if rid is None else None
            if rid is None and legacy:
                rid = legacy["revision_id"]
            latest = None
            for candidate in reversed(state["order"]):
                if state["revisions"][candidate]["status"] == "ready":
                    self._revision(unit, candidate, state)
                    latest = candidate
                    break
            if latest is None:
                previous = self._legacy(unit)
                if previous and previous["report"] is not None:
                    latest = previous["revision_id"]
            pending = state["selected_revision_id"]
            if pending and state["revisions"][pending]["status"] not in {"queued", "running"}:
                pending = None
            if rid is not None:
                result = self._view(unit, rid, state, current)
                return {
                    **result,
                    "current_revision_id": rid if result["is_current"] else None,
                    "latest_available_revision_id": latest,
                    "pending_revision_id": pending,
                }
            return {
                "status": "not_generated",
                "revision_id": None,
                "report": None,
                "stale": False,
                "stale_reason": None,
                "current_input": self._input_state(current),
                "current_revision_id": None,
                "latest_available_revision_id": None,
                "pending_revision_id": None,
            }

    def history(self, unit):
        with self.source_lock(unit):
            current = self._read_current(unit)
            state = self._state(unit)
            ids = list(reversed(state["order"]))
            legacy = self._legacy(unit)
            if legacy:
                ids.append(legacy["revision_id"])
            return [self._view(unit, rid, state, current) for rid in ids]

    def get(self, unit, revision_id):
        with self.source_lock(unit):
            return self._view(unit, revision_id, self._state(unit), self._read_current(unit))

    def request(self, unit, *, request_id, expected_input_sha256, retry_of=None):
        if type(request_id) is not str or not re.fullmatch("[A-Za-z0-9:_-]{1,200}", request_id):
            raise StateConflict("invalid report request id")
        binding = fingerprint({"input_sha256": expected_input_sha256, "retry_of": retry_of})
        with self._locked(unit):
            snapshot = self._snapshot(unit)
            digest = fingerprint(snapshot)
            state = self._state(unit)
            previous = state["requests"].get(request_id)
            if previous:
                if previous["binding"] != binding:
                    raise StateConflict("request id reused with different parameters")
                return self._view(unit, previous["revision_id"], state, snapshot)
            if expected_input_sha256 != digest:
                raise StateConflict("report input changed; reload before request")
            attempt = 1
            if retry_of is not None:
                if type(retry_of) is str and retry_of.startswith("legacy-"):
                    meta = self._legacy(unit)
                    if meta is None or meta["revision_id"] != retry_of:
                        raise StateConflict("unknown legacy retry")
                    row = meta["legacy_job"]
                    response = None
                else:
                    meta, _, row, response, _ = self._revision(unit, retry_of, state)
                if (
                    meta["input_sha256"] != digest
                    or row["status"] not in {"failed", "cancelled"}
                    or row["request_started"]
                    and (row.get("budget_receipt") or {}).get("state")
                    not in {"settled", "completed", "failed", "released", "not_reserved"}
                ):
                    raise StateConflict("revision cannot retry while dispatch outcome or input is unresolved")
                attempt = meta["attempt"] + 1
                for existing in reversed(state["order"]):
                    successor, _, _, _, _ = self._revision(unit, existing, state)
                    if successor.get("retry_of") == retry_of and successor["input_sha256"] == digest:
                        state["requests"][request_id] = {"binding": binding, "revision_id": existing}
                        self._write(unit, state)
                        return self._view(unit, existing, state, snapshot)
            else:
                for rid in reversed(state["order"]):
                    meta, _, row, _, report = self._revision(unit, rid, state)
                    if meta["input_sha256"] != digest:
                        continue
                    state["requests"][request_id] = {"binding": binding, "revision_id": rid}
                    state["selected_revision_id"] = rid
                    if report is not None and row["status"] == "ready":
                        state["current_revision_id"] = rid
                    self._write(unit, state)
                    return self._view(unit, rid, state, snapshot)
                legacy = self._legacy(unit)
                if legacy and legacy["input_sha256"] == digest:
                    state["requests"][request_id] = {"binding": binding, "revision_id": legacy["revision_id"]}
                    self._write(unit, state)
                    return self._view(unit, legacy["revision_id"], state, snapshot)
            rid = "drr-" + uuid.uuid4().hex
            legacy = self._legacy(unit)
            previous = state["order"][-1] if state["order"] else legacy["revision_id"] if legacy else None
            state["generation"] += 1
            meta = {
                "version": VERSION,
                "revision_id": rid,
                "unit": deepcopy(unit),
                "input_version": snapshot["identity"],
                "input_sha256": digest,
                "prompt_version": snapshot["version"],
                "model": self.model,
                "request_id": request_id,
                "attempt": attempt,
                "retry_of": retry_of,
                "previous_revision_id": previous,
                "generation": state["generation"],
                "created_at": utc_text(self.clock()),
            }
            directory = self._directory(unit) / "revisions" / rid
            self._once(directory / "input.json", snapshot)
            self._once(directory / "revision.json", meta)
            state["order"].append(rid)
            state["selected_revision_id"] = rid
            state["revisions"][rid] = {
                "revision_sha256": fingerprint(meta),
                "status": "queued",
                "request_started": False,
                "budget_receipt": None,
                "response_sha256": None,
                "report_sha256": None,
                "error": None,
                "updated_at": meta["created_at"],
                "completed_at": None,
            }
            state["requests"][request_id] = {"binding": binding, "revision_id": rid}
            self._write(unit, state)
            return self._view(unit, rid, state, snapshot)

    def claim(self, unit, rid):
        with self._locked(unit):
            state = self._state(unit)
            meta, _, row, _, _ = self._revision(unit, rid, state)
            if row["status"] != "queued":
                return False
            if meta["input_sha256"] != fingerprint(self._snapshot(unit)):
                row.update(status="cancelled", error="input_changed", completed_at=self.clock())
                self._write(unit, state)
                return False
            row.update(status="running", updated_at=self.clock())
            self._write(unit, state)
            return True

    def mark_dispatched(self, unit, rid, *, budget_receipt):
        with self._locked(unit):
            state = self._state(unit)
            meta, _, row, response, _ = self._revision(unit, rid, state)
            if row["status"] != "running" or row["request_started"] or response is not None:
                return False
            if meta["input_sha256"] != fingerprint(self._snapshot(unit)):
                raise StateConflict("input changed before dispatch")
            if type(budget_receipt) is not dict or not budget_receipt:
                raise StateConflict("budget receipt required")
            row.update(request_started=True, budget_receipt=deepcopy(budget_receipt), updated_at=self.clock())
            self._write(unit, state)
            return True

    def save_response(self, unit, rid, output, *, budget_receipt):
        with self._locked(unit):
            state = self._state(unit)
            meta, _, row, response, _ = self._revision(unit, rid, state)
            if row["status"] not in {"running", "cancelled", "outcome_unknown"} or not row["request_started"]:
                raise StateConflict("revision has no dispatched request")
            if type(budget_receipt) is not dict or not budget_receipt:
                raise StateConflict("budget receipt required")
            value = {
                "revision_id": rid,
                "input_sha256": meta["input_sha256"],
                "output": deepcopy(output),
                "output_sha256": fingerprint(output),
                "budget_receipt": deepcopy(budget_receipt),
                "received_at": response["received_at"] if response else utc_text(self.clock()),
            }
            self._once(self._directory(unit) / "revisions" / rid / "response.json", value)
            row.update(
                response_sha256=fingerprint(value), budget_receipt=deepcopy(budget_receipt), updated_at=self.clock()
            )
            if row["status"] == "outcome_unknown":
                row.update(status="cancelled", error="cancelled")
            self._write(unit, state)

    def finalize(self, unit, rid):
        with self._locked(unit):
            state = self._state(unit)
            meta, snapshot, row, response, report = self._revision(unit, rid, state)
            if row["status"] == "ready":
                return self._view(unit, rid, state, self._read_current(unit))
            if row["status"] != "running" or response is None:
                raise StateConflict("no response eligible for persistence")
            output = compile_report_output(response["output"], snapshot)
            report = {
                "report_id": "report-" + rid[4:],
                "revision_id": rid,
                "input_version": meta["input_version"],
                "input_sha256": meta["input_sha256"],
                "model": meta["model"],
                "prompt_version": meta["prompt_version"],
                "generated_at": report_generated_at(response["received_at"], report),
                "status": "draft",
                "scientific_validation": False,
                "budget_receipt": response["budget_receipt"],
                "output": output,
                "structured_table": snapshot["structured_table"],
                "study_label": snapshot["study_label"],
                "context": snapshot["context"],
                "citations": snapshot["citations"],
                "gaps": snapshot["gaps"],
                "input_summary": {
                    "fact_count": len(snapshot["facts"]),
                    "excluded_count": snapshot["excluded_count"],
                    "issue_count": len(snapshot["issues"]),
                    "review_counts": dict(Counter(f["review_status"] for f in snapshot["facts"])),
                },
            }
            audit = generation_audit(response["output"], snapshot)
            if audit is not None:
                report["generation"] = audit
            self._once(self._directory(unit) / "revisions" / rid / "report.json", report)
            current = self._read_current(unit)
            row.update(
                status="ready", report_sha256=fingerprint(report), updated_at=self.clock(), completed_at=self.clock()
            )
            if (
                current is not None
                and state["selected_revision_id"] == rid
                and meta["input_sha256"] == fingerprint(current)
            ):
                state["current_revision_id"] = rid
            self._write(unit, state)
            return self._view(unit, rid, state, current)

    def recover(self, unit, *, revision_ids=None):
        actions = []
        with self._locked(unit):
            state = self._state(unit)
            for rid in state["order"] if revision_ids is None else revision_ids:
                _, _, row, response, _ = self._revision(unit, rid, state)
                if row["status"] != "running":
                    continue
                if response is not None:
                    actions.append({"revision_id": rid, "action": "persist_only"})
                    continue
                row.update(
                    status="outcome_unknown" if row["request_started"] else "cancelled",
                    error="interrupted",
                    updated_at=self.clock(),
                    completed_at=self.clock(),
                )
            if state["order"]:
                self._write(unit, state)
        return actions

    def cancel(self, unit, rid):
        with self._locked(unit):
            state = self._state(unit)
            _, _, row, response, _ = self._revision(unit, rid, state)
            if row["status"] not in {"queued", "running"}:
                return
            row.update(
                status="outcome_unknown" if row["request_started"] and response is None else "cancelled",
                error="cancelled",
                updated_at=self.clock(),
                completed_at=self.clock(),
            )
            self._write(unit, state)

    def fail(self, unit, rid, *, code, budget_receipt=None):
        if code not in {"budget_exceeded", "provider_failed", "invalid_report", "model_unavailable"}:
            raise StateConflict("invalid report failure code")
        with self._locked(unit):
            state = self._state(unit)
            _, _, row, response, _ = self._revision(unit, rid, state)
            if row["status"] not in {"queued", "running"}:
                raise StateConflict("revision cannot fail again")
            receipt = deepcopy(budget_receipt if budget_receipt is not None else row["budget_receipt"])
            unknown = (
                row["request_started"]
                and response is None
                and (receipt or {}).get("state") not in {"settled", "completed", "failed", "released", "not_reserved"}
            )
            row.update(
                status="outcome_unknown" if unknown else "failed",
                error=code,
                budget_receipt=receipt,
                updated_at=self.clock(),
                completed_at=self.clock(),
            )
            self._write(unit, state)
