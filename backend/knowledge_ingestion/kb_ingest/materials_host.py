"""Validate explicit research configuration for the offline Wiki comparison."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .knowledge_scope import CollectionScope


class MaterialsConfigurationError(ValueError):
    """Stable safe-to-display configuration failure, without input values."""


def _invalid():
    raise MaterialsConfigurationError("Invalid trusted materials Host configuration.") from None


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        _invalid()
    return value


def _path(value, parent):
    if type(value) is not str or not value.strip() or "\x00" in value:
        _invalid()
    path = Path(value).expanduser()
    return (path if path.is_absolute() else parent / path).resolve()


@dataclass(frozen=True)
class MaterialsHostConfig:
    workspace_id: str
    project_id: str
    collections: tuple[CollectionScope, ...]
    snapshot_root: Path
    budget_root: Path | None
    model_config: Path | None
    session_token: str = field(repr=False)


def load_materials_config(path: Path, *, environ=None) -> MaterialsHostConfig:
    """Pure validation before Store construction; no writes and no generated secrets."""
    env = os.environ if environ is None else environ
    try:
        path = Path(path).expanduser().resolve()
        if path.stat().st_size > 65536:
            _invalid()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        required = {"workspace_id", "project_id", "collections", "snapshot_root", "budget_root", "session_token_env"}
        if not required <= set(data) or set(data) - required - {"model_config"}:
            _invalid()
        workspace = _identifier(data["workspace_id"])
        project = _identifier(data["project_id"])
        name = data["session_token_env"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name):
            _invalid()
        session_token = env.get(name)
        if not isinstance(session_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", session_token):
            _invalid()
        collections = data["collections"]
        if type(collections) is not list or not 1 <= len(collections) <= 128:
            _invalid()
        scopes = []
        for item in collections:
            if type(item) is not dict or set(item) != {"id", "version", "policy_version", "source_ids"}:
                _invalid()
            ids = item["source_ids"]
            if type(ids) is not list or len(ids) > 10000:
                _invalid()
            ids = [_identifier(v) for v in ids]
            if len(ids) != len(set(ids)):
                _invalid()
            scopes.append(
                CollectionScope(
                    workspace,
                    _identifier(item["id"]),
                    _identifier(item["version"]),
                    frozenset(ids),
                    _identifier(item["policy_version"]),
                )
            )
        if len(scopes) != len({s.collection_id for s in scopes}):
            _invalid()
        parent = path.parent
        return MaterialsHostConfig(
            workspace,
            project,
            tuple(scopes),
            _path(data["snapshot_root"], parent),
            _path(data["budget_root"], parent),
            _path(data["model_config"], parent) if "model_config" in data else None,
            session_token,
        )
    except (OSError, ValueError, TypeError, UnicodeError, KeyError):
        _invalid()
