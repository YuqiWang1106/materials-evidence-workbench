"""Read-only preflight checks for the native website and its readers."""

from __future__ import annotations

import importlib
import os
import platform
import socket
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from .core.config import ModelCredentials, ModelSettings, NativeConfig, credential_status, load_model_settings
from .core.errors import ConfigurationError, NativeError
from .core.native_config import research_budget_root, research_identity
from .core.workspace import matches_database_workspaces, saved_workspace_id
from .storage import revision_check as _revisions
from .storage import sqlite

if TYPE_CHECKING:
    from .materials_host import MaterialsHostConfig

_CAPABILITIES = ("documents", "research", "table", "wiki", "embedding")


def model_settings(config: NativeConfig) -> ModelSettings:
    # An omitted explicit file is unconfigured, never a project/env credential search.
    if config.model_config is None:
        return ModelSettings(ModelCredentials())
    return load_model_settings(config.model_config, environ={})


def research_config(config: NativeConfig, tokens: dict[str, str] | None = None) -> MaterialsHostConfig:
    from .materials_host import MaterialsHostConfig

    project_id, legacy_root = research_identity(config)
    tokens = tokens or {"session_token": ""}
    return MaterialsHostConfig(
        config.workspace_id, project_id, (), config.data_root, legacy_root, config.model_config, tokens["session_token"]
    )


def _workspace_identity(config: NativeConfig, database: Path | None) -> tuple[NativeConfig, bool]:
    """Check the database first: an unreadable database is not an identity mismatch."""
    owners = sqlite.workspace_ids(database) if database is not None else set()
    saved = saved_workspace_id(config.data_root)
    workspace = config.workspace_id or saved or ""
    if saved not in (None, workspace) or not matches_database_workspaces(config.data_root, workspace, owners):
        return config, False
    return replace(config, workspace_id=workspace), True


def verified_workspace(config: NativeConfig, database: Path | None) -> NativeConfig:
    config, matched = _workspace_identity(config, database)
    if not matched:
        message = "Existing workspace identity does not match this data root."
        raise NativeError(message)
    return config


def data_checks(config: NativeConfig, *, wait: bool) -> tuple[NativeConfig, bool, dict[str, str]]:
    """Return the identity result separately from the state of the database itself."""
    try:
        # A configured external execution ledger is a startup input; its fee data is irrelevant.
        with _revisions.external_database(research_budget_root(config), config.data_root, wait=wait):
            pass
        with _revisions.locked_database(config.data_root, wait=wait) as copied:
            config, matched = _workspace_identity(config, copied)
            if copied is None:
                return config, matched, {"status": "not_initialized"}
            return config, matched, {"status": "ready", "check": "quick_check"}
    except _revisions.IncompatibleRevisionError:
        return config, False, {"status": "not_ready", "reason": "incompatible_workspace_revision"}
    except BlockingIOError:
        return config, False, {"status": "not_ready", "reason": "workspace_initializing"}
    except (OSError, ValueError, sqlite.DatabaseError):
        return config, False, {"status": "not_ready", "reason": "existing_database_or_identity_unavailable"}


def _website_dependencies() -> dict[str, object]:
    missing = []
    for module in (
        "sqlalchemy",
        "alembic",
        "httpx",
        "fastapi",
        "uvicorn",
        "python_multipart",
        "yaml",
        "openpyxl",
        "xlrd",
        "pymupdf",
    ):
        # Mixed BaseExceptionGroups must propagate intact, including initialization errors.
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001
            missing.append(module)
    return {"status": "not_ready", "unavailable": missing} if missing else {"status": "ready"}


def _data_directory(root: Path) -> dict[str, str]:
    unavailable = {"status": "not_ready", "reason": "data_parent_write_or_traverse_unavailable"}
    try:
        nearest = root
        while not nearest.exists():
            if nearest == nearest.parent:
                return unavailable
            nearest = nearest.parent
        if not nearest.is_dir() or not os.access(nearest, os.W_OK | os.X_OK):
            return unavailable
        if any(not os.access(parent, os.X_OK) for parent in nearest.parents):
            return unavailable
    except OSError:
        return unavailable
    return {"status": "ready"}


def port_ready(config: NativeConfig) -> dict[str, str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((config.host, config.port))
    except OSError:
        return {"status": "not_ready", "reason": "loopback_port_occupied_or_unavailable"}
    return {"status": "ready"}


def agent_knowledge_token(config: NativeConfig) -> str | None:
    if config.agent_knowledge_token_file is None:
        return None
    message = "Read-only knowledge credential file is unavailable or invalid."
    try:
        token = config.agent_knowledge_token_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError, ValueError):
        raise NativeError(message) from None
    if not token or any(char.isspace() for char in token):
        raise NativeError(message) from None
    return token


def _check_identity(config: NativeConfig, result: dict[str, object]) -> None:
    try:
        result["agent_knowledge"] = {"status": "ready" if agent_knowledge_token(config) else "not_configured"}
    except NativeError:
        result["agent_knowledge"] = {"status": "not_ready", "reason": "readonly_credential_unavailable"}
    from .native_identity import authentication

    try:
        read_auth: Callable[[Path], dict[str, str] | None] = authentication
        auth = read_auth(config.data_root)
        result["initialization"] = {"status": "ready" if auth and config.workspace_id else "required"}
    except (OSError, ValueError, TypeError):
        result["initialization"] = {"status": "not_ready", "reason": "private_auth_state_invalid"}
        result["website"] = {"status": "not_ready", "reason": "private_auth_state_invalid"}
        result["status"] = "not_ready"


def _check_website(
    config: NativeConfig,
    result: dict[str, object],
    check_port: Callable[[NativeConfig], dict[str, str]],
    identity_ready: bool,
    database: dict[str, str],
) -> bool:
    if sys.version_info[:2] != (3, 12):
        result["website"] = {"status": "not_ready", "reason": "python_312_required"}
        result["status"] = "not_ready"
    result["database"] = database
    if not identity_ready:
        result["website"] = {"status": "not_ready", "reason": "existing_workspace_identity_unavailable"}
        result["status"] = "not_ready"
    dependencies = _website_dependencies()
    result["dependencies"] = dependencies
    directory = _data_directory(config.data_root)
    result["data_directory"] = directory
    port = check_port(config)
    result["port"] = port
    checks = (("dependencies", dependencies), ("data_directory", directory), ("database", database), ("port", port))
    blockers = [name for name, state in checks if state["status"] == "not_ready"]
    if blockers:
        result["website"] = {"status": "not_ready", "reason": "native_preflight_blocked", "blockers": blockers}
        result["status"] = "not_ready"
    return dependencies["status"] == "ready"


def _capabilities(config: NativeConfig) -> dict[str, dict[str, object]]:
    try:
        settings = model_settings(config)
        capabilities: dict[str, dict[str, object]] = {}
        for name in _CAPABILITIES:
            state = credential_status(getattr(settings, name))
            reason = {"model_credentials_missing": "api_key_missing", "model_ready": "configured_unverified"}.get(
                state["code"], state["code"]
            )
            if config.model_config is None:
                reason = "model_not_configured"
            capabilities[name] = {
                "status": "configured_unverified" if state["ready"] else "not_ready",
                "reason": reason,
                "provider_tested": False,
            }
    except ConfigurationError as error:
        return {
            name: {
                "status": "not_ready",
                "reason": getattr(error, "code", "model_config_invalid"),
                "provider_tested": False,
            }
            for name in _CAPABILITIES
        }
    return capabilities


def _models(config: NativeConfig, model_tool: Callable[[], ModuleType]) -> dict[str, object]:
    unavailable: dict[str, object] = {
        "status": "not_ready",
        "tier": "standard",
        "reason": "standard_runtime_or_model_files_unavailable",
    }
    if config.model_root is None or not config.mineru_command:
        return unavailable
    try:
        tool = model_tool()
        from .ingest.documents.readers.mineru_runtime import probe_mineru_runtime

        runtime = probe_mineru_runtime(list(config.mineru_command))
        if (
            runtime["status"] != "available"
            or not runtime["python"]["version"].startswith("3.12.")
            or any(
                runtime["packages"][name].get("status") == "not_installed"
                for name in ("onnxruntime", "mineru-llama-cpp")
            )
        ):
            return unavailable
        verified: dict[str, object] = tool.verify_models(
            config.model_root, config.model_root / "standard-model-manifest.json", command=config.mineru_command[0]
        )
    except (OSError, ValueError, KeyError, ImportError):
        return unavailable
    except Exception as error:
        # The external model verifier emits model-relative filenames; never relay them.
        if type(error).__name__ != "ModelError":
            raise
        return unavailable
    return verified


def _documents(api: dict[str, object], models: dict[str, object]) -> dict[str, object]:
    ready = api["status"] == "configured_unverified" and models["status"] == "ready"
    reason = "ready" if ready else (api["reason"] if api["status"] != "configured_unverified" else "parser_not_ready")
    return {"status": "ready" if ready else "not_ready", "tier": "standard", "reason": reason}


def doctor(
    config: NativeConfig,
    check_port: Callable[[NativeConfig], dict[str, str]],
    model_tool: Callable[[], ModuleType],
    local_ocr_status: Callable[[NativeConfig], dict[str, str]],
    *,
    wait: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "ready",
        "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "website": {"status": "ready"},
        "models": {"status": "not_ready", "tier": "standard"},
        "deprecated_fields": list(config.deprecated_fields),
        "research": {"status": "not_configured"},
        "inference_tested": False,
        "paid_provider_tested": False,
    }
    config, identity_ready, database = data_checks(config, wait=wait)
    _check_identity(config, result)
    if not _check_website(config, result, check_port, identity_ready, database):
        result["api"] = {"status": "not_checked"}
        result["documents"] = {"status": "not_ready", "tier": "standard", "reason": "website_dependencies_unavailable"}
        return result
    capabilities = _capabilities(config)
    result["capabilities"] = capabilities
    api = capabilities["documents"].copy()
    result["api"] = api
    result["research"] = capabilities["research"].copy()
    try:
        research_config(config)
    except NativeError:
        result["research"] = {"status": "not_ready", "reason": "legacy_identity_mismatch", "provider_tested": False}
    models = _models(config, model_tool)
    result["models"] = models
    result["documents"] = _documents(api, models)
    result["parsing"] = models.copy()
    result["local_ocr"] = local_ocr_status(config)
    return result
