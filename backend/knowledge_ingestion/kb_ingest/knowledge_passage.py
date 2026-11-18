"""Frozen original-byte/index resolver, independent of scientific records."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from .core import fingerprint
from .knowledge_contracts import PassageProvenance, PassageResult, SourcePassageRef, SourceSnapshotRef
from .knowledge_snapshot import KnowledgeIntegrityError


@dataclass(frozen=True)
class SourceReviewState:
    source_revision_id: str
    review_version: str
    source_review: str
    available: bool


@dataclass(frozen=True)
class SupportAssetVerification:
    """Trusted W05 receipt/readback adapter must supply each bound hash."""

    source_snapshot_ref: SourceSnapshotRef
    review_version: str
    quote_sha256: str
    verified_at: float


class SourceSnapshotPort(Protocol):
    def resolve(self, ref: SourcePassageRef, *, use_policy="source_reviewed") -> PassageResult: ...


class FrozenSourceSnapshotResolver:
    def __init__(
        self, snapshot_store, registry, workspace_id, collection_id, review_state, *, support_asset_verifier=None
    ):
        self.snapshots = snapshot_store
        self.registry = registry
        self.workspace_id = workspace_id
        self.collection_id = collection_id
        self.review_state = review_state
        self.support_asset_verifier = support_asset_verifier
        registry.resolve(workspace_id, collection_id)

    def _state(self, source_id, revision_id, review_version, use_policy):
        if source_id not in self.registry.resolve(self.workspace_id, self.collection_id).source_ids:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        state = self.review_state(source_id, revision_id)
        if not isinstance(state, SourceReviewState) or not state.available or state.source_review == "revoked":
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        if state.source_revision_id != revision_id or state.review_version != review_version:
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        if use_policy != "explore" and (use_policy != "source_reviewed" or state.source_review != "reviewed"):
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        return state

    def freeze(self, source_id, source_revision_id, original_bytes, index, *, review_version):
        self._state(source_id, source_revision_id, review_version, "explore")
        if not isinstance(original_bytes, bytes) or not original_bytes:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        try:
            segments = index["segments"]
            version = index["version"]
            if not segments or len({s["segment_id"] for s in segments}) != len(segments):
                raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
            for segment in segments:
                if (
                    segment.get("content_kind") not in {"verbatim", "parsed", "model_transcription", "unknown"}
                    or not isinstance(segment.get("quote"), str)
                    or not segment["quote"]
                ):
                    raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
                PassageProvenance.model_validate(segment.get("provenance", {}))
                # Validate every location, including segments not selected now.
                temporary = SourceSnapshotRef(
                    kind="source_snapshot",
                    workspace_id=self.workspace_id,
                    source_snapshot_id="validate",
                    source_id=source_id,
                    source_revision_id=source_revision_id,
                    source_asset_sha256="0" * 64,
                    source_index_version=version,
                    source_index_sha256="0" * 64,
                    manifest_sha256="0" * 64,
                )
                SourcePassageRef(
                    kind="source_passage",
                    workspace_id=self.workspace_id,
                    corpus_snapshot_id="validate",
                    source_snapshot_ref=temporary,
                    segment_id=segment["segment_id"],
                    quote_sha256=sha256(segment["quote"].encode()).hexdigest(),
                    physical_page=segment["physical_page"],
                    location_status=segment["location_status"],
                    bbox=segment.get("bbox"),
                )
                if segment.get("bbox"):
                    # Page image bytes are independently frozen by their hash.
                    self.snapshots.get_blob(segment["bbox"]["page_image_sha256"])
            asset_hash = self.snapshots.put_blob(original_bytes)
            index_hash = self.snapshots.put_json(index)
            manifest = {
                "workspace_id": self.workspace_id,
                "source_id": source_id,
                "source_revision_id": source_revision_id,
                "source_asset_sha256": asset_hash,
                "source_index_version": version,
                "source_index_sha256": index_hash,
                "review_version": review_version,
            }
            manifest_hash = self.snapshots.put_json(manifest)
            return SourceSnapshotRef(
                kind="source_snapshot",
                workspace_id=self.workspace_id,
                source_snapshot_id=manifest_hash,
                source_id=source_id,
                source_revision_id=source_revision_id,
                source_asset_sha256=asset_hash,
                source_index_version=version,
                source_index_sha256=index_hash,
                manifest_sha256=manifest_hash,
            )
        except KnowledgeIntegrityError:
            raise
        except (KeyError, TypeError, ValueError):
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH") from None

    def _frozen(self, ref, use_policy):
        if ref.workspace_id != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        if ref.source_snapshot_id != ref.manifest_sha256:
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
        manifest = self.snapshots.get_json(ref.manifest_sha256)
        for field in (
            "workspace_id",
            "source_id",
            "source_revision_id",
            "source_asset_sha256",
            "source_index_version",
            "source_index_sha256",
        ):
            if manifest.get(field) != getattr(ref, field):
                raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
        state = self._state(ref.source_id, ref.source_revision_id, manifest["review_version"], use_policy)
        self.snapshots.get_blob(ref.source_asset_sha256)
        index = self.snapshots.get_json(ref.source_index_sha256)
        if index["version"] != ref.source_index_version:
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
        return manifest, index, state

    def build_corpus(self, source_snapshots, *, use_policy="explore"):
        scope = self.registry.resolve(self.workspace_id, self.collection_id)
        for ref in source_snapshots:
            self._frozen(ref, use_policy)
        return self.snapshots.publish_corpus(
            {
                "workspace_id": scope.workspace_id,
                "collection_id": scope.collection_id,
                "collection_version": scope.version,
                "policy_version": scope.policy_version,
                "use_policy": use_policy,
                "records": [],
                "sources": sorted({s.source_id for s in source_snapshots}),
                "source_snapshots": [s.model_dump(mode="json") for s in source_snapshots],
            }
        )

    def passage_ref(self, source_snapshot_ref, segment_id, corpus_snapshot_id):
        _, index, _ = self._frozen(source_snapshot_ref, "explore")
        segment = next((s for s in index["segments"] if s["segment_id"] == segment_id), None)
        if segment is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        ref = SourcePassageRef(
            kind="source_passage",
            workspace_id=self.workspace_id,
            corpus_snapshot_id=corpus_snapshot_id,
            source_snapshot_ref=source_snapshot_ref,
            segment_id=segment_id,
            quote_sha256=sha256(segment["quote"].encode()).hexdigest(),
            physical_page=segment["physical_page"],
            location_status=segment["location_status"],
            bbox=segment.get("bbox"),
        )
        corpus = self.snapshots.read_corpus(corpus_snapshot_id)
        self.resolve(ref, use_policy=corpus["use_policy"])
        return ref

    def resolve(self, ref, *, use_policy="source_reviewed"):
        if ref.workspace_id != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        corpus = self.snapshots.read_corpus(ref.corpus_snapshot_id)
        self.registry.check_manifest(corpus)
        if corpus["workspace_id"] != self.workspace_id or corpus["collection_id"] != self.collection_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        if corpus["use_policy"] != use_policy:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        if ref.source_snapshot_ref.model_dump(mode="json") not in corpus.get("source_snapshots", []):
            raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        # All pinned source members are checked even when this passage is the only match.
        for member in corpus["source_snapshots"]:
            self._frozen(SourceSnapshotRef.model_validate(member), use_policy)
        manifest, index, state = self._frozen(ref.source_snapshot_ref, use_policy)
        segment = next((s for s in index["segments"] if s["segment_id"] == ref.segment_id), None)
        if segment is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        expected = SourcePassageRef(
            kind="source_passage",
            workspace_id=self.workspace_id,
            corpus_snapshot_id=ref.corpus_snapshot_id,
            source_snapshot_ref=ref.source_snapshot_ref,
            segment_id=segment["segment_id"],
            quote_sha256=sha256(segment["quote"].encode()).hexdigest(),
            physical_page=segment["physical_page"],
            location_status=segment["location_status"],
            bbox=segment.get("bbox"),
        )
        if ref != expected or segment.get("content_kind") not in {
            "verbatim",
            "parsed",
            "model_transcription",
            "unknown",
        }:
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
        if ref.bbox:
            self.snapshots.get_blob(ref.bbox.page_image_sha256)
        storage = "local_verified"
        eligible = False
        if self.support_asset_verifier is not None:
            verification = self.support_asset_verifier(ref, manifest["review_version"])
            if (
                not isinstance(verification, SupportAssetVerification)
                or verification.source_snapshot_ref != ref.source_snapshot_ref
                or verification.review_version != manifest["review_version"]
                or verification.quote_sha256 != ref.quote_sha256
                or verification.verified_at <= 0
            ):
                raise KnowledgeIntegrityError()
            storage = "archive_verified"
            eligible = state.source_review == "reviewed"
        self._state(
            ref.source_snapshot_ref.source_id,
            ref.source_snapshot_ref.source_revision_id,
            manifest["review_version"],
            use_policy,
        )
        return PassageResult(
            kind="source_passage",
            result_id=fingerprint(ref.model_dump(mode="json")),
            ref=ref,
            quote=segment["quote"],
            content_kind=segment["content_kind"],
            provenance=segment.get("provenance", {}),
            attribution=ref.source_snapshot_ref.source_id,
            source_review=state.source_review,
            availability="available",
            storage=storage,
            integrity="verified",
            eligible_for_publication=eligible,
        )
