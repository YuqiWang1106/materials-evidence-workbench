"""Workspace-owned model state, request logs and response files."""

import fcntl
import json
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..core.workspace import DEFAULT_WORKSPACE, validate_workspace_id, workspace_files
from ..storage.types import decode_utc
from .errors import ModelCallError
from .identity import Json

_CONFLICT = "model workspace identity mismatch"
_LINK = "model state path must be a regular workspace path"


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise ModelCallError(_CONFLICT)
    return path.absolute()


def _partitions(path: Path) -> list[Path]:
    """All reserved workspace partitions containing this path, including nested ones."""
    return [
        child
        for child in (path, *path.parents)
        if child.parent.name == "workspaces"
        and len(child.name) == 64
        and all(ch in "0123456789abcdef" for ch in child.name)
    ]


def _system_alias(path: Path) -> bool:
    """macOS exposes its system temporary directories through root-owned aliases."""
    return (
        sys.platform == "darwin"
        and path.parent == Path("/")
        and path.name in {"tmp", "var"}
        and path.resolve(strict=True) == Path("/private") / path.name
    )


def _marked_root(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if any(
            (parent / name).exists() or (parent / name).is_symlink()
            for name in (".ingestion-file-owner.json", ".native-workspace.json")
        ):
            return parent
    return None


@dataclass(frozen=True)
class FileScope:
    workspace_id: str
    workspace_root: Path | None = None

    def __post_init__(self) -> None:
        validate_workspace_id(self.workspace_id)
        if self.workspace_root is None:
            return
        root = _absolute(Path(self.workspace_root))
        object.__setattr__(self, "workspace_root", root)
        self.guard(root)
        partitions = _partitions(root)
        if partitions:
            if any(workspace_files(part.parent.parent, self.workspace_id) != part for part in partitions):
                raise ModelCallError(_CONFLICT)
        else:
            primary = _marked_root(root) or root
            if workspace_files(primary, self.workspace_id) != primary:
                raise ModelCallError(_CONFLICT)

    def path(self, value: Path | str | None, *, directory: bool = False) -> Path | None:
        if value is None:
            return None
        path = _absolute(Path(value))
        root = self.workspace_root
        if root is None and _partitions(path):
            raise ModelCallError(_CONFLICT)
        if root is not None:
            if not path.is_relative_to(root):
                raise ModelCallError(_CONFLICT)
            if path.relative_to(root).parts[:1] == ("workspaces",) or _partitions(path) != _partitions(root):
                raise ModelCallError(_CONFLICT)
        elif self.workspace_id == DEFAULT_WORKSPACE:
            primary = _marked_root(path.parent)
            if primary is not None and workspace_files(primary, self.workspace_id) != primary:
                raise ModelCallError(_CONFLICT)
        else:
            base = path if directory else path.parent
            root = base
            owner_dir = base / "workspaces" / sha256(self.workspace_id.encode()).hexdigest()
            path = owner_dir if directory else owner_dir / path.name
        self.guard(path, root)
        return path

    @staticmethod
    def guard(path: Path, root: Path | None = None) -> None:
        path = _absolute(path)
        root = _absolute(root) if root is not None else None
        for item in (path, *path.parents):
            try:
                info = item.lstat()
            except FileNotFoundError:
                pass
            else:
                single_file = item == path and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                if not stat.S_ISDIR(info.st_mode) and not single_file and not _system_alias(item):
                    raise ModelCallError(_LINK)
            if item == root:
                break

    def _legacy_primary(self) -> bool:
        return (
            self.workspace_id == DEFAULT_WORKSPACE
            if self.workspace_root is None
            else not _partitions(self.workspace_root)
        )

    def provider_key(self, key: str) -> str:
        if self.workspace_id == DEFAULT_WORKSPACE and self._legacy_primary():
            return key
        identity = json.dumps([self.workspace_id, key], ensure_ascii=True, separators=(",", ":"))
        return "model-" + sha256(identity.encode()).hexdigest()

    def verify(self, row: Json) -> None:
        valid = row["workspace_id"] == self.workspace_id if "workspace_id" in row else self._legacy_primary()
        if not valid:
            raise ModelCallError(_CONFLICT)


def read_scoped_attempts(path: Path, scope: FileScope) -> list[Json]:
    scope.guard(path, scope.workspace_root)
    with path.open(encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return _rows(stream, scope)


def _rows(lines: Iterable[str], scope: FileScope) -> list[Json]:
    rows: list[Json] = []
    for line in lines:
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                message = "invalid model attempt ledger"
                raise ValueError(message)
            scope.verify(row)
            if row.get("request_kind") == "table_chat_tools":
                # Budget recovery and public readers still consume seconds.
                if "started_at_epoch" in row or not isinstance(row.get("started_at"), str):
                    message = "invalid table chat attempt timestamp"
                    raise ValueError(message)
                row["started_at_epoch"] = decode_utc(row.pop("started_at")).timestamp()
            rows.append(row)
    return rows


def read_attempts(path: Path, workspace_id: str, *, workspace_root: Path | None = None) -> list[Json]:
    scope = FileScope(workspace_id, workspace_root)
    target = scope.path(path)
    if target is None or not target.exists():
        return []
    return read_scoped_attempts(target, scope)


def append_attempt(path: Path, row: Json, scope: FileScope) -> None:
    scope.guard(path, scope.workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        out.seek(0)
        _rows(out, scope)
        out.seek(0, os.SEEK_END)
        out.write(json.dumps({**row, "workspace_id": scope.workspace_id}, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())
