"""File and path primitives for offline v1 migration bundles."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Never

FORMAT = "kb-native-migration-v1"
BRANCH = "knowledgebase/entity-pipeline"


class MigrationError(RuntimeError):
    """Sanitized migration failure: no credentials or private file contents."""


def _fail(message: str) -> Never:
    raise MigrationError(message)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        message = "Invalid or missing private migration metadata"
        raise MigrationError(message) from None


def _fsync_directory(path):
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _remove_temporary(path: Path) -> None:
    # A failed existence check leaves the temporary file alone, as in v1.
    try:
        path.stat()
    except (OSError, ValueError):
        return
    path.unlink()


def _write(path, value, *, staging=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=staging or path.parent, prefix=".migration-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        _remove_temporary(temporary)


def _safe(root, relative):
    value = PurePosixPath(relative)
    if not relative or value.is_absolute() or ".." in value.parts or "\\" in relative:
        _fail("Unsafe migration relative path")
    root = Path(root).resolve()
    path = root.joinpath(*value.parts)
    if not path.resolve().is_relative_to(root):
        _fail("Migration path escapes its declared root")
    current = path
    while current != root:
        if current.is_symlink():
            _fail("Symbolic links are not accepted in migration roots")
        current = current.parent
    return path


def _location(descriptor, ref):
    try:
        return _safe(Path(descriptor["roots"][ref["root"]]["path"]), ref["path"])
    except (KeyError, TypeError):
        message = "Undeclared migration root reference"
        raise MigrationError(message) from None


def _relative(ref):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ref["root"]):
        _fail("Invalid root identifier")
    return f"roots/{ref['root']}/{ref['path']}"


def _files(root):
    def scan_error(_error):
        _fail("Cannot read a directory in the migration root")

    for directory, directories, files in os.walk(root, onerror=scan_error, followlinks=False):
        parent = Path(directory)
        for name in sorted(directories):
            path = parent / name
            if path.is_symlink() or not path.is_dir():
                _fail("Only regular files and directories may enter a migration bundle")
        for name in sorted(files):
            path = parent / name
            if path.is_symlink() or not path.is_file():
                _fail("Only regular files and directories may enter a migration bundle")
            yield path
