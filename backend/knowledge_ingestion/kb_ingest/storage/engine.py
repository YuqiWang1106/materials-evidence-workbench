"""SQLAlchemy engines for the coordination database and isolated SQLite clients."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import URL, Engine, create_engine, event
from sqlalchemy.engine import Connection as SQLConnection
from sqlalchemy.pool import ConnectionPoolEntry, NullPool, PoolProxiedConnection

from ..core.clock import IO_DEADLINE, seconds_until
from . import sqlite

if TYPE_CHECKING:
    from sqlite3 import Connection


def file_engine(path: Path, *, readonly: bool = False) -> Engine:
    url = (
        URL.create("sqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
        if readonly
        else URL.create("sqlite", database=str(path))
    )
    return create_engine(url)


@contextmanager
def file_connection(path: Path, *, readonly: bool = False) -> Iterator[SQLConnection]:
    """Release all handles before offline tools remove their temporary database."""
    engine = file_engine(path, readonly=readonly)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def _connect(connection: Connection, _: ConnectionPoolEntry) -> None:
    deadline = IO_DEADLINE.get()
    budget = None if deadline is None else partial(seconds_until, deadline)
    sqlite.configure_connection(connection, budget)


def _checkout(connection: Connection, _: ConnectionPoolEntry, _proxy: PoolProxiedConnection) -> None:
    deadline = IO_DEADLINE.get()
    if deadline is not None:
        sqlite.set_busy_timeout(connection, sqlite.busy_timeout_ms(seconds_until(deadline)))


def _checkin(connection: Connection | None, _: ConnectionPoolEntry) -> None:
    if connection is not None:
        sqlite.set_busy_timeout(connection, sqlite.DEFAULT_BUSY_TIMEOUT_MS)


def coordination_engine(root: Path) -> Engine:
    engine = create_engine(
        URL.create("sqlite", database=str(root / "coordination.sqlite3")), connect_args={"timeout": 30}
    )
    event.listen(engine, "connect", _connect)
    event.listen(engine, "checkout", _checkout)
    event.listen(engine, "checkin", _checkin)
    return engine


def _enable_foreign_keys(connection: Connection, _: ConnectionPoolEntry) -> None:
    sqlite.enable_foreign_keys(connection)


def unpooled_engine(url: URL, *, foreign_keys: bool = False) -> Engine:
    """Deadline-bound operations must not wait for a shared pool slot."""
    engine = create_engine(url, poolclass=NullPool, connect_args={"timeout": 0})
    if foreign_keys:
        event.listen(engine, "connect", _enable_foreign_keys)
    return engine
