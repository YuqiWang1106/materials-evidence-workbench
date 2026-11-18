"""Website authority for derived lexical candidates, with persistent frozen inventory.

Build scans exact sources once. Search checks metadata and file-change tokens, then reads only selected bodies.
Stat tokens signal invalidation. A separate content hash verifies each selected body.
"""

import json
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select

from .core.clock import IO_DEADLINE
from .core.serialization import canonical, fingerprint
from .knowledge_contracts import (
    AssertionRef,
    EvidenceRef,
    RecordRef,
    SampleTestContextRef,
    SourcePassageRef,
    SourceSnapshotRef,
    WikiRevisionRef,
)
from .knowledge_index_contracts import IndexDocument, IndexSnapshot
from .knowledge_index_store import IndexFailure
from .knowledge_projection import project_saved_package


class WebsiteIndexAuthority:
    def __init__(self, knowledge, index):
        self.knowledge = knowledge
        self.index = index
        self.directory = knowledge.documents
        self.store = index.store
        self.snapshots = knowledge.snapshots
        self.bundle = None
        self.prepared_documents = None
        self.record_reads = {}
        self.labels = {}

    @contextmanager
    def budget(self, deadline):
        from .knowledge_service import READ_DEADLINE

        inherited = READ_DEADLINE.get()
        if inherited is not None:
            deadline = min(deadline, inherited)
        old_io = IO_DEADLINE.get()
        if old_io is not None:
            deadline = min(deadline, old_io)
        io = IO_DEADLINE.set(deadline)
        read = READ_DEADLINE.set(deadline)
        try:
            self.knowledge._deadline(deadline)
            yield
            self.knowledge._deadline(deadline)
        finally:
            IO_DEADLINE.reset(io)
            READ_DEADLINE.reset(read)

    def _corpus(self, scope):
        if scope.workspace_id != self.knowledge.workspace_id:
            raise IndexFailure("WORKSPACE_MISMATCH")
        # Ordinary read_corpus verifies all package bodies. Here only this immutable
        # manifest is read; per-hit package verification occurs in resolve().
        manifest = json.loads(self.snapshots._read("manifests", scope.corpus_snapshot_id))
        if self.directory:
            self.directory.refresh()
        self.knowledge.registry.check_manifest(manifest)
        if any(manifest[k] != getattr(scope, k) for k in ("workspace_id", "collection_id", "use_policy")):
            raise IndexFailure("SOURCE_USE_POLICY_DENIED")
        expected = (
            "wiki" if manifest.get("wiki_format") else "source_passage" if "source_snapshots" in manifest else "record"
        )
        if manifest.get("failures"):
            raise IndexFailure("INDEX_NOT_READY")
        if expected != scope.layer:
            raise IndexFailure("SOURCE_USE_POLICY_DENIED")
        if scope.layer != "wiki" and (
            self.directory is None or scope.collection_id != self.directory.adapter.collection_id
        ):
            raise IndexFailure("INDEX_NOT_READY")
        return manifest

    def _path_token(self, relative):
        path = self.store.root / relative
        if path.is_absolute() and not path.is_relative_to(self.store.root):
            raise IndexFailure("INDEX_STALE")
        if ".." in Path(relative).parts:
            raise IndexFailure("INDEX_STALE")
        for p in (path, *path.parents):
            if p == self.store.root.parent:
                break
            if p.is_symlink():
                raise IndexFailure("INDEX_STALE")
        try:
            s = path.stat()
        except FileNotFoundError:
            return None
        return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]

    def _relative(self, path):
        return str(Path(path).relative_to(self.store.root))

    def _signature(self, scope, bundle, deadline):
        self.knowledge._deadline(deadline)
        manifest = self._corpus(scope)
        if manifest != bundle["corpus"]:
            raise IndexFailure("INDEX_STALE")
        signature = {"paths": {}, "directories": {}, "sources": {}, "tasks": {}}
        for relative in bundle["paths"]:
            self.knowledge._deadline(deadline)
            signature["paths"][relative] = self._path_token(relative)
        for relative in bundle["directories"]:
            folder = self.store.root / relative
            self._path_token(relative)
            signature["directories"][relative] = {
                p.name: self._path_token(self._relative(p)) for p in sorted(folder.glob("*.json"))
            }
        for sid in manifest["sources"]:
            self.knowledge._deadline(deadline)
            signature["sources"][sid] = self.store.source_state(sid)
        if scope.layer == "record":
            versions = manifest.get("website_source_versions", {})
            if set(versions) != set(manifest["sources"]) or any(
                versions[s]
                != {k: signature["sources"][s].get(k) for k in ("generation", "active_task_id", "published_task_id")}
                for s in versions
            ):
                raise IndexFailure("INDEX_STALE")
            for descriptor in manifest["records"]:
                task = self.store.get_task(descriptor["task_id"])
                if (
                    task["state"] != "saved"
                    or task.get("receipt", {}).get("package_sha256") != descriptor["package_sha256"]
                ):
                    raise IndexFailure("INDEX_STALE")
                signature["tasks"][task["task_id"]] = fingerprint(task)
        elif scope.layer == "source_passage":
            for sid in manifest["sources"]:
                state = signature["sources"][sid]
                if not state:
                    raise IndexFailure("INDEX_STALE")
                task = self.store.get_task(state.get("active_task_id") or state.get("published_task_id"))
                signature["tasks"][task["task_id"]] = fingerprint(task)
        else:
            signature["wiki"] = self._wiki_signature(bundle, deadline)
        return signature

    def _wiki_signature(self, bundle, deadline):
        from .storage.models import (
            KnowledgeItem as Item,
        )
        from .storage.models import (
            KnowledgePublication as Publication,
        )
        from .storage.models import (
            KnowledgeReviewEvent as Review,
        )
        from .storage.models import (
            KnowledgeRevision as Revision,
        )
        from .storage.reading_repository import get_draft

        wiki = self.knowledge.wiki_adapter.wiki
        result = {"pages": {}, "drafts": {}}
        with self.index._read(deadline) as session:
            if not wiki.store._ledger_valid(session):
                raise IndexFailure("INDEX_STALE")
            identities = list(bundle["corpus"]["wiki_revisions"])
            seen = set()
            for identity in identities:
                key = (identity["page_id"], identity["revision_id"])
                if key in seen:
                    continue
                seen.add(key)
                if len(seen) > 100:
                    raise IndexFailure("INDEX_STALE")
                page = session.get(Item, identity["page_id"])
                if (
                    page is None
                    or page.workspace_id != self.knowledge.workspace_id
                    or page.availability in {"withdrawn", "deleted", "availability_unknown"}
                    or page.published_revision_id != identity["revision_id"]
                ):
                    raise IndexFailure("INDEX_STALE")
                rev = session.execute(
                    select(Revision.content_hash, Revision.dependency_manifest_hash).where(
                        Revision.revision_id == identity["revision_id"]
                    )
                ).one()
                review = session.scalar(
                    select(Review)
                    .where(Review.revision_id == identity["revision_id"])
                    .order_by(Review.created_at.desc(), Review.review_id.desc())
                )
                publication = session.execute(
                    select(
                        Publication.publication_id,
                        Publication.review_id,
                        Publication.status,
                        Publication.content_hash,
                        Publication.dependency_manifest_hash,
                    ).where(
                        Publication.page_id == page.page_id,
                        Publication.revision_id == identity["revision_id"],
                        Publication.status == "published",
                    )
                ).all()
                from .knowledge_table_wiki_policy import accepted_review

                dependencies = session.scalar(
                    select(Revision.dependency_manifest).where(Revision.revision_id == identity["revision_id"])
                )
                if (
                    not accepted_review(review, {"dependency_manifest": dependencies})
                    or not publication
                    or tuple(rev) != (identity["content_hash"], identity["dependency_manifest_hash"])
                ):
                    raise IndexFailure("INDEX_STALE")
                if (review.content_hash, review.dependency_manifest_hash) != tuple(rev):
                    raise IndexFailure("INDEX_STALE")
                if fingerprint(dependencies) != identity["dependency_manifest_hash"]:
                    raise IndexFailure("INDEX_STALE")
                for support in dependencies["supports"]:
                    ref = support["ref"]
                    if ref["kind"] == "study_unit_snapshot":
                        from .study_units import StudyUnitService

                        unit = StudyUnitService(wiki.ingestion_service, workspace_id=wiki.workspace_id).resolve(ref)
                        result.setdefault("study_units", {})[ref["unit_id"]] = fingerprint(unit)
                    if ref["kind"] == "wiki_revision":
                        if ref["workspace_id"] != self.knowledge.workspace_id:
                            raise IndexFailure("INDEX_STALE")
                        identities.append(
                            {k: ref[k] for k in ("page_id", "revision_id", "content_hash", "dependency_manifest_hash")}
                        )
                result["pages"][page.page_id] = {
                    "published": page.published_revision_id,
                    "epoch": page.epoch,
                    "review": review.review_id,
                    "publications": [list(p) for p in publication],
                }
            for source in bundle["corpus"]["sources"]:
                draft = get_draft(session, self.store.workspace_id, source)
                if draft is not None:
                    result["drafts"][source] = fingerprint(self.store._row(draft))
                    continue
                state = self.store.source_state(source)
                if not state:
                    raise IndexFailure("INDEX_STALE")
                tids = {state.get("active_task_id"), state.get("published_task_id")} - {None}
                if not tids:
                    raise IndexFailure("INDEX_STALE")
                for tid in tids:
                    task = self.store.get_task(tid)
                    if task.get("source_type") not in {"paper", "patent"}:
                        raise IndexFailure("INDEX_STALE")
                    result.setdefault("document_tasks", {})[tid] = fingerprint(task)
        return result

    def _source_label(self, source_id):
        if source_id not in self.labels:
            _, task = self.directory.member(source_id)
            names = [self.store.get_asset(a)["original_name"] for a in task["asset_ids"]]
            self.labels[source_id] = ", ".join(names)[:512] or source_id
        return self.labels[source_id]

    def _document(self, ref, kind, text):
        marker = "\n[index text truncated]"
        if kind != "wiki_revision":
            payload = json.loads(text)
            sid = ref.source_snapshot_ref.source_id if ref.kind == "source_passage" else ref.source_id
            payload["source_label"] = self._source_label(sid)
            text = canonical(payload)
            if kind in {"source_passage", "record_evidence"} and len(text.encode()) > 32768:
                quote = payload["quote"]
                payload["index_text_truncated"] = True
                low, high = 0, len(quote)
                while low < high:
                    middle = (low + high + 1) // 2
                    payload["quote"] = quote[:middle]
                    if len(canonical(payload).encode()) <= 32768:
                        low = middle
                    else:
                        high = middle - 1
                payload["quote"] = quote[:low]
                text = canonical(payload)
        if len(text.encode()) > 32768:
            text = text.encode()[: 32768 - len(marker)].decode("utf-8", errors="ignore") + marker
        return IndexDocument(ref=ref, content_kind=kind, text=text, eligible=True)

    def _record_documents(self, ref, package):
        p = project_saved_package(package, set())
        owner = ref.model_dump(exclude={"kind"})
        objects = {o["object_id"]: o for o in p["objects"]}
        rows = []
        for obj in p["objects"]:
            if obj["kind"] == "sample":
                target = SampleTestContextRef(
                    kind="sample_test_context", sample_ref=obj["object_id"], test_ref=None, **owner
                )
                rows.append(self._document(target, "record_object", canonical(obj)))
        if not rows:
            rows.append(self._document(ref, "record_object", canonical(p["header"])))
        for a in p["assertions"]:
            target = AssertionRef(kind="assertion", assertion_id=a["assertion_id"], **owner)
            rows.append(
                self._document(
                    target, "record_assertion", canonical({"subject": objects.get(a["subject_ref"]), "assertion": a})
                )
            )
        assets = {a["asset_id"]: a for a in p["assets"]}
        for evidence in p["evidence"]:
            if not evidence.get("content"):
                continue
            target = EvidenceRef(
                kind="evidence",
                evidence_id=evidence["evidence_id"],
                asset_sha256=assets[evidence["asset_ref"]]["sha256"],
                **owner,
            )
            # Saved legacy verbatim flags alone do not establish reader provenance.
            rows.append(
                self._document(
                    target, "record_evidence", canonical({"quote": evidence["content"], "content_kind": "unknown"})
                )
            )
        return rows

    def _source_documents(self, scope, member, selected=None):
        resolver = self.knowledge.source_resolver
        _, index, _ = resolver._frozen(member, scope.use_policy)
        rows = []
        for segment in index["segments"]:
            if selected is not None and segment["segment_id"] != selected.segment_id:
                continue
            if segment.get("bbox"):
                self.snapshots.get_blob(segment["bbox"]["page_image_sha256"])
            ref = SourcePassageRef(
                kind="source_passage",
                workspace_id=scope.workspace_id,
                corpus_snapshot_id=scope.corpus_snapshot_id,
                source_snapshot_ref=member,
                segment_id=segment["segment_id"],
                quote_sha256=sha256(segment["quote"].encode()).hexdigest(),
                physical_page=segment["physical_page"],
                location_status=segment["location_status"],
                bbox=segment.get("bbox"),
            )
            rows.append(
                self._document(
                    ref,
                    "source_passage",
                    canonical(
                        {
                            "quote": segment["quote"],
                            "content_kind": segment["content_kind"],
                            "provenance": segment.get("provenance", {}),
                        }
                    ),
                )
            )
        return rows

    def prepare(self, scope, *, deadline):
        with self.budget(deadline):
            manifest = self._corpus(scope)
            bundle = {
                "scope": scope.model_dump(mode="json"),
                "corpus": manifest,
                "paths": [],
                "directories": [],
                "documents": [],
            }
            paths = set()
            directories = set()
            documents = []
            built_records = []
            paths.add(self._relative(self.snapshots._path("manifests", scope.corpus_snapshot_id)))
            if scope.layer == "record":
                adapter = self.directory.adapter
                for record in adapter.read_corpus(
                    scope.corpus_snapshot_id, use_policy=scope.use_policy, mode="live_readback"
                ):
                    self.knowledge._deadline(deadline)
                    built_records.append(record)
                    documents.extend(self._record_documents(record.ref, record.package))
                    task = self.store.get_task(record.ref.task_id)
                    directories.add(
                        self._relative(self.directory.service._document_workspace(task["task_id"]) / "reviews")
                    )
                    paths.add(self._relative(self.snapshots._path("objects", record.ref.package_sha256)))
                    for aid in task["asset_ids"]:
                        paths.add(self._relative(self.store.asset_path(aid)))
            elif scope.layer == "source_passage":
                for raw in manifest["source_snapshots"]:
                    member = SourceSnapshotRef.model_validate(raw)
                    description = self.directory.description(member.source_id)
                    task = description["task"]
                    root = self.store.pending_dir(task["task_id"]) / "document-v2"
                    if (root / "artifacts").exists():
                        root = root / "artifacts" / fingerprint(description["run"])
                    for name in ("document-ir.json", "source-registry.json"):
                        paths.add(self._relative(root / name))
                    for aid in task["asset_ids"]:
                        paths.add(self._relative(self.store.asset_path(aid)))
                    for digest in (member.manifest_sha256, member.source_index_sha256, member.source_asset_sha256):
                        paths.add(self._relative(self.snapshots._path("objects", digest)))
                    source_docs = self._source_documents(scope, member)
                    for doc in source_docs:
                        if doc.ref.bbox:
                            paths.add(self._relative(self.snapshots._path("objects", doc.ref.bbox.page_image_sha256)))
                    documents.extend(source_docs)
            else:
                adapter = self.knowledge.wiki_adapter
                if adapter is None:
                    raise IndexFailure("INDEX_NOT_READY")
                for identity in manifest["wiki_revisions"]:
                    ref = WikiRevisionRef(
                        kind="wiki_revision",
                        workspace_id=scope.workspace_id,
                        corpus_snapshot_id=scope.corpus_snapshot_id,
                        **identity,
                    )
                    row, _ = adapter.resolve(ref, use_policy=scope.use_policy)
                    documents.append(self._document(ref, "wiki_revision", row.body))
                    page = adapter.wiki.get_page(identity["page_id"], revision=identity["revision_id"])
                    self._wiki_paths(page, paths, directories)
            bundle["paths"] = sorted(paths)
            bundle["directories"] = sorted(directories)
            bundle["signature"] = self._signature(scope, bundle, deadline)
            # A review may arrive after the initial remote read. Validate cached
            # immutable packages against the live review ledger AFTER capturing
            # file tokens; core publication then requires those tokens unchanged.
            for record in built_records:
                state = adapter._state(self.store.get_task(record.ref.task_id), record.package)
                adapter._policy(state, scope.use_policy)
                descriptor = next(r for r in manifest["records"] if r["task_id"] == record.ref.task_id)
                if (
                    state.review_version != descriptor["review_version"]
                    or state.source_review != descriptor["source_review"]
                ):
                    raise IndexFailure("INDEX_STALE")
            if scope.layer == "source_passage":
                for raw in manifest["source_snapshots"]:
                    member = SourceSnapshotRef.model_validate(raw)
                    # One final authoritative read after recording change tokens,
                    # not one complete document read per indexed segment.
                    self.knowledge.source_resolver._frozen(member, scope.use_policy)
            elif scope.layer == "wiki":
                for doc in documents:
                    self.knowledge.wiki_adapter.resolve(doc.ref, use_policy=scope.use_policy)
            bundle["documents"] = [d.model_dump(mode="json") for d in documents]
            digest = self.snapshots.put_json(bundle)
            source_manifest = {
                "authority_version": "website-index-authority/1",
                "authority_blob": digest,
                "index_documents": {d.key: d.document_hash for d in documents},
            }
            snapshot = IndexSnapshot(scope=scope, source_manifest=source_manifest, documents=documents)
            self.bundle = (bundle, source_manifest)
            self.prepared_documents = {d.key: d for d in documents}
            return snapshot

    def _wiki_paths(self, page, paths, directories=None, seen=None):
        if directories is None:
            directories = set()
        if seen is None:
            seen = set()
        key = page["revision"]["revision_id"]
        if key in seen:
            return
        seen.add(key)
        if len(seen) > 100:
            raise IndexFailure("INDEX_NOT_READY")
        service = self.knowledge.wiki_adapter.wiki.ingestion_service
        # Frozen dependency files are read/validated by the Wiki authority above.
        # Subsequent queries watch only these files, not every page body.
        for dep in page["revision"]["dependency_manifest"]["dependencies"]:
            if dep["kind"] == "raw_table":
                ref = dep["ref"]
                paths.add(self._relative(self.store.asset_path(ref["asset_id"])))
                raw = service._raw_artifacts()
                for name in ("artifact.json", "manifest.json"):
                    paths.add(self._relative(raw.path("raw", ref["table_version_id"]) / name))
            elif dep["kind"] == "study_unit_snapshot":
                from .study_units import StudyUnitService

                unit = StudyUnitService(service, workspace_id=self.knowledge.workspace_id).resolve(dep["ref"])
                for source in unit["snapshot"]["payload"]["source_refs"]:
                    paths.add(self._relative(self.store.asset_path(source["asset_id"])))
            elif dep["kind"] == "chat_turn":
                files = service._table_chats()
                root = files.turn_path(dep["chat_id"], dep["turn_id"])
                for name in ("turn.json", "input.json"):
                    paths.add(self._relative(root / name))
                paths.add(self._relative(files.chat_path(dep["chat_id"]) / "chat.json"))
            elif dep["kind"] == "table_analysis_artifact":
                ref = dep["ref"]
                if ref.get("chat_kind") == "research":
                    from .research_chat_store import ResearchChatStore

                    files = ResearchChatStore(self.store)
                    root = files.turn_path(ref["chat_id"], ref["turn_id"])
                    for name in ("turn.json", "input.json"):
                        paths.add(self._relative(root / name))
                    paths.add(self._relative(files.chat_path(ref["chat_id"]) / "chat.json"))
                else:
                    files = service._table_chats()
                paths.add(
                    self._relative(
                        files.turn_path(ref["chat_id"], ref["turn_id"]) / "artifacts" / (ref["artifact_id"] + ".json")
                    )
                )
            elif dep["kind"] == "research_turn":
                from .research_chat_store import ResearchChatStore

                files = ResearchChatStore(self.store)
                root = files.turn_path(dep["chat_id"], dep["turn_id"])
                for name in ("turn.json", "input.json"):
                    paths.add(self._relative(root / name))
            elif dep["kind"] == "research_reference":
                ref = dep["ref"]
                if ref["kind"] in {"record_revision", "sample_test_context", "assertion", "evidence"}:
                    task = self.store.get_task(ref["task_id"])
                    directories.add(self._relative(service._document_workspace(task["task_id"]) / "reviews"))
                    paths.add(self._relative(self.snapshots._path("objects", ref["package_sha256"])))
                    for aid in task["asset_ids"]:
                        paths.add(self._relative(self.store.asset_path(aid)))
                elif ref["kind"] == "source_passage":
                    member = ref["source_snapshot_ref"]
                    description = self.directory.description(member["source_id"])
                    root = self.store.pending_dir(description["task"]["task_id"]) / "document-v2"
                    if (root / "artifacts").exists():
                        root = root / "artifacts" / fingerprint(description["run"])
                    for name in ("document-ir.json", "source-registry.json"):
                        paths.add(self._relative(root / name))
                    for aid in description["task"]["asset_ids"]:
                        paths.add(self._relative(self.store.asset_path(aid)))
                elif ref["kind"] == "wiki_revision":
                    upstream = self.knowledge.wiki_adapter.wiki.store.get_page(ref["page_id"], ref["revision_id"])
                    self._wiki_paths(upstream, paths, directories, seen)
                elif ref["kind"] in {"table_snapshot", "table_analysis_artifact", "study_unit_snapshot"}:
                    # Unified summaries also retain a typed material dependency,
                    # whose branch above tracks the actual files and owner.
                    expected = {"table_snapshot": "raw_table"}.get(ref["kind"], ref["kind"])
                    if not any(
                        d["kind"] == expected and d.get("ref") == ref
                        for d in page["revision"]["dependency_manifest"]["dependencies"]
                    ):
                        raise IndexFailure("INDEX_NOT_READY")
                else:
                    raise IndexFailure("INDEX_NOT_READY")
            else:
                raise IndexFailure("INDEX_NOT_READY")

    def _load(self, scope, deadline):
        if self.bundle is None:
            manifest = self.index.binding(scope, deadline=deadline)
            if manifest.get("authority_version") != "website-index-authority/1":
                raise IndexFailure("INDEX_STALE")
            digest = manifest["authority_blob"]
            cached = self.knowledge._index_bundles.get(digest)
            if cached is not None:
                self.bundle = cached
                return self.bundle
            bundle = self.snapshots.get_json(digest)
            if bundle["scope"] != scope.model_dump(mode="json"):
                raise IndexFailure("INDEX_STALE")
            snapshot = IndexSnapshot(scope=scope, source_manifest=manifest, documents=bundle["documents"])
            self.bundle = (bundle, snapshot.source_manifest)
            if len(self.knowledge._index_bundles) >= 8:
                self.knowledge._index_bundles.pop(next(iter(self.knowledge._index_bundles)))
            self.knowledge._index_bundles[digest] = self.bundle
        return self.bundle

    def current_manifest(self, scope, *, deadline):
        with self.budget(deadline):
            bundle, manifest = self._load(scope, deadline)
            if self._signature(scope, bundle, deadline) != bundle["signature"]:
                raise IndexFailure("INDEX_STALE")
            return manifest

    def resolve(self, scope, ref, content_kind, *, deadline):
        with self.budget(deadline):
            self.current_manifest(scope, deadline=deadline)
            if self.prepared_documents is not None:
                key = fingerprint([ref.model_dump(mode="json"), content_kind])
                if key not in self.prepared_documents:
                    raise IndexFailure("INDEX_STALE")
                return self.prepared_documents[key]
            if scope.layer == "record":
                bundle, _ = self._load(scope, deadline)
                descriptor = next((r for r in bundle["corpus"]["records"] if r["task_id"] == ref.task_id), None)
                if descriptor is None:
                    raise IndexFailure("INDEX_STALE")
                owner = RecordRef(
                    kind="record_revision",
                    workspace_id=scope.workspace_id,
                    corpus_snapshot_id=scope.corpus_snapshot_id,
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
                key = (owner.task_id, owner.package_sha256)
                if key not in self.record_reads:
                    self.record_reads[key] = self.directory.adapter.read_saved_package(
                        owner.task_id, owner.package_sha256, mode="live_readback"
                    )
                package = self.record_reads[key]
                state = self.directory.adapter._state(self.store.get_task(owner.task_id), package)
                self.directory.adapter._policy(state, scope.use_policy)
                if (
                    state.review_version != descriptor["review_version"]
                    or state.source_review != descriptor["source_review"]
                ):
                    raise IndexFailure("INDEX_STALE")
                docs = self._record_documents(owner, package)
            elif scope.layer == "source_passage":
                docs = self._source_documents(scope, ref.source_snapshot_ref, selected=ref)
            else:
                row, _ = self.knowledge.wiki_adapter.resolve(ref, use_policy=scope.use_policy)
                docs = [self._document(ref, "wiki_revision", row.body)]
            result = next((d for d in docs if d.ref == ref and d.content_kind == content_kind), None)
            if result is None:
                raise IndexFailure("INDEX_STALE")
            self.current_manifest(scope, deadline=deadline)
            return result

    def display(self, scope, hit, deadline):
        bundle, _ = self._load(scope, deadline)
        doc = next(
            d for d in bundle["documents"] if d["ref"] == hit["ref"] and d["content_kind"] == hit["content_kind"]
        )
        text = doc["text"]
        kind = "unknown"
        provenance = None
        indexed_truncated = text.endswith("\n[index text truncated]")
        source_label = (
            text.split("\n", 1)[0][:512]
            if scope.layer == "wiki"
            else self._source_label(hit["ref"].get("source_id") or hit["ref"]["source_snapshot_ref"]["source_id"])
        )
        if hit["content_kind"] in {"source_passage", "record_evidence"}:
            payload = json.loads(text)
            text = payload["quote"]
            kind = payload["content_kind"]
            provenance = payload.get("provenance")
            indexed_truncated = payload.get("index_text_truncated", False)
        return {
            "snippet": text[:800],
            "snippet_truncated": len(text) > 800,
            "indexed_text_truncated": indexed_truncated,
            "source_label": source_label,
            "source_content_kind": kind,
            "provenance": provenance,
        }
