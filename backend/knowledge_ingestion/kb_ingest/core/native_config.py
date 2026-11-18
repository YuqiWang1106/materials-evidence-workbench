"""Read-only parsing and validation of the explicit native website configuration."""

import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Never

from .errors import NativeError
from .workspace import data_root_path, saved_workspace_id

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_INVALID_CONFIG = "Native configuration is invalid; check required fields and absolute paths."


def _invalid() -> Never:
    raise NativeError(_INVALID_CONFIG) from None


def _absolute(value: object, *, resolve: bool = True) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or not Path(value).is_absolute():
        _invalid()
    path = Path(value)
    return path.resolve() if resolve else path


@dataclass(frozen=True, repr=False)
class NativeConfig:
    workspace_id: str
    data_root: Path
    model_root: Path | None
    mineru_command: tuple[str, ...]
    model_config: Path | None
    research_config: Path | None = None
    entity_budget_root: Path | None = None
    entity_batch_id: str | None = None
    host: str = "127.0.0.1"
    port: int = 8789
    workers: int = 2
    deprecated_fields: tuple[str, ...] = ()
    agent_knowledge_token_file: Path | None = None
    agent_knowledge_max_response_bytes: int = 16 * 1024 * 1024
    second_reader_python: Path | None = None
    second_reader_model: Path | None = None


def _workspace_id(root: Path, explicit: object) -> str:
    if not isinstance(explicit, str):
        _invalid()
    saved = saved_workspace_id(root)
    if saved is not None and explicit and explicit != saved:
        _invalid()
    return saved or explicit


def _server_options(data: dict[str, Any]) -> tuple[str, int, int, int]:
    host, port, workers = data.get("host", "127.0.0.1"), data.get("port", 8789), data.get("workers", 2)
    if (
        host != "127.0.0.1"
        or type(port) is not int
        or not 1 <= port <= 65535
        or type(workers) is not int
        or not 1 <= workers <= 8
    ):
        _invalid()
    max_bytes = data.get("agent_knowledge_max_response_bytes", 16 * 1024 * 1024)
    if type(max_bytes) is not int or max_bytes <= 0:
        _invalid()
    return host, port, workers, max_bytes


def _parse(data: dict[str, Any]) -> NativeConfig:
    allowed = NativeConfig.__dataclass_fields__.keys() - {"deprecated_fields"}
    if "data_root" not in data or set(data) - allowed:
        _invalid()
    workspace = data.get("workspace_id", "")
    if "workspace_id" in data and (not isinstance(workspace, str) or not _ID.fullmatch(workspace)):
        _invalid()
    root = data_root_path(_absolute(data["data_root"], resolve=False))
    model_root = _absolute(data["model_root"]) if data.get("model_root") else None
    if model_root is not None and (model_root == root or root in model_root.parents or model_root in root.parents):
        _invalid()
    command = data.get("mineru_command", [])
    if not isinstance(command, list) or len(command) > 1:
        _invalid()
    host, port, workers, max_bytes = _server_options(data)
    return NativeConfig(
        workspace_id=workspace,
        data_root=root,
        model_root=model_root,
        mineru_command=tuple(str(_absolute(executable)) for executable in command),
        model_config=_absolute(data["model_config"]) if data.get("model_config") else None,
        research_config=_absolute(data["research_config"]) if data.get("research_config") else None,
        entity_budget_root=_absolute(data["entity_budget_root"]) if data.get("entity_budget_root") else None,
        entity_batch_id=data.get("entity_batch_id"),
        host=host,
        port=port,
        workers=workers,
        deprecated_fields=tuple(
            key for key in ("entity_budget_root", "entity_batch_id", "research_config") if key in data
        ),
        agent_knowledge_token_file=_absolute(data["agent_knowledge_token_file"])
        if "agent_knowledge_token_file" in data
        else None,
        agent_knowledge_max_response_bytes=max_bytes,
        # Resolving a venv's Python symlink would select the system interpreter.
        second_reader_python=_absolute(data["second_reader_python"], resolve=False)
        if "second_reader_python" in data
        else None,
        second_reader_model=_absolute(data["second_reader_model"]) if "second_reader_model" in data else None,
    )


def parse_native_config(path: Path) -> NativeConfig:
    """No fallback to cwd, private local deployment, or environment config."""
    try:
        path = Path(path)
        if not path.is_absolute() or path.stat().st_size > 65536:
            _invalid()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        config = _parse(data)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        _invalid()
    return config


def load_native_config(path: Path) -> NativeConfig:
    config = parse_native_config(path)
    try:
        return replace(config, workspace_id=_workspace_id(config.data_root, config.workspace_id))
    except (OSError, ValueError):
        _invalid()


def research_budget_root(config: NativeConfig) -> Path | None:
    """Locate the explicitly configured execution ledger before publishing a workspace identity."""
    path = config.research_config
    if path is None or not path.exists():
        return None
    try:
        budget = tomllib.loads(path.read_text()).get("budget_root")
        return path.parent / budget if budget else None
    except (OSError, ValueError, TypeError):
        message = "Legacy research identity/configuration is invalid."
        raise NativeError(message) from None


def _research_identity(data: dict[str, Any], config: NativeConfig, directory: Path) -> tuple[str, Path | None]:
    project_id = data.get("project_id", config.workspace_id)
    if data.get("workspace_id", config.workspace_id) != config.workspace_id:
        raise ValueError
    if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
        raise ValueError
    if data.get("model_config") and (directory / data["model_config"]).resolve() != config.model_config:
        raise ValueError
    budget_root = directory / data["budget_root"] if data.get("budget_root") else None
    return project_id, budget_root


def research_identity(config: NativeConfig) -> tuple[str, Path | None]:
    """Read the optional historical research binding before composing host services."""
    path = config.research_config
    if path is None or not path.exists():
        return config.workspace_id, None
    try:
        data = tomllib.loads(path.read_text())
        settings = _research_identity(data, config, path.parent)
    except (OSError, ValueError, TypeError):
        message = "Legacy research identity/configuration is invalid."
        raise NativeError(message) from None
    return settings
