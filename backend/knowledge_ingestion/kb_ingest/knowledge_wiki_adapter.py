"""Current reviewed Wiki retrieval, separate from experimental assertions."""

from __future__ import annotations

from .core import fingerprint
from .knowledge_contracts import WikiResult, WikiRevisionRef
from .knowledge_publication import PublicationError
from .knowledge_snapshot import KnowledgeIntegrityError


class KnowledgeWikiAdapter:
    def __init__(self, wiki, publication):
        self.wiki, self.publication = wiki, publication
        self.workspace_id, self.collection_id = wiki.workspace_id, wiki.collection_id
        self.registry, self.snapshots = wiki.registry, wiki.snapshot_store

    def _published_page(self, page_id):
        head = self.wiki.get_page(page_id)
        if head["availability"] in {"withdrawn", "deleted", "availability_unknown"} or not head.get(
            "published_revision_id"
        ):
            return None
        page = self.wiki.get_page(page_id, revision=head["published_revision_id"])
        if page.get("gaps") or not page.get("review") or self.wiki.validate_current(page["revision"]):
            return None
        return page

    def _current(self, page_id):
        page = self._published_page(page_id)
        if page is None:
            return None
        revision = page["revision"]
        intent = self.wiki.store.published_publication(page_id, revision["revision_id"])
        if intent is None:
            return None
        try:
            self.publication.verify_published(intent)
        except PublicationError as exc:
            raise KnowledgeIntegrityError(exc.code) from None
        # A new draft does not replace the published pointer. Check that exact
        # pointer's own review and dependencies again after external readback.
        latest = self._published_page(page_id)
        if latest is None or latest["revision"]["revision_id"] != revision["revision_id"]:
            return None
        return latest

    @staticmethod
    def _identity(page):
        r = page["revision"]
        return {
            "page_id": page["page_id"],
            "revision_id": r["revision_id"],
            "content_hash": r["content_hash"],
            "dependency_manifest_hash": r["dependency_manifest_hash"],
        }

    def _manifest(self, corpus):
        manifest = self.snapshots.read_corpus(corpus)
        scope = self.registry.check_manifest(manifest)
        if scope.workspace_id != self.workspace_id or scope.collection_id != self.collection_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        if manifest.get("wiki_format") != "current-wiki-corpus-v1" or manifest.get("records") != []:
            raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        return manifest

    def _row(self, page, corpus):
        r = page["revision"]
        content = r["content"]
        sections = content.get("sections", [])
        body = "\n\n".join(
            [content["title"], content.get("research_question", "")]
            + ["\n".join([s.get("heading", "")] + [c["text"] for c in s.get("claims", [])]) for s in sections]
        )
        refs = [s["ref"] for s in r["supports"]]
        return WikiResult(
            kind="wiki_revision",
            result_id=r["revision_id"],
            ref=WikiRevisionRef(
                kind="wiki_revision", workspace_id=self.workspace_id, corpus_snapshot_id=corpus, **self._identity(page)
            ),
            title=content["title"],
            body=body,
            content=content,
            dependency_refs=refs,
            claim_types=sorted({c["claim_type"] for s in sections for c in s.get("claims", [])}),
        )

    def resolve(self, ref, *, use_policy="source_reviewed"):
        if ref.workspace_id != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        if use_policy not in {"source_reviewed", "explore"}:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        manifest = self._manifest(ref.corpus_snapshot_id)
        identity = {k: getattr(ref, k) for k in ("page_id", "revision_id", "content_hash", "dependency_manifest_hash")}
        if identity not in manifest.get("wiki_revisions", []):
            raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        try:
            page = self._current(ref.page_id)
        except (ValueError, KeyError) as exc:
            if isinstance(exc, KnowledgeIntegrityError):
                raise
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED") from None
        if page is None:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        if self._identity(page) != identity:
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        return self._row(page, ref.corpus_snapshot_id), manifest

    def source_ids(self, manifest):
        sources = {
            d["ref"]["source_id"] for d in manifest["dependencies"] if d["kind"] in {"raw_table", "study_unit_snapshot"}
        }
        research = manifest.get("research_summary")
        if research:
            from .research_summary_unified import is_unified, source_ids

            sources.update(
                source_ids(manifest["supports"], wiki=self.wiki)
                if is_unified(manifest)
                else research["selection"]["source_ids"]
            )
        return sources

    def freeze_selected(self, source_ids):
        """Filter trusted dependency metadata before resolving unrelated content."""
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from .storage.models import KnowledgeItem, KnowledgeRevision

        selected = set(source_ids)
        candidates = []
        with Session(self.wiki.ingestion_service.store.engine) as session:
            rows = session.execute(
                select(KnowledgeItem.page_id, KnowledgeRevision.dependency_manifest)
                .join(KnowledgeRevision, KnowledgeRevision.revision_id == KnowledgeItem.published_revision_id)
                .where(
                    KnowledgeItem.workspace_id == self.workspace_id,
                    KnowledgeItem.availability.notin_(["withdrawn", "deleted", "availability_unknown"]),
                )
            )
            for page_id, dependencies in rows:
                sources = self.source_ids(dependencies)
                if sources and sources <= selected:
                    candidates.append(page_id)
        if len(candidates) > 100:
            raise KnowledgeIntegrityError("INVALID_QUERY")
        pages = []
        for pid in sorted(candidates):
            try:
                page = self._current(pid)
            except KeyError:
                if not self.wiki.store.is_deleted(pid):
                    raise
                continue
            if page is not None and self.source_ids(page["revision"]["dependency_manifest"]) <= selected:
                pages.append(page)
        if not pages:
            return None
        return self._freeze_pages(pages)

    def freeze_bindings(self, bindings):
        """Pin exact already-read current Wiki versions for downstream support refs."""
        if not bindings or len(bindings) > 100:
            raise KnowledgeIntegrityError("INVALID_QUERY")
        pages = []
        for binding in bindings:
            page = self._current(binding["page_id"])
            if page is None or self._identity(page) != binding:
                raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
            pages.append(page)
        return self._freeze_pages(pages)

    def _freeze_pages(self, pages):
        self.wiki._refresh_scope()
        scope = self.registry.resolve(self.workspace_id, self.collection_id)
        sources = sorted(set().union(*(self.source_ids(p["revision"]["dependency_manifest"]) for p in pages)))
        manifest = {
            "workspace_id": self.workspace_id,
            "collection_id": self.collection_id,
            "collection_version": scope.version,
            "policy_version": scope.policy_version,
            "sources": sources,
            "records": [],
            "use_policy": "source_reviewed",
            "wiki_format": "current-wiki-corpus-v1",
            "wiki_revisions": [self._identity(p) for p in pages],
        }
        self.registry.check_manifest(manifest)
        corpus = self.snapshots.publish_corpus(manifest)
        return {
            "workspace_id": self.workspace_id,
            "collection_id": self.collection_id,
            "corpus_snapshot_id": corpus,
            "use_policy": "source_reviewed",
            "source_ids": sources,
            "manifest_sha256": fingerprint(manifest),
            "wiki_refs": [self._row(p, corpus).ref.model_dump(mode="json") for p in pages],
        }

    def search(self, request):
        if request.collection_id != self.collection_id:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        if request.source_kinds != ["wiki"]:
            raise KnowledgeIntegrityError("INDEX_NOT_READY")
        if request.use_policy not in {"source_reviewed", "explore"} or request.allow_stale_index:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        scope = self.registry.resolve(self.workspace_id, self.collection_id)
        pinned = self._manifest(request.corpus_snapshot_id) if request.corpus_snapshot_id else None
        page_ids = (
            [r["page_id"] for r in pinned["wiki_revisions"]]
            if pinned
            else [p["page_id"] for p in self.wiki.list_pages()["pages"]]
        )
        pages = []
        for page_id in page_ids:
            try:
                page = self._current(page_id)
            except (ValueError, KeyError) as exc:
                if isinstance(exc, KnowledgeIntegrityError):
                    raise
                continue
            if page is not None:
                if pinned and self._identity(page) not in pinned["wiki_revisions"]:
                    raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
                pages.append(page)
        if pinned and len(pages) != len(pinned["wiki_revisions"]):
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        sources = sorted(set().union(*(self.source_ids(p["revision"]["dependency_manifest"]) for p in pages)))
        manifest = pinned or {
            "workspace_id": self.workspace_id,
            "collection_id": self.collection_id,
            "collection_version": scope.version,
            "policy_version": scope.policy_version,
            "sources": sources,
            "records": [],
            "use_policy": request.use_policy,
            "wiki_format": "current-wiki-corpus-v1",
            "wiki_revisions": [self._identity(p) for p in pages],
        }
        self.registry.check_manifest(manifest)
        corpus = request.corpus_snapshot_id or self.snapshots.publish_corpus(manifest)
        terms = request.query.casefold().split()
        matches = []
        for page in pages:
            row = self._row(page, corpus)
            score = sum(row.body.casefold().count(t) for t in terms)
            if score:
                matches.append((score, row))
        matches.sort(key=lambda pair: (-pair[0], pair[1].result_id))
        return [r for _, r in matches[: request.top_k]], corpus, manifest, len(pages)
