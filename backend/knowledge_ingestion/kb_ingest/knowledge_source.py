"""Read-only saved ledger adapter. Candidate fallback is never reachable."""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from .contract import validate_package
from .core import fingerprint
from .knowledge_contracts import AssertionRef, EvidenceRef, RecordRef, SampleTestContextRef
from .knowledge_projection import project_saved_package
from .knowledge_snapshot import KnowledgeIntegrityError, checked_hash


@dataclass(frozen=True)
class RecordEligibility:
    review_version: str
    source_review: str
    eligible: bool
    source_reviewed_allowed: bool = False
    review_basis: str = "unknown"


@dataclass(frozen=True)
class SavedRecord:
    package: dict
    projected_package: dict
    ref: RecordRef
    verified_at: float
    freshness: str
    source_review: str
    currently_eligible: bool
    storage: str = "archive_verified"
    integrity: str = "verified"
    review_basis: str = "unknown"


class PublishedRecordPort(Protocol):
    def list_saved_heads(self, *, use_policy="saved_extractions", include_history=False) -> list[dict]: ...
    def read_saved_package(self, task_id, expected_package_sha256, *, mode="live_readback") -> dict: ...
    def build_corpus(self, *, use_policy="source_reviewed", include_history=False, allow_partial=False) -> str: ...
    def corpus_manifest(self, corpus_snapshot_id) -> dict: ...
    def read_corpus(
        self, corpus_snapshot_id, *, use_policy="saved_extractions", mode="verified_cache"
    ) -> list[SavedRecord]: ...
    def resolve(self, ref, *, use_policy="source_reviewed", mode="live_readback"): ...


class IngestionPublishedRecordAdapter:
    def __init__(
        self,
        service,
        registry,
        workspace_id,
        collection_id,
        snapshot_store,
        *,
        cache_ttl_seconds=300,
        clock=time.time,
        eligibility=None,
    ):
        if cache_ttl_seconds <= 0:
            raise ValueError("Positive cache TTL required")
        self.service = service
        self.registry = registry
        self.workspace_id = workspace_id
        self.collection_id = collection_id
        self.snapshots = snapshot_store
        self.cache_ttl_seconds = cache_ttl_seconds
        self.clock = clock
        self.eligibility = eligibility
        # Trusted process-owned verification times. Restart requires fresh remote read.
        self._verified = {}
        self._asset_verified = {}
        registry.resolve(workspace_id, collection_id)

    @staticmethod
    def _formal_hash(task):
        """A saved receipt pins the published package, not the retained candidate."""
        receipt = task.get("receipt")
        if not isinstance(receipt, dict):
            raise KnowledgeIntegrityError()
        return checked_hash(receipt.get("package_sha256"))

    def _task(self, task_id, expected_hash):
        try:
            task = self.service.store.get_task(task_id)
        except (KeyError, ValueError):
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND") from None
        scope = self.registry.resolve(self.workspace_id, self.collection_id)
        if task["source_id"] not in scope.source_ids:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        if task.get("state") != "saved":
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        receipt = task.get("receipt") or {}
        if (
            self._formal_hash(task) != checked_hash(expected_hash)
            or receipt.get("source_id") != task["source_id"]
            or receipt.get("status") != "ready"
            or receipt.get("readback_verified") is not True
            or receipt.get("assets_verified") is not True
            or receipt.get("backend") != "local"
            or not receipt.get("archive_manifest_sha256")
            or not receipt.get("snapshot_id")
        ):
            raise KnowledgeIntegrityError()
        return task

    def _state(self, task, package):
        blocked = self.service._document_revocations(task["task_id"], package)
        if blocked:
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        if self.eligibility is not None:
            state = self.eligibility(task, package)
        else:
            header = package["header"]
            reviewed = header.get("review_policy") == "human_reviewed_candidate_v1"
            from .document_workflow import document_query_metadata

            projected = project_saved_package(package, set())
            metadata = document_query_metadata(projected)
            is_document = task.get("source_type") in {"paper", "patent"} or any(
                x.get("source_type") in {"paper", "patent"} for x in package.get("sources", [])
            )
            allowed = not (is_document and header.get("document_extraction_mode") == "program-only-v1" and not reviewed)
            state = RecordEligibility(
                header["record_revision_id"],
                "reviewed" if reviewed else "unreviewed",
                True,
                allowed,
                metadata["source_review_basis"] if metadata else "existing_saved_table_contract",
            )
        if (
            not isinstance(state, RecordEligibility)
            or not state.eligible
            or state.source_review not in {"reviewed", "unreviewed", "unknown"}
        ):
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        return state

    def _validate_package(self, task, package):
        receipt = task["receipt"]
        header = package.get("header", {})
        validate_package(package)
        if fingerprint(package) != self._formal_hash(task):
            raise KnowledgeIntegrityError()
        for key in ("source_id", "source_revision_id", "record_revision_id"):
            if not header.get(key) or header[key] != receipt.get(key):
                raise KnowledgeIntegrityError()
        assets = receipt.get("assets")
        if not isinstance(assets, dict) or receipt["snapshot_id"] != fingerprint(
            {"package_sha256": self._formal_hash(task), "assets": assets}
        ):
            raise KnowledgeIntegrityError()
        if {a["asset_id"] for a in package["assets"]} != set(assets):
            raise KnowledgeIntegrityError()
        for asset in package["assets"]:
            if asset["sha256"] != assets[asset["asset_id"]].get("sha256"):
                raise KnowledgeIntegrityError()
        self._state(task, package)

    def read_saved_package(self, task_id, expected_package_sha256, *, mode="live_readback"):
        task = self._task(task_id, expected_package_sha256)
        try:
            if mode == "live_readback":
                try:
                    package = self.service.records(task_id)
                except Exception as exc:
                    from .archive import ArchiveIntegrityError

                    if isinstance(exc, ArchiveIntegrityError):
                        raise KnowledgeIntegrityError() from None
                    raise KnowledgeIntegrityError("ARCHIVE_UNAVAILABLE") from None
                self._validate_package(task, package)
                self.snapshots.put_json(package)
                self._verified[(task_id, expected_package_sha256)] = self.clock()
            elif mode == "verified_cache":
                timestamp = self._verified.get((task_id, expected_package_sha256))
                if timestamp is None or not 0 <= self.clock() - timestamp <= self.cache_ttl_seconds:
                    raise KnowledgeIntegrityError("INDEX_STALE")
                package = self.snapshots.get_json(expected_package_sha256)
                self._validate_package(task, package)
            else:
                raise KnowledgeIntegrityError("INVALID_QUERY")
            # Recheck ledger after external I/O; revoke/change during read fails closed.
            current = self._task(task_id, expected_package_sha256)
            if current["receipt"] != task["receipt"]:
                raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
            self._state(current, package)
            return deepcopy(package)
        except KnowledgeIntegrityError:
            raise
        except Exception:
            raise KnowledgeIntegrityError("INTEGRITY_FAILURE") from None

    def list_saved_heads(self, *, use_policy="saved_extractions", include_history=False):
        if include_history != (use_policy == "history") or use_policy not in {
            "source_reviewed",
            "saved_extractions",
            "history",
        }:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        scope = self.registry.resolve(self.workspace_id, self.collection_id)
        result = []
        for task in self.service.store.list_tasks():
            if task.get("state") != "saved" or task["source_id"] not in scope.source_ids:
                continue
            head = self.service.store.source_state(task["source_id"])
            if include_history or head and head.get("published_task_id") == task["task_id"]:
                result.append(task)
        return sorted(result, key=lambda t: t["task_id"])

    def _policy(self, state, use_policy):
        if (
            use_policy not in {"source_reviewed", "saved_extractions", "history"}
            or use_policy == "source_reviewed"
            and not (state.source_reviewed_allowed or state.source_review == "reviewed")
        ):
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")

    def build_corpus(
        self, *, use_policy="source_reviewed", include_history=False, allow_partial=False, source_ids=None
    ):
        scope = self.registry.resolve(self.workspace_id, self.collection_id)
        records = []
        failures = []
        selected = self.list_saved_heads(use_policy=use_policy, include_history=include_history)
        if source_ids is not None:
            if not source_ids or len(set(source_ids)) != len(source_ids) or not set(source_ids) <= scope.source_ids:
                raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
            selected = [task for task in selected if task["source_id"] in source_ids]
            if {task["source_id"] for task in selected} != set(source_ids):
                raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        # Freeze all selected owner identities before the first remote read.
        descriptors = []
        for task in selected:
            formal_hash = self._formal_hash(task)
            self._task(task["task_id"], formal_hash)
            receipt = task["receipt"]
            descriptors.append(
                {k: task[k] for k in ("task_id", "source_id")}
                | {"package_sha256": formal_hash}
                | {k: receipt[k] for k in ("source_revision_id", "record_revision_id")}
                | {"record_snapshot_id": receipt["snapshot_id"]}
            )
        for task, descriptor in zip(selected, descriptors, strict=True):
            try:
                package = self.read_saved_package(task["task_id"], descriptor["package_sha256"])
            except KnowledgeIntegrityError as exc:
                if not allow_partial or exc.code != "ARCHIVE_UNAVAILABLE":
                    raise
                failures.append({**descriptor, "code": exc.code})
                continue
            state = self._state(task, package)
            self._policy(state, use_policy)
            records.append({**descriptor, "review_version": state.review_version, "source_review": state.source_review})
        manifest = {
            "workspace_id": scope.workspace_id,
            "collection_id": scope.collection_id,
            "collection_version": scope.version,
            "policy_version": scope.policy_version,
            "use_policy": use_policy,
            "records": records,
            "sources": sorted({r["source_id"] for r in descriptors}),
            "selected_records": descriptors,
            "failures": failures,
        }
        self.registry.check_manifest(manifest)
        return self.snapshots.publish_corpus(manifest)

    def corpus_manifest(self, corpus_snapshot_id):
        manifest = self.snapshots.read_corpus(corpus_snapshot_id)
        self.registry.check_manifest(manifest)
        if manifest["workspace_id"] != self.workspace_id or manifest["collection_id"] != self.collection_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        return manifest

    def read_corpus(self, corpus_snapshot_id, *, use_policy="saved_extractions", mode="verified_cache"):
        manifest = self.corpus_manifest(corpus_snapshot_id)
        if manifest["use_policy"] != use_policy:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        records = []
        for failure in manifest.get("failures", []):
            task = self._task(failure["task_id"], failure["package_sha256"])
            for key in ("source_id", "source_revision_id", "record_revision_id"):
                if task["receipt"].get(key) != failure[key]:
                    raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
            if task["receipt"]["snapshot_id"] != failure["record_snapshot_id"]:
                raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        # The pinned list is stable after new publication; every member still has
        # its current eligibility/revocation checked. History is creation policy.
        for descriptor in manifest["records"]:
            package = self.read_saved_package(descriptor["task_id"], descriptor["package_sha256"], mode=mode)
            task = self._task(descriptor["task_id"], descriptor["package_sha256"])
            state = self._state(task, package)
            self._policy(state, use_policy)
            if (
                state.review_version != descriptor["review_version"]
                or state.source_review != descriptor["source_review"]
            ):
                raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
            for key in ("source_id", "source_revision_id", "record_revision_id"):
                if descriptor[key] != package["header"][key]:
                    raise KnowledgeIntegrityError()
            if descriptor["record_snapshot_id"] != task["receipt"]["snapshot_id"]:
                raise KnowledgeIntegrityError()
            ref = RecordRef(
                kind="record_revision",
                workspace_id=self.workspace_id,
                corpus_snapshot_id=corpus_snapshot_id,
                **{
                    k: descriptor[k]
                    for k in (
                        "task_id",
                        "source_id",
                        "source_revision_id",
                        "record_revision_id",
                        "record_snapshot_id",
                        "package_sha256",
                    )
                },
            )
            records.append(
                SavedRecord(
                    package,
                    project_saved_package(package, set()),
                    ref,
                    self._verified[(ref.task_id, ref.package_sha256)],
                    "live" if mode == "live_readback" else "verified_cache",
                    state.source_review,
                    True,
                    review_basis=state.review_basis,
                )
            )
        return records

    def resolve(self, ref, *, use_policy="source_reviewed", mode="live_readback"):
        if ref.workspace_id != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        records = self.read_corpus(ref.corpus_snapshot_id, use_policy=use_policy, mode=mode)
        record = next((r for r in records if r.ref.task_id == ref.task_id), None)
        if record is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        for field, value in record.ref.model_dump().items():
            if field != "kind" and getattr(ref, field, None) != value:
                raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        if isinstance(ref, RecordRef) and ref.kind == "record_revision":
            return record
        p = record.projected_package
        if isinstance(ref, AssertionRef):
            item = next((a for a in p["assertions"] if a["assertion_id"] == ref.assertion_id), None)
            if item is None:
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            return {
                "ref": ref,
                "assertion": deepcopy(item),
                "verified_at": record.verified_at,
                "freshness": record.freshness,
            }
        if isinstance(ref, SampleTestContextRef):
            sample = next((o for o in p["objects"] if o["object_id"] == ref.sample_ref and o["kind"] == "sample"), None)
            test = next((o for o in p["objects"] if o["object_id"] == ref.test_ref), None) if ref.test_ref else None
            if sample is None or ref.test_ref and (test is None or test["kind"] not in {"reported_test", "test"}):
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            from .ingest.tables.table_quality import blocked_objects, verify_table_bindings
            from .query import eligible_relations
            from .scientific_subjects import ownership_parents

            if p["header"].get("table_quality_version"):
                verify_table_bindings(p)
            blocked = blocked_objects(p)
            if ref.sample_ref in blocked or ref.test_ref in blocked:
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            if ref.test_ref:
                parents = ownership_parents(p, list(enumerate(eligible_relations(p))))
                owners = {parent for parent, _ in parents.get(ref.test_ref, [])}
                if owners != {ref.sample_ref}:
                    raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            return {"ref": ref, "sample": deepcopy(sample), "test": deepcopy(test), "record": record}
        if isinstance(ref, EvidenceRef):
            evidence = next((e for e in p["evidence"] if e["evidence_id"] == ref.evidence_id), None)
            asset = next((a for a in p["assets"] if evidence and a["asset_id"] == evidence["asset_ref"]), None)
            if asset is None or asset["sha256"] != ref.asset_sha256:
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            key = (ref.task_id, ref.package_sha256, ref.asset_sha256)
            if mode == "verified_cache":
                stamp = self._asset_verified.get(key)
                if stamp is None or not 0 <= self.clock() - stamp <= self.cache_ttl_seconds:
                    raise KnowledgeIntegrityError("INDEX_STALE")
                data = self.snapshots.get_blob(ref.asset_sha256)
            else:
                try:
                    data = self.service.archive.read_asset(
                        self._task(ref.task_id, ref.package_sha256)["receipt"], asset["asset_id"]
                    )
                except Exception:
                    raise KnowledgeIntegrityError() from None
                if sha256(data).hexdigest() != ref.asset_sha256:
                    raise KnowledgeIntegrityError()
                self.snapshots.put_blob(data)
                self._asset_verified[key] = self.clock()
            self._state(self._task(ref.task_id, ref.package_sha256), record.package)
            return {
                "ref": ref,
                "evidence": deepcopy(evidence),
                "asset": deepcopy(asset),
                "asset_bytes": data,
                "verified_at": min(record.verified_at, self._asset_verified[key]),
                "freshness": record.freshness,
            }
        raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
