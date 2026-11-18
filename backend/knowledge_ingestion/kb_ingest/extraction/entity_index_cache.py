"""Bounded in-process reuse of derived scoped retrieval state only."""

import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future


def estimated_size(value: object, *, stop_after: int) -> int:
    """Count the retained Python object graph once, stopping above the admission budget."""
    pending: list[object] = [value]
    seen: set[int] = set()
    total = 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if total > stop_after:
            return total
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
        else:
            if hasattr(item, "__dict__"):
                pending.append(vars(item))
            for name in list(getattr(type(item), "__slots__", ())):
                if hasattr(item, name):
                    pending.append(getattr(item, name))
    return total


class ScopedIndexCache[V]:
    """LRU and idle expiry; one off-lock build per key, including uncacheable results."""

    def __init__(
        self,
        *,
        max_entries: int = 8,
        idle_ttl: float = 300,
        max_bytes: int = 128 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries, self.idle_ttl, self.max_bytes = max_entries, idle_ttl, max_bytes
        self._clock = clock
        self._entries: OrderedDict[object, tuple[V, int, float]] = OrderedDict()
        self._building: dict[object, Future[V]] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        while self._entries:
            _, (_, size, touched) = next(iter(self._entries.items()))
            if now - touched < self.idle_ttl:
                break
            self._entries.popitem(last=False)
            self._bytes -= size

    def get_or_build(self, key: object, build: Callable[[], V]) -> V:
        with self._lock:
            now = self._clock()
            self._expire(now)
            entry = self._entries.pop(key, None)
            if entry is not None:
                value, size, _ = entry
                self._entries[key] = (value, size, now)
                return value
            pending = self._building.get(key)
            owner = pending is None
            if pending is None:
                pending = self._building[key] = Future[V]()
        if not owner:
            return pending.result()
        try:
            value = build()
            size = estimated_size((key, value), stop_after=self.max_bytes)
        except BaseException as exc:
            with self._lock:
                self._building.pop(key)
                pending.set_exception(exc)
            raise
        with self._lock:
            now = self._clock()
            self._expire(now)
            if self.max_entries > 0 and self.idle_ttl > 0 and size <= self.max_bytes:
                while self._entries and (len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes):
                    _, (_, removed_size, _) = self._entries.popitem(last=False)
                    self._bytes -= removed_size
                self._entries[key] = (value, size, now)
                self._bytes += size
            self._building.pop(key)
            pending.set_result(value)
        return value
