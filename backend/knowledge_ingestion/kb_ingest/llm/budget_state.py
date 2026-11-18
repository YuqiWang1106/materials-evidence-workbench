"""Validate, lock and durably replace a task budget snapshot."""

import fcntl
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.files import atomic_bytes
from .errors import ModelCallError
from .identity import Json
from .ledger import FileScope

_LIMITS = ("max_calls", "max_output_tokens", "max_total_tokens", "max_wall_seconds")
_INVALID = "task budget state invalid; refusing to reset accumulated usage"


def _nonnegative(value: object) -> bool:
    return type(value) in (int, float) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def validate(state: Json) -> bool:
    pending = state["pending"]
    return (
        state["version"] == 1
        and isinstance(pending, dict)
        and isinstance(state["settled"], dict)
        and all(type(state[k]) is int and state[k] >= 0 for k in ("calls", "output_tokens", "total_tokens"))
        and all(type(v) is int and v >= 0 for r in pending.values() for v in (r["output"], r["total"]))
        and all(math.isfinite(state["limits"][k]) and state["limits"][k] >= 1 for k in _LIMITS)
        and type(state["started_at_epoch"]) in (float, int)
        and math.isfinite(state["started_at_epoch"])
        and (
            not state.get("active_time")
            or (_nonnegative(state["active_seconds"]) and all(_nonnegative(r["seconds"]) for r in pending.values()))
        )
    )


@contextmanager
def locked(path: Path, scope: FileScope) -> Iterator[None]:
    scope.guard(path, scope.workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    scope.guard(lock, scope.workspace_root)
    with lock.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def load(path: Path, scope: FileScope) -> Json:
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or not validate(state):
            raise ModelCallError(_INVALID)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ModelCallError(_INVALID) from None
    scope.verify(state)
    return state


def save(path: Path, state: Json) -> None:
    atomic_bytes(path, json.dumps(state, sort_keys=True, allow_nan=False).encode())
