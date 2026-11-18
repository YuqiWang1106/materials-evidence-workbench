"""SQLite connection settings, bounded queries, lexical search and offline inspection."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement, Connection, Engine
    from sqlalchemy.orm import InstrumentedAttribute, Session

DatabaseError = sqlite3.Error

DEFAULT_BUSY_TIMEOUT_MS = 30_000
_CREATE_FTS = (
    "CREATE VIRTUAL TABLE knowledge_index_fts USING fts5(tokens, generation_id UNINDEXED, "
    "entry_id UNINDEXED, tokenize='ascii')"
)
_DELETE_FTS_GENERATION = "DELETE FROM knowledge_index_fts WHERE generation_id=:generation"
_INSERT_FTS_ENTRY = "INSERT INTO knowledge_index_fts(tokens,generation_id,entry_id) VALUES (:tokens,:generation,:entry)"
_RANK_SCOPED_ENTRIES = """
    SELECT e.entry_id, bm25(knowledge_index_fts) AS score FROM knowledge_index_fts
    JOIN knowledge_index_entries e ON e.entry_id=knowledge_index_fts.entry_id
    WHERE knowledge_index_fts MATCH :query AND knowledge_index_fts.generation_id=:generation
      AND e.generation_id=:generation AND e.scope_key=:scope
    ORDER BY score, e.entry_key LIMIT :top_k
"""
_RANK_FTS_ENTRIES = """
    SELECT entry_id,bm25(knowledge_index_fts) AS rank FROM knowledge_index_fts
    WHERE knowledge_index_fts MATCH :query AND generation_id=:generation
    ORDER BY rank,entry_id LIMIT :limit
"""


def is_sqlite(bind: Connection | Engine) -> bool:
    return bind.dialect.name == "sqlite"


def is_file_database(engine: Engine) -> bool:
    url = engine.url
    return url.database not in (None, "", ":memory:") and url.query.get("mode") != "memory"


def busy_timeout_ms(seconds: float) -> int:
    return max(1, int(seconds * 1000))


def set_busy_timeout(raw: sqlite3.Connection, milliseconds: int) -> None:
    raw.execute("PRAGMA busy_timeout=" + str(int(milliseconds)))


def enable_foreign_keys(raw: sqlite3.Connection) -> None:
    raw.execute("PRAGMA foreign_keys=ON")


def configure_connection(raw: sqlite3.Connection, remaining_seconds: Callable[[], float] | None) -> None:
    # WAL setup can itself wait on a lock. Charge it to the inherited budget.
    if remaining_seconds is not None:
        set_busy_timeout(raw, busy_timeout_ms(remaining_seconds()))
    raw.execute("PRAGMA journal_mode=WAL")
    enable_foreign_keys(raw)
    timeout = DEFAULT_BUSY_TIMEOUT_MS if remaining_seconds is None else busy_timeout_ms(remaining_seconds())
    set_busy_timeout(raw, timeout)


def begin_immediate(transaction: Session | Connection) -> None:
    from sqlalchemy import text

    transaction.execute(text("BEGIN IMMEDIATE"))


@contextmanager
def migration_transaction(connection: Connection) -> Iterator[None]:
    """Include Alembic's version table and stamp in the same SQLite transaction as the schema."""
    raw = cast("sqlite3.Connection", connection.connection.driver_connection)
    external = connection.in_transaction() or raw.in_transaction
    with nullcontext() if external else connection.begin():
        # sqlite3 legacy mode delays BEGIN past reads and DDL, including SQLAlchemy's logical begin().
        if not raw.in_transaction:
            # An already-current database only needs to read the revision, even under an active writer.
            connection.exec_driver_sql("BEGIN")
        # A failed migration must not roll back other work in a caller-owned transaction.
        with connection.begin_nested() if external else nullcontext():
            yield


@contextmanager
def query_deadline(connection: Connection, deadline: float | None) -> Iterator[None]:
    if deadline is None:
        yield
        return
    raw = cast("sqlite3.Connection", connection.connection.driver_connection)
    previous = raw.execute("PRAGMA busy_timeout").fetchone()[0]
    set_busy_timeout(raw, busy_timeout_ms(deadline - time.monotonic()))
    try:
        raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        yield
    finally:
        raw.set_progress_handler(None, 0)
        set_busy_timeout(raw, previous)


def create_reading_version_triggers(connection: Connection) -> None:
    """Install the immutable reading-version triggers for the initial schema."""
    for suffix, event in (("no_update", "UPDATE"), ("no_delete", "DELETE")):
        connection.exec_driver_sql(
            f"CREATE TRIGGER reading_versions_{suffix} BEFORE {event} ON reading_versions "
            "BEGIN SELECT RAISE(ABORT, 'Reading versions are immutable'); END"
        )


def create_fts_index(connection: Connection) -> None:
    """Install the FTS5 virtual table and its shadow tables for the initial schema."""
    from sqlalchemy.exc import DatabaseError

    try:
        connection.exec_driver_sql(_CREATE_FTS)
    except DatabaseError as exc:
        message = "FTS5_UNAVAILABLE: install SQLite with FTS5 and retry migration"
        raise RuntimeError(message) from exc


def online_backup(source: Path, destination: Path, *, standalone: bool = False) -> bool:
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(destination)) as copied:
        original.backup(copied)
        if standalone:
            copied.execute("PRAGMA journal_mode=DELETE")
            return bool(copied.execute("PRAGMA integrity_check").fetchone() == ("ok",))
        return True


def table_names(connection: Connection, *, include_internal: bool = False) -> list[str]:
    query = (
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        if include_internal
        else "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return list(connection.exec_driver_sql(query).scalars())


def column_names(connection: Connection, name: str) -> list[str]:
    return list(connection.exec_driver_sql("SELECT name FROM pragma_table_info(?) ORDER BY cid", (name,)).scalars())


def schema_objects(connection: Connection) -> list[tuple[object, ...]]:
    query = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    return [tuple(row) for row in connection.exec_driver_sql(query)]


def table_definition(connection: Connection, name: str) -> object:
    return connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).scalar()


def integrity_ok(connection: Connection) -> bool:
    return connection.exec_driver_sql("PRAGMA integrity_check").first() == ("ok",)


def foreign_keys_ok(connection: Connection) -> bool:
    return connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None


def is_current_revision(database: Path, revision: str, *, readonly: bool) -> bool:
    """Read the stamp through SQLite, which keeps this process's POSIX locks on the file.

    A read-only connection never recovers a journal or changes the database. Beside a WAL
    database it may still create the -shm and an empty -wal file. A private copy is opened
    writable so that SQLite can recover its hot journal there.
    """
    location = database.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    # Query only ordinary main-schema tables.
    with closing(sqlite3.connect(location, uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        tables = connection.execute("PRAGMA main.table_list(alembic_version)").fetchall()
        if not any(row[:3] == ("main", "alembic_version", "table") for row in tables):
            return False
        columns = connection.execute("PRAGMA main.table_xinfo(alembic_version)").fetchall()
        if len(columns) != 1 or columns[0][1] != "version_num" or columns[0][6] != 0:
            return False
        return connection.execute("SELECT version_num FROM main.alembic_version LIMIT 2").fetchall() == [(revision,)]


def workspace_ids(copy: Path) -> set[str]:
    # Doctor must work before optional website dependencies have been installed.
    with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            message = "Existing database integrity check failed."
            raise ValueError(message)
        identities: set[str] = set()
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            names = connection.execute("SELECT name FROM pragma_table_info(?)", (name,))
            if ("workspace_id",) in names:
                # Identifiers cannot be bound as parameters. Escape the schema-supplied table name.
                identifier = '"' + name.replace('"', '""') + '"'
                query = " ".join(("SELECT DISTINCT workspace_id FROM", identifier))
                identities.update(value for (value,) in connection.execute(query) if value)
    return identities


def receipt_rows(connection: Connection, name: str) -> list[tuple[int, str]]:
    from sqlalchemy import column, select, table

    target = table(name, column("rowid"), column("receipt"))
    statement = select(target.c.rowid, target.c.receipt).where(target.c.receipt.is_not(None))
    return [(cast("int", rowid), cast("str", receipt)) for rowid, receipt in connection.execute(statement)]


def replace_receipt(connection: Connection, name: str, rowid: int, receipt: str) -> None:
    from sqlalchemy import column, table

    target = table(name, column("rowid"), column("receipt"))
    connection.execute(target.update().where(target.c.rowid == rowid).values(receipt=receipt))


def delete_fts_generation(session: Session, generation: str) -> None:
    from sqlalchemy import text

    session.execute(text(_DELETE_FTS_GENERATION), {"generation": generation})


def insert_fts_entry(session: Session, tokens: str, generation: str, entry: int) -> None:
    from sqlalchemy import text

    session.execute(text(_INSERT_FTS_ENTRY), {"tokens": tokens, "generation": generation, "entry": entry})


def rank_scoped_entries(
    session: Session, query: str, generation: str, scope: str, top_k: int
) -> list[tuple[int, float]]:
    from sqlalchemy import text

    params = {"query": query, "generation": generation, "scope": scope, "top_k": top_k}
    return [
        (cast("int", entry), cast("float", score))
        for entry, score in session.execute(text(_RANK_SCOPED_ENTRIES), params)
    ]


def rank_generation(session: Session, query: str, generation: str, limit: int) -> list[tuple[int, float]]:
    from sqlalchemy import text

    params = {"query": query, "generation": generation, "limit": limit}
    return [
        (cast("int", entry), cast("float", score)) for entry, score in session.execute(text(_RANK_FTS_ENTRIES), params)
    ]


def json_array_member(column: InstrumentedAttribute[list[str]], value: str) -> ColumnElement[bool]:
    """Check individual JSON array values without substring or wildcard matching."""
    from sqlalchemy import func, select

    entries = func.json_each(column).table_valued("value")
    return select(entries.c.value).where(entries.c.value == value).exists()
