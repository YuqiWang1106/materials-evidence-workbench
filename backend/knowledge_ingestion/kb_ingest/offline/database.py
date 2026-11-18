"""SQLite snapshots and integrity summaries for offline migration."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import column, literal_column, select, table

from ..storage import sqlite
from ..storage.engine import file_connection
from .files import _canonical, _fail, _hash

_INTEGRITY_FAILED = "SQLite integrity check failed"


def _is_sqlite(path):
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\0"


def _sqlite_copy(source, target):
    journal = Path(str(source) + "-journal")
    if journal.exists() or journal.is_symlink():
        _fail("SQLite rollback journal requires offline recovery before backup")
    # Copy all WAL state first, then use SQLite backup on the disposable copy.
    # Even read-only WAL connections can create shared-memory files: never open
    # the actual source database with sqlite3.
    with tempfile.TemporaryDirectory(prefix="kb-migration-sqlite-") as temporary:
        local = Path(temporary) / "source.sqlite"
        shutil.copyfile(source, local)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, Path(str(local) + suffix))
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if journal.exists() or journal.is_symlink():
            _fail("SQLite rollback journal appeared during backup")
        if not sqlite.online_backup(local, target, standalone=True):
            _fail(_INTEGRITY_FAILED)
        target.chmod(0o600)


def _all_rows(connection, name):
    return connection.execute(select(literal_column("*")).select_from(table(name)))


def _receipted_rows(connection, name, selected):
    source = table(name, *(column(key) for key in selected))
    return connection.execute(select(source).where(source.c.receipt.is_not(None)))


def _json_value(value):
    if isinstance(value, bytes):
        return {"sqlite_blob_hex": value.hex()}
    return value


def _db_summary(path, reverse=None):
    result = {}
    with file_connection(path, readonly=True) as db:
        if not sqlite.integrity_ok(db):
            _fail(_INTEGRITY_FAILED)
        if not sqlite.foreign_keys_ok(db):
            _fail("SQLite foreign-key check failed")
        for table in sqlite.table_names(db):
            columns = sqlite.column_names(db, table)
            rows = []
            for raw in _all_rows(db, table):
                row = [_json_value(item) for item in raw]
                if "receipt" in columns and reverse:
                    index = columns.index("receipt")
                    if row[index]:
                        receipt = json.loads(row[index])
                        key = _hash(_canonical(receipt))
                        if key in reverse:
                            row[index] = reverse[key]
                rows.append(_canonical(row).decode())
            result[table] = {"count": len(rows), "sha256": _hash(_canonical(sorted(rows)))}
    return result


def _require_stopped_tasks(connection, columns):
    active = (
        "SELECT 1 FROM tasks WHERE state IN ('running','reading','extracting','validating',"
        "'publishing','write_unknown') LIMIT 1"
    )
    leased = (
        "SELECT 1 FROM tasks WHERE lease_until IS NOT NULL AND state NOT IN ('saved',"
        "'failed','cancelled','needs_review') LIMIT 1"
    )
    if connection.exec_driver_sql(active).fetchone():
        _fail("Source has active or unknown scientific writes")
    if "lease_until" in columns and connection.exec_driver_sql(leased).fetchone():
        _fail("Source still has task ownership leases")
