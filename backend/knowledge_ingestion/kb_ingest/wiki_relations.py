"""Bounded navigation derived only from actually cited, fixed typed supports."""

import time
from contextlib import contextmanager
from copy import copy, deepcopy

from pydantic import TypeAdapter
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import aliased

from .core import StateConflict, fingerprint
from .knowledge_contracts import KnowledgeRef, WikiRevisionRef
from .knowledge_index_store import IndexFailure, KnowledgeIndexStore
from .knowledge_snapshot import KnowledgeIntegrityError
from .knowledge_wiki_adapter import KnowledgeWikiAdapter
from .storage.engine import unpooled_engine
from .storage.models import KnowledgeItem
from .storage.models import KnowledgeRelation as Row

VERSION = "wiki-relations-v1"
MAX_REFRESH = 128
MAX_ROWS = 256
MAX_NEIGHBORS = 60
BINDING = ("page_id", "revision_id", "content_hash", "dependency_manifest_hash")
OWNER = (
    "workspace_id",
    "task_id",
    "source_id",
    "source_revision_id",
    "record_revision_id",
    "record_snapshot_id",
    "package_sha256",
)


def binding(page):
    return KnowledgeWikiAdapter._identity(page)


def projection_rows(page, workspace_id, artifact_loader=None):
    """Pure identities, except optional immutable artifact verification. No inference."""
    from .core.clock import remaining

    revision = page["revision"]
    used = {}
    for section in revision["content"]["sections"]:
        for claim in section["claims"]:
            remaining()
            for sid in claim["support_ids"]:
                used.setdefault(sid, []).append(claim["claim_id"])
    rows = []
    for support in revision["supports"]:
        remaining()
        if len(rows) > MAX_ROWS:
            break
        if support["support_id"] not in used:
            continue
        ref = TypeAdapter(KnowledgeRef).validate_python(support["ref"]).model_dump(mode="json")
        if ref["workspace_id"] != workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        basis = dict(
            binding=binding(page),
            support_id=support["support_id"],
            claim_ids=sorted(used[support["support_id"]]),
            ref_hash=fingerprint(ref),
            ref=ref,
        )

        def add(kind, anchor, target=None, **extra):
            row = dict(
                workspace_id=workspace_id,
                **binding(page),
                projection_version=VERSION,
                relation_kind=kind,
                anchor_key=fingerprint(anchor),
                target_binding=target,
                basis={**basis, **extra},
            )
            rows.append(dict(row_id=fingerprint(row), **row))

        if ref["kind"] == "wiki_revision":
            target = {key: ref[key] for key in BINDING}
            add("wiki_reference", target, target)
        else:
            source = ref.get("source_id") or ref.get("source_snapshot_ref", {}).get("source_id")
            if source:
                add("shared_source", [workspace_id, source])
            if ref["kind"] == "sample_test_context":
                owner = [ref[k] for k in OWNER]
                add("shared_entity", [*owner, "sample", ref["sample_ref"]], entity_kind="sample")
                if ref["test_ref"] is not None:
                    add("shared_entity", [*owner, "test", ref["sample_ref"], ref["test_ref"]], entity_kind="test")
            if ref["kind"] == "table_analysis_artifact" and artifact_loader is not None:
                artifact = artifact_loader(ref)
                versions = (
                    [artifact["table_version_id"]]
                    if "table_version_id" in artifact
                    else [t["table_version_id"] for t in artifact.get("tables", [])]
                )
                if not versions or not set(versions) <= set(ref["table_version_ids"]):
                    raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
                for dependency in revision["dependency_manifest"]["dependencies"]:
                    raw = dependency.get("ref", {})
                    if dependency["kind"] == "raw_table" and raw.get("table_version_id") in versions:
                        add("shared_source", [workspace_id, raw["source_id"]], source_ref=deepcopy(raw))
    return sorted({row["row_id"]: row for row in rows}.values(), key=lambda r: r["row_id"])


def invalidate_relations(session, workspace_id, page_id):
    session.execute(delete(Row).where(Row.workspace_id == workspace_id, Row.page_id == page_id))


class WikiRelationsService:
    def __init__(self, adapter):
        self.adapter = adapter
        self.wiki = adapter.wiki
        self.workspace_id = adapter.workspace_id
        # SQLite file connections have no shared-pool checkout wait. Only this
        # derived projection uses the view; the application's Store is unchanged.
        local = copy(self.wiki.ingestion_service.store)
        local.engine = unpooled_engine(local.engine.url, foreign_keys=True)
        self.db = KnowledgeIndexStore(local)

    @contextmanager
    def _budget(self, deadline):
        from .core.clock import IO_DEADLINE
        from .knowledge_service import READ_DEADLINE

        deadline = min(d for d in (deadline, READ_DEADLINE.get(), IO_DEADLINE.get()) if d is not None)
        tokens = (READ_DEADLINE.set(deadline), IO_DEADLINE.set(deadline))
        try:
            yield deadline
        finally:
            READ_DEADLINE.reset(tokens[0])
            IO_DEADLINE.reset(tokens[1])

    @staticmethod
    def _check(deadline):
        from .knowledge_service import READ_CANCELED

        if time.monotonic() >= deadline:
            raise IndexFailure("INDEX_TIMEOUT")
        if READ_CANCELED.get() is not None and READ_CANCELED.get().is_set():
            raise IndexFailure("INDEX_TIMEOUT")

    def current(self, expected, deadline):
        self._check(deadline)
        try:
            with self._budget(deadline):
                page = self.adapter._current(expected if isinstance(expected, str) else expected["page_id"])
        except (KeyError, ValueError, StateConflict):
            page = None
        self._check(deadline)
        if page is None or (isinstance(expected, dict) and binding(page) != expected):
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        return page

    def resolve(self, ref, deadline):
        self._check(deadline)
        with self._budget(deadline):
            self.adapter.resolve(WikiRevisionRef.model_validate(ref))
        return self.current({key: ref[key] for key in BINDING}, deadline)

    def _artifact(self, ref):
        payload = self.wiki.ingestion_service.get_table_chat_artifact(
            ref["chat_id"], ref["turn_id"], ref["artifact_id"]
        )
        if fingerprint(payload) != ref["artifact_sha256"]:
            raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        return payload["artifact"]

    def _rows(self, page):
        return projection_rows(page, self.workspace_id, self._artifact)

    def _refresh(self, seeds, pages, deadline, reasons):
        available = {}
        unavailable = 0
        if pages is None:
            with self.db._read(deadline) as session:
                ids = list(
                    session.scalars(
                        select(KnowledgeItem.page_id)
                        .where(
                            KnowledgeItem.workspace_id == self.workspace_id,
                            KnowledgeItem.published_revision_id.is_not(None),
                            KnowledgeItem.availability.notin_(["deleted", "withdrawn", "availability_unknown"]),
                        )
                        .order_by(KnowledgeItem.page_id)
                        .limit(MAX_REFRESH + 1)
                    )
                )
            candidates = list(dict.fromkeys([s["page_id"] for s in seeds] + ids))
        else:
            by_id = {p["page_id"]: p for p in pages}
            candidates = list(dict.fromkeys([s["page_id"] for s in seeds] + sorted(by_id)))
        if len(candidates) > MAX_REFRESH:
            reasons.add("projection_incomplete")
        for pid in candidates[:MAX_REFRESH]:
            try:
                self._check(deadline)
                page = self.current(pid, deadline) if pages is None else by_id.get(pid)
                if page is None:
                    raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
                rows = self._rows(page)
                if len(rows) > MAX_ROWS:
                    reasons.add("projection_incomplete")
                    rows = rows[:MAX_ROWS]
                with self.db._write(deadline) as session:
                    old = [
                        {c.name: getattr(row, c.name) for c in Row.__table__.columns}
                        for row in session.scalars(
                            select(Row)
                            .where(Row.workspace_id == self.workspace_id, Row.page_id == pid)
                            .limit(MAX_ROWS + 1)
                        )
                    ]
                    if {fingerprint(r) for r in old} != {fingerprint(r) for r in rows}:
                        invalidate_relations(session, self.workspace_id, pid)
                        session.add_all(Row(**row) for row in rows)
                available[pid] = page
            except (IndexFailure, TimeoutError):
                reasons.update(("deadline", "projection_incomplete"))
                break
            except (ValueError, KeyError, StateConflict):
                unavailable += 1
        return available, unavailable

    def validate_edge(self, edge, deadline):
        left = self.current(edge["basis"]["from"]["binding"], deadline)
        right = self.current(edge["basis"]["to"]["binding"], deadline)

        def matches(page, basis):
            return any(
                row["relation_kind"] == edge["type"]
                and row["anchor_key"] == edge["basis"]["anchor_key"]
                and row["basis"] == basis
                for row in self._rows(page)
            )

        if not matches(left, edge["basis"]["from"]):
            raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        if edge["type"] == "wiki_reference":
            target = self.resolve(edge["basis"]["from"]["ref"], deadline)
            if binding(target) != binding(right):
                raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        elif not matches(right, edge["basis"]["to"]):
            raise KnowledgeIntegrityError("SNAPSHOT_MISMATCH")
        return left, right

    def _edge(self, left, right, kind, anchor):
        if kind != "wiki_reference" and left["binding"]["page_id"] > right["binding"]["page_id"]:
            left, right = right, left
        core = {
            "from": left["binding"]["page_id"],
            "to": right["binding"]["page_id"],
            "type": kind,
            "directed": kind == "wiki_reference",
            "basis": {"from": deepcopy(left), "to": deepcopy(right), "anchor_key": anchor},
        }
        # Multiple claims/supports on one anchor explain the same pair once.
        core["id"] = "relation-" + fingerprint([kind, anchor, left["binding"], right["binding"]])
        return core

    def _neighbors(self, page, available, deadline, reasons):
        pid = page["page_id"]
        other = aliased(Row)

        def current_query(query):
            return query.join(KnowledgeItem, KnowledgeItem.page_id == Row.page_id).where(
                Row.workspace_id == self.workspace_id,
                Row.projection_version == VERSION,
                Row.revision_id == KnowledgeItem.published_revision_id,
                Row.page_id.in_(list(available)),
            )

        with self.db._read(deadline) as session:
            own = list(
                session.scalars(
                    select(Row)
                    .where(
                        Row.workspace_id == self.workspace_id,
                        Row.page_id == pid,
                        Row.revision_id == page["revision"]["revision_id"],
                        Row.projection_version == VERSION,
                    )
                    .order_by(Row.row_id)
                    .limit(MAX_ROWS + 1)
                )
            )
            shared = list(
                session.scalars(
                    current_query(
                        select(Row).join(
                            other,
                            and_(
                                other.anchor_key == Row.anchor_key,
                                other.relation_kind == Row.relation_kind,
                                other.workspace_id == Row.workspace_id,
                                other.page_id == pid,
                                other.revision_id == page["revision"]["revision_id"],
                                other.projection_version == VERSION,
                            ),
                        )
                    )
                    .where(Row.page_id != pid, Row.relation_kind.in_(["shared_source", "shared_entity"]))
                    .distinct()
                    .order_by(Row.page_id, Row.row_id)
                    .limit(MAX_NEIGHBORS + 1)
                )
            )
            incoming = list(
                session.scalars(
                    current_query(select(Row))
                    .where(
                        Row.relation_kind == "wiki_reference",
                        Row.anchor_key == fingerprint(binding(page)),
                        Row.page_id != pid,
                    )
                    .order_by(Row.page_id, Row.row_id)
                    .limit(MAX_NEIGHBORS + 1)
                )
            )
            # All scalar fields are materialized while the bounded session is open.
            own = [{c.name: getattr(r, c.name) for c in Row.__table__.columns} for r in own]
            shared = [{c.name: getattr(r, c.name) for c in Row.__table__.columns} for r in shared]
            incoming = [{c.name: getattr(r, c.name) for c in Row.__table__.columns} for r in incoming]
        if len(shared) > MAX_NEIGHBORS or len(incoming) > MAX_NEIGHBORS or len(own) > MAX_ROWS:
            reasons.add("candidate_limit")
        edges = {}
        anchors = {(r["relation_kind"], r["anchor_key"]): r for r in reversed(own[:MAX_ROWS])}
        for r in shared[:MAX_NEIGHBORS]:
            left = anchors.get((r["relation_kind"], r["anchor_key"]))
            if left:
                edge = self._edge(left["basis"], r["basis"], r["relation_kind"], r["anchor_key"])
                edges[edge["id"]] = edge
        for r in [r for r in own[:MAX_ROWS] if r["relation_kind"] == "wiki_reference"] + incoming[:MAX_NEIGHBORS]:
            target = r["target_binding"]
            if target["page_id"] not in available or binding(available[target["page_id"]]) != target:
                continue
            edge = self._edge(r["basis"], {"binding": target}, "wiki_reference", r["anchor_key"])
            edges[edge["id"]] = edge
        return list(edges.values())

    def walk(self, seeds, depth, *, deadline, pages=None, choose=None, max_extra=None):
        if type(depth) is not int or depth not in (0, 1, 2) or not 1 <= len(seeds) <= 3:
            raise ValueError("INVALID_REQUEST")
        reasons = set()
        unavailable = 0
        nodes = {}
        edges = {}
        paths = {}
        path_edges = {}
        with self._budget(deadline) as deadline:
            for seed in seeds:
                p = self.current(seed, deadline)
                pid = p["page_id"]
                nodes[pid] = p
                paths[pid] = [binding(p)]
                path_edges[pid] = []
            if depth:
                work = deadline - min(2.0, max(0, deadline - time.monotonic()) * 0.2)
                available, unavailable = self._refresh(seeds, pages, work, reasons)
                frontier = list(nodes)
                extra = 0
                try:
                    for _ in range(depth):
                        candidates = {}
                        pending = []
                        for pid in frontier:
                            self._check(work)
                            if pid not in available:
                                continue
                            for edge in self._neighbors(nodes[pid], available, work, reasons):
                                target = edge["to"] if edge["from"] == pid else edge["from"]
                                if target not in available:
                                    continue
                                if target not in nodes:
                                    candidates.setdefault(target, (pid, edge))
                                pending.append(edge)
                                if len(pending) >= 121:
                                    reasons.add("candidate_limit")
                                    break
                            if len(pending) >= 121:
                                break
                        room = min(30 - len(nodes), max_extra - extra if max_extra is not None else 30)
                        selected = (
                            choose([available[p] for p in candidates], max(0, room))
                            if choose
                            else list(candidates)[: max(0, room)]
                        )
                        if len(candidates) > len(selected):
                            reasons.add(
                                "expansion_limit"
                                if max_extra is not None and len(selected) >= room
                                else "node_limit"
                                if room < len(candidates)
                                else "query_filtered"
                            )
                        frontier = []
                        for pid in selected[: max(0, room)]:
                            parent, edge = candidates[pid]
                            nodes[pid] = available[pid]
                            frontier.append(pid)
                            extra += 1
                            paths[pid] = paths[parent] + [binding(nodes[pid])]
                            path_edges[pid] = path_edges[parent] + [edge]
                        for edge in pending:
                            if edge["from"] in nodes and edge["to"] in nodes and edge["id"] not in edges:
                                if len(edges) >= 60:
                                    reasons.add("edge_limit")
                                    break
                                edges[edge["id"]] = edge
                        if not frontier or len(edges) >= 60:
                            break
                except (IndexFailure, TimeoutError):
                    reasons.add("deadline")
            # Authority, original refs and bindings are rechecked before exposure.
            valid = {}
            for pid, p in nodes.items():
                try:
                    valid[pid] = self.current(binding(p), deadline)
                except (IndexFailure, TimeoutError):
                    reasons.add("deadline")
                    break
                except (ValueError, KeyError, StateConflict):
                    unavailable += 1
            for key, edge in list(edges.items()):
                try:
                    if edge["from"] not in valid or edge["to"] not in valid:
                        raise ValueError()
                    self.validate_edge(edge, deadline)
                except (IndexFailure, TimeoutError):
                    edges.pop(key)
                    reasons.add("deadline")
                except (ValueError, KeyError, StateConflict):
                    edges.pop(key)
                    unavailable += 1
            # Rebuild paths from final valid edges, one BFS level at a time.
            # A removed edge cannot leave an orphan or a hidden longer route.
            reached = {s["page_id"] for s in seeds if s["page_id"] in valid}
            frontier = set(reached)
            paths = {pid: [binding(valid[pid])] for pid in reached}
            path_edges = {pid: [] for pid in reached}
            for _ in range(depth):
                following = set()
                for edge in edges.values():
                    for parent, target in ((edge["from"], edge["to"]), (edge["to"], edge["from"])):
                        if parent in frontier and target not in reached and target not in following:
                            following.add(target)
                            paths[target] = paths[parent] + [binding(valid[target])]
                            path_edges[target] = path_edges[parent] + [edge]
                reached.update(following)
                frontier = following
            valid = {pid: p for pid, p in valid.items() if pid in reached}
            edges = {k: e for k, e in edges.items() if e["from"] in valid and e["to"] in valid}
            result = dict(
                root_binding=seeds[0],
                depth=depth,
                nodes=[
                    dict(page_id=pid, title=p["revision"]["content"]["title"], binding=binding(p))
                    for pid, p in valid.items()
                ],
                edges=list(edges.values()),
                projection_version=VERSION,
                versions={pid: binding(p) for pid, p in valid.items()},
                truncated=bool(reasons),
                truncation_reasons=sorted(reasons),
                unavailable_count=unavailable,
                exhaustive_within_scope=False,
            )
            return result, {pid: (paths[pid], path_edges[pid]) for pid in valid}

    def graph(self, page_id, depth=1):
        deadline = time.monotonic() + 30
        with self._budget(deadline) as deadline:
            root = self.current(page_id, deadline)
            return self.walk([binding(root)], depth, deadline=deadline)[0]
