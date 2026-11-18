"""Server-side model configuration; never discovers or imports another repository."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Never

from . import paths
from .endpoints import PROTOCOLS, normalize_base_url
from .errors import ConfigurationError
from .native_config import NativeConfig as NativeConfig
from .native_config import load_native_config as load_native_config

DEFAULT_DOCUMENT_MODEL = "gpt-6-luna"
DEFAULT_API_URL = "https://api.openai.com/v1"
REASONING_EFFORTS = frozenset({"omit", "none", "minimal", "low", "medium", "high", "xhigh"})


@dataclass(frozen=True)
class ModelCredentials:
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_API_URL
    model: str = DEFAULT_DOCUMENT_MODEL
    protocol: str = "responses"
    dimensions: int | None = None
    reasoning_effort: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"api_key": self.api_key, "base_url": self.base_url, "protocol": self.protocol}
        if self.model:
            result["model"] = self.model
        if self.dimensions is not None:
            result["dimensions"] = self.dimensions
        if self.reasoning_effort is not None:
            result["reasoning_effort"] = self.reasoning_effort
        return result


@dataclass(frozen=True)
class ModelSettings:
    documents: ModelCredentials
    research: ModelCredentials | None = None
    table: ModelCredentials | None = None
    wiki: ModelCredentials | None = None
    embedding: ModelCredentials | None = None

    def __post_init__(self) -> None:
        for name, model in (
            ("research", "gpt-6-luna"),
            ("table", "gpt-6-sol"),
            ("wiki", "gpt-6-luna"),
            ("embedding", "text-embedding-3-small"),
        ):
            if getattr(self, name) is None:
                object.__setattr__(
                    self, name, replace(self.documents, model=model, dimensions=1536 if name == "embedding" else None)
                )


def default_config_path() -> Path | None:
    """Anchor source checkouts to their own distribution; wheels use explicit config/env."""
    root = paths.package_root().parent
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)
    except (OSError, ValueError):
        return None
    if project.get("project", {}).get("name") == "materials-knowledge-ingestion":
        return root / "config.local.toml"
    return None


def _reject(message: str, code: str = "model_config_invalid") -> Never:
    raise ConfigurationError(message, code) from None


def _text(section: Mapping[str, Any], name: str, default: str = "") -> str:
    value = section.get(name, default)
    if not isinstance(value, str):
        _reject("API config values must be strings")
    return value.strip()


def _from_file(path: Path) -> ModelSettings:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        _reject("API config file not found; check --config or KB_CONFIG_FILE")
    except (OSError, ValueError):
        _reject("Cannot read API config; check file permissions and TOML syntax")
    names = {"api", "documents", "research", "table", "wiki", "embedding"}
    if set(data) - names:
        _reject("API config contains unsupported sections")
    for name, section in data.items():
        allowed = {"api_key", "base_url", "protocol"}
        if name != "api":
            allowed.add("model")
        if name == "embedding":
            allowed.add("dimensions")
        if name == "research":
            allowed.add("reasoning_effort")
        if not isinstance(section, dict) or set(section) - allowed:
            _reject("API config contains unsupported fields or section types")
    common = _credentials(data.get("api", {}), ModelCredentials())
    values = {}
    for name, model in (
        ("documents", DEFAULT_DOCUMENT_MODEL),
        ("research", "gpt-6-luna"),
        ("table", "gpt-6-sol"),
        ("wiki", "gpt-6-luna"),
        ("embedding", "text-embedding-3-small"),
    ):
        values[name] = _credentials(
            data.get(name, {}), replace(common, model=model, dimensions=1536 if name == "embedding" else None)
        )
    return ModelSettings(**values)


def _credentials(section: Mapping[str, Any], inherited: ModelCredentials) -> ModelCredentials:
    key = _text(section, "api_key", inherited.api_key)
    if key and any(ord(c) < 33 or ord(c) > 126 for c in key):
        _reject("API credential format is invalid")
    try:
        endpoint = normalize_base_url(_text(section, "base_url", inherited.base_url) or DEFAULT_API_URL)
    except ValueError:
        _reject("API base_url must be a valid API root without credentials, query or endpoint")
    protocol = _text(section, "protocol", inherited.protocol)
    # A service with an unsupported protocol is unavailable independently; other
    # explicitly configured services must not be disabled by this one field.
    if not protocol or len(protocol) > 80 or any(ord(c) < 33 or ord(c) > 126 for c in protocol):
        _reject("API protocol format is invalid", code="unsupported_protocol")
    model = _text(section, "model", inherited.model) or inherited.model
    if len(model) > 200 or any(ord(c) < 33 or ord(c) > 126 for c in model):
        _reject("API model identifier is invalid")
    dimensions = section.get("dimensions", inherited.dimensions)
    if dimensions is not None and (type(dimensions) is not int or not 1 <= dimensions <= 65536):
        _reject("Embedding dimensions must be a positive integer no greater than 65536")
    effort = section.get("reasoning_effort", inherited.reasoning_effort)
    if effort is not None and (type(effort) is not str or effort not in REASONING_EFFORTS):
        _reject("Research reasoning_effort must be a supported effort name")
    return ModelCredentials(key, endpoint, model, protocol, dimensions, effort)


def credential_status(credentials: object) -> dict[str, Any]:
    """Read-only local configuration check, never an online provider probe."""
    if not isinstance(credentials, ModelCredentials):
        return {"ready": False, "code": "model_config_invalid"}
    if credentials.protocol not in PROTOCOLS:
        return {"ready": False, "code": "unsupported_protocol"}
    if not credentials.api_key:
        return {"ready": False, "code": "model_credentials_missing"}
    try:
        _credentials({}, credentials)
    except ConfigurationError:
        return {"ready": False, "code": "model_config_invalid"}
    return {"ready": True, "code": "model_ready"}


def _environment_group(environ: Mapping[str, str], groups: tuple[tuple[str, str, str], ...]) -> ModelCredentials:
    # Select endpoint and credential as a pair. Never inherit a lower-priority URL.
    for key_name, url_name, model_name in groups:
        key = environ.get(key_name, "").strip()
        if key:
            protocol_name = "KB_MODEL_PROTOCOL" if key_name == "KB_MODEL_API_KEY" else "OPENAI_API_PROTOCOL"
            return _credentials(
                {
                    "api_key": key,
                    "base_url": environ.get(url_name, "").strip() or DEFAULT_API_URL,
                    "model": environ.get(model_name, "").strip() if model_name else "",
                    "protocol": environ.get(protocol_name, "responses"),
                },
                ModelCredentials(),
            )
    return ModelCredentials()


def load_model_settings(config_path: Path | None = None, *, environ: Mapping[str, str] | None = None) -> ModelSettings:
    """Explicit file > project file > environment; a present empty file disables API use.

    This loader does not modify os.environ and does not read .env files or CWD parents.
    Missing explicit files fail rather than silently using an unrelated credential.
    """
    values = os.environ if environ is None else environ
    explicit = config_path if config_path is not None else values.get("KB_CONFIG_FILE")
    if explicit is not None:
        return _from_file(Path(explicit).expanduser())
    default = default_config_path()
    if default is not None:
        # A dangling symlink is still a configured path: report its missing target.
        try:
            default.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            _reject("Cannot inspect project API config; check permissions")
        else:
            return _from_file(default)
    common = ("KB_MODEL_API_KEY", "KB_MODEL_URL", "KB_MODEL")
    openai = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
    return ModelSettings(_environment_group(values, (common, openai)))
