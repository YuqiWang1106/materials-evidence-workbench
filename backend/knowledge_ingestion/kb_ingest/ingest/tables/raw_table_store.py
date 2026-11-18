"""Immutable raw-table/analysis artifacts above the existing coordination store."""

import json
import re
from copy import deepcopy

from kb_ingest.contract import strict_equal
from kb_ingest.core import StateConflict, atomic_json, clock, fingerprint
from kb_ingest.storage.file_time import time_order, utc_text
from kb_ingest.storage.time_source import sourced_epoch

from .raw_table_snapshot import snapshot_version

KINDS = {"raw": "rawtable", "analyses": "analysis"}
STATUSES = {"local_saved", "archive_verified", "sync_failed", "write_unknown"}


def analysis_version(payload):
    return "analysis-" + fingerprint(
        {key: payload[key] for key in ("version", "table_version_id", "engine_version", "config")}
    )


def newest_artifacts_first(rows: list[dict[str, object]], id_key: str) -> list[dict[str, object]]:
    """Sort stored instants before reducing them to the public epoch precision."""
    ordered = sorted(rows, key=lambda row: (time_order(row["created_at"]), str(row[id_key])), reverse=True)
    return [{**row, "created_at": sourced_epoch(row["created_at"])} for row in ordered]


class RawTableArtifactStore:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "table_artifacts"

    def path(self, kind, identifier):
        if (
            kind not in KINDS
            or not isinstance(identifier, str)
            or not re.fullmatch(KINDS[kind] + r"-[0-9a-f]{64}", identifier)
        ):
            raise ValueError("Invalid table artifact identifier")
        path = self.root / kind / identifier
        if any(part.is_symlink() for part in (self.root, self.root / kind, path)):
            raise StateConflict("Table artifact integrity failure: symbolic link")
        return path

    def _read(self, path):
        if path.is_symlink():
            raise StateConflict("Table artifact integrity failure: symbolic link")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateConflict("Table artifact integrity failure") from exc

    def put(self, kind, identifier, payload, audit):
        """Caller holds the artifact lock. A content identity never changes bytes."""
        self.store.require_source(payload["source_id"])
        path = self.path(kind, identifier)
        envelope = {
            "format": "table-artifact-v1",
            "header": {
                "source_id": payload["source_id"],
                "source_revision_id": payload["asset_sha256"],
                "record_revision_id": identifier,
            },
            "sources": [
                {"source_id": payload["source_id"], "source_kind": "raw_table" if kind == "raw" else "table_analysis"}
            ],
            "payload": deepcopy(payload),
        }
        if (path / "artifact.json").exists():
            existing = self.load(kind, identifier)
            if not strict_equal(existing, envelope):
                raise StateConflict("Table artifact identity already names different content")
            return existing
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        created_at = audit["created_at"] if "created_at" in audit else clock.now()
        atomic_json(path / "audit.json", {**audit, "created_at": utc_text(created_at)})
        atomic_json(
            path / "manifest.json",
            {
                "kind": kind,
                "id": identifier,
                "artifact_sha256": fingerprint(envelope),
                "payload_sha256": fingerprint(payload),
            },
        )
        # artifact.json is the local commit marker; incomplete preparation is not listed.
        atomic_json(path / "artifact.json", envelope)
        return envelope

    def load(self, kind, identifier):
        path = self.path(kind, identifier)
        if not (path / "artifact.json").exists():
            raise KeyError("Table artifact not found")
        envelope = self._read(path / "artifact.json")
        manifest = self._read(path / "manifest.json")
        try:
            payload = envelope["payload"]
            content_id = snapshot_version(payload) if kind == "raw" else analysis_version(payload)
            valid = (
                manifest["kind"] == kind
                and manifest["id"] == identifier
                and manifest["artifact_sha256"] == fingerprint(envelope)
                and manifest["payload_sha256"] == fingerprint(payload)
                and envelope["header"]["record_revision_id"] == identifier
                and content_id == identifier
                and payload["table_version_id" if kind == "raw" else "analysis_id"] == identifier
                and envelope["header"]["source_id"] == payload["source_id"]
                and envelope["header"]["source_revision_id"] == payload["asset_sha256"]
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if valid:
            self.store.require_source(payload["source_id"])
            from sqlalchemy.orm import Session

            from kb_ingest.storage.lifecycle_repository import reparses

            with Session(self.store.engine) as session:
                for op in reparses(session, self.store.workspace_id):
                    if identifier in op.manifest.get("withdrawn_artifacts", []):
                        raise StateConflict("来源正在重新解析，旧结果已撤下")
        if not valid:
            raise StateConflict("Table artifact integrity failure")
        return envelope

    def audit(self, kind, identifier):
        return self._read(self.path(kind, identifier) / "audit.json")

    def publication(self, kind, identifier):
        path = self.path(kind, identifier) / "publication.json"
        if not path.exists():
            return {"status": "local_saved", "receipt": None, "error": None}
        value = self._read(path)
        if not isinstance(value, dict) or value.get("status") not in STATUSES:
            raise StateConflict("Table publication state integrity failure")
        return value

    def set_publication(self, kind, identifier, value):
        if "updated_at" in value:
            value = {**value, "updated_at": utc_text(value["updated_at"])}
        atomic_json(self.path(kind, identifier) / "publication.json", value)

    def identifiers(self, kind):
        directory = self.root / kind
        if self.root.is_symlink() or directory.is_symlink():
            raise StateConflict("Table artifact integrity failure: symbolic link")
        if not directory.exists():
            return []
        return sorted(
            path.name
            for path in directory.iterdir()
            if re.fullmatch(KINDS[kind] + r"-[0-9a-f]{64}", path.name) and (path / "artifact.json").exists()
        )
