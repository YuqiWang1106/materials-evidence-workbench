"""Research-topic drafts from verified existing turns; no model dispatch or fake records."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy

from .core import StateConflict, fingerprint
from .knowledge_claim_contracts import KnowledgeSupport, WikiContent
from .knowledge_scope import CollectionScope, KnowledgeScopeRegistry
from .knowledge_snapshot import KnowledgeSnapshotStore
from .knowledge_store import KnowledgeStore
from .knowledge_support_resolver import KnowledgeSupportResolver
from .knowledge_table_wiki_policy import is_table_summary, is_unified_summary
from .table_chat_store import request_id


def fields(payload, allowed, required):
    if not isinstance(payload, dict) or set(payload) - set(allowed) or not set(required) <= set(payload):
        raise ValueError("INVALID_WIKI_REQUEST")
    if "request_id" in payload:
        request_id(payload["request_id"])
    if "expected_revision" in payload and (
        type(payload["expected_revision"]) is not int or payload["expected_revision"] < 0
    ):
        raise ValueError("INVALID_REVISION")


class SharedSupportResolver(KnowledgeSupportResolver):
    def validate(self, revision):
        from .study_wiki_evidence import shared_support_context

        with shared_support_context(self.wiki):
            return super().validate(revision)


class WikiService:
    def __init__(
        self, ingestion_service, *, workspace_id=None, registry=None, snapshot_store=None, collection_id="research-wiki"
    ):
        self.ingestion_service = ingestion_service
        self.workspace_id = workspace_id or ingestion_service.store.workspace_id
        self.collection_id = collection_id
        self._local_registry = registry is None
        self.registry = registry or KnowledgeScopeRegistry()
        self.snapshot_store = snapshot_store or KnowledgeSnapshotStore(
            ingestion_service.store.root / "knowledge-snapshots"
        )
        self.store = KnowledgeStore(ingestion_service.store, self.workspace_id)
        self.resolver = SharedSupportResolver(self)
        self._refresh_scope()

    def _refresh_scope(self):
        if self._local_registry:
            # Scope derives solely from locally registered drafts, never a request source list.
            from sqlalchemy.orm import Session

            from .storage.lifecycle_repository import draft_source_ids

            with Session(self.ingestion_service.store.engine) as session:
                sources = draft_source_ids(session, self.ingestion_service.store.workspace_id)
            documents = {
                t["source_id"]
                for t in self.ingestion_service.store.list_tasks()
                if t.get("source_type") in {"paper", "patent"}
                and self.ingestion_service.store.source_state(t["source_id"])
            }
            sources = sources | documents
            self.registry.replace(
                CollectionScope(self.workspace_id, self.collection_id, "wiki-local-v1", sources, "wiki-source-v1")
            )

    def _corpus(self, sources, *, kind="trusted-table-analysis-corpus"):
        self._refresh_scope()
        scope = self.registry.resolve(self.workspace_id, self.collection_id)
        manifest = {
            "workspace_id": self.workspace_id,
            "collection_id": self.collection_id,
            "collection_version": scope.version,
            "policy_version": scope.policy_version,
            "use_policy": "explore",
            "records": [],
            "sources": sorted(set(sources)),
            "kind": kind,
            "scientific_validation": False,
        }
        self.registry.check_manifest(manifest)
        return self.snapshot_store.publish_corpus(manifest)

    @contextmanager
    def dependency_guard(self, revision):
        sources = sorted(
            {
                d["ref"]["source_id"]
                for d in revision["dependency_manifest"]["dependencies"]
                if d["kind"] in {"raw_table", "study_unit_snapshot"}
            }
        )
        context = revision["dependency_manifest"].get("research_summary")
        if context:
            from .research_summary_unified import is_unified, source_ids

            sources = sorted(
                set(sources)
                | set(
                    source_ids(revision["supports"], wiki=self)
                    if is_unified(revision["dependency_manifest"])
                    else context["selection"]["source_ids"]
                )
            )
        with ExitStack() as locks:
            for source in sources:
                locks.enter_context(self.ingestion_service.store.lock("source:" + source))
            yield

    def _require_scope(self, revision):
        from .knowledge_snapshot import KnowledgeIntegrityError

        manifest = self.snapshot_store.read_corpus(revision["corpus_snapshot_id"])
        if manifest["workspace_id"] != self.workspace_id or manifest["collection_id"] != self.collection_id:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        self._refresh_scope()
        self.registry.check_manifest(manifest)

    def validate_current(self, revision):
        self._refresh_scope()
        return self.resolver.validate(revision)

    def get_page(self, page_id, revision=None):
        page = self.store.get_page(page_id, revision)
        self._require_scope(page["revision"])
        gaps = self.validate_current(page["revision"])
        self.store.record_availability(page_id, gaps, revision_id=page["revision"]["revision_id"])
        page = self.store.get_page(page_id, revision)
        page["gaps"] = list(gaps)
        for reason in page["availability_reason"]:
            if reason not in page["gaps"]:
                page["gaps"].append(reason)
        page["revision"]["claim_revisions"] = {
            claim["claim_id"]: "claimrev-" + fingerprint([page["revision"]["revision_id"], claim["claim_id"]])
            for section in page["revision"]["content"]["sections"]
            for claim in section["claims"]
        }
        from .study_wiki_policy import is_study_summary

        study = page["revision"]["dependency_manifest"].get("study_unit", {})
        page["publication_mode"] = (
            "unified_chat_summary_auto"
            if is_unified_summary(page["revision"])
            else "study_unit_auto"
            if is_study_summary(page["revision"])
            else "table_auto"
            if is_table_summary(page["revision"])
            else "human_review"
        )
        page["manual_edited"] = bool(study.get("manual_edited"))
        jobs = getattr(self.ingestion_service, "study_wiki_jobs", None)
        page["update_available"] = bool(jobs and study and jobs.proposal(study["unit_id"]))
        return page

    def list_pages(self):
        from .knowledge_snapshot import KnowledgeIntegrityError

        pages = []
        for page in self.store.list_pages():
            try:
                pages.append(self.get_page(page["page_id"]))
            except KeyError:
                if not self.store.is_deleted(page["page_id"]):
                    raise
            except KnowledgeIntegrityError as exc:
                if exc.code not in {
                    "SOURCE_USE_POLICY_DENIED",
                    "WORKSPACE_MISMATCH",
                    "REFERENCE_NOT_FOUND",
                    "SNAPSHOT_REVIEW_CHANGED",
                }:
                    raise
        return {"pages": pages}

    def draft_from_chat(self, payload):
        fields(
            payload,
            {"request_id", "chat_id", "turn_id", "page_id", "expected_revision", "title", "research_question"},
            {"request_id", "chat_id", "turn_id"},
        )
        digest = fingerprint(["from-chat", payload])
        old = self.store.find_revision_request(payload["request_id"], digest)
        if old:
            return self.get_page(old["page_id"], old["revision"])
        page_id = payload.get("page_id") or "wiki-" + fingerprint([self.workspace_id, payload["request_id"]])
        current = None
        if payload.get("page_id"):
            if "expected_revision" not in payload:
                raise ValueError("EXPECTED_REVISION_REQUIRED")
            current = self.get_page(page_id)
            if current["revision"]["dependency_manifest"].get("summary"):
                raise ValueError("SUMMARY_TEMPLATE_FIXED")
            if current["current_revision"] != payload["expected_revision"]:
                raise StateConflict("REVISION_CONFLICT")
        turn, snapshot, raws, artifacts = self.resolver.completed_turn(payload["chat_id"], payload["turn_id"])
        old_deps = deepcopy(current["revision"]["dependency_manifest"]["dependencies"]) if current else []
        sources = [r["source_id"] for r in raws] + [d["ref"]["source_id"] for d in old_deps if d["kind"] == "raw_table"]
        corpus_id = self._corpus(sources)
        supports, deps, usable = self.resolver.supports(turn, raws, artifacts, corpus_id)
        section = {
            "section_id": "section-" + fingerprint([turn["chat_id"], turn["turn_id"]]),
            "heading": turn["message"][:300],
            "claims": [
                {
                    "claim_id": "claim-" + fingerprint([turn["chat_id"], turn["turn_id"]]),
                    "claim_type": "editorial_synthesis",
                    "text": turn["assistant_text"],
                    "support_ids": usable,
                    "scientific_validation": False,
                }
            ],
        }
        if current:
            content = deepcopy(current["revision"]["content"])
            if section["section_id"] in {s["section_id"] for s in content["sections"]}:
                raise StateConflict("TURN_ALREADY_IMPORTED")
            content["sections"].append(section)
            supports = deepcopy(current["revision"]["supports"]) + supports
            deps = old_deps + deps
        else:
            content = {
                "title": payload.get("title") or turn["message"][:300],
                "research_question": payload.get("research_question", turn["message"]),
                "sections": [section],
                "conditions": [],
                "uncertainties": [],
            }
        content = WikiContent.model_validate(content).model_dump(mode="json", exclude_none=True)
        supports = [KnowledgeSupport.model_validate(s).model_dump(mode="json") for s in supports]
        manifest = {
            "version": "knowledge-dependencies-v1",
            "corpus_snapshot_id": corpus_id,
            "supports": list({s["support_id"]: s for s in supports}.values()),
            "dependencies": list({d["dependency_id"]: d for d in deps}.values()),
        }
        self.store.save_revision(
            page_id=page_id,
            expected_revision=payload.get("expected_revision", 0),
            request_id=payload["request_id"],
            request_hash=digest,
            content=content,
            dependency_manifest=manifest,
        )
        return self.get_page(page_id)

    def _prune_manifest(self, manifest, content):
        if manifest.get("summary") or manifest.get("research_summary") or manifest.get("study_unit"):
            # Every original turn, including uncited corrections, remains a dependency.
            return deepcopy(manifest)
        used = {sid for section in content["sections"] for claim in section["claims"] for sid in claim["support_ids"]}
        supports = [s for s in manifest["supports"] if s["support_id"] in used]
        table_ids = {s["ref"]["table_version_id"] for s in supports if s["ref"]["kind"] == "table_snapshot"}
        artifact_ids = set()
        for support in supports:
            if support["ref"]["kind"] == "table_analysis_artifact":
                artifact_ids.add(support["ref"]["artifact_id"])
                table_ids.update(support["ref"]["table_version_ids"])
        # Calculations retain their complete pinned input set even when only the
        # calculation is cited by the edited claim. Raw references remain explicit.
        supports = [
            s
            for s in manifest["supports"]
            if s["support_id"] in used
            or s["ref"]["kind"] == "table_snapshot"
            and s["ref"]["table_version_id"] in table_ids
        ]
        deps = [
            d
            for d in manifest["dependencies"]
            if (d["kind"] == "raw_table" and d["ref"]["table_version_id"] in table_ids)
            or (d["kind"] == "table_analysis_artifact" and d["ref"]["artifact_id"] in artifact_ids)
            or (d["kind"] == "chat_turn" and used.intersection(d["support_ids"]))
        ]
        sources = [d["ref"]["source_id"] for d in deps if d["kind"] == "raw_table"]
        return {
            **deepcopy(manifest),
            "supports": supports,
            "dependencies": deps,
            "corpus_snapshot_id": self._corpus(sources),
        }

    def save_revision(self, page_id, payload):
        from .study_wiki_policy import is_study_summary

        page = self.store.get_page(page_id)
        if is_study_summary(page["revision"]):
            uid = page["revision"]["dependency_manifest"]["study_unit"]["unit_id"]
            with self.ingestion_service.store.lock("study-wiki-unit:" + uid):
                return self._save_revision(page_id, payload)
        return self._save_revision(page_id, payload)

    def _save_revision(self, page_id, payload):
        fields(payload, {"request_id", "expected_revision", "content"}, {"request_id", "expected_revision", "content"})
        digest = fingerprint(["save", page_id, payload])
        old = self.store.find_revision_request(payload["request_id"], digest)
        if old:
            page = self.get_page(old["page_id"], old["revision"])
            from .study_wiki_policy import is_study_summary

            return (
                self.ensure_unified_published(page_id, old["revision"])
                if is_unified_summary(page["revision"])
                else self.ensure_study_published(page_id)
                if is_study_summary(page["revision"])
                else self.ensure_table_published(page_id)
                if is_table_summary(page["revision"])
                else page
            )
        page = self.get_page(page_id)
        from .knowledge_summary_contracts import SummaryContent

        contract = (
            SummaryContent
            if page["revision"]["dependency_manifest"].get("summary")
            or page["revision"]["dependency_manifest"].get("research_summary")
            else WikiContent
        )
        content = contract.model_validate(payload["content"]).model_dump(mode="json", exclude_none=True)
        research = page["revision"]["dependency_manifest"].get("research_summary")
        if research:
            from .research_summary_supports import normalize_edit

            content = normalize_edit(page["revision"]["dependency_manifest"], content)
        supports = {s["support_id"]: s for s in page["revision"]["supports"]}
        for section in content["sections"]:
            for claim in section["claims"]:
                from .study_wiki_policy import is_study_summary

                if (
                    claim["claim_type"] == "reported_measurement"
                    and not research
                    and not is_study_summary(page["revision"])
                ):
                    raise ValueError("MEASUREMENT_EVIDENCE_REQUIRED")
                if any(sid not in supports for sid in claim["support_ids"]):
                    raise ValueError("CLAIM_SUPPORT_INVALID")
                if claim["claim_type"] == "computed_observation" and not any(
                    supports[sid]["support_type"] == "calculation" for sid in claim["support_ids"]
                ):
                    raise ValueError("CALCULATION_EVIDENCE_REQUIRED")
        manifest = self._prune_manifest(page["revision"]["dependency_manifest"], content)
        from .study_wiki_policy import is_study_summary

        if is_study_summary(page["revision"]):
            manifest["study_unit"]["manual_edited"] = True
        saved = self.store.save_revision(
            page_id=page_id,
            expected_revision=payload["expected_revision"],
            request_id=payload["request_id"],
            request_hash=digest,
            content=content,
            dependency_manifest=manifest,
        )
        return (
            self.ensure_unified_published(page_id, saved["revision"])
            if is_unified_summary(saved)
            else self.ensure_study_published(page_id)
            if is_study_summary(page["revision"])
            else self.ensure_table_published(page_id)
            if is_table_summary(page["revision"])
            else self.get_page(page_id)
        )

    def ensure_unified_published(self, page_id, expected_revision):
        from .knowledge_table_wiki_lifecycle import ensure_published

        return ensure_published(self, page_id, unified=True, expected_revision=expected_revision)

    def ensure_table_published(self, page_id):
        from .knowledge_table_wiki_lifecycle import ensure_published

        return ensure_published(self, page_id)

    def ensure_study_published(self, page_id):
        from .knowledge_table_wiki_lifecycle import ensure_published

        return ensure_published(self, page_id, study=True)

    def delete(self, page_id, payload):
        fields(payload, {"request_id", "expected_revision"}, {"request_id", "expected_revision"})
        if payload["expected_revision"] < 1:
            raise ValueError("INVALID_REVISION")
        from .study_wiki_policy import is_study_summary

        jobs = getattr(self.ingestion_service, "study_wiki_jobs", None)
        if self.store.is_deleted(page_id):
            if jobs is not None:
                try:
                    return jobs.delete(page_id, payload)
                except KeyError:
                    pass
            return self.store.delete(page_id, payload)
        page = self.store.get_page(page_id)
        if is_study_summary(page["revision"]):
            jobs = getattr(self.ingestion_service, "study_wiki_jobs", None)
            if jobs is None:
                raise StateConflict("STUDY_WIKI_NOT_READY")
            return jobs.delete(page_id, payload)
        return self.store.delete(page_id, payload)

    def review(self, page_id, payload):
        fields(
            payload,
            {"request_id", "expected_revision", "content_hash", "dependency_manifest_hash", "reviewer", "decision"},
            {"request_id", "expected_revision", "content_hash", "dependency_manifest_hash", "reviewer", "decision"},
        )
        if (
            payload["decision"] not in {"approved", "rejected"}
            or not isinstance(payload["reviewer"], str)
            or not payload["reviewer"].strip()
            or len(payload["reviewer"]) > 200
        ):
            raise ValueError("INVALID_REVIEW")
        page = self.get_page(page_id)
        with self.dependency_guard(page["revision"]):
            page = self.get_page(page_id)
            if page["gaps"] and payload["decision"] == "approved":
                raise StateConflict("DEPENDENCY_REVIEW_REQUIRED")
            self.store.review(page_id, payload)
        return self.get_page(page_id)

    def withdraw(self, page_id, payload):
        fields(payload, {"request_id", "expected_revision", "reason"}, {"request_id", "expected_revision", "reason"})
        if not isinstance(payload["reason"], str) or not payload["reason"].strip() or len(payload["reason"]) > 8000:
            raise ValueError("INVALID_WITHDRAW_REASON")
        self.get_page(page_id)
        self.store.withdraw(page_id, payload)
        return self.get_page(page_id)

    def publication_material(self, page_id, expected_revision=None):
        page = self.get_page(page_id)
        if expected_revision is not None and page["current_revision"] != expected_revision:
            raise StateConflict("REVISION_CONFLICT")
        revision = page["revision"]
        gaps = deepcopy(page["gaps"])
        if page["review"] is None:
            gaps.append({"code": "REVIEW_REQUIRED"})
        if not gaps:
            raws, artifacts, originals = self.resolver.material(revision)
        else:
            raws, artifacts, originals = [], [], {}
        documents = {}
        if not gaps and revision["dependency_manifest"].get("research_summary"):
            from .research_summary_unified import is_unified

            if is_unified(revision["dependency_manifest"]):
                documents = self.resolver.unified_material(revision)
                raws = documents.pop("raw_tables")
                artifacts = documents.pop("artifacts")
            else:
                documents = self.resolver.research_material(revision)
            originals.update(documents.pop("originals"))
        if not gaps and revision["dependency_manifest"].get("study_unit"):
            from .study_units import StudyUnitService

            unit = StudyUnitService(self.ingestion_service, workspace_id=self.workspace_id).resolve(
                revision["dependency_manifest"]["dependencies"][0]["ref"]
            )
            documents["document_asset_hashes"] = {
                a["asset_id"]: a["asset_sha256"] for a in unit["snapshot"]["payload"]["source_refs"]
            }
        return {
            **documents,
            "page": page,
            "revision": revision,
            "review": page["review"],
            "gaps": gaps,
            "refs": [s["ref"] for s in revision["supports"]],
            "dependency_manifest": revision["dependency_manifest"],
            "raw_tables": raws,
            "artifacts": artifacts,
            "originals": originals,
            "corpus_manifest": self.snapshot_store.read_corpus(revision["corpus_snapshot_id"]),
        }
