"""Verify restored files, databases and immutable archives against their backup."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path, PurePosixPath

from ..storage import sqlite
from ..storage.engine import file_connection
from .bundle import _manifest, verify_bundle
from .database import _all_rows, _json_value, _receipted_rows
from .files import _canonical, _fail, _files, _hash, _read, _relative, _safe
from .receipts import _migrated_file


def _path_changes(config, refs, mapping):
    changes = []
    for ref in refs:
        owner = config
        for key in ref["pointer"][:-1]:
            owner = owner[key]
        key = ref["pointer"][-1]
        old = owner[key]
        if not isinstance(old, str):
            _fail("Runtime path field must contain a scalar path")
        if old in mapping:
            changes.append((ref["pointer"], mapping[old]))
            owner[key] = mapping[old]
    return changes


def _toml_assignment(lines, pointer):
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in pointer):
        _fail("TOML path mapping supports explicit ordinary keys only")
    section: list[str] = []
    matches = []
    for index, line in enumerate(lines):
        heading = re.fullmatch(r"\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?(?:\n)?", line)
        if heading:
            section = heading[1].split(".")
            continue
        assignment = re.match(r"^(\s*" + re.escape(pointer[-1]) + r'\s*=\s*)(["\']).*', line)
        if section == pointer[:-1] and assignment:
            matches.append((index, assignment[1]))
    if len(matches) != 1:
        _fail("TOML runtime path assignment cannot be rewritten unambiguously")
    return matches[0]


def _mapped_bytes(data, relative, value, mapping):
    refs = [ref for ref in value["runtime_paths"] if _relative(ref) == relative]
    if not refs or not mapping:
        return data
    formats = {ref.get("format", "json") for ref in refs}
    if len(formats) != 1:
        _fail("One configuration file must have one explicit format")
    format_name = next(iter(formats))
    config = tomllib.loads(data.decode()) if format_name == "toml" else json.loads(data)
    changes = _path_changes(config, refs, mapping)
    if format_name == "json":
        return _canonical(config)
    # Preserve TOML comments, unrelated scientific fields and secret values.
    # Only ordinary explicitly declared string assignments are rewritten.
    lines = data.decode().splitlines(keepends=True)
    for pointer, new in changes:
        index, prefix = _toml_assignment(lines, pointer)
        lines[index] = prefix + json.dumps(new, ensure_ascii=False) + "\n"
    result = "".join(lines).encode()
    if tomllib.loads(result.decode()) != config:
        _fail("TOML mapping changed undeclared configuration fields")
    return result


def _check_target_files(target, value):
    for path in _files(target):
        relative = path.relative_to(target).as_posix()
        if relative in value["files"]:
            continue
        if value.get("source_kind") == "native" and relative in {
            "roots/data/.restore-required.json",
            "roots/data/.restore-complete.json",
        }:
            continue
        if relative.startswith((_archive_relative(value) + "/", ".migration/")):
            continue
        _fail("Unknown file in migration target; refusing to overwrite or merge")


def _archive_relative(value):
    return str(PurePosixPath(value["database"]).parent / "archive")


def _state(target):
    return _read(target / ".migration/state.json")


def _legacy_receipts(before, table, columns):
    legacy_by_snapshot: dict[object, list[object]] = {}
    if "receipt" in columns:
        for (original,) in _receipted_rows(before, table, ["receipt"]):
            candidate = json.loads(original)
            if isinstance(candidate, dict):
                legacy_by_snapshot.setdefault(candidate.get("snapshot_id"), []).append(candidate)
    return legacy_by_snapshot


def _normalize_row(row, columns, receipts, legacy_by_snapshot, require_migrated):
    if "receipt" in columns and row[columns.index("receipt")]:
        index = columns.index("receipt")
        receipt = json.loads(row[index])
        snapshot = receipt.get("snapshot_id") if isinstance(receipt, dict) else None
        if snapshot in receipts:
            old = receipts[snapshot]
            if require_migrated and receipt != old:
                _fail("Target database receipt is not migrated")
            if receipt != old and receipt not in legacy_by_snapshot.get(snapshot, []):
                _fail("Receipt contents changed unexpectedly")
            row[index] = _canonical(old).decode()
    return _canonical(row)


def _verify_table(before, after, table, receipts, require_migrated):
    columns = sqlite.column_names(before, table)
    if columns != sqlite.column_names(after, table):
        _fail("Target database schema changed")
    if sqlite.table_definition(before, table) != sqlite.table_definition(after, table):
        _fail("Target table schema changed")
    legacy_by_snapshot = _legacy_receipts(before, table, columns)

    def rows(db, migrated):
        result = []
        for raw in _all_rows(db, table):
            row = [_json_value(item) for item in raw]
            result.append(_normalize_row(row, columns, receipts, legacy_by_snapshot, migrated))
        return sorted(result)

    if rows(before, False) != rows(after, require_migrated):
        _fail("Target identities, scientific content or budget rows changed")


def _verify_database(source, target, receipts, *, require_migrated=False):
    # Preserve every row/value, including identities and all settled/unknown
    # financial reservations. Only entire durable receipt columns may change.
    with file_connection(source, readonly=True) as before, file_connection(target, readonly=True) as after:
        if sqlite.schema_objects(before) != sqlite.schema_objects(after):
            _fail("Target database schema changed")
        if sqlite.table_names(before) != sqlite.table_names(after):
            _fail("Target database table inventory changed")
        for table in sqlite.table_names(before):
            _verify_table(before, after, table, receipts, require_migrated)
        if not sqlite.integrity_ok(after) or not sqlite.foreign_keys_ok(after):
            _fail("Target SQLite integrity failed")


def _verify_files(bundle, target, value, state, *, allow_incomplete=False):
    _check_target_files(target, value)
    for relative, _metadata in value["files"].items():
        destination = _safe(target, relative)
        native_ref = next((ref for ref in value["runtime_paths"] if ref.get("pointer") == ["data_root"]), None)
        native_config = value.get("source_kind") == "native" and native_ref and relative == _relative(native_ref)
        if native_config and allow_incomplete and not destination.exists():
            _mapped_bytes((bundle / relative).read_bytes(), relative, value, state["path_mapping"])
            continue
        if relative in value["databases"]:
            continue
        original = (bundle / relative).read_bytes()
        if relative in value["file_receipts"]:
            migrated = _migrated_file(json.loads(original), value["file_receipts"][relative], state["receipts"])
            if _read(destination) != migrated:
                _fail("Target file publication receipt/status was not migrated")
            continue
        expected = _mapped_bytes(original, relative, value, state["path_mapping"])
        if not destination.is_file() or destination.read_bytes() != expected:
            _fail("Target private data, configuration or ledger differs from backup")


def _verify_archives(bundle, target, value, state):
    from ..archive import LocalArchiveStore

    store = LocalArchiveStore(target / _archive_relative(value))
    if set(state["receipts"]) != set(value["archives"]):
        _fail("Target historical archives are incomplete")
    for snapshot, archive in value["archives"].items():
        receipt = state["receipts"][snapshot]
        if (
            receipt.get("snapshot_id") != snapshot
            or _hash(_canonical(store.read(receipt))) != archive["legacy_receipt"]["package_sha256"]
        ):
            _fail("Target archive readback failed")
        for asset_id, path in archive["assets"].items():
            if store.read_asset(receipt, asset_id) != (bundle / path).read_bytes():
                _fail("Target asset readback failed")


def _verify_native_restore_gate(root):
    from ..core.errors import NativeError
    from ..core.restore_gate import require_restore_complete

    if not (root / ".restore-required.json").is_file() or not (root / ".restore-complete.json").is_file():
        _fail("Native target restore evidence is missing")
    try:
        require_restore_complete(root)
    except NativeError:
        _fail("Native target restore evidence is invalid")


def verify_target(bundle, target, *, _allow_incomplete=False):
    verify_bundle(bundle)
    bundle, value, bundle_id = _manifest(bundle)
    target = Path(target).resolve()
    state = _state(target)
    if state.get("bundle_id") != bundle_id or state.get("workspace_id") != value["workspace_id"]:
        _fail("Target migration identity mismatch")
    complete = target / ".migration/complete.json"
    expected_complete = {"bundle_id": bundle_id, "workspace_id": value["workspace_id"], "verified": True}
    if complete.exists() and _read(complete) != expected_complete:
        _fail("Target completion marker identity mismatch")
    if not _allow_incomplete and (not state.get("complete") or not complete.exists()):
        _fail("Target migration is not complete")
    if not _allow_incomplete and value.get("source_kind") == "native":
        _verify_native_restore_gate(target / "roots/data")
    _verify_files(bundle, target, value, state, allow_incomplete=_allow_incomplete)
    for relative in value["databases"]:
        _verify_database(bundle / relative, target / relative, state["receipts"], require_migrated=True)
    _verify_archives(bundle, target, value, state)
    return {
        "verified": True,
        "bundle_id": bundle_id,
        "workspace_id": value["workspace_id"],
        "files": len(value["files"]),
        "archives": len(value["archives"]),
        "database_tables": {name: row["count"] for name, row in value["database_summary"].items()},
        "data_root": str(target / PurePosixPath(value["database"]).parent),
        "database": str(target / value["database"]),
        "archive_root": str(target / _archive_relative(value)),
    }
