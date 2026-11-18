"""Wall-clock instants and a context-local monotonic I/O deadline.

``now()`` returns an aware UTC datetime and can be injected in tests. File
locks and database reads keep using ``time.monotonic()`` through
``IO_DEADLINE``. The wall clock and the monotonic clock are not interchangeable.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import overload

IO_DEADLINE: ContextVar[float | None] = ContextVar("knowledge_index_io_deadline", default=None)
_NOW: ContextVar[Callable[[], datetime] | None] = ContextVar("wall_clock_now", default=None)
_EXPIRED = "Knowledge read deadline exceeded"
_NAIVE_NOW = "clock.now() rejects naive datetime"
_UTC = UTC


def seconds_until(deadline: float) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError(_EXPIRED)
    return budget


@overload
def remaining(default: None = None) -> float | None: ...


@overload
def remaining(default: float) -> float: ...


def remaining(default: float | None = None) -> float | None:
    deadline = IO_DEADLINE.get()
    if deadline is None:
        return default
    budget = seconds_until(deadline)
    return budget if default is None else min(default, budget)


def _aware_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(_NAIVE_NOW)
    return instant.astimezone(_UTC)


def now() -> datetime:
    provider = _NOW.get()
    instant = datetime.now(_UTC) if provider is None else provider()
    return _aware_utc(instant)


@contextmanager
def using_now(provider: Callable[[], datetime]) -> Iterator[None]:
    token = _NOW.set(provider)
    try:
        yield
    finally:
        _NOW.reset(token)
