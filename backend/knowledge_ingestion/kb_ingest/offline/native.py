"""Preserve a verified native workspace without inventing legacy identities."""

import shutil
import tempfile
from pathlib import Path

from .bundle import verify_bundle
from .database import _sqlite_copy
from .export import LocalArchiveExportReader, _collect_receipts, _copy_source_files, _export_archives
from .files import FORMAT, _canonical, _fail, _file_hash, _files, _fsync_directory, _hash, _relative, _safe, _write
from .receipts import _receipts


def _paths(config_path, bundle):
    from ..core.native_config import parse_native_config

    config_path = Path(config_path)
    if not config_path.is_absolute() or config_path.is_symlink() or not config_path.is_file():
        _fail("Explicit native config must be an absolute regular file")
    root, config_root = parse_native_config(config_path).data_root, config_path.parent.resolve()
    if root.is_symlink() or not root.is_dir():
        _fail("Existing native roots are required")
    if root == config_root or root in config_root.parents or config_root in root.parents:
        _fail("Overlapping root layouts are not supported")
    bundle = Path(bundle).resolve()
    if bundle.is_relative_to(root) or bundle.is_relative_to(config_root):
        _fail("Backup destination overlaps a source root")
    if bundle.exists() or bundle.is_symlink():
        _fail("Backup destination already exists")
    if not bundle.parent.is_dir():
        _fail("Backup destination parent must already exist")
    return config_path, root, config_root, bundle


def _identity(config_path, root, config_root, expected_workspace_id):
    import tomllib

    from ..core.native_config import load_native_config
    from ..core.workspace import saved_workspace_id
    from ..native_identity import authentication

    config_hash = _file_hash(config_path)
    config = load_native_config(config_path)
    persisted = saved_workspace_id(root)
    if (
        config.data_root != root
        or not persisted
        or persisted != expected_workspace_id
        or config.workspace_id != persisted
    ):
        _fail("Native persisted workspace identity does not match the selected instance")
    if authentication(root) is None:
        _fail("Native authentication state is missing")
    for name in (".restore-required.json", ".restore-complete.json"):
        marker = root / name
        if marker.exists() or marker.is_symlink():
            _fail("Previously restored workspaces require explicit restore-marker preservation")
    if config.research_config or config.entity_budget_root:
        _fail("External native state needs explicit inventory")
    raw = tomllib.loads(config_path.read_text())
    model_relative = _declared_file(config_root, raw, "model_config", config.model_config)
    token_relative = _declared_file(config_root, raw, "agent_knowledge_token_file", config.agent_knowledge_token_file)
    return config_hash, persisted, model_relative, token_relative


def _declared_file(config_root, raw, key, resolved):
    if resolved is None:
        return None
    original = Path(raw[key])
    if not original.is_absolute() or not original.is_relative_to(config_root):
        _fail("External native configuration needs explicit inventory")
    relative = original.relative_to(config_root).as_posix()
    if _safe(config_root, relative) != resolved or not resolved.is_file():
        _fail("Native configuration reference is not a safe file")
    return relative


def _publish_native(bundle, root, config_root, config_path, persisted, config_hash, model_relative, token_relative):
    value = {
        "workspace_id": persisted,
        "archive_backend": "local" if (root / "archive").exists() else "none",
        "roots": {"data": {"path": str(root), "roles": ["data", "budget"]}},
        "database": {"root": "data", "path": "coordination.sqlite3"},
    }
    archive = LocalArchiveExportReader(root / "archive") if value["archive_backend"] == "local" else None
    with tempfile.TemporaryDirectory(prefix="native-preflight-") as temporary:
        temp = Path(temporary)
        snapshot = temp / "snapshot.sqlite3"
        _sqlite_copy(root / "coordination.sqlite3", snapshot)
        _receipts(snapshot, data_root=root)
        stage = temp / "config"
        stage.mkdir(mode=0o700)
        relative_config = config_path.relative_to(config_root).as_posix()
        for relative in dict.fromkeys(
            [relative_config]
            + ([model_relative] if model_relative else [])
            + ([token_relative] if token_relative else [])
        ):
            original = _safe(config_root, relative)
            target = _safe(stage, relative)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            before = _file_hash(original)
            shutil.copyfile(original, target)
            target.chmod(0o600)
            if before != _file_hash(original) or before != _file_hash(target):
                _fail("Native configuration changed during backup")
        if config_hash != _file_hash(config_path):
            _fail("Native configuration changed during backup")
        value["roots"]["config"] = {"path": str(stage), "roles": ["config"]}
        bundle.mkdir(parents=True, exist_ok=True, mode=0o700)
        bundle.chmod(0o700)
        db_relative = _relative(value["database"])
        files, databases = _copy_source_files(bundle, value, root / "coordination.sqlite3")
        receipts, file_receipts = _collect_receipts(bundle, files, databases, db_relative)
        archives = _export_archives(bundle, value, receipts, archive)
        pointers = (
            ["data_root"]
            + (["model_config"] if model_relative else [])
            + (["agent_knowledge_token_file"] if token_relative else [])
        )
        runtime_paths = [
            {"root": "config", "path": relative_config, "pointer": [key], "format": "toml"} for key in pointers
        ]
        manifest = {
            "format": FORMAT,
            "source_kind": "native",
            "workspace_id": persisted,
            "files": files,
            "file_receipts": file_receipts,
            "database": db_relative,
            "database_summary": databases[db_relative],
            "databases": databases,
            "archives": archives,
            "native_model_config_relative": model_relative,
            "native_agent_token_relative": token_relative,
            "native_source_config_root": str(config_root),
            "runtime_paths": runtime_paths,
            "roots": {name: {"roles": row["roles"]} for name, row in value["roots"].items()},
        }
        _write(bundle / "manifest.json", manifest)
        _write(bundle / "bundle-id.json", {"sha256": _hash(_canonical(manifest))})
        return verify_bundle(bundle)


def _sync_bundle(bundle):
    import os

    for path in _files(bundle):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def fail(_error):
        _fail("Cannot synchronize backup directories")

    for directory, _dirs, _files_in_directory in os.walk(bundle, topdown=False, onerror=fail, followlinks=False):
        _fsync_directory(Path(directory))


def _exclusive_publish(bundle, root, config_root, config_path, persisted, config_hash, model_relative, token_relative):
    stage = Path(tempfile.mkdtemp(prefix=".native-backup-", dir=bundle.parent))
    try:
        result = _publish_native(
            stage, root, config_root, config_path, persisted, config_hash, model_relative, token_relative
        )
        _sync_bundle(stage)
        bundle.mkdir(mode=0o700)
        stage.rename(bundle)
        _fsync_directory(bundle.parent)
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def export_native_bundle(config_path, bundle, *, expected_workspace_id):
    """Limited prototype for a single native workspace; requires existing owner lock."""
    import fcntl
    import os
    import stat

    from .files import MigrationError

    try:
        config_path, root, config_root, bundle = _paths(config_path, bundle)
        fd = os.open(root / ".native-owner.lock", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                _fail("Native owner lock is not a regular file")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            config_hash, persisted, model_relative, token_relative = _identity(
                config_path, root, config_root, expected_workspace_id
            )
            return _exclusive_publish(
                bundle, root, config_root, config_path, persisted, config_hash, model_relative, token_relative
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        message = "Native backup blocked by missing or invalid source evidence"
        raise MigrationError(message) from exc
