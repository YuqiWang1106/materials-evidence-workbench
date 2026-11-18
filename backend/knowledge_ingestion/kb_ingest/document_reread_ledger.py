"""Small durable ledger for bounded, task-owned local document rereads.

An uncertain attempt retains its reservations after a restart. Only an immutable
completion receipt with measured durations releases unused time. This ledger
accounts for execution; a successful parser receipt is not a quality verdict.
"""

from __future__ import annotations

import fcntl
import json
import math
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import atomic_json

LIMITS = {"execution_seconds": 600, "queue_seconds": 300, "attempt_execution_seconds": 300, "attempts_per_page": 2}
SCHEMA = "materials.document_reread_ledger.v1"


def _seconds(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Reread durations must be known finite nonnegative numbers")
    return float(value)


def _pages(value) -> list[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(page) is not int or page < 1 for page in value)
        or len(value) != len(set(value))
    ):
        raise ValueError("Rereads require unique positive physical page numbers")
    return sorted(value)


def _identity(request_key, source_sha256, pages) -> dict:
    if not isinstance(request_key, str) or not request_key:
        raise ValueError("Reread request key is required")
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        raise ValueError("Reread source SHA256 is required")
    return {"request_key": request_key, "source_sha256": source_sha256, "pages": _pages(pages)}


def _result(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("status"), str) or not value["status"]:
        raise ValueError("Reread result must have a status")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("Reread result must be finite JSON data") from exc


class RereadLedger:
    def __init__(self, path: Path, task_binding: str):
        if not isinstance(task_binding, str) or not task_binding:
            raise ValueError("Reread task binding is required")
        self.path = Path(path)
        self.task_binding = task_binding

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield self._load()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema": SCHEMA, "task_binding": self.task_binding, "limits": dict(LIMITS), "attempts": []}
        try:
            state = json.loads(self.path.read_text())
            if (
                state["schema"] != SCHEMA
                or state["task_binding"] != self.task_binding
                or state["limits"] != LIMITS
                or not isinstance(state["attempts"], list)
            ):
                raise ValueError("Reread ledger binding or limits changed")
            ids = set()
            for attempt in state["attempts"]:
                identity = _identity(attempt["request_key"], attempt["source_sha256"], attempt["pages"])
                if identity["pages"] != attempt["pages"]:
                    raise ValueError("Noncanonical reread pages")
                aid = attempt["attempt_id"]
                if not isinstance(aid, str) or not aid or aid in ids:
                    raise ValueError("Duplicate or missing reread attempt")
                ids.add(aid)
                for kind in ("queue", "execution"):
                    if not 0 < _seconds(attempt[f"reserved_{kind}_seconds"]) <= 300:
                        raise ValueError("Invalid reread reservation")
                if attempt["status"] == "settled":
                    self._receipt(attempt, attempt["receipt"])
                elif attempt["status"] != "pending" or attempt.get("receipt") is not None:
                    raise ValueError("Invalid reread state")
            return state
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Reread ledger is invalid or belongs to another task") from exc

    @staticmethod
    def _totals(state) -> dict:
        totals = {}
        for kind in ("execution", "queue"):
            used = sum(a["receipt"][f"{kind}_seconds"] for a in state["attempts"] if a["status"] == "settled")
            reserved = sum(a[f"reserved_{kind}_seconds"] for a in state["attempts"] if a["status"] == "pending")
            totals.update(
                {
                    f"{kind}_seconds_used": used,
                    f"{kind}_seconds_reserved": reserved,
                    f"{kind}_seconds_remaining": max(0, LIMITS[f"{kind}_seconds"] - used - reserved),
                }
            )
        return totals

    def snapshot(self) -> dict:
        with self._locked() as state:
            return {**state, "totals": self._totals(state)}

    def reserve(self, request_key: str, source_sha256: str, pages: list[int], *, allow_retry: bool = False) -> dict:
        identity = _identity(request_key, source_sha256, pages)
        with self._locked() as state:
            matches = [a for a in state["attempts"] if a["request_key"] == request_key]
            if any(any(a[k] != identity[k] for k in identity) for a in matches):
                return {"status": "blocked", "reason": "request_identity_conflict"}
            if matches:
                previous = matches[-1]
                if previous["status"] == "pending":
                    return {"status": "blocked", "reason": "attempt_pending", "attempt_id": previous["attempt_id"]}
                if previous["receipt"]["result"]["status"] == "ok":
                    return {
                        "status": "reused",
                        "receipt": previous["receipt"],
                        "result": previous["receipt"]["result"],
                        "attempt_id": previous["attempt_id"],
                    }
                if not allow_retry:
                    return {"status": "blocked", "reason": "retry_not_authorized", "attempt_id": previous["attempt_id"]}
            for page in identity["pages"]:
                count = sum(a["source_sha256"] == source_sha256 and page in a["pages"] for a in state["attempts"])
                if count >= LIMITS["attempts_per_page"]:
                    return {"status": "blocked", "reason": "page_attempt_limit", "page": page}
            totals = self._totals(state)
            for kind in ("execution", "queue"):
                if totals[f"{kind}_seconds_remaining"] <= 0:
                    return {"status": "blocked", "reason": f"{kind}_budget_exhausted"}
            attempt = {
                **identity,
                "attempt_id": uuid.uuid4().hex,
                "status": "pending",
                "receipt": None,
                "reserved_execution_seconds": min(300, totals["execution_seconds_remaining"]),
                "reserved_queue_seconds": totals["queue_seconds_remaining"],
            }
            state["attempts"].append(attempt)
            atomic_json(self.path, state)
            return {
                "status": "reserved",
                "attempt_id": attempt["attempt_id"],
                "execution_timeout_seconds": attempt["reserved_execution_seconds"],
                "queue_timeout_seconds": attempt["reserved_queue_seconds"],
            }

    @staticmethod
    def _receipt(attempt, receipt) -> dict:
        if not isinstance(receipt, dict):
            raise ValueError("Reread completion receipt is required")
        for field in ("attempt_id", "request_key", "source_sha256", "pages"):
            if receipt.get(field) != attempt[field]:
                raise ValueError("Reread receipt does not match the reserved attempt")
        return {
            **{key: attempt[key] for key in ("attempt_id", "request_key", "source_sha256", "pages")},
            "result": _result(receipt.get("result")),
            "queue_seconds": _seconds(receipt.get("queue_seconds")),
            "execution_seconds": _seconds(receipt.get("execution_seconds")),
        }

    def _settle(self, state, attempt, receipt) -> dict:
        receipt = self._receipt(attempt, receipt)
        if attempt["status"] == "settled":
            if attempt["receipt"] != receipt:
                raise ValueError("A settled reread receipt cannot be replaced")
        else:
            attempt.update(status="settled", receipt=receipt)
            atomic_json(self.path, state)
        return {"status": "settled", "attempt_id": attempt["attempt_id"], "receipt": receipt}

    @staticmethod
    def _find(state, attempt_id):
        found = next((a for a in state["attempts"] if a["attempt_id"] == attempt_id), None)
        if found is None:
            raise ValueError("Unknown reread attempt")
        return found

    def settle(self, attempt_id: str, result: dict, queue_seconds: float, execution_seconds: float) -> dict:
        with self._locked() as state:
            attempt = self._find(state, attempt_id)
            receipt = {
                **{key: attempt[key] for key in ("attempt_id", "request_key", "source_sha256", "pages")},
                "result": result,
                "queue_seconds": queue_seconds,
                "execution_seconds": execution_seconds,
            }
            return self._settle(state, attempt, receipt)

    def recover(self, attempt_id: str, receipt: dict) -> dict:
        """Recover only a known completion; elapsed wall time is never evidence."""
        with self._locked() as state:
            return self._settle(state, self._find(state, attempt_id), receipt)
