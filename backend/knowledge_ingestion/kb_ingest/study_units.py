"""Server-owned study identity and complete, unreviewed immutable evidence inputs."""

import json
import time
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import StateConflict, fingerprint
from .core.workspace import OMITTED_WORKSPACE, graph_workspace, validate_workspace_id
from .document_reports import build_report_input
from .storage.models import StudyUnit, StudyUnitRevision


def _encode(payload):
    # Immutable local storage is not the provider's bounded model text wire.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _remap_refs(value, mapping, field=None):
    if isinstance(value, dict):
        return {
            k: deepcopy(v) if k in {"raw_study", "reviews", "evidence"} else _remap_refs(v, mapping, k)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_remap_refs(v, mapping, field) for v in value]
    if isinstance(value, str) and field in {
        "id",
        "fact_id",
        "citation_refs",
        "refs",
        "supporting_refs",
        "source_fact_id",
        "evidence_refs",
    }:
        return mapping.get(value, value)
    return value


def unit_identity(source_id, extraction_batch, candidate_id, study_ref):
    return "unit-" + fingerprint([source_id, extraction_batch, candidate_id, study_ref])[:32]


def _row(row):
    return {c.name: deepcopy(getattr(row, c.name)) for c in row.__table__.columns}


class StudyUnitService:
    def __init__(self, service, *, workspace_id=OMITTED_WORKSPACE):
        self.service = service
        self.store = service.store
        if workspace_id is OMITTED_WORKSPACE:
            self.workspace_id = graph_workspace(service)
        else:
            self.workspace_id = validate_workspace_id(workspace_id)

    def _batch(self, task):
        seen = set()
        while task["options"].get("_document_review_parent"):
            if task["task_id"] in seen:
                raise StateConflict("STUDY_LINEAGE_INVALID")
            seen.add(task["task_id"])
            task = self.store.get_task(task["options"]["_document_review_parent"])
        return task["task_id"]

    def sync_task(self, task_id):
        task = self.store.get_task(task_id)
        try:
            rows = self._sync_task(task_id)
        except Exception:
            try:
                self.store.record_study_unit_sync(task_id, task["attempt_token"], error_code="STUDY_UNIT_SYNC_FAILED")
            except StateConflict:
                pass
            raise
        if rows:
            try:
                self.store.record_study_unit_sync(task_id, task["attempt_token"], error_code=None)
            except StateConflict:
                pass
        return rows

    def _sync_task(self, task_id):
        task = self.store.get_task(task_id)
        if task["source_type"] not in ("paper", "patent"):
            return []
        source = self.store.source_state(task["source_id"]) or {}
        if source.get("active_task_id") != task_id:
            return []
        view = self.service._document_candidate_snapshot(task_id, offset=0, limit=10000)
        if view.get("status") != "available":
            return []
        batch = self._batch(task)
        prepared = []
        summary_path = self.service._document_artifact_root(task) / "group-results.json"
        summaries = json.loads(summary_path.read_text()) if summary_path.is_file() else None
        metadata = {}
        offered = set()
        if summaries is not None:
            for summary in summaries:
                cid = "raw-" + fingerprint(summary.get("group_id"))[:24]
                mappings = summary.get("study_units", [])
                if mappings:
                    cid = mappings[0]["candidate_id"]
                metadata[cid] = summary
                if summary.get("status") == "completed":
                    for mapping in mappings:
                        expected = unit_identity(
                            task["source_id"], batch, mapping["candidate_id"], mapping["study_ref"]
                        )
                        continuation = mapping.get("continuation_of")
                        if continuation is not None and continuation not in offered:
                            raise StateConflict("STUDY_CONTINUATION_SCOPE")
                        if mapping["unit_id"] != (continuation or expected):
                            raise StateConflict("STUDY_IDENTITY_INVALID")
                        offered.add(mapping["unit_id"])
        ordered_groups = sorted(
            view["groups"],
            key=lambda g: list(metadata).index(g["candidate_id"]) if g["candidate_id"] in metadata else len(metadata),
        )
        for group in ordered_groups:
            if group.get("raw_status") != "available":
                continue
            raw = group["raw"]
            cid = group["candidate_id"]
            summary = metadata.get(cid)
            if summaries is not None:
                if summary is None or summary.get("status") != "completed":
                    continue
                if summary.get("raw_sha256") != fingerprint(raw):
                    raise StateConflict("STUDY_RAW_INTEGRITY")
            mapping_by_ref = {m["study_ref"]: m for m in (summary or {}).get("study_units", [])}
            for i, study in enumerate(raw.get("studies", [])):
                ref = study.get("ref")
                if not isinstance(ref, str) or not ref:
                    raise StateConflict("STUDY_IDENTITY_MISSING")
                uid = mapping_by_ref.get(ref, {}).get("unit_id") or unit_identity(task["source_id"], batch, cid, ref)
                projection = build_report_input(
                    view,
                    task_id=task_id,
                    candidate_id=cid,
                    study_ref=ref,
                    source_type=task["source_type"],
                    enforce_input_limit=False,
                )
                # Global candidate/review digests are not unit content: changing
                # another study must not revise this study's fixed input.
                projection["identity"] = {"candidate_id": cid, "study_ref": ref}
                projection["version"] = "study-unit-projection-v1"
                for fact in projection["facts"] + projection["structured_table"]:
                    fact.pop("placeholder", None)
                    fact.pop("label_placeholders", None)
                pointer = f"/studies/{i}"
                relevant = {f.get("source_pointer") for f in projection["facts"]}
                reviews = [
                    deepcopy(r)
                    for r in group.get("reviews", [])
                    if r["candidate_ref"].startswith(pointer + "/")
                    or r["candidate_ref"] == pointer
                    or r["candidate_ref"] in relevant
                ]
                payload = {
                    "version": "study-unit/v1",
                    "candidate_id": cid,
                    "study_ref": ref,
                    "study_pointer": pointer,
                    "raw_study": deepcopy(study),
                    "projection": projection,
                    "reviews": reviews,
                    "coverage": {"source_type": task["source_type"], "record_gaps": deepcopy(projection["gaps"])},
                    "source_refs": [
                        {"asset_id": aid, "asset_sha256": self.store.get_asset(aid)["sha256"]}
                        for aid in task["asset_ids"]
                    ],
                    "scientific_validation": False,
                    "human_expert_review": False,
                    "use_status": "unreviewed_candidate",
                }
                _encode(payload)
                prepared.append((uid, cid, ref, payload))
        consolidated = {}
        for uid, cid, ref, payload in prepared:
            # Packet-local references remain packet-local even inside one unit.
            replacements = {
                r["id"]: cid + ":" + ref + ":" + r["id"]
                for key in ("facts", "citations", "gaps")
                for r in payload["projection"][key]
            }
            component = _remap_refs(payload, replacements)
            for key in ("facts", "citations", "gaps", "context", "structured_table"):
                for entry in component["projection"][key]:
                    entry.update(candidate_id=cid, study_ref=ref)
            if uid not in consolidated:
                base = deepcopy(component)
                base["components"] = [component]
                consolidated[uid] = (uid, cid, ref, base)
            else:
                base = consolidated[uid][3]
                base["components"].append(component)
                for key in ("facts", "citations", "gaps", "issues", "structured_table"):
                    base["projection"][key].extend(deepcopy(component["projection"][key]))
                base["projection"]["context"].extend(deepcopy(component["projection"]["context"]))
                base["raw_study"]["samples"].extend(deepcopy(component["raw_study"].get("samples", [])))
                base["reviews"].extend(deepcopy(component["reviews"]))
            _encode(consolidated[uid][3])
        prepared = list(consolidated.values())
        now = time.time()
        result = []
        with self.store._write() as session:
            # Fence against newer extraction between projection and transaction.
            from .storage.source_repository import require_registered

            if require_registered(session, self.store.workspace_id, task["source_id"]).active_task_id != task_id:
                raise StateConflict("STUDY_SOURCE_CHANGED")
            current_ids = {p[0] for p in prepared}
            for old in session.scalars(
                select(StudyUnit).where(
                    StudyUnit.workspace_id == self.workspace_id, StudyUnit.source_id == task["source_id"]
                )
            ):
                old.current_available = old.unit_id in current_ids
            for uid, cid, ref, payload in prepared:
                row = session.get(StudyUnit, uid)
                if row is not None and row.workspace_id != self.workspace_id:
                    raise StateConflict("STUDY_WORKSPACE_CONFLICT")
                if row is None:
                    row = StudyUnit(
                        unit_id=uid,
                        workspace_id=self.workspace_id,
                        source_id=task["source_id"],
                        extraction_batch=batch,
                        candidate_id=cid,
                        study_ref=ref,
                        source_task_id=task_id,
                        current_available=True,
                        auto_generation_disabled=False,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                old = session.get(StudyUnitRevision, row.current_snapshot_id) if row.current_snapshot_id else None
                basis_hash = fingerprint(payload)
                if old is None or old.payload.get("basis_hash") != basis_hash:
                    revision = old.revision + 1 if old else 1
                    sid = "study-snapshot-" + fingerprint([uid, revision, basis_hash])[:40]
                    namespace = sid + ":"
                    replacements = {
                        r["id"]: namespace + r["id"]
                        for key in ("facts", "citations", "gaps")
                        for r in payload["projection"][key]
                    }
                    payload = _remap_refs(payload, replacements)
                    payload["basis_hash"] = basis_hash
                    payload["fact_namespace"] = {
                        "candidate_id": cid,
                        "snapshot_id": sid,
                        "original_to_fixed": replacements,
                        "fact_sources": {
                            f["id"]: {
                                "candidate_id": f["candidate_id"],
                                "study_ref": f["study_ref"],
                                "source_pointer": f["source_pointer"],
                            }
                            for f in payload["projection"]["facts"]
                        },
                    }
                    _encode(payload)
                    fixed = StudyUnitRevision(
                        snapshot_id=sid,
                        unit_id=uid,
                        revision=revision,
                        source_task_id=task_id,
                        content_hash=fingerprint(payload),
                        payload=payload,
                        created_at=now,
                    )
                    session.add(fixed)
                    session.flush()
                    row.current_snapshot_id = sid
                    row.updated_at = now
                row.source_task_id = task_id
                row.current_available = True
                result.append(_row(row))
        jobs = getattr(self.service, "study_wiki_jobs", None)
        if jobs is not None:
            jobs.on_snapshots(result)
        return result

    def invalidate_review(self, task_id, candidate_id, pointers):
        """Revoke affected fixed inputs before committing mutable review bytes."""
        task = self.store.get_task(task_id)

        def overlaps(left, right):
            return left == right or left.startswith(right + "/") or right.startswith(left + "/")

        with self.store._write() as session:
            for unit in session.scalars(
                select(StudyUnit).where(
                    StudyUnit.workspace_id == self.workspace_id,
                    StudyUnit.source_id == task["source_id"],
                    StudyUnit.current_available == True,
                )
            ):
                snapshot = session.get(StudyUnitRevision, unit.current_snapshot_id)
                for component in snapshot.payload.get("components", [snapshot.payload]):
                    if component["candidate_id"] != candidate_id:
                        continue
                    owned = [component["study_pointer"]] + [
                        r.get("source_pointer", "")
                        for key in ("facts", "context")
                        for r in component["projection"][key]
                    ]
                    if any(overlaps(p, changed) for p in owned if p for changed in pointers):
                        unit.current_available = False
                        break

    def revision(self, snapshot_id):
        with Session(self.store.engine) as session:
            row = session.get(StudyUnitRevision, snapshot_id)
            owner = session.get(StudyUnit, row.unit_id) if row is not None else None
            if row is None or owner is None or owner.workspace_id != self.workspace_id:
                raise KeyError(snapshot_id)
            result = _row(row)
            if fingerprint(result["payload"]) != result["content_hash"]:
                raise StateConflict("STUDY_SNAPSHOT_INTEGRITY")
            return result

    def get(self, unit_id):
        with Session(self.store.engine) as session:
            row = session.get(StudyUnit, unit_id)
            if row is None or row.workspace_id != self.workspace_id:
                raise KeyError(unit_id)
            result = _row(row)
        source = self.store.source_state(result["source_id"]) or {}
        result["current_available"] = (
            result["current_available"] and source.get("active_task_id") == result["source_task_id"]
        )
        result["status"] = "current_unreviewed" if result["current_available"] else "historical_unreviewed"
        result["snapshot"] = self.revision(result["current_snapshot_id"])
        result["samples"] = deepcopy(result["snapshot"]["payload"]["raw_study"].get("samples", []))
        return result

    def list_source(self, source_id):
        with Session(self.store.engine) as session:
            ids = list(
                session.scalars(
                    select(StudyUnit.unit_id)
                    .where(StudyUnit.source_id == source_id, StudyUnit.workspace_id == self.workspace_id)
                    .order_by(StudyUnit.created_at, StudyUnit.unit_id)
                )
            )
        return [self.get(uid) for uid in ids]

    def reference(self, unit_id, *, fact_id=None):
        row = self.get(unit_id)
        snap = row["snapshot"]
        ref = {
            "kind": "study_unit_snapshot",
            "workspace_id": self.workspace_id,
            "unit_id": unit_id,
            "snapshot_id": snap["snapshot_id"],
            "content_hash": snap["content_hash"],
            "source_id": row["source_id"],
            "fact_id": fact_id,
        }
        self.resolve(ref)
        return ref

    def resolve(self, ref):
        from .knowledge_contracts import StudyUnitSnapshotRef

        ref = StudyUnitSnapshotRef.model_validate(ref).model_dump()
        row = self.get(ref["unit_id"])
        if ref["workspace_id"] != self.workspace_id or ref["source_id"] != row["source_id"]:
            raise StateConflict("STUDY_REFERENCE_SCOPE")
        if not row["current_available"] or row["current_snapshot_id"] != ref["snapshot_id"]:
            raise StateConflict("STUDY_REFERENCE_STALE")
        snap = row["snapshot"]
        if ref["content_hash"] != snap["content_hash"]:
            raise StateConflict("STUDY_REFERENCE_INTEGRITY")
        facts = snap["payload"]["projection"]["facts"]
        if ref["fact_id"] is not None and ref["fact_id"] not in {f["id"] for f in facts}:
            raise StateConflict("STUDY_FACT_SCOPE")
        for source in snap["payload"]["source_refs"]:
            from hashlib import sha256

            if sha256(self.store.asset_bytes(source["asset_id"])).hexdigest() != source["asset_sha256"]:
                raise StateConflict("SOURCE_UNAVAILABLE")
        return row
