"""Transactional shared Wiki state with immutable revisions and an availability ledger."""

import json
import uuid
from contextlib import contextmanager
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import StateConflict, atomic_json, clock, fingerprint
from .knowledge_table_wiki_policy import (
    POLICY_VERSION,
    UNIFIED_POLICY_VERSION,
    accepted_review,
    is_table_summary,
    is_unified_summary,
)
from .storage import table_dependencies
from .storage.knowledge_rows import knowledge_row
from .storage.models import (
    KnowledgeDependency as Dependency,
)
from .storage.models import (
    KnowledgeEvent as Event,
)
from .storage.models import (
    KnowledgeEventConsumer as Consumer,
)
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
from .storage.models import (
    KnowledgeSupportRow as Support,
)
from .storage.time_source import sourced_epoch


class KnowledgeStore:
    def __init__(self, store, workspace_id, *, initialize=True):
        self.store = store
        self.workspace_id = workspace_id
        self.anchor = store.data_root / "knowledge-availability.json"
        if not initialize:
            return
        with store.lock("knowledge-ledger"), store._write() as session:
            consumer = session.get(Consumer, "availability")
            if consumer is None and session.scalar(select(Item.page_id).limit(1)) is None and not self.anchor.exists():
                consumer = Consumer(
                    consumer_id="availability", epoch=uuid.uuid4().hex, last_sequence=0, status="current"
                )
                session.add(consumer)
                session.flush()
                atomic_json(self.anchor, {"epoch": consumer.epoch, "last_sequence": 0, "event_hash": fingerprint([])})

    _row = staticmethod(knowledge_row)

    def _events(self, session):
        return session.scalars(select(Event).order_by(Event.sequence)).all()

    def _ledger_valid(self, session):
        consumer = session.get(Consumer, "availability")
        if consumer is None or consumer.status != "current" or self.anchor.is_symlink():
            return False
        events = self._events(session)
        if [e.sequence for e in events] != list(range(1, consumer.last_sequence + 1)):
            return False
        if any(e.epoch != consumer.epoch for e in events):
            return False
        by_page = {}
        for event in events:
            by_page.setdefault(event.page_id, []).append(event)
        for item in session.scalars(select(Item)):
            history = by_page.get(item.page_id, [])
            if not history or item.epoch != consumer.epoch or item.last_event_seq != history[-1].sequence:
                return False
            revisions = [event for event in history if event.event_type == "revision_created"]
            if not revisions or item.current_revision_id != revisions[-1].payload["revision_id"]:
                return False
            if any(event.event_type == "deleted" for event in history) and (
                item.availability != "deleted" or item.published_revision_id is not None
            ):
                return False
            if any(event.event_type == "withdrawn" for event in history) and item.availability not in {
                "withdrawn",
                "deleted",
                "availability_unknown",
            }:
                return False
        try:
            anchor = json.loads(self.anchor.read_text())
            expected = {
                "epoch": consumer.epoch,
                "last_sequence": consumer.last_sequence,
                "event_hash": fingerprint([self._row(e) for e in events]),
            }
            return anchor == expected
        except (OSError, ValueError):
            return False

    def _anchor(self):
        with Session(self.store.engine) as session:
            consumer = session.get(Consumer, "availability")
            atomic_json(
                self.anchor,
                {
                    "epoch": consumer.epoch,
                    "last_sequence": consumer.last_sequence,
                    "event_hash": fingerprint([self._row(e) for e in self._events(session)]),
                },
            )

    @contextmanager
    def _write(self, *, require_ledger=True):
        with self.store.lock("knowledge-ledger"):
            with self.store._write() as session:
                valid = self._ledger_valid(session)
                if require_ledger and not valid:
                    raise StateConflict("AVAILABILITY_UNKNOWN")
                yield session
            if valid:
                self._anchor()

    def _item(self, session, page_id, *, allow_deleted=False):
        row = session.get(Item, page_id)
        if row is None or row.workspace_id != self.workspace_id:
            raise KeyError("WIKI_NOT_FOUND")
        if not allow_deleted and (
            row.availability == "deleted"
            or session.scalar(
                select(Event.sequence)
                .where(Event.page_id == page_id, Event.workspace_id == self.workspace_id, Event.event_type == "deleted")
                .limit(1)
            )
            is not None
        ):
            raise KeyError("WIKI_NOT_FOUND")
        return row

    def _event(self, session, item, kind, payload):
        from .wiki_query_index import invalidate_current

        invalidate_current(session, self.workspace_id)
        from .wiki_relations import invalidate_relations

        invalidate_relations(session, self.workspace_id, item.page_id)
        consumer = session.get(Consumer, "availability")
        consumer.last_sequence += 1
        event = Event(
            sequence=consumer.last_sequence,
            event_id=uuid.uuid4().hex,
            workspace_id=item.workspace_id,
            page_id=item.page_id,
            epoch=consumer.epoch,
            event_type=kind,
            payload=deepcopy(payload),
            created_at=clock.now(),
        )
        session.add(event)
        item.last_event_seq = event.sequence
        item.updated_at = event.created_at

    def _revision(self, session, revision_id):
        row = session.get(Revision, revision_id)
        if row is None or row.workspace_id != self.workspace_id:
            raise KeyError("WIKI_REVISION_NOT_FOUND")
        if row.content.get("deleted") is True:
            raise KeyError("WIKI_REVISION_DELETED")
        result = knowledge_row(row, sourced_epoch)
        supports = session.scalars(
            select(Support).where(Support.revision_id == revision_id).order_by(Support.support_id)
        ).all()
        dependencies = session.scalars(
            select(Dependency).where(Dependency.revision_id == revision_id).order_by(Dependency.dependency_id)
        ).all()
        if (
            fingerprint(row.content) != row.content_hash
            or fingerprint(row.dependency_manifest) != row.dependency_manifest_hash
            or any(fingerprint(s.payload["ref"]) != s.ref_hash for s in supports)
            or any(fingerprint(d.payload) != d.dependency_hash for d in dependencies)
            or sorted((s.payload for s in supports), key=lambda s: s["support_id"])
            != sorted(row.dependency_manifest["supports"], key=lambda s: s["support_id"])
            or sorted((d.payload for d in dependencies), key=lambda d: d["dependency_id"])
            != sorted(row.dependency_manifest["dependencies"], key=lambda d: d["dependency_id"])
        ):
            raise StateConflict("WIKI_INTEGRITY_FAILURE")
        result["supports"] = [self._row(s)["payload"] for s in supports]
        result["corpus_snapshot_id"] = row.dependency_manifest["corpus_snapshot_id"]
        return result

    def _review(self, session, revision):
        row = session.scalar(
            select(Review)
            .where(Review.revision_id == revision["revision_id"])
            .order_by(Review.created_at.desc(), Review.review_id.desc())
        )
        if (
            accepted_review(row, revision)
            and row.content_hash == revision["content_hash"]
            and row.dependency_manifest_hash == revision["dependency_manifest_hash"]
        ):
            return knowledge_row(row, sourced_epoch)
        return None

    def get_page(self, page_id, revision=None):
        """Carry the selected row instants through epoch-valued views to the website serializer."""
        with self.store.lock("knowledge-ledger"), Session(self.store.engine) as session:
            item = self._item(session, page_id)
            result = knowledge_row(item, sourced_epoch)
            rev_id = item.current_revision_id
            if revision is not None:
                row = (
                    session.scalar(select(Revision).where(Revision.page_id == page_id, Revision.revision == revision))
                    if type(revision) is int
                    else session.get(Revision, revision)
                )
                if row is None or row.page_id != page_id:
                    raise KeyError("WIKI_REVISION_NOT_FOUND")
                rev_id = row.revision_id
            result["revision"] = self._revision(session, rev_id)
            result["review"] = self._review(session, result["revision"])
            if rev_id != item.current_revision_id and item.availability not in {
                "withdrawn",
                "deleted",
                "availability_unknown",
            }:
                blockers = self._revision_blockers(session, rev_id)
                result["availability"] = "stale" if blockers else "current"
                result["availability_reason"] = blockers
            if not self._ledger_valid(session):
                result["availability"] = "availability_unknown"
                result["availability_reason"] = [{"code": "AVAILABILITY_UNKNOWN"}]
            result["history"] = [
                {
                    "revision_id": r.revision_id,
                    "revision": r.revision,
                    "content_hash": r.content_hash,
                    "created_at": sourced_epoch(r.created_at),
                }
                for r in session.scalars(
                    select(Revision).where(Revision.page_id == page_id).order_by(Revision.revision.desc())
                )
            ]
            return result

    def list_pages(self):
        with Session(self.store.engine) as session:
            ids = list(
                session.scalars(
                    select(Item.page_id).where(Item.workspace_id == self.workspace_id).order_by(Item.updated_at.desc())
                )
            )
        pages = []
        for pid in ids:
            if self.is_deleted(pid):
                continue
            try:
                pages.append(self.get_page(pid))
            except KeyError:
                # A deletion may commit between the inventory and page lookup.
                if not self.is_deleted(pid):
                    raise
        return pages

    def find_revision_request(self, request_id, request_hash):
        with Session(self.store.engine) as session:
            row = session.scalar(
                select(Revision).where(Revision.workspace_id == self.workspace_id, Revision.request_id == request_id)
            )
            if row:
                if row.request_hash != request_hash:
                    raise StateConflict("REQUEST_ID_CONFLICT")
                return self._row(row)
        return None

    def save_revision(self, *, page_id, expected_revision, request_id, request_hash, content, dependency_manifest):
        with self._write() as session:
            if session.get(Item, page_id) is not None:
                self._item(session, page_id)
            old = session.scalar(
                select(Revision).where(Revision.workspace_id == self.workspace_id, Revision.request_id == request_id)
            )
            if old:
                if old.request_hash != request_hash:
                    raise StateConflict("REQUEST_ID_CONFLICT")
                return self._row(old)
            item = session.get(Item, page_id)
            now = clock.now()
            if item is None:
                if expected_revision != 0:
                    raise StateConflict("REVISION_CONFLICT")
                epoch = session.get(Consumer, "availability").epoch
                item = Item(
                    page_id=page_id,
                    workspace_id=self.workspace_id,
                    item_kind="research_topic",
                    title=content["title"],
                    current_revision=0,
                    current_revision_id="",
                    published_revision_id=None,
                    availability="current",
                    availability_reason=[],
                    epoch=epoch,
                    last_event_seq=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
                session.flush()
            else:
                item = self._item(session, page_id)
                if item.availability == "withdrawn":
                    raise StateConflict("WIKI_WITHDRAWN")
            if item.current_revision != expected_revision:
                raise StateConflict("REVISION_CONFLICT")
            revision = item.current_revision + 1
            rid = "wikirev-" + fingerprint([page_id, revision, content, dependency_manifest])
            row = Revision(
                revision_id=rid,
                workspace_id=self.workspace_id,
                page_id=page_id,
                revision=revision,
                request_id=request_id,
                request_hash=request_hash,
                content=deepcopy(content),
                content_hash=fingerprint(content),
                dependency_manifest=deepcopy(dependency_manifest),
                dependency_manifest_hash=fingerprint(dependency_manifest),
                created_at=now,
            )
            session.add(row)
            session.flush()
            for support in dependency_manifest["supports"]:
                session.add(
                    Support(
                        revision_id=rid,
                        support_id=support["support_id"],
                        ref_kind=support["ref"]["kind"],
                        ref_hash=fingerprint(support["ref"]),
                        payload=deepcopy(support),
                    )
                )
            for dep in dependency_manifest["dependencies"]:
                session.add(
                    Dependency(
                        revision_id=rid,
                        dependency_id=dep["dependency_id"],
                        dependency_kind=dep["kind"],
                        dependency_hash=fingerprint(dep),
                        payload=deepcopy(dep),
                    )
                )
            item.current_revision = revision
            item.current_revision_id = rid
            item.title = content["title"]
            item.availability = "current"
            item.availability_reason = []
            self._event(
                session,
                item,
                "revision_created",
                {
                    "revision_id": rid,
                    "content_hash": row.content_hash,
                    "dependency_manifest_hash": row.dependency_manifest_hash,
                },
            )
            return self._row(row)

    def review(self, page_id, payload):
        request_hash = fingerprint([page_id, payload])
        with self._write() as session:
            old = session.scalar(
                select(Review).where(
                    Review.workspace_id == self.workspace_id, Review.request_id == payload["request_id"]
                )
            )
            if old:
                if old.request_hash != request_hash:
                    raise StateConflict("REQUEST_ID_CONFLICT")
                return self._row(old)
            item = self._item(session, page_id)
            rev = self._revision(session, item.current_revision_id)
            if item.availability != "current":
                raise StateConflict("WIKI_NOT_CURRENT")
            if (
                item.current_revision != payload["expected_revision"]
                or rev["content_hash"] != payload["content_hash"]
                or rev["dependency_manifest_hash"] != payload["dependency_manifest_hash"]
            ):
                raise StateConflict("REVIEW_REVISION_CONFLICT")
            row = Review(
                review_id="review-" + uuid.uuid4().hex,
                workspace_id=self.workspace_id,
                page_id=page_id,
                revision_id=rev["revision_id"],
                request_id=payload["request_id"],
                request_hash=request_hash,
                content_hash=rev["content_hash"],
                dependency_manifest_hash=rev["dependency_manifest_hash"],
                reviewer=payload["reviewer"],
                decision=payload["decision"],
                created_at=clock.now(),
            )
            session.add(row)
            self._event(
                session,
                item,
                "review_recorded",
                {"review_id": row.review_id, "decision": row.decision, "revision_id": row.revision_id},
            )
            return self._row(row)

    def auto_accept(self, page_id, expected_revision):
        with self._write() as session:
            item = self._item(session, page_id)
            revision = self._revision(session, item.current_revision_id)
            if item.current_revision != expected_revision:
                raise StateConflict("REVISION_CONFLICT")
            if item.availability != "current":
                raise StateConflict("WIKI_NOT_CURRENT")
            from .study_wiki_policy import POLICY_VERSION as STUDY_POLICY
            from .study_wiki_policy import is_study_summary

            if not (is_table_summary(revision) or is_study_summary(revision) or is_unified_summary(revision)):
                raise ValueError("TABLE_SUMMARY_REQUIRED")
            old = self._review(session, revision)
            if old:
                return old
            policy = (
                UNIFIED_POLICY_VERSION
                if is_unified_summary(revision)
                else STUDY_POLICY
                if is_study_summary(revision)
                else POLICY_VERSION
            )
            rid = "wiki-auto-accept-" + fingerprint([policy, revision["revision_id"]])
            row = Review(
                review_id="review-" + fingerprint(rid),
                workspace_id=self.workspace_id,
                page_id=page_id,
                revision_id=revision["revision_id"],
                request_id=rid,
                request_hash=fingerprint([rid, revision["content_hash"], revision["dependency_manifest_hash"]]),
                content_hash=revision["content_hash"],
                dependency_manifest_hash=revision["dependency_manifest_hash"],
                reviewer=policy,
                decision="auto_accepted",
                created_at=clock.now(),
            )
            session.add(row)
            self._event(
                session,
                item,
                "automatic_publication_authorized",
                {"review_id": row.review_id, "policy_version": policy, "revision_id": row.revision_id},
            )
            return self._row(row)

    def is_deleted(self, page_id):
        with Session(self.store.engine) as session:
            item = session.get(Item, page_id)
            if item is None or item.workspace_id != self.workspace_id:
                return False
            return (
                item.availability == "deleted"
                or session.scalar(
                    select(Event.sequence)
                    .where(
                        Event.page_id == page_id, Event.workspace_id == self.workspace_id, Event.event_type == "deleted"
                    )
                    .limit(1)
                )
                is not None
            )

    def delete(self, page_id, payload):
        digest = fingerprint([page_id, payload])
        with self._write() as session:
            item = self._item(session, page_id, allow_deleted=True)
            revision = self._revision(session, item.current_revision_id)
            from .study_wiki_policy import is_study_summary

            if not (is_table_summary(revision) or is_study_summary(revision)):
                raise ValueError("TABLE_SUMMARY_REQUIRED")
            events = session.scalars(select(Event).where(Event.page_id == page_id, Event.event_type == "deleted")).all()
            for event in events:
                if event.payload["request_id"] == payload["request_id"]:
                    if event.payload["request_hash"] != digest:
                        raise StateConflict("REQUEST_ID_CONFLICT")
                    return {"page_id": page_id, "deleted": True}
            if item.current_revision != payload["expected_revision"]:
                raise StateConflict("REVISION_CONFLICT")
            if events or item.availability == "deleted":
                raise KeyError("WIKI_NOT_FOUND")
            item.availability = "deleted"
            item.availability_reason = [{"code": "WIKI_DELETED"}]
            item.published_revision_id = None
            self._event(
                session,
                item,
                "deleted",
                {"request_id": payload["request_id"], "request_hash": digest, "revision_id": item.current_revision_id},
            )
            return {"page_id": page_id, "deleted": True}

    def _revision_blockers(self, session, revision_id):
        events = session.scalars(
            select(Event)
            .where(Event.event_type.in_(["dependency_unavailable", "publication_validation_recovered"]))
            .order_by(Event.sequence)
        ).all()
        matches = [e.payload.get("gaps", []) for e in events if e.payload.get("revision_id") == revision_id]
        return matches[-1] if matches else []

    def revision_blockers(self, revision_id):
        with Session(self.store.engine) as session:
            self._revision(session, revision_id)
            return deepcopy(self._revision_blockers(session, revision_id))

    def record_availability(self, page_id, gaps, revision_id=None):
        with self._write(require_ledger=False) as session:
            item = self._item(session, page_id)
            if not self._ledger_valid(session):
                item.availability = "availability_unknown"
                item.availability_reason = [{"code": "AVAILABILITY_UNKNOWN"}]
                consumer = session.get(Consumer, "availability")
                if consumer:
                    consumer.status = "availability_unknown"
                return
            target = revision_id or item.current_revision_id
            if target != item.current_revision_id:
                if gaps and self._revision_blockers(session, target) != gaps:
                    self._event(
                        session, item, "dependency_unavailable", {"gaps": deepcopy(gaps), "revision_id": target}
                    )
                return
            # Stale and revoked revisions are never silently resurrected by source reappearance.
            if gaps and item.availability not in {"withdrawn", "deleted", "availability_unknown"}:
                if item.availability != "stale" or item.availability_reason != gaps:
                    item.availability = "stale"
                    item.availability_reason = deepcopy(gaps)
                    self._event(
                        session,
                        item,
                        "dependency_unavailable",
                        {"gaps": deepcopy(gaps), "revision_id": item.current_revision_id},
                    )

    def withdraw(self, page_id, payload):
        digest = fingerprint([page_id, payload])
        with self._write() as session:
            item = self._item(session, page_id)
            events = session.scalars(
                select(Event).where(Event.page_id == page_id, Event.event_type == "withdrawn")
            ).all()
            for event in events:
                if event.payload["request_id"] == payload["request_id"]:
                    if event.payload["request_hash"] != digest:
                        raise StateConflict("REQUEST_ID_CONFLICT")
                    return
            if item.current_revision != payload["expected_revision"]:
                raise StateConflict("REVISION_CONFLICT")
            item.availability = "withdrawn"
            item.availability_reason = [{"code": "WIKI_WITHDRAWN", "reason": payload["reason"]}]
            self._event(
                session,
                item,
                "withdrawn",
                {"request_id": payload["request_id"], "request_hash": digest, "reason": payload["reason"]},
            )

    def find_publication(self, page_id, request_id):
        with Session(self.store.engine) as session:
            row = session.scalar(
                select(Publication).where(
                    Publication.workspace_id == self.workspace_id,
                    Publication.page_id == page_id,
                    Publication.request_id == request_id,
                )
            )
            return self._row(row) if row else None

    def get_publication(self, publication_id):
        with Session(self.store.engine) as session:
            row = session.get(Publication, publication_id)
            if row is None or row.workspace_id != self.workspace_id:
                raise KeyError("PUBLICATION_NOT_FOUND")
            return self._row(row)

    def reserve_publication(
        self, page_id, *, request_id, request_hash, expected_revision, package, asset_hashes, publication_id=None
    ):
        with self._write() as session:
            old = session.scalar(
                select(Publication).where(
                    Publication.workspace_id == self.workspace_id, Publication.request_id == request_id
                )
            )
            if old:
                if old.page_id != page_id or old.request_hash != request_hash:
                    raise StateConflict("REQUEST_ID_CONFLICT")
                return self._row(old)
            item = self._item(session, page_id)
            rev = self._revision(session, item.current_revision_id)
            review = self._review(session, rev)
            if item.current_revision != expected_revision:
                raise StateConflict("REVISION_CONFLICT")
            if item.availability != "current" or review is None:
                raise StateConflict("REVIEW_REQUIRED")
            expected = {
                "workspace_id": self.workspace_id,
                "page_id": page_id,
                "revision_id": rev["revision_id"],
                "revision": rev["revision"],
                "content_hash": rev["content_hash"],
                "dependency_manifest_hash": rev["dependency_manifest_hash"],
                "content": rev["content"],
                "supports": rev["supports"],
                "dependency_manifest": rev["dependency_manifest"],
                "review": review,
            }
            if (
                any(package.get(k) != v for k, v in expected.items())
                or package.get("format") != "knowledge-publication-v1"
                or publication_id is not None
                and package.get("publication_id") != publication_id
                or {k: v["sha256"] for k, v in package.get("assets", {}).items()} != asset_hashes
            ):
                raise StateConflict("PUBLICATION_BINDING_CONFLICT")
            active = session.scalar(
                select(Publication).where(
                    Publication.page_id == page_id, Publication.status.in_(["prepared", "publishing", "write_unknown"])
                )
            )
            if active:
                raise StateConflict("PUBLICATION_PENDING_RECONCILE")
            now = clock.now()
            row = Publication(
                publication_id=publication_id or "publication-" + uuid.uuid4().hex,
                workspace_id=self.workspace_id,
                page_id=page_id,
                revision_id=rev["revision_id"],
                revision=rev["revision"],
                content_hash=rev["content_hash"],
                dependency_manifest_hash=rev["dependency_manifest_hash"],
                review_id=review["review_id"],
                request_id=request_id,
                request_hash=request_hash,
                status="prepared",
                package=deepcopy(package),
                asset_hashes=deepcopy(asset_hashes),
                receipt=None,
                error_code=None,
                epoch=item.epoch,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            self._event(
                session,
                item,
                "publication_prepared",
                {"publication_id": row.publication_id, "revision_id": row.revision_id},
            )
            return self._row(row)

    def set_publication_outcome(self, publication_id, *, status, receipt=None, error_code=None):
        if status not in {"prepared", "publishing", "write_unknown", "failed", "historical"}:
            raise ValueError("INVALID_PUBLICATION_STATUS")
        with self._write(require_ledger=False) as session:
            row = session.get(Publication, publication_id)
            if row is None or row.workspace_id != self.workspace_id:
                raise KeyError("PUBLICATION_NOT_FOUND")
            if row.status in {"published", "historical"}:
                return self._row(row)
            row.status = status
            row.receipt = deepcopy(receipt)
            row.error_code = error_code
            row.updated_at = clock.now()
            return self._row(row)

    def finalize_publication(self, publication_id, *, receipt, current_gaps=None, recover_validation=False):
        with self._write(require_ledger=False) as session:
            row = session.get(Publication, publication_id)
            if row is None or row.workspace_id != self.workspace_id:
                raise KeyError("PUBLICATION_NOT_FOUND")
            recovering = recover_validation and row.status == "historical"
            if row.status == "published" or row.status == "historical" and not recovering:
                return self._row(row)
            item = self._item(session, row.page_id, allow_deleted=True)
            try:
                rev = self._revision(session, item.current_revision_id)
                review = self._review(session, rev)
            except (KeyError, StateConflict):
                rev = {}
                review = None
            recoverable = False
            if recovering:
                from .knowledge_table_wiki_policy import is_unified_summary

                events = session.scalars(
                    select(Event).where(Event.page_id == row.page_id, Event.event_type == "dependency_unavailable")
                ).all()
                prior_gaps = [
                    g
                    for event in events
                    if event.payload.get("revision_id") == row.revision_id
                    for g in event.payload.get("gaps", [])
                ]
                recoverable = (
                    is_unified_summary(rev)
                    and row.receipt == receipt
                    and bool(receipt)
                    and row.error_code == "PUBLICATION_SUPERSEDED"
                    and item.availability in {"current", "stale"}
                    and all(g.get("code") == "RESEARCH_SUPPORT_INVALID" for g in prior_gaps + item.availability_reason)
                )
                if not recoverable:
                    return self._row(row)
            valid = (
                self._ledger_valid(session)
                and not current_gaps
                and (item.availability == "current" or recoverable)
                and item.epoch == row.epoch
                and item.current_revision_id == row.revision_id
                and rev["content_hash"] == row.content_hash
                and rev["dependency_manifest_hash"] == row.dependency_manifest_hash
                and review is not None
                and review["review_id"] == row.review_id
            )
            row.receipt = deepcopy(receipt)
            row.status = "published" if valid else "historical"
            row.updated_at = clock.now()
            row.error_code = None if valid else "PUBLICATION_SUPERSEDED"
            if valid:
                item.published_revision_id = row.revision_id
                if recovering:
                    item.availability = "current"
                    item.availability_reason = []
                    self._event(
                        session,
                        item,
                        "publication_validation_recovered",
                        {"publication_id": row.publication_id, "revision_id": row.revision_id},
                    )
            if self._ledger_valid(session):
                self._event(
                    session, item, "publication_verified", {"publication_id": row.publication_id, "status": row.status}
                )
            return self._row(row)

    def published_publication(self, page_id, revision_id):
        with Session(self.store.engine) as session:
            self._item(session, page_id)
            row = session.scalar(
                select(Publication)
                .where(
                    Publication.page_id == page_id,
                    Publication.revision_id == revision_id,
                    Publication.status == "published",
                )
                .order_by(Publication.created_at.desc())
            )
            return self._row(row) if row else None

    def invalidate_table_in_session(self, session, draft):
        """Called with source -> knowledge-ledger locks in the source write transaction.

        The same commit changes the table and records every affected revision;
        restoring the range later cannot erase this event. No Wiki rows is a no-op.
        """
        dependencies = table_dependencies.for_source(session, self.workspace_id, draft.source_id)
        affected = {}
        for row in dependencies:
            dep = row.payload
            code = self._changed_table_region(draft, dep)
            if code:
                affected.setdefault(row.revision_id, []).append({"code": code, "dependency_id": dep["dependency_id"]})
        if not affected:
            return
        valid = self._ledger_valid(session)
        for revision_id, gaps in affected.items():
            revision = session.get(Revision, revision_id)
            item = session.get(Item, revision.page_id)
            if not valid:
                item.availability = "availability_unknown"
                item.availability_reason = [{"code": "AVAILABILITY_UNKNOWN"}]
                consumer = session.get(Consumer, "availability")
                if consumer:
                    consumer.status = "availability_unknown"
                continue
            if self._revision_blockers(session, revision_id) == gaps:
                continue
            if item.current_revision_id == revision_id and item.availability not in {
                "withdrawn",
                "deleted",
                "availability_unknown",
            }:
                item.availability = "stale"
                item.availability_reason = deepcopy(gaps)
            self._event(session, item, "dependency_unavailable", {"gaps": deepcopy(gaps), "revision_id": revision_id})

    @staticmethod
    def _changed_table_region(draft, dependency):
        from .knowledge_support_resolver import REGION_FIELDS

        ref = dependency["ref"]
        regions = draft.recipe.get("region_review", {}).get("regions", [])
        region = next((row for row in regions if row["id"] == ref["source_region_id"]), None)
        if region is None:
            return "TABLE_REGION_REMOVED"
        fields = {key: region.get(key, [] if key.endswith("ranges") else None) for key in REGION_FIELDS}
        if (
            fields != dependency["region"]
            or draft.reader_options.get(ref["asset_id"], {}) != dependency["reader_options"]
            or draft.asset_hashes.get(ref["asset_id"]) != ref["asset_sha256"]
        ):
            return "TABLE_DEFINITION_CHANGED"
        return None
