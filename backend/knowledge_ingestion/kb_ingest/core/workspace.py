"""Trusted ingestion identity and file locations within a coordination data root."""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .files import atomic_json

DEFAULT_WORKSPACE = "local"
WORKSPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_INVALID_ID = "Invalid workspace identity."
# Constructor omit vs explicit None: production StudyUnitService callers either
# omit workspace_id (service.py, document_workflow) or pass a concrete string
# (wiki, study_unit_api via wiki.workspace_id, support/index/jobs). routes()
# previously forwarded its own default None as an explicit keyword; a sentinel
# keeps omit as graph_workspace(service) and rejects explicit None/blank.
OMITTED_WORKSPACE = object()


def validate_workspace_id(value: object) -> str:
    if not isinstance(value, str) or not WORKSPACE_ID.fullmatch(value):
        raise ValueError(_INVALID_ID)
    return value


def graph_workspace(service: object) -> str:
    """Resolve the authoritative entity-graph workspace once for journal and writes.

    This is not the task publication identity. TaskPublication.workspace_id must
    still match EntityStore / Store.workspace_id; an explicit study or wiki graph
    space may legally differ from the store. A missing study_unit_workspace_id
    defaults to the stable service/store workspace_id and is never derived from a
    data-root path fingerprint. Explicit None, empty, or blank values are rejected.
    """
    default = getattr(service, "workspace_id", None)
    if default is None:
        default = getattr(getattr(service, "store", None), "workspace_id", None)
    try:
        configured = object.__getattribute__(service, "study_unit_workspace_id")
    except AttributeError:
        return validate_workspace_id(default)
    return validate_workspace_id(configured)


def _read_marker(marker: Path) -> str | None:
    if marker.is_symlink():
        raise ValueError(_INVALID_ID)
    if not marker.exists():
        return None
    if marker.stat().st_size > 16384:
        raise ValueError(_INVALID_ID)
    stored = json.loads(marker.read_bytes())
    if not isinstance(stored, dict) or set(stored) != {"workspace_id"}:
        raise ValueError(_INVALID_ID)
    return validate_workspace_id(stored["workspace_id"])


def data_root_path(path: Path) -> Path:
    """Resolve every parent but keep a final symlink visible to the no-follow directory open.

    A final ".." is never a link itself. It is resolved as well, because the checks that keep
    the model directory outside the data root need the directory that would actually be used.
    """
    path = path.absolute()
    return path.resolve() if path.name == ".." else path.parent.resolve() / path.name


def saved_workspace_id(root: Path) -> str | None:
    return _read_marker(root / ".native-workspace.json")


def primary_workspace_id(data_root: Path) -> str:
    """Bind legacy root-level originals once, under the common migration lock."""
    marker = data_root / ".ingestion-file-owner.json"
    owner = _read_marker(marker)
    if owner is None:
        owner = saved_workspace_id(data_root) or DEFAULT_WORKSPACE
        atomic_json(marker, {"workspace_id": owner})
    return owner


@dataclass(frozen=True)
class Workspace:
    data_root: Path
    workspace_id: str

    @classmethod
    def local(cls, root: Path) -> "Workspace":
        return cls(root, saved_workspace_id(root) or DEFAULT_WORKSPACE)


def workspace_files(data_root: Path, workspace_id: str) -> Path:
    """Locate files without creating a marker, including when inspecting an offline backup."""
    validate_workspace_id(workspace_id)
    owner = _read_marker(data_root / ".ingestion-file-owner.json") or saved_workspace_id(data_root) or DEFAULT_WORKSPACE
    if workspace_id == owner:
        return data_root
    directory = data_root / "workspaces" / sha256(workspace_id.encode()).hexdigest()
    if directory.is_symlink() or directory.parent.is_symlink():
        message = "Workspace directory must not be a symlink."
        raise ValueError(message)
    return directory


def matches_database_workspaces(data_root: Path, workspace_id: str, owners: set[str]) -> bool:
    """An initialized primary may have no rows while a registered secondary has data."""
    if not owners or workspace_id in owners:
        return True
    if (
        _read_marker(data_root / ".ingestion-file-owner.json") != workspace_id
        or saved_workspace_id(data_root) != workspace_id
    ):
        return False
    return all(workspace_files(data_root, owner).is_dir() for owner in owners)
