"""Inventory and export a stopped instance to a private migration bundle."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import TypedDict

from .bundle import _check_archive, verify_bundle
from .database import _db_summary, _is_sqlite, _sqlite_copy
from .files import (
    BRANCH,
    FORMAT,
    _canonical,
    _fail,
    _file_hash,
    _files,
    _hash,
    _location,
    _read,
    _relative,
    _safe,
    _write,
)
from .receipts import _file_receipt, _receipts
from .source import _descriptor, _stopped_source


class LocalArchiveExportReader:
    """Read-only inventory adapter for a stopped local archive tree."""

    def __init__(self, root):
        root = Path(root).resolve()
        if not root.is_dir() or any(
            not (root / name).is_dir() or (root / name).is_symlink() for name in ("objects", "manifests", "commits")
        ):
            _fail("Existing source archive directories must be complete")
        from ..archive import LocalArchiveStore

        self.root = root
        self.store = LocalArchiveStore(root)

    def search(self, filters):
        if filters:
            _fail("Migration archive inventory must be complete")
        receipts = []
        for path in sorted((self.root / "commits").iterdir()):
            if path.is_symlink() or not path.is_file() or not re.fullmatch("[0-9a-f]{64}", path.name):
                _fail("Unknown historical archive commit")
            receipt = _read(path)
            self.store.read(receipt)
            for asset_id in receipt["assets"]:
                self.store.read_asset(receipt, asset_id)
            receipts.append(receipt)
        return receipts

    def read(self, receipt):
        return self.store.read(receipt)

    def read_asset(self, receipt, asset_id):
        return self.store.read_asset(receipt, asset_id)


def inventory(source_manifest, *, expected_instance_id):
    value = _descriptor(source_manifest, expected_instance_id)
    with _stopped_source(value), tempfile.TemporaryDirectory(prefix="kb-inventory-") as temporary:
        database = Path(temporary) / "state.sqlite"
        _sqlite_copy(_location(value, value["database"]), database)
        receipts = _receipts(database, data_root=_location(value, value["database"]).parent)
        counts = {name: sum(1 for _ in _files(Path(row["path"]))) for name, row in value["roots"].items()}
        return {
            "instance_id": value["instance_id"],
            "workspace_id": value["workspace_id"],
            "root_file_counts": counts,
            "database_tables": {name: row["count"] for name, row in _db_summary(database).items()},
            "receipt_count": len(receipts),
            "stopped": True,
        }


def _copy_source_files(bundle, value, source_db):
    files = {}
    databases = {}
    # Every SQLite file, including separate expense ledgers, gets a WAL-aware
    # snapshot. The descriptor's database names the main coordinator only.
    source_files = {name: list(_files(Path(row["path"]).resolve())) for name, row in value["roots"].items()}
    sqlite_files = {path for paths in source_files.values() for path in paths if _is_sqlite(path)}
    if source_db not in sqlite_files:
        _fail("Main coordinator is not a recognized SQLite database")
    sidecars = {Path(str(path) + suffix) for path in sqlite_files for suffix in ("-wal", "-shm")}
    for name, row in value["roots"].items():
        root = Path(row["path"]).resolve()
        for path in source_files[name]:
            if path in sidecars:
                continue
            relative = f"roots/{name}/{path.relative_to(root).as_posix()}"
            target = _safe(bundle, relative)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path in sqlite_files:
                _sqlite_copy(path, target)
                databases[relative] = _db_summary(target)
            else:
                shutil.copyfile(path, target)
                target.chmod(0o600)
                if _file_hash(path) != _file_hash(target):
                    _fail("Source changed while copying")
            files[relative] = {"sha256": _file_hash(target), "size": target.stat().st_size}
    return files, databases


def _collect_receipts(bundle, files, databases, db_relative):
    receipts = []
    file_receipts = {}
    for relative in databases:
        receipts.extend(_receipts(bundle / relative))
    for relative in files:
        if PurePosixPath(relative).name not in {"receipt.json", "publication.json"}:
            continue
        reference = _file_receipt(relative, _read(bundle / relative), PurePosixPath(db_relative).parent)
        if reference:
            kind, receipt = reference
            receipts.append(receipt)
            file_receipts[relative] = {"kind": kind, "snapshot_id": receipt["snapshot_id"]}
    return receipts, file_receipts


class _ArchiveEntry(TypedDict):
    package: str
    assets: dict[str, str]
    legacy_receipt: dict[str, object]


def _export_snapshot(bundle, archive_reader, receipt, snapshot) -> _ArchiveEntry:
    package = archive_reader.read(receipt)
    assets = {asset_id: archive_reader.read_asset(receipt, asset_id) for asset_id in receipt.get("assets", {})}
    _check_archive(package, assets, receipt)
    relative = f"archives/{snapshot}/package.json"
    _write(bundle / relative, package)
    paths = {}
    for asset_id, data in assets.items():
        asset_relative = f"archives/{snapshot}/assets/{_hash(asset_id.encode())}"
        target = _safe(bundle, asset_relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        target.chmod(0o600)
        paths[asset_id] = asset_relative
    return {"package": relative, "assets": paths, "legacy_receipt": receipt}


def _export_archives(bundle, value, receipts, archive_reader):
    if value["archive_backend"] != "none":
        if archive_reader is None:
            _fail("Complete historical archive export requires an explicit read-only archive adapter")
        receipts.extend(archive_reader.search({}))
    elif receipts:
        _fail("Receipts exist but the source declares no archive backend")
    archives: dict[str, _ArchiveEntry] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("status") != "ready":
            _fail("Unresolved or unknown archive receipt blocks migration")
        snapshot = receipt.get("snapshot_id", "")
        if not re.fullmatch("[0-9a-f]{64}", snapshot):
            _fail("Unknown archive snapshot identity")
        if snapshot in archives:
            if (
                receipt["package_sha256"] != archives[snapshot]["legacy_receipt"]["package_sha256"]
                or receipt["assets"] != archives[snapshot]["legacy_receipt"]["assets"]
            ):
                _fail("Conflicting receipts for one archive snapshot")
            continue
        archives[snapshot] = _export_snapshot(bundle, archive_reader, receipt, snapshot)
    return archives


def export_bundle(source_manifest, bundle, *, expected_instance_id, archive_reader=None):
    value = _descriptor(source_manifest, expected_instance_id)
    bundle = Path(bundle).resolve()
    if any(bundle.is_relative_to(Path(row["path"]).resolve()) for row in value["roots"].values()):
        _fail("Export destination must be outside all source roots")
    if bundle.exists() and any(bundle.iterdir()):
        _fail("Export requires a new empty private directory")
    bundle.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundle.chmod(0o700)
    with _stopped_source(value):
        db_relative = _relative(value["database"])
        source_db = _location(value, value["database"])
        files, databases = _copy_source_files(bundle, value, source_db)
        receipts, file_receipts = _collect_receipts(bundle, files, databases, db_relative)
        archives = _export_archives(bundle, value, receipts, archive_reader)
        manifest = {
            "format": FORMAT,
            "instance_id": value["instance_id"],
            "workspace_id": value["workspace_id"],
            "branch": BRANCH,
            "files": files,
            "file_receipts": file_receipts,
            "database": db_relative,
            "database_summary": databases[db_relative],
            "databases": databases,
            "archives": archives,
            "runtime_paths": value.get("runtime_paths", []),
            "roots": {name: {"roles": row["roles"]} for name, row in value["roots"].items()},
        }
        _write(bundle / "manifest.json", manifest)
        _write(bundle / "bundle-id.json", {"sha256": _hash(_canonical(manifest))})
    return verify_bundle(bundle)
