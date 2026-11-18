"""Decide whether a data root is new, current, or must be refused, before anything is written to it."""

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.errors import NativeError
from . import sqlite
from .snapshot import readonly_snapshot

CURRENT_REVISION = "kbs_413_model_results"
REFUSAL_MESSAGE = "工作区数据库不属于当前修订链，请保留原数据根，停写并按核准方案升级副本"
_DATABASE = "coordination.sqlite3"
_UNAVAILABLE = "Workspace database inspection is unavailable."
# Everything a first start writes before the coordination database exists. Finder adds .DS_Store
# when an operator clears the directory by hand. Any other entry may be material of an earlier
# workspace, so the root is not treated as new.
_STARTUP_FILES = frozenset({".native-owner.lock", ".native-workspace.json", ".native-auth.json", ".DS_Store"})
_STARTUP_LOCKS = "locks"


class IncompatibleRevisionError(NativeError):
    def __init__(self) -> None:
        super().__init__(REFUSAL_MESSAGE)


def _unavailable() -> NativeError:
    return NativeError(_UNAVAILABLE)


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _regular_file(path: Path) -> bool:
    """Symlinks and special files are never followed or opened."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise _unavailable()
    return True


def _written_by_first_start(entry: os.DirEntry[str]) -> bool:
    if entry.name == _STARTUP_LOCKS:
        if not entry.is_dir(follow_symlinks=False):
            return False
        with os.scandir(entry.path) as locks:
            return all(lock.name.endswith(".lock") and lock.is_file(follow_symlinks=False) for lock in locks)
    return entry.name in _STARTUP_FILES and entry.is_file(follow_symlinks=False)


def _new_root(root: Path) -> bool:
    with os.scandir(root) as entries:
        return all(_written_by_first_start(entry) for entry in entries)


def _require_current(database: Path, *, readonly: bool) -> None:
    try:
        current = sqlite.is_current_revision(database, CURRENT_REVISION, readonly=readonly)
    except sqlite.DatabaseError:
        raise _unavailable() from None
    if not current:
        raise IncompatibleRevisionError


@contextmanager
def directory_lock(root: Path, *, write: bool = False, wait: bool = True) -> Iterator[int | None]:
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if not write:
            yield None
            return
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        flag = fcntl.LOCK_EX if write else fcntl.LOCK_SH
        fcntl.flock(descriptor, flag if wait else flag | fcntl.LOCK_NB)
        if not os.path.samestat(root.lstat(), os.fstat(descriptor)):
            raise _unavailable()
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _current_copy(database: Path) -> Iterator[Path]:
    """Doctor and ledger imports must not leave SQLite auxiliary files beside the source."""
    with readonly_snapshot(database) as copied:
        _require_current(copied, readonly=False)
        yield copied


@contextmanager
def locked_database(root: Path, *, write: bool = False, wait: bool = True) -> Iterator[Path | None]:
    """Hold the directory lock from classification until the caller has finished initializing.

    Yields None for a new root, otherwise a current database that may be opened read-only:
    the database itself for a writer, a private copy for a reader. Any other root is refused
    before a workspace file is created, changed or removed in it.
    """
    with directory_lock(root, write=write, wait=wait) as descriptor:
        if descriptor is None:
            yield None
            return
        database = root / _DATABASE
        if not _regular_file(database):
            if not _new_root(root):
                raise _unavailable()
            yield None
        elif write:
            _require_current(database, readonly=True)
            yield database
        else:
            with _current_copy(database) as copied:
                yield copied


@contextmanager
def external_database(budget: Path | None, primary: Path, *, wait: bool = True) -> Iterator[Path | None]:
    """A binding imports only an existing, separate coordination ledger."""
    if budget is None or budget.resolve() == primary.resolve():
        yield None
        return
    database = budget / _DATABASE
    names = [database.name + suffix for suffix in ("", "-wal", "-shm", "-journal")]
    with directory_lock(budget, wait=wait) as descriptor:
        if descriptor is None or not any(_present(budget / name) for name in names):
            yield None
            return
        with _current_copy(database) as copied:
            yield copied
