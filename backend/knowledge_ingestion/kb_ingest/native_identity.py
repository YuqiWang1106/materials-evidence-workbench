"""Private first-run identity and authentication; all writes require root ownership."""

import json
import os
import re
import secrets
import stat
from pathlib import Path


def read_private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError("Private state permissions are invalid.")
        return json.load(stream)


def authentication(root, *, initialize=False):
    path = Path(root) / ".native-auth.json"
    try:
        value = read_private_json(path)
    except FileNotFoundError:
        if not initialize:
            return None
        from .core import atomic_json

        value = {"session_token": secrets.token_urlsafe(48)}
        atomic_json(path, value)
    token = value.get("session_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("Private authentication state is invalid.")
    return {"session_token": token}


def new_workspace_id():
    return "workspace-" + secrets.token_hex(16)
