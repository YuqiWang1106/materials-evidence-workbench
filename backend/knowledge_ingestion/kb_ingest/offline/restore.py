"""Import a verified backup, publishing completion after target readback."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

from ..storage import sqlite
from ..storage.engine import file_connection
from .bundle import _manifest, verify_bundle
from .files import MigrationError, _canonical, _fail, _fsync_directory, _hash, _read, _remove_temporary, _safe, _write
from .receipts import _migrated_file
from .target import _archive_relative, _check_target_files, _mapped_bytes, _state, _verify_database, verify_target

_IMPORTER_BUSY = "Another importer owns this migration target"


def _import_state(target, value, bundle_id, mapping):
    if target.exists() and any(target.iterdir()):
        state = _state(target)
        if state.get("bundle_id") != bundle_id or state.get("path_mapping") != mapping:
            _fail("Target is nonempty or belongs to a different migration")
    else:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.chmod(0o700)
        state = {
            "bundle_id": bundle_id,
            "workspace_id": value["workspace_id"],
            "path_mapping": mapping,
            "receipts": {},
            "complete": False,
        }
        _write(target / ".migration/state.json", state)
    return state


def _prepare_staging(target):
    staging = target / ".migration/staging"
    staging.mkdir(exist_ok=True, mode=0o700)
    for leftover in staging.iterdir():
        if (
            not leftover.is_file()
            or leftover.is_symlink()
            or not leftover.name.startswith((".copy-", ".config-", ".migration-"))
        ):
            _fail("Unknown migration staging content")
        leftover.unlink()
    return staging


@contextmanager
def _staged_replacement(staging, prefix, destination):
    fd, name = tempfile.mkstemp(dir=staging, prefix=prefix)
    path = Path(name)
    try:
        yield fd
        path.replace(destination)
        _fsync_directory(destination.parent)
    finally:
        _remove_temporary(path)


def _native_config_ref(value):
    if value.get("source_kind") != "native":
        return None
    return next((ref for ref in value["runtime_paths"] if ref.get("pointer") == ["data_root"]), None)


def _is_native_config(relative, value):
    from .files import _relative

    ref = _native_config_ref(value)
    return ref is not None and relative == _relative(ref)


def _publish_native_config(bundle, target, value, state):
    from .files import _relative

    ref = _native_config_ref(value)
    if ref is None:
        _fail("Native backup must declare its configuration")
    relative = _relative(ref)
    destination = _safe(target, relative)
    original = (bundle / relative).read_bytes()
    mapped = _mapped_bytes(original, relative, value, state["path_mapping"])
    if destination.exists():
        if destination.read_bytes() != mapped:
            _fail("Native configuration already exists with conflicting contents")
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = target / ".migration/staging"
    with _staged_replacement(staging, ".config-", destination) as fd, os.fdopen(fd, "wb") as output:
        output.write(mapped)
        output.flush()
        os.fsync(output.fileno())


def _copy_files(bundle, target, value, staging):
    for relative, _metadata in value["files"].items():
        destination = _safe(target, relative)
        if _is_native_config(relative, value):
            continue
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (
            _staged_replacement(staging, ".copy-", destination) as fd,
            os.fdopen(fd, "wb") as output,
            _safe(bundle, relative).open("rb") as source,
        ):
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())


def _restore_archives(bundle, target, value, state):
    # Import and read back every immutable package before touching receipts.
    from ..archive import LocalArchiveStore

    store = LocalArchiveStore(target / _archive_relative(value))
    for snapshot, archive in value["archives"].items():
        receipt = store.import_verified(
            _read(bundle / archive["package"]),
            {key: bundle / path for key, path in archive["assets"].items()},
            target / ".migration/pending" / snapshot,
            legacy_receipt=archive["legacy_receipt"],
            schema_profile=archive["legacy_receipt"].get("schema_profile")
            or (
                "knowledge-publication-v1"
                if archive["legacy_receipt"].get("schema_mainfile") == "knowledge_publication.schema.archive.yaml"
                else "scientific"
            ),
        )
        if _hash(_canonical(store.read(receipt))) != archive["legacy_receipt"]["package_sha256"]:
            _fail("Imported immutable archive failed package readback")
        for asset_id in archive["assets"]:
            store.read_asset(receipt, asset_id)
        state["receipts"][snapshot] = receipt
    _write(target / ".migration/state.json", state)


def _restore_database_receipts(bundle, target, value, state):
    # Baseline verification catches foreign/tampered rows before receipt rewrite.
    # Compare semantic receipts, because original formatting is also preserved.
    for relative in value["databases"]:
        _verify_database(bundle / relative, target / relative, state["receipts"])
    for relative in value["databases"]:
        with file_connection(target / relative) as db:
            sqlite.begin_immediate(db)
            for table in sqlite.table_names(db):
                if "receipt" not in sqlite.column_names(db, table):
                    continue
                for rowid, raw in sqlite.receipt_rows(db, table):
                    old = json.loads(raw)
                    if isinstance(old, dict) and old.get("snapshot_id") in state["receipts"]:
                        receipt = _canonical(state["receipts"][old["snapshot_id"]]).decode()
                        sqlite.replace_receipt(db, table, rowid, receipt)


def _restore_files(bundle, target, value, state, mapping):
    staging = target / ".migration/staging"
    for relative in value["files"]:
        destination = target / relative
        original = (bundle / relative).read_bytes()
        if _is_native_config(relative, value):
            continue
        if relative in value["file_receipts"]:
            migrated = _migrated_file(json.loads(original), value["file_receipts"][relative], state["receipts"])
            if destination.read_bytes() != original and _read(destination) != migrated:
                _fail("File publication metadata changed during migration")
            _write(destination, migrated, staging=staging)
        else:
            mapped = _mapped_bytes(original, relative, value, mapping)
            if mapped != original:
                if destination.read_bytes() not in {original, mapped}:
                    _fail("Runtime config changed during migration")
                with _staged_replacement(staging, ".config-", destination) as fd, os.fdopen(fd, "wb") as output:
                    output.write(mapped)
                    output.flush()
                    os.fsync(output.fileno())


def _restore_locked(bundle, target, value, state, mapping):
    staging = _prepare_staging(target)
    if state.get("complete") or (value.get("source_kind") == "native" and state.get("prepared")):
        return verify_target(bundle, target, _allow_incomplete=True)

    _copy_files(bundle, target, value, staging)
    _restore_archives(bundle, target, value, state)
    _restore_database_receipts(bundle, target, value, state)
    _restore_files(bundle, target, value, state, mapping)
    result = verify_target(bundle, target, _allow_incomplete=True)
    if value.get("source_kind") == "native":
        state["prepared"] = True
    else:
        state["complete"] = True
    _write(target / ".migration/state.json", state)
    return result


def _native_mapping(bundle, value, target, mapping):
    refs = value.get("runtime_paths", [])
    data_ref = next((ref for ref in refs if ref.get("pointer") == ["data_root"]), None)
    if data_ref is None:
        _fail("Native backup must declare its data root mapping")
    native = tomllib.loads(_safe(bundle, "roots/" + data_ref["root"] + "/" + data_ref["path"]).read_text())
    original_root = native.get("data_root")
    if not isinstance(original_root, str) or not Path(original_root).is_absolute():
        _fail("Native source data root is invalid")
    source_root = Path(original_root).resolve()
    if target == source_root or target.is_relative_to(source_root) or source_root.is_relative_to(target):
        _fail("Import target and original data root must be separate trees")
    original_config = value.get("native_source_config_root")
    if not isinstance(original_config, str) or not Path(original_config).is_absolute():
        _fail("Native source configuration root is invalid")
    config_root = Path(original_config).resolve()
    if target == config_root or target.is_relative_to(config_root) or config_root.is_relative_to(target):
        _fail("Import target and source configuration root must be separate trees")
    mapped_root = mapping.get(original_root)
    if (
        not mapped_root
        or ".." in Path(mapped_root).parts
        or Path(mapped_root).resolve() != (target / "roots/data").resolve()
    ):
        _fail("Native data root must map to the restored target")
    mapping[original_root] = str((target / "roots/data").resolve())
    _native_file_mapping(native, "model_config", "native_model_config_relative", value, target, mapping)
    _native_file_mapping(native, "agent_knowledge_token_file", "native_agent_token_relative", value, target, mapping)


def _native_file_mapping(native, key, field, value, target, mapping):
    if not native.get(key):
        return
    mapped = mapping.get(native[key])
    relative = value.get(field)
    if not isinstance(relative, str) or not relative or ".." in Path(relative).parts:
        _fail("Native configuration reference is not mapped to a backed-up file")
    backed_up = "roots/config/" + relative
    expected = _safe(target / "roots/config", relative)
    if (
        backed_up not in value["files"]
        or not mapped
        or ".." in Path(mapped).parts
        or Path(mapped).resolve() != expected.resolve()
    ):
        _fail("Native configuration reference must map to the backed-up file")
    mapping[native[key]] = str(expected.resolve())


def _restore_required(target, value, bundle_id):
    root = target / "roots/data"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = {
        "format": "native-restore-v1",
        "backup_bundle": bundle_id,
        "target_root": str(root.resolve()),
        "workspace_id": value["workspace_id"],
    }
    required = root / ".restore-required.json"
    if required.exists():
        if _read(required) != marker:
            _fail("Native restore marker conflicts with this target")
    else:
        _write(required, marker)
    return marker


def import_bundle(bundle, target, *, path_mapping=None):
    verify_bundle(bundle)
    bundle, value, bundle_id = _manifest(bundle)
    target = Path(target).resolve()
    mapping = dict(path_mapping or {})
    if target == bundle or target.is_relative_to(bundle) or bundle.is_relative_to(target):
        _fail("Import target and backup must be separate trees")
    if any(
        not isinstance(key, str)
        or not isinstance(path, str)
        or not Path(key).is_absolute()
        or not Path(path).is_absolute()
        for key, path in mapping.items()
    ):
        _fail("Runtime path mappings require explicit absolute paths")
    if value.get("source_kind") == "native":
        _native_mapping(bundle, value, target, mapping)
    state = _import_state(target, value, bundle_id, mapping)
    _check_target_files(target, value)
    lock = target / ".migration/import.lock"
    lock.touch(mode=0o600, exist_ok=True)
    with lock.open("rb") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError(_IMPORTER_BUSY) from None
        native = value.get("source_kind") == "native"
        marker = _restore_required(target, value, bundle_id) if native else None
        result = _restore_locked(bundle, target, value, state, mapping)
        if native:
            _publish_native_config(bundle, target, value, state)
            result = verify_target(bundle, target, _allow_incomplete=True)
            state["complete"] = True
            _write(target / ".migration/state.json", state)
            _write(target / "roots/data/.restore-complete.json", marker)
            from ..core.restore_gate import require_restore_complete

            require_restore_complete(target / "roots/data")
        _write(
            target / ".migration/complete.json",
            {"bundle_id": bundle_id, "workspace_id": value["workspace_id"], "verified": True},
        )
        return verify_target(bundle, target) if native else result
