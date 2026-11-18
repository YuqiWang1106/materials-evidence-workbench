"""Validate individual task paths and pin directory handles for candidate writes."""

import errno
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from ..core.errors import raise_state_conflict as conflict

_INVALID = "Invalid task storage path"
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def checked_path(data_root: Path, workspace_root: Path, task_id: str, parts: tuple[str | Path, ...]) -> Path:
    if not task_id or task_id in {".", ".."} or Path(task_id).name != task_id:
        conflict(_INVALID)
    return checked_child(data_root, workspace_root.relative_to(data_root), "pending", task_id, *parts)


def checked_child(root: Path, *parts: str | Path) -> Path:
    if any(Path(part).is_absolute() or ".." in Path(part).parts for part in parts):
        conflict(_INVALID)
    current = root
    if current.is_symlink():
        conflict(_INVALID)
    for name in Path(*parts).parts:
        current = current / name
        if current.is_symlink():
            conflict(_INVALID)
    return current


def checked_files(root: Path, pattern: str) -> Iterator[Path]:
    return (checked_child(root, path.name) for path in root.glob(pattern))


def _open_directory(name: str | Path, parent: int | None = None) -> int:
    try:
        return os.open(name, _DIRECTORY, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            conflict(_INVALID)
        raise


def _open_child(parent: int, name: str) -> int:
    try:
        return _open_directory(name, parent)
    except FileNotFoundError:
        with suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
        return _open_directory(name, parent)


@contextmanager
def directory(data_root: Path, target: Path) -> Iterator[int]:
    """Walk below the trusted root without reopening an ancestor by a mutable path."""
    parts = target.relative_to(data_root).parts
    if ".." in parts:
        conflict(_INVALID)
    descriptor = _open_directory(data_root)
    try:
        for name in parts:
            child = _open_child(descriptor, name)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def atomic_write(data_root: Path, target: Path, data: bytes) -> None:
    with directory(data_root, target.parent) as parent:
        temporary = ".pending-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)
