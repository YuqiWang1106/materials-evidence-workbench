"""The timeline of a task's current attempt for the task page: a cached snapshot and its live stream."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol

from kb_ingest.core import clock
from kb_ingest.core.errors import StateConflict

from .journal import JOURNAL, available
from .snapshot import ACTIVE, RECORDS, elapsed_seconds, snapshot

MAX_CACHED = 128
HEARTBEAT_TICKS = 15
_cache: OrderedDict[tuple[object, ...], dict[str, object]] = OrderedDict()
_cache_guard = threading.Lock()
_build_lock = threading.Lock()

type Snapshot = dict[str, object]


class _Tasks(Protocol):
    def get_task(self, task_id: str, /) -> Mapping[str, object]: ...


class TaskSource(Protocol):
    """What the timeline needs from the ingestion service: the task record and where its attempt wrote."""

    @property
    def store(self) -> _Tasks: ...
    @property
    def document_options(self) -> Mapping[str, object]: ...
    def _document_artifact_root(self, task: Mapping[str, object], /) -> Path: ...


class Disconnectable(Protocol):
    def is_disconnected(self) -> Awaitable[bool]: ...


def for_task(service: TaskSource, task_id: str) -> Snapshot:
    """The snapshot of the task's current attempt. Reads existing files only; creates nothing.

    Observers of an unchanged attempt share one built snapshot; only the observation time and the running
    clock are refreshed per call.
    """
    task = service.store.get_task(task_id)
    root = service._document_artifact_root(task)
    forbidden = _secrets(service.document_options)
    key = (
        str(root),
        *(task.get(name) for name in ("task_id", "attempt", "generation", "state", "created_at", "updated_at")),
        tuple(hashlib.sha256(secret.encode()).hexdigest() for secret in forbidden),
        available(root),
        *(_signature(root / name) for name in (JOURNAL, *RECORDS)),
    )
    built = _cached(key)
    if built is None:
        with _build_lock:
            built = _cached(key)
            if built is None:
                built = snapshot(root, task, forbidden=forbidden)
                _remember(key, built)
    now = clock.now().timestamp()
    value = {**built, "observed_at": now}
    if task.get("state") in ACTIVE:
        value["elapsed_seconds"] = elapsed_seconds(task, now)
    return value


def _secrets(options: Mapping[str, object]) -> tuple[str, ...]:
    config = options.get("model_config")
    key = config.get("api_key") if isinstance(config, Mapping) else None
    return (key,) if isinstance(key, str) and key else ()


def _signature(path: Path) -> tuple[object, ...] | None:
    """Changes whenever the file is replaced, grows or is rewritten; None while it does not exist."""
    try:
        if path.is_symlink():
            return ("link",)
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def _cached(key: tuple[object, ...]) -> Snapshot | None:
    with _cache_guard:
        built = _cache.get(key)
        if built is not None:
            _cache.move_to_end(key)
        return built


def _remember(key: tuple[object, ...], built: Snapshot) -> None:
    with _cache_guard:
        _cache[key] = built
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)


async def stream(
    request: Disconnectable,
    read: Callable[[], Snapshot],
    *,
    interval: float = 1.0,
    public: Callable[[Snapshot], Snapshot] = lambda value: value,
) -> AsyncGenerator[str]:
    """Server-sent events: the whole snapshot whenever its cursor changes, a heartbeat while it does not.

    Every message is a complete snapshot, so a client that connects or reconnects never has to merge events.

    Args:
        request: The client connection, polled for disconnection.
        read: Builds the current snapshot; runs in a worker thread.
        interval: Seconds between reads.
        public: Projects a snapshot into its public form just before it is sent.
    """
    last: object = None
    ticks = 0
    while not await request.is_disconnected():
        try:
            value = await asyncio.to_thread(read)
        except (KeyError, ValueError, OSError, StateConflict):
            yield 'event: unavailable\ndata: {"reason":"observation_unavailable"}\n\n'
            return
        cursor = value["cursor"]
        if cursor != last:
            payload = json.dumps({**public(value), "resync": True}, ensure_ascii=False, separators=(",", ":"))
            yield f"id: {cursor}\nevent: snapshot\ndata: {payload}\n\n"
            last, ticks = cursor, 0
        else:
            ticks += 1
            if ticks >= HEARTBEAT_TICKS:
                yield ": heartbeat\n\n"
                ticks = 0
        await asyncio.sleep(interval)
