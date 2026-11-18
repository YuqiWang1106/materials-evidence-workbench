"""Durable local formal archives; business SQL pointers remain caller-owned.

A pinned intent precedes every write. Content blobs and manifest are immutable;
only a fully read-back commit becomes visible. Reconciliation performs reads only.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from .core.clock import remaining
from .core.serialization import canonical
from .knowledge_snapshot import KnowledgeIntegrityError, KnowledgeSnapshotStore, checked_hash


class ArchiveError(RuntimeError):
    """Sanitized local archive failure."""


class ArchiveIntegrityError(ArchiveError):
    pass


class ArchiveWriteUnknown(ArchiveError):
    pass


def _bytes(value):
    try:
        return canonical(value).encode("utf-8")
    except (ValueError, TypeError):
        raise ArchiveIntegrityError("Archive requires canonical finite JSON") from None


def _safe_path(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ArchiveIntegrityError("Archive paths cannot traverse symlinks")


def _atomic(path, value):
    _safe_path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ArchiveIntegrityError("Archive intent cannot use symlinks")
    fd, name = tempfile.mkstemp(prefix=".atomic-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _load(path):
    _safe_path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ArchiveIntegrityError("Archive metadata cannot use symlinks")
    try:
        data = path.read_bytes()
        value = json.loads(data)
        if not isinstance(value, dict) or _bytes(value) != data:
            raise ArchiveIntegrityError("Archive metadata is not canonical JSON")
        return value
    except (ValueError, UnicodeError):
        raise ArchiveIntegrityError("Archive metadata is unreadable") from None


@contextmanager
def _intent_lock(root):
    _safe_path(root)
    if root.is_symlink():
        raise ArchiveIntegrityError("Archive intent cannot use symlinks")
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".lock"
    if path.is_symlink():
        raise ArchiveIntegrityError("Archive lock cannot use symlinks")
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class LocalArchiveStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        _safe_path(self.root)
        try:
            self.blobs = KnowledgeSnapshotStore(self.root)
            self._commits = self.root / "commits"
            if self._commits.is_symlink():
                raise ArchiveIntegrityError("Archive commits cannot use symlinks")
            self._commits.mkdir(exist_ok=True)
        except (OSError, KnowledgeIntegrityError):
            raise ArchiveIntegrityError("Archive root is unavailable or invalid") from None

    @staticmethod
    def _metadata(package):
        header = package.get("header") or {}
        sources = package.get("sources") or []
        source = sources[0] if sources and isinstance(sources[0], dict) else {}
        return {
            "source_id": str(source.get("source_id") or header.get("source_id") or header.get("record_id") or ""),
            "source_revision_id": str(header.get("source_revision_id") or source.get("source_revision_id") or ""),
            "record_revision_id": str(header.get("record_revision_id") or ""),
            "source_kind": str(source.get("source_type") or source.get("source_kind") or source.get("kind") or ""),
        }

    @staticmethod
    def _asset_identity(package, assets):
        declared = package.get("assets")
        if declared is None:
            return
        if isinstance(declared, list):
            if any(not isinstance(item, dict) or not isinstance(item.get("asset_id"), str) for item in declared):
                raise ArchiveIntegrityError("Package original identities are malformed")
            identities = {item["asset_id"]: item.get("sha256") for item in declared}
            if len(identities) != len(declared):
                raise ArchiveIntegrityError("Package original identities are duplicated")
        elif isinstance(declared, dict):
            if any(not isinstance(item, dict) for item in declared.values()):
                raise ArchiveIntegrityError("Package original identities are malformed")
            identities = {key: item.get("sha256") for key, item in declared.items()}
        else:
            raise ArchiveIntegrityError("Package originals must be a list or mapping")
        if identities != {key: item["sha256"] for key, item in assets.items()}:
            raise ArchiveIntegrityError("Package originals differ from archived bytes")

    def _manifest(self, receipt):
        remaining()
        try:
            if not isinstance(receipt, dict) or receipt.get("backend") != "local" or receipt.get("format_version") != 1:
                raise ArchiveIntegrityError("Receipt is not a local archive")
            digest = checked_hash(receipt.get("archive_manifest_sha256"))
            manifest = self.blobs.get_json(digest)
            if not isinstance(manifest, dict):
                raise ArchiveIntegrityError("Invalid archive manifest")
            if manifest.get("format") != "kb-local-archive-v1":
                raise ArchiveIntegrityError("Unknown archive manifest format")
            expected = self._receipt(manifest, digest)
            if receipt != expected:
                raise ArchiveIntegrityError("Receipt differs from the committed manifest")
            if (
                manifest["snapshot_id"]
                != sha256(
                    _bytes({"package_sha256": manifest["package_sha256"], "assets": manifest["assets"]})
                ).hexdigest()
            ):
                raise ArchiveIntegrityError("Logical snapshot identity changed")
            package = self.blobs.get_json(manifest["package_sha256"])
            if not isinstance(package, dict) or self._metadata(package) != manifest["metadata"]:
                raise ArchiveIntegrityError("Package identity changed")
            self._asset_identity(package, manifest["assets"])
            if set(manifest["blobs"]) != set(manifest["assets"]):
                raise ArchiveIntegrityError("Asset identities changed")
            for key, asset in manifest["assets"].items():
                remaining()
                content = self.blobs.get_blob(manifest["blobs"][key])
                if sha256(content).hexdigest() != asset["sha256"] or len(content) != asset["size"]:
                    raise ArchiveIntegrityError("Original asset bytes changed")
            return manifest, package
        except (KnowledgeIntegrityError, KeyError, TypeError, ValueError, OSError):
            raise ArchiveIntegrityError("Archive manifest or payload failed verification") from None

    @staticmethod
    def _receipt(manifest, digest):
        return {
            "backend": "local",
            "format_version": 1,
            "archive_manifest_sha256": digest,
            "snapshot_id": manifest["snapshot_id"],
            "package_sha256": manifest["package_sha256"],
            "schema_profile": manifest["schema_profile"],
            "assets": deepcopy(manifest["assets"]),
            **manifest["metadata"],
            "status": "ready",
            "readback_verified": True,
            "assets_verified": True,
        }

    def _committed(self, receipt):
        manifest, package = self._manifest(receipt)
        try:
            commit = _load(self._commits / checked_hash(receipt["archive_manifest_sha256"]))
            if commit != receipt:
                raise ArchiveIntegrityError("Archive completion marker changed")
        except (OSError, KnowledgeIntegrityError):
            raise ArchiveIntegrityError("Archive completion marker is unavailable") from None
        return manifest, package

    def read(self, receipt: dict) -> dict:
        return deepcopy(self._committed(receipt)[1])

    def read_asset(self, receipt: dict, asset_id: str) -> bytes:
        manifest, _ = self._committed(receipt)
        try:
            return self.blobs.get_blob(manifest["blobs"][asset_id])
        except (KeyError, KnowledgeIntegrityError):
            raise ArchiveIntegrityError("Archive asset is unavailable") from None

    def publish(self, package, assets, pending_dir, *, schema_profile=None):
        return self._publish(package, assets, pending_dir, schema_profile=schema_profile)

    def import_verified(self, package, assets, pending_dir, *, legacy_receipt, schema_profile=None):
        return self._publish(package, assets, pending_dir, schema_profile=schema_profile, legacy_receipt=legacy_receipt)

    def _publish(self, package, assets, pending_dir, *, schema_profile=None, legacy_receipt=None):
        profile = schema_profile or "scientific"
        if profile not in {"scientific", "knowledge-publication-v1"}:
            raise ArchiveIntegrityError("Unknown archive schema profile")
        if not isinstance(package, dict) or not isinstance(assets, dict):
            raise ArchiveIntegrityError("Package and assets must be mappings")
        serialized = _bytes(package)
        digest = sha256(serialized).hexdigest()
        content, metadata = {}, {}
        try:
            for key, value in sorted(assets.items()):
                if not isinstance(key, str) or not key:
                    raise ArchiveIntegrityError("Asset identity is invalid")
                if isinstance(value, bytes):
                    data = value
                    name = key
                else:
                    path = Path(value)
                    if path.is_symlink() or not path.is_file():
                        raise ArchiveIntegrityError("Asset must be a regular non-symlink file")
                    data = path.read_bytes()
                    name = path.name
                content[key] = data
                h = sha256(data).hexdigest()
                metadata[key] = {"sha256": h, "size": len(data), "original_name": name, "path": "objects/" + h}
            if legacy_receipt is not None:
                if legacy_receipt.get("package_sha256") != digest or set(legacy_receipt.get("assets", {})) != set(
                    assets
                ):
                    raise ArchiveIntegrityError("Legacy receipt does not match the exported package")
                metadata = deepcopy(legacy_receipt["assets"])
                for key, item in metadata.items():
                    if item.get("sha256") != sha256(content[key]).hexdigest() or item.get("size") != len(content[key]):
                        raise ArchiveIntegrityError("Legacy asset identity does not match the exported bytes")
            self._asset_identity(package, metadata)
            snapshot = sha256(_bytes({"package_sha256": digest, "assets": metadata})).hexdigest()
            if legacy_receipt is not None and legacy_receipt.get("snapshot_id") != snapshot:
                raise ArchiveIntegrityError("Legacy snapshot identity is invalid")
            manifest = {
                "format": "kb-local-archive-v1",
                "schema_profile": profile,
                "snapshot_id": snapshot,
                "package_sha256": digest,
                "metadata": self._metadata(package),
                "assets": metadata,
                "blobs": {key: sha256(data).hexdigest() for key, data in content.items()},
            }
            manifest_digest = sha256(_bytes(manifest)).hexdigest()
            intent = {
                "backend": "local",
                "format_version": 1,
                "manifest": manifest,
                "archive_manifest_sha256": manifest_digest,
            }
            pending = Path(pending_dir)
            with _intent_lock(pending):
                path = pending / "intent.json"
                if path.exists() or path.is_symlink():
                    if _load(path) != intent:
                        raise ArchiveIntegrityError("Pending intent belongs to a different snapshot")
                else:
                    # Never overwrite orphaned state whose identity cannot be proved.
                    if (pending / "receipt.json").exists():
                        raise ArchiveIntegrityError("Archive intent is missing")
                    _atomic(path, intent)
                recovered = self._reconcile(package, pending)
                if recovered is not None:
                    return recovered
                self.blobs.put_blob(serialized)
                for data in content.values():
                    self.blobs.put_blob(data)
                self.blobs.put_json(manifest)
                receipt = self._receipt(manifest, manifest_digest)
                self._manifest(receipt)  # Verify every package, asset and manifest before committing.
                commit = self._commits / manifest_digest
                if commit.exists():
                    if _load(commit) != receipt:
                        raise ArchiveIntegrityError("Archive commit differs from its intent")
                else:
                    _atomic(commit, receipt)
                _atomic(pending / "receipt.json", receipt)
                return receipt
        except ArchiveError:
            raise
        except KnowledgeIntegrityError:
            raise ArchiveIntegrityError("Archive content failed verification") from None
        except (OSError, ValueError, TypeError):
            raise ArchiveWriteUnknown("Archive write interrupted; reconcile the pinned intent") from None

    def _reconcile(self, package, pending):
        intent_path = pending / "intent.json"
        if not intent_path.exists():
            if intent_path.is_symlink() or (pending / "receipt.json").exists():
                raise ArchiveWriteUnknown("Durable archive intent is missing")
            return None
        intent = _load(intent_path)
        try:
            manifest = intent["manifest"]
            digest = checked_hash(intent["archive_manifest_sha256"])
            if (
                intent.get("backend") != "local"
                or intent.get("format_version") != 1
                or sha256(_bytes(manifest)).hexdigest() != digest
            ):
                raise ArchiveIntegrityError("Archive intent identity changed")
            if manifest["package_sha256"] != sha256(_bytes(package)).hexdigest():
                raise ArchiveIntegrityError("Archive intent belongs to another package")
            commit = self._commits / digest
            receipt_path = pending / "receipt.json"
            if not commit.exists():
                if receipt_path.exists() or receipt_path.is_symlink():
                    raise ArchiveIntegrityError("Archive completion marker is missing")
                # Partial immutable objects may be resumed only through explicit publish.
                for h in [manifest["package_sha256"], *manifest["blobs"].values(), digest]:
                    target = self.root / "objects" / checked_hash(h)
                    if target.exists() or target.is_symlink():
                        self.blobs.get_blob(h)
                return None
            receipt = _load(commit)
            if receipt != self._receipt(manifest, digest):
                raise ArchiveIntegrityError("Archive commit identity changed")
            self._manifest(receipt)
            if (receipt_path.exists() or receipt_path.is_symlink()) and _load(receipt_path) != receipt:
                raise ArchiveIntegrityError("Pending receipt identity changed")
            return receipt
        except (KeyError, TypeError, ValueError, KnowledgeIntegrityError):
            raise ArchiveIntegrityError("Archive intent is invalid") from None

    def reconcile_pending(self, package, pending_dir):
        pending = Path(pending_dir)
        _safe_path(pending)
        # The durable intent is immutable and commit publication is atomic. No
        # lock/file creation or repairs occur in this read-only recovery path.
        try:
            return self._reconcile(package, pending)
        except OSError:
            raise ArchiveWriteUnknown("Archive state is unavailable") from None

    def publication_state(self, pending_dir):
        pending = Path(pending_dir)
        try:
            _safe_path(pending)
            intent_path = pending / "intent.json"
            if pending.is_symlink() or intent_path.is_symlink():
                raise ArchiveIntegrityError("Invalid intent")
            if not intent_path.exists():
                if (pending / "receipt.json").exists():
                    raise ArchiveIntegrityError("Missing intent")
                return {"outcome": "not_written", "status": "not_started"}
            intent = _load(intent_path)
            manifest = intent["manifest"]
            # Package bytes can be absent in a safely pinned partial intent.
            package = (
                self.blobs.get_json(manifest["package_sha256"])
                if (self.root / "objects" / manifest["package_sha256"]).exists()
                else None
            )
            if package is None:
                if (self._commits / intent["archive_manifest_sha256"]).exists():
                    raise ArchiveIntegrityError("Missing committed package")
                if sha256(_bytes(manifest)).hexdigest() != intent["archive_manifest_sha256"]:
                    raise ArchiveIntegrityError("Invalid intent")
                return {"outcome": "not_written", "status": "prepared"}
            receipt = self._reconcile(package, pending)
            return {"outcome": "verified" if receipt else "not_written", "status": "ready" if receipt else "prepared"}
        except (ArchiveError, OSError, ValueError, KeyError, TypeError, KnowledgeIntegrityError):
            return {"outcome": "unknown", "status": "unreadable_or_invalid"}
