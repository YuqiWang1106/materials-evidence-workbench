"""Validate the declared roots, source identity and stopped-process proof."""

import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path

from .files import BRANCH, MigrationError, _fail, _location, _read


def _check_roots(value):
    roles = set()
    for name, row in value["roots"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            _fail("Invalid root identifier")
        root = Path(row["path"])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            _fail("Every declared private root must exist as a real absolute directory")
        roles.update(row["roles"])
    if not {"data", "config", "budget"} <= roles:
        _fail("Data, configuration and budget roots must all be explicitly declared")


def _check_source_identity(value, expected_instance_id):
    identity = _read(_location(value, value["identity"]))
    if any(identity.get(key) != value[key] for key in ("instance_id", "workspace_id", "branch")):
        _fail("Source identity marker does not match the selected instance")
    proof = _read(_location(value, value["stopped_proof"]))
    if (
        proof.get("instance_id") != expected_instance_id
        or proof.get("stopped") is not True
        or not isinstance(proof.get("process_ids"), list)
    ):
        _fail("Explicit stopped-process proof is required")
    _check_processes(proof["process_ids"])


def _check_processes(process_ids):
    for pid in process_ids:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            _fail("Invalid process proof")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            message = "Source writer process cannot be confirmed stopped"
            raise MigrationError(message) from None
        _fail("Source writer process is still active")


def _check_runtime_paths(value):
    for ref in value.get("runtime_paths", []):
        if ref["root"] not in value["roots"] or "config" not in value["roots"][ref["root"]]["roles"]:
            _fail("Runtime path mapping requires a declared configuration root")
        if not isinstance(ref["pointer"], list) or not ref["pointer"]:
            _fail("Runtime path mapping requires an explicit JSON field pointer")
        if ref.get("format", "json") not in {"json", "toml"}:
            _fail("Unknown runtime configuration format")
        _location(value, ref)


def _descriptor(path, expected_instance_id):
    value = _read(Path(path))
    try:
        if (
            value["format"] != "kb-native-source-v1"
            or value["branch"] != BRANCH
            or value["instance_id"] != expected_instance_id
            or not value["workspace_id"]
            or value["complete_roots"] is not True
            or value["archive_backend"] not in {"none", "oasis", "local"}
        ):
            _fail("Source identity, completeness or archive backend is unconfirmed")
        _check_roots(value)
        _check_source_identity(value, expected_instance_id)
        _location(value, value["database"])
        _check_runtime_paths(value)
    except (KeyError, TypeError):
        message = "Source descriptor is incomplete"
        raise MigrationError(message) from None
    return value


@contextmanager
def _stopped_source(value):
    lock = _location(value, value["lock"])
    try:
        with lock.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except (OSError, BlockingIOError):
        message = "Cannot acquire the existing stopped-source ownership lock"
        raise MigrationError(message) from None
