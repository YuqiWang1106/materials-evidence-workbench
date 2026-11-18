"""Stable snapshots of a live SQLite file for read-only inspection."""

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.errors import NativeError


def _file_state(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode):
        message = "SQLite source snapshot is unavailable."
        raise NativeError(message)
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _copy_readonly(source: Path, target: Path | None = None) -> bytes:
    """Read source bytes without opening SQLite or creating its auxiliary files."""
    digest = hashlib.sha256()
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        output = target.open("wb") if target is not None else None
        try:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
        finally:
            if output is not None:
                output.close()
    return digest.digest()


@contextmanager
def readonly_snapshot(database: Path) -> Iterator[Path]:
    """Only SQLite-open a stable temporary copy, including committed WAL state.

    No source SQLite connection is opened: even mode=ro can mutate/create SHM.
    Compare identities, metadata and bytes across the copy to reject concurrent
    checkpoint/commit changes. SHM is deliberately rebuilt only in the temporary
    directory. WAL replay therefore sees committed frames omitted by immutable=1.
    An actively changing source may conservatively report unavailable after three
    attempts; an idle writer lock alone does not block this inspection.
    """
    sources = [database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-journal")]
    for _ in range(3):
        with tempfile.TemporaryDirectory(prefix="kb-doctor-sqlite-") as directory:
            target = Path(directory) / database.name
            before = [_file_state(path) for path in sources]
            if before[0] is None:
                message = "Existing SQLite database is unavailable."
                raise NativeError(message)
            try:
                copied = [
                    _copy_readonly(path, Path(directory) / path.name) if state is not None else None
                    for path, state in zip(sources, before, strict=True)
                ]
                if before != [_file_state(path) for path in sources]:
                    continue
                verified = [
                    _copy_readonly(path) if state is not None else None
                    for path, state in zip(sources, before, strict=True)
                ]
                if copied != verified or before != [_file_state(path) for path in sources]:
                    continue
            except FileNotFoundError:
                continue
            yield target
            return
    message = "SQLite source is changing; retry the read-only health check."
    raise NativeError(message)
