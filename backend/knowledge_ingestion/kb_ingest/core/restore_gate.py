"""Fail closed when a native restore has not finished and been verified."""

import json
import os
import re
import stat
from pathlib import Path
from typing import Never

from .errors import NativeError
from .workspace import saved_workspace_id

_MESSAGE = "Restore is incomplete; startup refused."


def _invalid() -> Never:
    raise NativeError(_MESSAGE) from None


def _read_private(path: Path) -> object:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
            _invalid()
        payload: object = json.load(stream)
        return payload


def require_restore_complete(root: Path) -> None:
    """Verify restore evidence before a native instance starts writing."""
    required = root / ".restore-required.json"
    try:
        required.lstat()
    except FileNotFoundError:
        try:
            (root / ".restore-complete.json").lstat()
        except FileNotFoundError:
            return
        _invalid()
    try:
        marker = _read_private(required)
        if (
            not isinstance(marker, dict)
            or set(marker) != {"format", "backup_bundle", "target_root", "workspace_id"}
            or marker["format"] != "native-restore-v1"
            or not isinstance(marker["backup_bundle"], str)
            or not re.fullmatch("[0-9a-f]{64}", marker["backup_bundle"])
            or marker["target_root"] != str(root.resolve())
            or marker["workspace_id"] != saved_workspace_id(root)
            or _read_private(root / ".restore-complete.json") != marker
        ):
            _invalid()
    except (OSError, ValueError, TypeError, KeyError):
        _invalid()
