"""Durable Wiki intents over the existing strict local archive."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from .archive import ArchiveError, ArchiveIntegrityError, ArchiveWriteUnknown
from .core import StateConflict, fingerprint
from .knowledge_publication_contract import PublicationRequest


class PublicationError(ValueError):
    def __init__(self, code, gaps=()):
        self.code, self.gaps = code, list(gaps)
        super().__init__(code)


class KnowledgePublicationService:
    def __init__(self, wiki):
        self.wiki = wiki
        self.store = wiki.store
        self.ingestion = wiki.ingestion_service
        self.root = self.ingestion.store.root / "knowledge-publications"

    def preview(self, page_id, expected_revision=None):
        material = self.wiki.publication_material(page_id, expected_revision)
        gaps = list(material["gaps"])
        if not material.get("review"):
            gaps.append({"code": "REVIEW_REQUIRED"})
        if self.ingestion.archive is None:
            gaps.append({"code": "ARCHIVE_UNAVAILABLE"})
        revision = material["revision"]
        return {
            "page_id": page_id,
            "revision_id": revision["revision_id"],
            "revision": revision["revision"],
            "content_hash": revision["content_hash"],
            "dependency_manifest_hash": revision["dependency_manifest_hash"],
            "can_publish": not gaps,
            "gaps": gaps,
            "scientific_validation": False,
        }

    def _package(self, material, publication_id):
        revision = material["revision"]
        originals = material["originals"]
        assets = {}
        expected = {
            dep["ref"]["asset_id"]: dep["ref"]["asset_sha256"]
            for dep in material["dependency_manifest"]["dependencies"]
            if dep["kind"] == "raw_table"
        }
        expected.update(material.get("document_asset_hashes", {}))
        for key, value in sorted(originals.items()):
            path = Path(value)
            if path.is_symlink() or not path.is_file():
                raise PublicationError("SOURCE_UNAVAILABLE")
            content = path.read_bytes()
            digest = sha256(content).hexdigest()
            if key not in expected or digest != expected[key]:
                raise PublicationError("INTEGRITY_FAILURE")
            assets[key] = {"sha256": digest, "size_bytes": len(content)}
        if set(assets) != set(expected):
            raise PublicationError("SOURCE_UNAVAILABLE")
        package = {
            "format": "knowledge-publication-v1",
            "publication_id": publication_id,
            "header": {"source_id": material["page"]["page_id"], "record_revision_id": revision["revision_id"]},
            "sources": [{"source_id": material["page"]["page_id"], "source_kind": "wiki"}],
            "workspace_id": self.wiki.workspace_id,
            "page_id": material["page"]["page_id"],
            "revision_id": revision["revision_id"],
            "revision": revision["revision"],
            "content_hash": revision["content_hash"],
            "dependency_manifest_hash": revision["dependency_manifest_hash"],
            "content": deepcopy(revision["content"]),
            "supports": deepcopy(revision["supports"]),
            "dependency_manifest": deepcopy(material["dependency_manifest"]),
            "refs": deepcopy(material["refs"]),
            "review": deepcopy(material["review"]),
            "raw_tables": deepcopy(material["raw_tables"]),
            "artifacts": deepcopy(material["artifacts"]),
            "assets": assets,
            "scientific_validation": False,
        }
        for key in ("document_records", "document_evidence", "source_passages"):
            if key in material:
                package[key] = deepcopy(material[key])
        return package, {key: entry["sha256"] for key, entry in assets.items()}

    def _public(self, row):
        # A transport receipt contains local endpoint configuration. Keep it local.
        result = {
            key: deepcopy(row.get(key))
            for key in (
                "publication_id",
                "page_id",
                "revision_id",
                "revision",
                "content_hash",
                "dependency_manifest_hash",
                "status",
                "error_code",
            )
        }
        result.update(currently_eligible=False, availability="not_published", gaps=[])
        if row["status"] in {"published", "historical"}:
            try:
                page = self.wiki.get_page(row["page_id"], revision=row["revision"])
                result.update(availability=page["availability"], gaps=page["gaps"])
                result["currently_eligible"] = bool(
                    row["status"] == "published"
                    and not page["gaps"]
                    and page.get("review")
                    and page.get("published_revision_id") == row["revision_id"]
                )
                if not result["currently_eligible"] and not result["gaps"]:
                    result["availability"] = "superseded"
                    result["gaps"] = [{"code": "PUBLICATION_SUPERSEDED"}]
            except (ValueError, KeyError, StateConflict):
                result.update(availability="availability_unknown", gaps=[{"code": "AVAILABILITY_UNKNOWN"}])
        return result

    def publish(self, page_id, payload):
        if self.store.is_deleted(page_id):
            raise KeyError("WIKI_NOT_FOUND")
        data = PublicationRequest.model_validate(payload).model_dump()
        request_hash = fingerprint(data)
        with self.ingestion.store.lock("wiki-publication:" + page_id):
            previous = self.store.find_publication(page_id, data["request_id"])
            if previous:
                if previous["request_hash"] != request_hash:
                    raise PublicationError("REQUEST_ID_CONFLICT")
                if previous["status"] == "published":
                    return self._public(previous)
                # Every existing intent uses reads only, including a process crash
                # between transport acceptance and the local outcome transaction.
                return self._reconcile(previous, allow_retry=True)
            material = self.wiki.publication_material(page_id, data["expected_revision"])
            revision = material["revision"]
            if any(data[key] != revision[key] for key in ("content_hash", "dependency_manifest_hash")):
                raise PublicationError("REVISION_CONFLICT")
            gaps = list(material["gaps"])
            if not material.get("review"):
                gaps.append({"code": "REVIEW_REQUIRED"})
            if self.ingestion.archive is None:
                gaps.append({"code": "ARCHIVE_UNAVAILABLE"})
            if gaps:
                raise PublicationError(gaps[0]["code"], gaps)
            publication_id = "publication-" + fingerprint(
                [self.wiki.workspace_id, page_id, revision["revision_id"], data["request_id"]]
            )
            package, asset_hashes = self._package(material, publication_id)
            intent = self.store.reserve_publication(
                page_id,
                request_id=data["request_id"],
                request_hash=request_hash,
                expected_revision=data["expected_revision"],
                package=package,
                asset_hashes=asset_hashes,
                publication_id=publication_id,
            )
            self.store.set_publication_outcome(intent["publication_id"], status="publishing")
            try:
                receipt = self.ingestion.archive.publish(
                    package,
                    material["originals"],
                    self.root / intent["publication_id"],
                    schema_profile="knowledge-publication-v1",
                )
                return self._finish(intent, receipt)
            except ArchiveWriteUnknown:
                return self._outcome(intent, "write_unknown", "WRITE_UNKNOWN")
            except ArchiveError as exc:
                state = self.ingestion.archive.publication_state(self.root / intent["publication_id"])
                unknown = state["outcome"] != "not_written"
                code = "INTEGRITY_FAILURE" if isinstance(exc, ArchiveIntegrityError) else "ARCHIVE_UNAVAILABLE"
                return self._outcome(intent, "write_unknown" if unknown else "failed", code)

    def _outcome(self, intent, status, code):
        return self._public(
            self.store.set_publication_outcome(intent["publication_id"], status=status, error_code=code)
        )

    def reconcile(self, publication_id):
        intent = self.store.get_publication(publication_id)
        with self.ingestion.store.lock("wiki-publication:" + intent["page_id"]):
            return self._reconcile(self.store.get_publication(publication_id))

    def _reconcile(self, intent, *, allow_retry=False):
        if intent["status"] == "published":
            return self._public(intent)
        if intent["status"] == "historical":
            return self._recover_validation(intent)
        if self.ingestion.archive is None:
            return self._outcome(intent, intent["status"], "ARCHIVE_UNAVAILABLE")
        try:
            receipt = self.ingestion.archive.reconcile_pending(intent["package"], self.root / intent["publication_id"])
            if receipt is None:
                if not allow_retry:
                    return self._outcome(intent, "failed", "WRITE_NOT_STARTED")
                material = self.wiki.publication_material(intent["page_id"], intent["revision"])
                package, hashes = self._package(material, intent["publication_id"])
                if (
                    material["gaps"]
                    or not material.get("review")
                    or package != intent["package"]
                    or hashes != intent["asset_hashes"]
                ):
                    return self._outcome(intent, "failed", "PUBLICATION_SUPERSEDED")
                self.store.set_publication_outcome(intent["publication_id"], status="publishing")
                receipt = self.ingestion.archive.publish(
                    intent["package"],
                    material["originals"],
                    self.root / intent["publication_id"],
                    schema_profile="knowledge-publication-v1",
                )
            return self._finish(intent, receipt)
        except ArchiveWriteUnknown:
            return self._outcome(intent, "write_unknown", "WRITE_UNKNOWN")
        except ArchiveError as exc:
            return self._outcome(
                intent,
                "write_unknown",
                "INTEGRITY_FAILURE" if isinstance(exc, ArchiveIntegrityError) else "ARCHIVE_UNAVAILABLE",
            )

    def _recover_validation(self, intent):
        """Explicit retry of an unchanged unified intent; remote reads only."""
        from .knowledge_table_wiki_policy import is_unified_summary

        page = self.store.get_page(intent["page_id"], intent["revision"])
        revision = page["revision"]
        if (
            not is_unified_summary(revision)
            or not intent.get("receipt")
            or page["current_revision_id"] != intent["revision_id"]
            or page["availability"] not in {"current", "stale"}
        ):
            return self._public(intent)
        with self.wiki.dependency_guard(revision):
            # Revalidate before and after I/O. Store CAS checks review, hashes,
            # epoch, ledger and all prior invalidation reasons atomically.
            if self.wiki.validate_current(revision):
                return self._public(intent)
            self._verify_receipt(intent)
            gaps = self.wiki.validate_current(revision)
            row = self.store.finalize_publication(
                intent["publication_id"], receipt=intent["receipt"], current_gaps=gaps, recover_validation=True
            )
        return self._public(row)

    def _finish(self, intent, receipt):
        # Transport has strictly compared package and original bytes. Revalidate
        # local dependencies after I/O, then the store checks revision/review/epoch
        # again while atomically moving the published pointer.
        try:
            page = self.wiki.get_page(intent["page_id"], revision=intent["revision"])
            with self.wiki.dependency_guard(page["revision"]):
                checked = self.wiki.get_page(intent["page_id"], revision=intent["revision"])
                gaps = checked["gaps"] + self.wiki.validate_current(checked["revision"])
                row = self.store.finalize_publication(intent["publication_id"], receipt=receipt, current_gaps=gaps)
        except (ValueError, KeyError, StateConflict):
            row = self.store.finalize_publication(
                intent["publication_id"], receipt=receipt, current_gaps=[{"code": "AVAILABILITY_UNKNOWN"}]
            )
        return self._public(row)

    def verify_published(self, intent):
        if self.ingestion.archive is None:
            raise PublicationError("ARCHIVE_UNAVAILABLE")
        receipt = intent.get("receipt")
        if intent["status"] != "published" or not receipt:
            raise PublicationError("WIKI_NOT_PUBLISHED")
        return self._verify_receipt(intent)

    def _verify_receipt(self, intent):
        if self.ingestion.archive is None:
            raise PublicationError("ARCHIVE_UNAVAILABLE")
        receipt = intent["receipt"]
        try:
            if self.ingestion.archive.read(receipt) != intent["package"]:
                raise ArchiveIntegrityError("Wiki package changed")
            for key, digest in intent["asset_hashes"].items():
                if sha256(self.ingestion.archive.read_asset(receipt, key)).hexdigest() != digest:
                    raise ArchiveIntegrityError("Wiki asset changed")
        except ArchiveIntegrityError:
            raise PublicationError("INTEGRITY_FAILURE") from None
        except ArchiveError:
            raise PublicationError("ARCHIVE_UNAVAILABLE") from None
        return intent["package"]
