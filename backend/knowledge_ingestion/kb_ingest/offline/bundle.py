"""Validate v1 bundle manifests, files, databases and archive references."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .database import _db_summary
from .files import FORMAT, _canonical, _fail, _file_hash, _files, _hash, _read, _safe
from .receipts import _file_receipt, _receipts


def _check_archive(package, assets, receipt):
    if receipt.get("status") != "ready":
        _fail("Archive receipt is not ready")
    if _hash(_canonical(package)) != receipt.get("package_sha256"):
        _fail("Archive package differs from its pinned receipt")
    if set(assets) != set(receipt.get("assets", {})):
        _fail("Archive assets are incomplete")
    for asset_id, data in assets.items():
        metadata = receipt["assets"][asset_id]
        if _hash(data) != metadata.get("sha256") or len(data) != metadata.get("size"):
            _fail("Archive asset differs from its pinned receipt")
    if _hash(_canonical({"package_sha256": receipt["package_sha256"], "assets": receipt["assets"]})) != receipt.get(
        "snapshot_id"
    ):
        _fail("Archive snapshot identity differs from its pinned receipt")


def _manifest(bundle):
    bundle = Path(bundle).resolve()
    value = _read(bundle / "manifest.json")
    if (
        not isinstance(value.get("file_receipts"), dict)
        or value.get("format") != FORMAT
        or _read(bundle / "bundle-id.json").get("sha256") != _hash(_canonical(value))
    ):
        _fail("Unknown format or damaged migration manifest")
    return bundle, value, _hash(_canonical(value))


def _verify_files(bundle, value):
    expected = {"manifest.json", "bundle-id.json"}
    for relative, metadata in value["files"].items():
        path = _safe(bundle, relative)
        expected.add(relative)
        if not path.is_file() or path.stat().st_size != metadata["size"] or _file_hash(path) != metadata["sha256"]:
            _fail("Migration file is missing or damaged")
    for snapshot, archive in value["archives"].items():
        expected.add(archive["package"])
        expected.update(archive["assets"].values())
        _check_archive(
            _read(_safe(bundle, archive["package"])),
            {key: _safe(bundle, path).read_bytes() for key, path in archive["assets"].items()},
            archive["legacy_receipt"],
        )
        if archive["legacy_receipt"]["snapshot_id"] != snapshot:
            _fail("Archive manifest identity mismatch")
    actual = {path.relative_to(bundle).as_posix() for path in _files(bundle)}
    if actual != expected:
        _fail("Bundle contains unmanifested or missing files")


def _verify_receipts(bundle, value):
    required_receipts = {}
    for relative in value["files"]:
        if PurePosixPath(relative).name not in {"receipt.json", "publication.json"}:
            continue
        reference = _file_receipt(relative, _read(bundle / relative), PurePosixPath(value["database"]).parent)
        if reference:
            kind, receipt = reference
            required_receipts[relative] = {"kind": kind, "snapshot_id": receipt["snapshot_id"]}
            if receipt["snapshot_id"] not in value["archives"]:
                _fail("File archive reference is absent from bundle")
    if required_receipts != value["file_receipts"]:
        _fail("Typed file receipt inventory differs from manifest")
    for relative in value["databases"]:
        for receipt in _receipts(bundle / relative):
            if receipt["snapshot_id"] not in value["archives"]:
                _fail("Database archive reference is absent from bundle")


def verify_bundle(bundle):
    bundle, value, bundle_id = _manifest(bundle)
    _verify_files(bundle, value)
    for relative, summary in value["databases"].items():
        if _db_summary(bundle / relative) != summary:
            _fail("Database rows differ from the bundle manifest")
    _verify_receipts(bundle, value)
    return {
        "verified": True,
        "bundle_id": bundle_id,
        "workspace_id": value["workspace_id"],
        "files": len(value["files"]),
        "archives": len(value["archives"]),
    }
