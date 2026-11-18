"""Append-only event journal of one attempt, and bounded reads of the attempt's line-delimited records."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from kb_ingest.core import clock

from .schema import Event, safe_event

logger = logging.getLogger(__name__)

JOURNAL = "observation-events.jsonl"
MAX_READ_BYTES = 8 * 1024 * 1024
_TAIL_BYTES = 65536
_LOCK_SECONDS = 0.25
_locks: dict[str, threading.Lock] = {}
_failed: set[str] = set()
_guard = threading.Lock()
# A restarted process cannot prove that the previous writer recorded every event before it exited, so a
# journal written by another process is never reported as a complete history.
_session = uuid.uuid4().hex


class JournalError(ValueError):
    """The journal on disk cannot be continued safely."""


def session() -> str:
    """The identity of this process as a journal writer."""
    return _session


def journal_path(root: Path) -> Path:
    return Path(root) / JOURNAL


def mark_unavailable(root: Path) -> None:
    """Stop writing this attempt's journal; its history is reported as partial from now on."""
    with _guard:
        _failed.add(str(journal_path(root)))


def available(root: Path) -> bool:
    with _guard:
        return str(journal_path(root)) not in _failed


def _lock_for(path: Path) -> threading.Lock:
    with _guard:
        return _locks.setdefault(str(path), threading.Lock())


@contextmanager
def _bounded_lock(path: Path) -> Iterator[bool]:
    """Wait briefly for the journal: a slow diagnostic write must never hold the pipeline."""
    lock = _lock_for(path)
    acquired = lock.acquire(timeout=_LOCK_SECONDS)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def record(root: Path, **fields: object) -> bool:
    """Append one allowlisted event to the attempt's journal; returns whether it was written.

    An event outside the vocabulary or a journal that cannot be written makes the journal unavailable (logged
    once) instead of raising: the timeline is diagnostic and its failure must not change the task. From then on
    nothing more is written and the snapshot reports the history as partial.
    """
    root = Path(root)
    path = journal_path(root)
    if not available(root):
        return False
    event = safe_event(fields)
    if event is None:
        _give_up(root, "event outside the vocabulary")
        return False
    event.setdefault("at", clock.now().timestamp())
    event["_session"] = _session
    try:
        with _bounded_lock(path) as acquired:
            if not acquired:
                _give_up(root, "journal lock timeout")
                return False
            if not available(root):
                return False
            _append(root, path, event)
    except (OSError, ValueError, TypeError, KeyError) as error:
        _give_up(root, type(error).__name__)
        return False
    return True


def _give_up(root: Path, reason: str) -> None:
    mark_unavailable(root)
    logger.warning("observation journal unavailable attempt_root=%s reason=%s", Path(root).name, reason)


def _append(root: Path, path: Path, event: Event) -> None:
    if path.is_symlink():
        raise JournalError("link")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "a+b") as stream:
        event["seq"] = _next_sequence(stream)
        stream.write((json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        stream.flush()


def _next_sequence(stream: BinaryIO) -> int:
    """One more than the last event's number, read from a bounded tail; an unfinished tail is not continued."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if not size:
        return 1
    amount = min(size, _TAIL_BYTES)
    stream.seek(size - amount)
    tail = stream.read(amount)
    last = tail.rstrip(b"\n").rsplit(b"\n", 1)[-1]
    if not tail.endswith(b"\n") or (size > amount and len(last) == amount):
        raise JournalError("tail")
    sequence = json.loads(last)["seq"] + 1
    if type(sequence) is not int or sequence < 1:
        raise JournalError("sequence")
    return sequence


def read_lines(path: Path, limit: int = MAX_READ_BYTES) -> tuple[list[dict[str, object]], bool]:
    """(rows, incomplete) of a line-delimited JSON file, reading at most its last `limit` bytes.

    A link, an unreadable file, a cut-off beginning and every line that is not a JSON object count as incomplete.
    A file that does not exist yet is an empty, complete history.
    """
    if path.is_symlink():
        return [], True
    if not path.is_file():
        return [], False
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            incomplete = size > limit
            stream.seek(max(0, size - limit))
            if incomplete:
                stream.readline()  # the partial first line of the bounded tail
            data = stream.read(limit)
    except OSError:
        return [], True
    rows: list[dict[str, object]] = []
    for line in data.splitlines():
        row = _row(line)
        if row is None:
            incomplete = True
        else:
            rows.append(row)
    return rows, incomplete


def _row(line: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
