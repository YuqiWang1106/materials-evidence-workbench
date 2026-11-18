"""Document-specific artifact/query/review boundary, separate from table workflow."""

import json

from . import document_workflow_review_batch as review_batch
from . import document_workflow_reviews as source_reviews
from . import pipeline_versions
from .core import StateConflict, atomic_json, fingerprint
from .core.errors import raise_state_conflict
from .document_workflow_artifacts import ArtifactRequest, artifact_root, has_current_candidate
from .document_workflow_candidates import (
    CandidateFiles,
    CandidatePage,
    CandidateSnapshot,
    candidate_page_png,
    candidate_page_target,
)
from .document_workflow_publication import PublicationSources, materialize_publication, publication_draft
from .document_workflow_revision import ReviewHistory, ReviewRevision, require_review_version, require_saved_originals
from .storage.task_files import checked_files
from .store import Store

DOCUMENT_EXTRACTION_MODES = ("legacy", "direct-six-v1", "program-only-v1", "entity-graph-v1")
V2_EXTRACTION_MODES = ("direct-six-v1", "program-only-v1", "entity-graph-v1")


def document_extraction_mode(options):
    """Validate server-owned routing before storage, parsing or model spending."""
    mode = options.get("document_extraction_mode", "legacy")
    if mode not in DOCUMENT_EXTRACTION_MODES:
        message = "Unknown document extraction mode"
        raise ValueError(message)
    if mode in V2_EXTRACTION_MODES and options.get("document_pipeline_v2") is not True:
        message = "Document extraction mode requires document_pipeline_v2"
        raise ValueError(message)
    if mode == "program-only-v1" and options.get("review_policy", "program_only_v1") != "program_only_v1":
        message = "Program-only extraction requires program_only_v1 review policy"
        raise ValueError(message)
    return mode


def document_query_metadata(package):
    """Describe document fact-use boundaries without elevating scientific status.

    Called on the filtered query package, not a replacement for quality checks.
    Tables retain their own existing query contract.
    """
    if not any(s.get("source_type") in ("paper", "patent") for s in package.get("sources", [])):
        return None
    header = package["header"]
    current = header.get("document_pipeline_version") == "2"
    outcome = header.get("document_quality", {}).get("outcome") if current else None
    program = header.get("document_extraction_mode") == "program-only-v1"
    human_program = program and header.get("review_policy") == "human_reviewed_candidate_v1"
    revoked = bool(package.get("query_context", {}).get("current_review_required"))
    return {
        "document_pipeline_version": header.get("document_pipeline_version"),
        "document_extraction_mode": header.get("document_extraction_mode", "legacy"),
        "record_revision_id": header.get("record_revision_id"),
        "source_revision_id": header.get("source_revision_id"),
        "processing_outcome": outcome,
        "current_use_status": ("review_required" if revoked else outcome) if current else "legacy_not_revalidated",
        "source_review_basis": (
            "human_source_reviewed"
            if human_program
            else "program_checks_only"
            if program
            else "source_model_and_program_checks"
        )
        if current
        else "legacy_not_revalidated",
        "human_expert_review": False,
        "scientific_validation": False,
        "queryable_use_status": "human_reviewed"
        if human_program
        else "program_checked"
        if program
        else "document_verified",
        "queryable_assertions": sum(
            a.get("use_status")
            == ("human_reviewed" if human_program else "program_checked" if program else "document_verified")
            for a in package["assertions"]
        )
        if current
        else None,
    }


class DocumentWorkflowMixin:
    store: Store
    pipeline_fingerprint: str

    def document_enabled(self):
        return self.document_options.get("document_pipeline_v2") is True

    def document_mode(self):
        return document_extraction_mode(self.document_options)

    def document_fingerprint(self) -> str:
        return pipeline_versions.document_pipeline_version(
            self.document_mode(), self.document_options.get("document_input_strategy", "ir_batches")
        )

    def _require_document_configuration(self, task):
        """A restart can use only the task's originally selected extractor."""
        if task["source_type"] not in ("paper", "patent"):
            return
        self._require_native_document_readiness()
        v2 = task["options"].get("_document_pipeline_version") == "2"
        mode = task["options"].get("_document_extraction_mode", "legacy")
        strategy = task["options"].get("_document_input_strategy", "ir_batches")
        if (
            mode not in DOCUMENT_EXTRACTION_MODES
            or mode != self.document_mode()
            or strategy != self.document_options.get("document_input_strategy", "ir_batches")
            or (v2 and not self.document_enabled())
            or (mode in V2_EXTRACTION_MODES and not v2)
            or (mode == "program-only-v1" and task["options"].get("_review_policy") != "program_only_v1")
        ):
            raise_state_conflict("Document extraction mode changed; submit a new source revision")
        expected = self.document_fingerprint() if v2 else self.pipeline_fingerprint
        pinned = task["options"].get("_pipeline_fingerprint")
        if (pinned and pinned != expected) or (mode in V2_EXTRACTION_MODES and not pinned):
            raise_state_conflict("Pipeline configuration changed; submit a new source revision")

    def _document_workspace(self, task_id, *parts):
        return self.store.pending_path(task_id, "document-v2", *parts)

    def _document_artifact_root(self, task, package=None):
        root = self._document_workspace(task["task_id"])
        if package is None and has_current_candidate(task):
            package = self.candidate(task["task_id"])
        return artifact_root(root, task, package)

    def document_artifact(self, task_id, kind="quality", *, asset_id=None, page=None, offset=0, limit=50):
        request = ArtifactRequest(kind, asset_id, page, offset, limit)
        task = self.store.get_task(task_id)
        if (
            kind == "quality"
            and task["options"].get("_document_pipeline_version") == "2"
            and not has_current_candidate(task)
        ):
            return {"status": "not_processed"}
        path = request.path(self._document_artifact_root(task))
        if not path.is_file():
            return {"status": "not_available", "reason": "not_processed_by_document_v2"}
        if kind != "quality":
            return request.read(path)
        if not has_current_candidate(task):
            return {"status": "not_processed"}
        package = self.records(task_id)
        value = json.loads(path.read_text())
        if value != package["header"].get("document_quality"):
            return {"status": "stale", "reason": "quality report does not match current candidate"}
        invalidated = self._document_revocations(task_id, package)
        return {
            "status": "available",
            "data": {
                **value,
                "current_use_status": "review_required" if invalidated else value["outcome"],
                "review_invalidation": {"target_count": len(invalidated), "requires_reprocessing": bool(invalidated)},
            },
        }

    def _document_source_assets(self, task):
        assets = {
            aid: {**self.store.get_asset(aid), "local_path": str(self.store.asset_path(aid))}
            for aid in task["asset_ids"]
        }
        for alias, registered in task.get("options", {}).get("asset_bindings", {}).items():
            if registered in assets:
                assets[alias] = assets[registered]
        return assets

    def document_candidates(self, task_id, *, offset=0, limit=20):
        return self._document_candidate_snapshot(task_id, offset=offset, limit=limit, with_locations=True)

    def _document_candidate_snapshot(
        self, task_id, *, offset=0, limit=10000, with_locations=False
    ) -> CandidateSnapshot:
        """Return untouched Sol groups with optional display locations, including unresolved evidence."""
        page = CandidatePage(offset, limit)
        task = self.store.get_task(task_id)
        if task["source_type"] not in ("paper", "patent"):
            msg = "Sol candidates are only available for paper/patent tasks"
            raise ValueError(msg)
        root = self._document_artifact_root(task)
        paths = sorted(checked_files(root, "raw-*.json"))
        if not paths:
            return page.empty()

        def source_hashes() -> dict[str, str]:
            return {aid: self.store.get_asset(aid)["sha256"] for aid in task["asset_ids"]}

        # Review writes share candidate identity without doing PDF display work.
        files = CandidateFiles(root, source_hashes, self._document_source_assets(task) if with_locations else None)
        rows = files.read(paths)
        rows.attach_scopes(lambda: self._document_artifact_root(task), source_hashes())
        return rows.snapshot(page, task.get("package_sha256"), lambda: self._document_workspace(task_id, "reviews"))

    def document_candidate_page_image(self, task_id, candidate_id, evidence_ref, *, expected_candidate_sha256):
        """Render a candidate-cited physical page only when its source resolves."""
        view = self.document_candidates(task_id, offset=0, limit=10000)
        asset_ref, page = candidate_page_target(view, expected_candidate_sha256, candidate_id, evidence_ref)
        task = self.store.get_task(task_id)
        package = self.candidate(task_id)
        msg = "candidate source is not registered to this task"
        if asset_ref not in {asset["asset_id"] for asset in package.get("assets", [])}:
            raise StateConflict(msg)
        registered = task.get("options", {}).get("asset_bindings", {}).get(asset_ref, asset_ref)
        if registered not in task["asset_ids"]:
            raise StateConflict(msg)
        return candidate_page_png(self.store, registered, page)

    def document_candidate_review(
        self,
        task_id,
        *,
        expected_candidate_sha256,
        candidate_id,
        candidate_ref,
        decision,
        operator,
        note="",
        correction=None,
        expected_review_sha256=None,
        ownership_target_ref=None,
    ):
        # The one-item endpoint accepts an omitted review version.
        item: review_batch.Decision = {
            "candidate_ref": candidate_ref,
            "decision": decision,
            "note": note,
            "correction": correction,
            "ownership_target_ref": ownership_target_ref,
        }
        batch = review_batch.CandidateBatch(
            candidate_id, expected_candidate_sha256, expected_review_sha256, operator, [item]
        )
        return self._review_workspace(task_id).review(task_id, batch)["reviews"][0]

    def document_candidate_reviews_batch(
        self,
        task_id,
        *,
        expected_candidate_sha256,
        expected_review_sha256,
        candidate_id,
        operator,
        items,
        review_scope=None,
    ):
        review_batch.require_review_digest(expected_review_sha256)
        batch = review_batch.CandidateBatch(
            candidate_id, expected_candidate_sha256, expected_review_sha256, operator, items, review_scope
        )
        return self._review_workspace(task_id).review(task_id, batch)

    def _review_workspace(self, task_id: str) -> review_batch.ReviewWorkspace:
        from .study_units import StudyUnitService

        return review_batch.ReviewWorkspace(
            self.store,
            lambda: self._document_candidate_snapshot(task_id, offset=0, limit=10000),
            self._document_artifact_root,
            lambda: StudyUnitService(self),
        )

    def document_review_revision(self, task_id, *, expected_candidate_sha256, expected_review_sha256):
        """Continue a saved review without scheduling extraction or mutating old artifacts."""
        initial = self.store.get_task(task_id)
        with self.store.lock("source:" + initial["source_id"]):
            task = self.store.get_task(task_id)
            view = self._document_candidate_snapshot(task_id, offset=0, limit=10000)
            require_review_version(
                view.get("candidate_sha256"),
                view.get("review_sha256"),
                expected_candidate_sha256,
                expected_review_sha256,
            )
            package = self.candidate(task_id)
            require_saved_originals(self.store, task["state"], task["asset_ids"], package)
            source_root = self._document_artifact_root(task, package)
            history = ReviewHistory.load(
                self._document_workspace(task_id, "reviews"),
                lambda: self.store.pending_path(task_id, "publication-package.json"),
                lambda published: bool(self._document_revocations(task_id, published)),
            )
            revision = ReviewRevision(self.store.root, source_root, task_id, task["source_id"], package, history)
            key = fingerprint(["document-review-revision", task_id, expected_candidate_sha256, expected_review_sha256])
            result = self.store.create_document_review_revision(task_id, key, revision.prepare)
            from .study_units import StudyUnitService

            StudyUnitService(self).sync_task(result["task_id"])
            return result

    def document_publication_package(self, task_id, *, expected_candidate_sha256, expected_review_sha256=None):
        """Materialize only explicitly reviewed Sol facts into the published package."""
        candidate = self.candidate(task_id)
        view = self._document_candidate_snapshot(task_id, offset=0, limit=10000)
        published = publication_draft(candidate, view, expected_candidate_sha256, expected_review_sha256)
        root = self._document_artifact_root(self.store.get_task(task_id), candidate)
        assets = self._document_source_assets(self.store.get_task(task_id))
        return materialize_publication(
            published, view, PublicationSources.load(root, assets), expected_candidate_sha256
        )

    def document_review(self, task_id, *, expected_package_sha256, target_refs, decision, note, operator):
        """Write an audit entry bound to the displayed package under the publication lock."""
        initial = self.store.get_task(task_id)
        with self.store.lock("source:" + initial["source_id"]):
            self.store.get_task(task_id)
            p = self.records(task_id)
            if p["header"].get("document_pipeline_version") != "2":
                raise_state_conflict("historical candidate must be re-extracted before v2 review")
            if fingerprint(p) != expected_package_sha256:
                raise_state_conflict("reviewed result version changed")
            review = source_reviews.SourceReview(expected_package_sha256, target_refs, decision, note, operator)
            sections = [(p[section], key) for section, key in source_reviews.TARGET_KEYS]
            digest = source_reviews.review_targets(review, sections)
            row = source_reviews.audit_entry(task_id, review, p["assets"], p["header"]["record_revision_id"], digest)
            atomic_json(self._document_workspace(task_id, "reviews", f"{row['review_id']}.json"), row)
            return row

    def document_reviews(self, task_id, *, target_ref=None, offset=0, limit=100):
        """Read only audit entries bound to the currently displayed candidate."""
        query = source_reviews.ReviewQuery(target_ref, offset, limit)
        query.validate()
        package = self.records(task_id)
        digest = fingerprint(package)
        rows = source_reviews.current_audits(self._document_workspace(task_id, "reviews"), digest, target_ref)
        blocked = self._document_revocations(task_id, package)
        return source_reviews.review_page(
            query, digest, rows, blocked, package["header"].get("document_pipeline_version") == "2"
        )

    def document_evidence_page_image(self, task_id, evidence_id, page):
        """Render a physical PDF page named by evidence in this task's candidate."""
        source_reviews.require_physical_page(page)
        task = self.store.get_task(task_id)
        source_reviews.require_document_preview(task["source_type"])
        package = self.records(task_id)
        asset_id = source_reviews.evidence_asset_ref(package["evidence"], evidence_id, page)
        original = source_reviews.evidence_original(package["assets"], asset_id)
        registered = task["options"].get("asset_bindings", {}).get(asset_id, asset_id)
        return source_reviews.render_evidence(self.store, original, registered, task["asset_ids"], page)

    def _document_revocations(self, task_id, package):
        digest = fingerprint(package)
        return source_reviews.revoked_targets(self._document_workspace(task_id, "reviews"), digest)

    def _document_publish_check(self, task, package, expected_hash):
        header = package["header"]
        if header.get("document_pipeline_version") != "2":
            return
        if header.get("review_policy") == "human_reviewed_candidate_v1":
            candidate_digest = self._document_candidate_snapshot(task["task_id"], offset=0, limit=10000).get(
                "candidate_sha256"
            )
            if not expected_hash or candidate_digest != expected_hash:
                raise_state_conflict("Sol 候选版本已变化，请刷新后重新确认")
            source_reviews.check_package(package, fingerprint(package))
        else:
            source_reviews.check_package(package, expected_hash)
            source_reviews.check_report(self._document_artifact_root(task, package), header)
            if self._document_revocations(task["task_id"], package):
                raise_state_conflict("source review invalidated this candidate; reprocess the affected result")
        source_reviews.check_originals(
            self.store,
            package["assets"],
            lambda asset_id: task["options"].get("asset_bindings", {}).get(asset_id, asset_id),
        )

    def _document_query_package(self, task_id):
        from .knowledge_projection import project_saved_package

        package = self.records(task_id)
        blocked = (
            self._document_revocations(task_id, package)
            if package["header"].get("document_pipeline_version") == "2"
            else set()
        )
        return project_saved_package(package, blocked)
