"""Immutable derived artifacts. This store never constructs the ingestion DB."""

from __future__ import annotations

import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path

from .core.serialization import canonical
from .knowledge_contracts import SAFE_MESSAGES


class KnowledgeIntegrityError(ValueError):
    def __init__(self, code="INTEGRITY_FAILURE"):
        self.code = code if code in SAFE_MESSAGES else "INTEGRITY_FAILURE"
        super().__init__(SAFE_MESSAGES[self.code])


def checked_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise KnowledgeIntegrityError()
    return value


class KnowledgeSnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise KnowledgeIntegrityError()
        for name in ("objects", "manifests"):
            path = self.root / name
            if path.is_symlink():
                raise KnowledgeIntegrityError()
            path.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace, digest):
        path = self.root / namespace / checked_hash(digest)
        if self.root.is_symlink() or path.parent.is_symlink() or path.is_symlink():
            raise KnowledgeIntegrityError()
        return path

    def _write(self, namespace, data):
        digest = sha256(data).hexdigest()
        target = self._path(namespace, digest)
        fd, name = tempfile.mkstemp(prefix=".unpublished-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Atomic no-clobber publication: another writer cannot overwrite.
                os.link(temporary, target)
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                pass
            if self._read(namespace, digest) != data:
                raise KnowledgeIntegrityError()
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def _read(self, namespace, digest):
        try:
            path = self._path(namespace, digest)
            data = path.read_bytes()
            if sha256(data).hexdigest() != digest:
                raise KnowledgeIntegrityError()
            return data
        except OSError:
            raise KnowledgeIntegrityError() from None

    def put_blob(self, data: bytes):
        if not isinstance(data, bytes):
            raise KnowledgeIntegrityError()
        return self._write("objects", data)

    def get_blob(self, digest):
        return self._read("objects", digest)

    def put_json(self, value):
        try:
            return self.put_blob(canonical(value).encode("utf-8"))
        except (TypeError, ValueError):
            raise KnowledgeIntegrityError() from None

    def get_json(self, digest):
        try:
            data = self.get_blob(digest)
            value = json.loads(data)
            if canonical(value).encode("utf-8") != data:
                raise KnowledgeIntegrityError()
            return value
        except (ValueError, UnicodeError):
            raise KnowledgeIntegrityError() from None

    def publish_corpus(self, manifest):
        required = {
            "workspace_id",
            "collection_id",
            "collection_version",
            "policy_version",
            "use_policy",
            "records",
            "sources",
        }
        if not isinstance(manifest, dict) or not required <= manifest.keys():
            raise KnowledgeIntegrityError()
        # Verify every record object before the final manifest becomes visible.
        for record in manifest["records"]:
            self.get_json(record["package_sha256"])
        return self._write("manifests", canonical(manifest).encode("utf-8"))

    def read_corpus(self, snapshot_id):
        try:
            data = self._read("manifests", snapshot_id)
            manifest = json.loads(data)
            if canonical(manifest).encode("utf-8") != data:
                raise KnowledgeIntegrityError()
            for record in manifest["records"]:
                self.get_json(record["package_sha256"])
            return manifest
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise KnowledgeIntegrityError() from None
