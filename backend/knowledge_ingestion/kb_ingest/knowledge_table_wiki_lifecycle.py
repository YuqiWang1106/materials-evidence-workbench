"""Automatic table Wiki publication, retaining exact source and local archive verification."""

from copy import deepcopy

from .core import StateConflict, fingerprint
from .knowledge_publication import KnowledgePublicationService, PublicationError
from .knowledge_table_wiki_policy import POLICY_VERSION, UNIFIED_POLICY_VERSION, is_table_summary, is_unified_summary


def _reconcile_previous(wiki, publisher, page):
    # A lost response on an older revision must not permanently block edits.
    # Reconcile is read-only: never dispatch an upload for a superseded revision.
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from .storage.models import KnowledgePublication

    with Session(wiki.ingestion_service.store.engine) as session:
        ids = list(
            session.scalars(
                select(KnowledgePublication.publication_id)
                .where(
                    KnowledgePublication.workspace_id == wiki.workspace_id,
                    KnowledgePublication.page_id == page["page_id"],
                    KnowledgePublication.revision_id != page["current_revision_id"],
                    KnowledgePublication.status.in_(["prepared", "publishing", "write_unknown"]),
                )
                .order_by(KnowledgePublication.created_at)
            )
        )
    for publication_id in ids:
        outcome = publisher.reconcile(publication_id)
        if outcome["status"] in {"published", "historical"}:
            continue
        if outcome["status"] == "failed" and outcome.get("error_code") == "WRITE_NOT_STARTED":
            continue
        raise PublicationError(outcome.get("error_code") or "PUBLICATION_PENDING_RECONCILE", outcome.get("gaps", []))


def ensure_published(wiki, page_id, *, study=False, unified=False, expected_revision=None):
    from .study_wiki_policy import POLICY_VERSION as STUDY_POLICY
    from .study_wiki_policy import is_study_summary

    if unified and (type(expected_revision) is not int or expected_revision < 1):
        raise ValueError("INVALID_REVISION")
    policy = UNIFIED_POLICY_VERSION if unified else STUDY_POLICY if study else POLICY_VERSION

    def pin(page):
        # Unified save/apply authorization belongs to this exact saved revision.
        if unified and page["current_revision"] != expected_revision:
            raise StateConflict("REVISION_CONFLICT")
        return page

    # Separate from transport's lock: publication can resolve a prior write without
    # dispatching the model or generating another page/revision.
    with wiki.ingestion_service.store.lock("table-wiki-lifecycle:" + page_id):
        if unified:
            saved = pin(wiki.store.get_page(page_id))["revision"]
            rid = "wiki-auto-publish-" + fingerprint([policy, saved["revision_id"]])
            prior = wiki.store.find_publication(page_id, rid)
            if prior and prior["status"] == "historical":
                KnowledgePublicationService(wiki).reconcile(prior["publication_id"])
        page = pin(wiki.get_page(page_id))
        eligible = (
            is_unified_summary(page["revision"])
            if unified
            else is_study_summary(page["revision"])
            if study
            else is_table_summary(page["revision"])
        )
        if not eligible:
            raise ValueError(
                "UNIFIED_SUMMARY_REQUIRED" if unified else "STUDY_WIKI_REQUIRED" if study else "TABLE_SUMMARY_REQUIRED"
            )
        with wiki.dependency_guard(page["revision"]):
            page = pin(wiki.get_page(page_id))
            current_gaps = wiki.validate_current(page["revision"])
            legacy_reasons = page["availability_reason"]
            if (
                not study
                and not unified
                and page["availability"] == "stale"
                and not current_gaps
                and legacy_reasons
                and all(g["code"] == "CLAIM_SUPPORT_MISSING" for g in legacy_reasons)
            ):
                revision = page["revision"]
                rid = "table-policy-upgrade-" + fingerprint([POLICY_VERSION, revision["revision_id"]])
                wiki.store.save_revision(
                    page_id=page_id,
                    expected_revision=page["current_revision"],
                    request_id=rid,
                    request_hash=fingerprint([rid, revision["content_hash"], revision["dependency_manifest_hash"]]),
                    content=deepcopy(revision["content"]),
                    dependency_manifest=deepcopy(revision["dependency_manifest"]),
                )
                page = pin(wiki.get_page(page_id))
            if page["gaps"] or page["availability"] != "current":
                raise PublicationError(page["gaps"][0]["code"] if page["gaps"] else "WIKI_NOT_CURRENT", page["gaps"])
            if not page["review"]:
                wiki.store.auto_accept(page_id, page["current_revision"])
                page = pin(wiki.get_page(page_id))
        revision = page["revision"]
        publisher = KnowledgePublicationService(wiki)
        _reconcile_previous(wiki, publisher, page)
        intent = wiki.store.published_publication(page_id, revision["revision_id"])
        if intent and page["published_revision_id"] == revision["revision_id"]:
            publisher.verify_published(intent)
        else:
            receipt = publisher.publish(
                page_id,
                {
                    "request_id": "wiki-auto-publish-" + fingerprint([policy, revision["revision_id"]]),
                    "expected_revision": revision["revision"],
                    "content_hash": revision["content_hash"],
                    "dependency_manifest_hash": revision["dependency_manifest_hash"],
                },
            )
            if receipt["status"] != "published" or not receipt["currently_eligible"]:
                raise PublicationError(receipt.get("error_code") or "PUBLICATION_SUPERSEDED", receipt.get("gaps", []))
        final = wiki.get_page(page_id)
        if (
            final["published_revision_id"] != revision["revision_id"]
            or final["current_revision_id"] != revision["revision_id"]
            or final["gaps"]
        ):
            raise StateConflict("REVISION_CONFLICT")
        return final
