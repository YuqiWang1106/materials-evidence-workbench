"""Website-only document scopes and existing artifact reads; never run a reader/model."""

import base64
import json
from hashlib import sha256

from .core import fingerprint
from .knowledge_contracts import VERSION, CorpusResult
from .knowledge_passage import FrozenSourceSnapshotResolver, SourceReviewState
from .knowledge_scope import CollectionScope
from .knowledge_snapshot import KnowledgeIntegrityError
from .knowledge_source import IngestionPublishedRecordAdapter

COLLECTION = "documents"
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_SEGMENT_CHARS = 32000


def entity_only_receipt(receipt):
    """Recognize the existing entity publication contract, never a damaged record archive.

    A formal or mixed receipt must still pass the record adapter's integrity checks.
    Missing/unknown receipt shapes are deliberately not a reason to skip that adapter.
    """
    return (
        isinstance(receipt, dict)
        and isinstance(receipt.get("entity_graph"), dict)
        and isinstance(receipt.get("operator"), str)
        and set(receipt) <= {"entity_graph", "operator", "entity_supplements"}
    )


def block_provenance(description, block, quote):
    reader = block.get("source", "unknown")
    kind = (
        "parsed"
        if reader in {"mineru", "pymupdf", "pymupdf_unverified", "local_ocr"}
        else "model_transcription"
        if reader in {"visual", "model", "model_visual", "handwriting"}
        else "unknown"
    )
    if kind == "unknown":
        reader = "unknown"
    text = block.get("text", "")
    relation = (
        "exact_segment"
        if quote and quote == text
        else "exact_substring"
        if quote and isinstance(text, str) and quote in text
        else "unverified"
    )
    if relation == "unverified":
        kind = "unknown"
    provenance = {
        "reader": reader,
        "task_id": description["task"]["task_id"],
        "processing_run_id": description["run"],
        "asset_id": block["asset_ref"],
        "block_id": block["block_id"],
        "ir_region": block.get("ir_region"),
        "artifact_sha256": description["artifact_hash"],
        "quote_relation": relation,
        "requires_image_verification": bool(block.get("requires_image_verification")),
    }
    for key in ("reading_sha256", "verification_state"):
        if block.get(key):
            provenance[key] = block[key]
    return kind, provenance


class WebsitePublishedAdapter(IngestionPublishedRecordAdapter):
    def __init__(self, directory):
        self.directory = directory
        super().__init__(directory.service, directory.registry, directory.workspace_id, COLLECTION, directory.snapshots)

    def list_saved_heads(self, **options):
        self.directory.refresh()
        return super().list_saved_heads(**options)

    def _task(self, task_id, expected_hash):
        self.directory.refresh()
        return super()._task(task_id, expected_hash)

    def build_corpus(self, **options):
        members = options.get("source_ids")
        if members is None:
            members = sorted(
                {
                    t["source_id"]
                    for t in self.list_saved_heads(
                        use_policy=options.get("use_policy", "source_reviewed"),
                        include_history=options.get("include_history", False),
                    )
                }
            )
        before = {s: self.directory.signature(s) for s in members}
        cid = super().build_corpus(**options)
        if before != {s: self.directory.signature(s) for s in members}:
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        manifest = self.snapshots.read_corpus(cid)
        manifest["website_source_versions"] = before
        return self.snapshots.publish_corpus(manifest)

    def corpus_manifest(self, corpus_snapshot_id):
        self.directory.refresh()
        manifest = super().corpus_manifest(corpus_snapshot_id)
        versions = manifest.get("website_source_versions")
        if versions is None or set(versions) != set(manifest["sources"]):
            raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        if any(v != self.directory.signature(s) for s, v in versions.items()):
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        return manifest


class WebsiteDocuments:
    def __init__(self, service, workspace_id, registry, snapshots):
        self.service = service
        self.workspace_id = workspace_id
        self.registry = registry
        self.snapshots = snapshots
        self.knowledge = None
        self.refresh()
        self.adapter = WebsitePublishedAdapter(self)
        self.resolver = FrozenSourceSnapshotResolver(snapshots, registry, workspace_id, COLLECTION, self.review_state)

    def checkpoint(self):
        if self.knowledge is not None:
            self.knowledge._deadline(float("inf"))

    def refresh(self):
        tasks = {t["task_id"]: t for t in self.service.store.list_tasks()}
        members = {}
        for source_id in sorted({t["source_id"] for t in tasks.values()}):
            state = self.service.store.source_state(source_id)
            if not state:
                continue
            task = tasks.get(state.get("active_task_id")) or tasks.get(state.get("published_task_id"))
            if task and task.get("source_type") in {"paper", "patent"}:
                members[source_id] = (state, task)
        self.registry.replace(
            CollectionScope(
                self.workspace_id, COLLECTION, "website-documents-1", frozenset(members), "website-document-policy-1"
            )
        )
        return members

    def member(self, source_id):
        member = self.refresh().get(source_id)
        if member is None:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        return member

    def signature(self, source_id):
        state, task = self.member(source_id)
        return {k: state.get(k) for k in ("generation", "active_task_id", "published_task_id")}

    def inventory(self):
        from .document_progress import document_title

        result = []
        for source_id, (state, task) in self.refresh().items():
            self.checkpoint()
            assets = [self.service.store.get_asset(a) for a in task["asset_ids"]]
            try:
                self.description(source_id, verify_original=False)
                reading = "available"
                reason = None
            except KnowledgeIntegrityError as exc:
                reading = "not_ready"
                reason = exc.code
            published = state.get("published_task_id")
            formal = self.service.store.get_task(published) if published else None
            result.append(
                {
                    "source_id": source_id,
                    "source_type": task["source_type"],
                    "generation": state["generation"],
                    "task_id": task["task_id"],
                    "published_task_id": published,
                    "label": document_title(self.service.store, task),
                    "assets": [
                        {"asset_id": a["asset_id"], "sha256": a["sha256"], "name": a["original_name"]} for a in assets
                    ],
                    "formal_status": (
                        "entity_graph_only" if entity_only_receipt(formal.get("receipt")) else "saved_receipt"
                    )
                    if formal and formal["state"] == "saved"
                    else "not_saved",
                    "record_status": (
                        "entity_graph"
                        if formal and formal["state"] == "saved" and entity_only_receipt(formal.get("receipt"))
                        else "saved_receipt"
                        if formal and formal["state"] == "saved"
                        else "not_saved"
                    ),
                    "reading_status": reading,
                    "reading_reason": reason,
                    "source_review": "unreviewed",
                }
            )
        return {"workspace_id": self.workspace_id, "collection_id": COLLECTION, "sources": result}

    @staticmethod
    def _read(path):
        if path.is_symlink() or not path.is_file():
            raise KnowledgeIntegrityError("INDEX_NOT_READY")
        try:
            if path.stat().st_size > MAX_INDEX_BYTES:
                raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
            data = path.read_bytes()
            if len(data) > MAX_INDEX_BYTES:
                raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
            value = json.loads(data)
        except (OSError, ValueError):
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH") from None
        return value, sha256(data).hexdigest()

    def description(self, source_id, *, verify_original=True):
        state, task = self.member(source_id)
        if task["state"] not in {"saved", "needs_review"} or task["attempt"] < 1:
            raise KnowledgeIntegrityError("INDEX_NOT_READY")
        root = self.service.store.pending_dir(task["task_id"]) / "document-v2"
        run = task["task_id"] + ":attempt" + str(task["attempt"])
        artifacts = root / "artifacts"
        if artifacts.exists():
            root = artifacts / fingerprint(run)
        if root.is_symlink():
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
        reading, reading_hash = self._read(root / "document-ir.json")
        registry, registry_hash = ({}, None)
        if (root / "source-registry.json").exists():
            registry, registry_hash = self._read(root / "source-registry.json")
        assets = {a: self.service.store.get_asset(a) for a in task["asset_ids"]}
        if verify_original:
            for a in assets:
                self.checkpoint()
                try:
                    self.service.store.asset_bytes(a)
                except Exception:
                    raise KnowledgeIntegrityError("INTEGRITY_FAILURE") from None
        from .document_source_index import build_source_index

        index = build_source_index(assets, registry, reading)
        if index.error or index.asset_errors or index.block_errors:
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
        revision = source_id + ":source:" + fingerprint(sorted((a, r["sha256"]) for a, r in assets.items()))[:20]
        version = fingerprint(
            {
                "source": self.signature(source_id),
                "task_id": task["task_id"],
                "run": run,
                "assets": {a: r["sha256"] for a, r in assets.items()},
                "reading": reading_hash,
                "registry": registry_hash,
            }
        )
        return {
            "task": task,
            "assets": assets,
            "index": index,
            "source_revision_id": revision,
            "review_version": version,
            "run": run,
            "reading_hash": reading_hash,
            "artifact_hash": registry_hash if registry else reading_hash,
        }

    def review_state(self, source_id, revision):
        try:
            description = self.description(source_id)
        except KnowledgeIntegrityError as exc:
            if exc.code == "INDEX_NOT_READY":
                return SourceReviewState(revision, "unavailable", "unreviewed", False)
            raise
        return SourceReviewState(description["source_revision_id"], description["review_version"], "unreviewed", True)

    def evidence_provenance(self, record, evidence):
        if not evidence.get("program_block_id"):
            return {}
        try:
            description = self.description(record.ref.source_id)
        except KnowledgeIntegrityError:
            return {}
        if (
            description["task"]["task_id"] != record.ref.task_id
            or description["source_revision_id"] != record.ref.source_revision_id
            or description["run"] != record.package["header"].get("processing_run_id")
        ):
            return {}
        block = description["index"].registry.get(evidence["program_block_id"])
        if not block or block["asset_ref"] != evidence["asset_ref"]:
            return {}
        kind, provenance = block_provenance(description, block, evidence.get("content"))
        return {"effective_content_kind": kind, "provenance": provenance}

    def _segments(self, description, asset_id, original):
        import pymupdf

        from .document_evidence_location import EvidenceLocator
        from .knowledge_preview import MAX_ASSET_BYTES, MAX_EDGE, _image

        if len(original) > MAX_ASSET_BYTES:
            raise KnowledgeIntegrityError("PREVIEW_UNSUPPORTED")
        _asset = description["assets"][asset_id]
        segments = []
        images = {}
        blocks = [b for b in description["index"].registry.values() if b["asset_ref"] == asset_id and b.get("text")]
        if not blocks or len(blocks) > 10000:
            raise KnowledgeIntegrityError("INDEX_NOT_READY")
        try:
            with pymupdf.open(stream=original, filetype="pdf") as pdf:
                for block in blocks:
                    self.checkpoint()
                    text = block["text"]
                    page = block["page"]
                    if not isinstance(text, str) or len(text) > MAX_SEGMENT_CHARS:
                        raise KnowledgeIntegrityError("PREVIEW_UNSUPPORTED")
                    if page > len(pdf):
                        raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
                    kind, provenance = block_provenance(description, block, text)
                    segment = {
                        "segment_id": block["block_id"],
                        "quote": text,
                        "content_kind": kind,
                        "provenance": provenance,
                        "physical_page": page,
                        "location_status": "page_only",
                        "bbox": None,
                    }
                    anchor = block.get("anchor") or {}
                    coordinates = anchor.get("coordinate_region")
                    reliable = all(
                        anchor.get(k) in {None, "located", "page_only"}
                        for k in ("location_status", "localization_status")
                    )
                    same_page = isinstance(coordinates, dict) and (
                        "page" not in coordinates or type(coordinates["page"]) is int and coordinates["page"] == page
                    )
                    if reliable and same_page and coordinates.get("precision") not in {"unknown", "page_bbox"}:
                        region = EvidenceLocator._region(coordinates, pdf[page - 1], page)
                        if region:
                            if page not in images:
                                source = pdf[page - 1]
                                scale = min(1.5, MAX_EDGE / max(source.rect.width, source.rect.height))
                                image = _image(source.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False))
                                self.snapshots.put_blob(base64.b64decode(image.data_uri.split(",", 1)[1]))
                                images[page] = image
                            width, height = pdf[page - 1].rect.width, pdf[page - 1].rect.height
                            x0, y0, x1, y1 = region["bbox"]
                            segment["bbox"] = {
                                "x0": x0 / width,
                                "y0": y0 / height,
                                "x1": x1 / width,
                                "y1": y1 / height,
                                "coordinate_space": "normalized_page",
                                "page_image_sha256": images[page].sha256,
                            }
                            segment["location_status"] = "located"
                    segments.append(segment)
        except KnowledgeIntegrityError:
            raise
        except Exception:
            raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH") from None
        return sorted(segments, key=lambda s: (s["physical_page"], s["segment_id"]))

    def freeze(self, request):
        if request.collection_id != COLLECTION:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        members = self.refresh()
        if not set(request.source_ids) <= set(members):
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        before = {s: self.signature(s) for s in request.source_ids}
        records = []
        sources = []
        if request.kind == "formal_records":
            cid = self.adapter.build_corpus(
                use_policy=request.use_policy,
                include_history=request.use_policy == "history",
                source_ids=request.source_ids,
            )
            records = [r.ref for r in self.adapter.read_corpus(cid, use_policy=request.use_policy)]
        else:
            if request.use_policy != "explore":
                raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
            descriptions = {s: self.description(s) for s in request.source_ids}
            for sid, description in descriptions.items():
                for asset_id in description["assets"]:
                    self.checkpoint()
                    original = self.service.store.asset_bytes(asset_id)
                    index = {
                        "version": "website-source-index/1",
                        "segments": self._segments(description, asset_id, original),
                    }
                    sources.append(
                        self.resolver.freeze(
                            sid,
                            description["source_revision_id"],
                            original,
                            index,
                            review_version=description["review_version"],
                        )
                    )
            cid = self.resolver.build_corpus(sources, use_policy=request.use_policy)
        if before != {s: self.signature(s) for s in request.source_ids}:
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        return CorpusResult(
            schema_version=VERSION,
            request_id=request.request_id,
            workspace_id=self.workspace_id,
            collection_id=COLLECTION,
            kind=request.kind,
            corpus_snapshot_id=cid,
            use_policy=request.use_policy,
            source_ids=sorted(request.source_ids),
            record_refs=records,
            source_snapshot_refs=sources,
        )


def website_knowledge(service, wiki_service, wiki_adapter):
    from .knowledge_service import KnowledgeService

    documents = WebsiteDocuments(service, wiki_service.workspace_id, wiki_service.registry, wiki_service.snapshot_store)
    knowledge = KnowledgeService(
        wiki_service.workspace_id,
        wiki_service.registry,
        {COLLECTION: documents.adapter},
        wiki_service.snapshot_store,
        source_resolver=documents.resolver,
        wiki_adapter=wiki_adapter,
    )
    from .knowledge_index_store import KnowledgeIndexStore

    knowledge.lexical_index = KnowledgeIndexStore(service.store)
    knowledge.documents = documents
    documents.knowledge = knowledge
    return knowledge
