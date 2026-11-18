"""Read only the fixed candidate fact or cited source entry authorized by a Wiki support."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .core import StateConflict
from .study_units import StudyUnitService
from .wiki_query_index import text_parts

_ACTIVE: ContextVar["StudySupportContext | None"] = ContextVar("study_support_context", default=None)


def _registered_citation(units, unit, citation):
    from .document_source_binding import resolve_evidence_anchor

    result = deepcopy(citation)
    binding = resolve_evidence_anchor(
        units.service,
        task_id=unit["source_task_id"],
        candidate_id=citation.get("candidate_id") or unit["candidate_id"],
        evidence=citation.get("evidence") or {},
    )
    if binding["status"] == "resolved":
        result["resolved_registered_location"] = {
            k: binding["resolved_anchor"].get(k) for k in ("asset_ref", "page", "block_id")
        }
        result["resolved_registered_location"]["resolution_basis"] = binding["resolution_basis"]
    return result


class StudySupportContext:
    """仅在单次操作内共享、不跨操作或线程。"""

    _wiki: object
    _owner: "StudySupportContext | None"
    units: StudyUnitService

    def __init__(self, wiki):
        active = _ACTIVE.get()
        self._wiki = wiki
        if active is not None and active._wiki is wiki:
            self._owner = active
            self.units = active.units
            return
        self._owner = None
        self.units = StudyUnitService(wiki.ingestion_service, workspace_id=wiki.workspace_id)
        self._units = {}
        self._facts = {}
        self._citations = {}
        self._registered = {}

    def unit(self, ref):
        if self._owner is not None:
            return self._owner.unit(ref)
        from .knowledge_contracts import StudyUnitSnapshotRef

        ref = StudyUnitSnapshotRef.model_validate(ref).model_dump()
        uid = ref["unit_id"]
        if uid not in self._units:
            # Full current snapshot/source integrity once; field scope is checked
            # below for every support, even when its base pin was already read.
            unit = self.units.resolve({**ref, "fact_id": None})
            self._units[uid] = unit
            projection = unit["snapshot"]["payload"]["projection"]
            self._facts[uid] = {f["id"]: f for f in projection["facts"]}
            self._citations[uid] = {c["id"]: c for c in projection["citations"]}
        unit = self._units[uid]
        if ref["workspace_id"] != self.units.workspace_id or ref["source_id"] != unit["source_id"]:
            raise StateConflict("STUDY_REFERENCE_SCOPE")
        if ref["snapshot_id"] != unit["current_snapshot_id"]:
            raise StateConflict("STUDY_REFERENCE_STALE")
        if ref["content_hash"] != unit["snapshot"]["content_hash"]:
            raise StateConflict("STUDY_REFERENCE_INTEGRITY")
        if ref["fact_id"] is not None and ref["fact_id"] not in self._facts[uid]:
            raise StateConflict("STUDY_FACT_SCOPE")
        return unit

    def fact(self, unit, fact_id):
        if self._owner is not None:
            return self._owner.fact(unit, fact_id)
        return self._facts[unit["unit_id"]][fact_id]

    def citations(self, unit, ids):
        if self._owner is not None:
            return self._owner.citations(unit, ids)
        selected = set(ids)
        # Preserve immutable projection order for persisted evidence_index handles.
        return [
            self.registered(unit, citation)
            for cid, citation in self._citations[unit["unit_id"]].items()
            if cid in selected
        ]

    def registered(self, unit, citation):
        if self._owner is not None:
            return self._owner.registered(unit, citation)
        key = (unit["unit_id"], citation["id"])
        if key not in self._registered:
            self._registered[key] = _registered_citation(self.units, unit, citation)
        return deepcopy(self._registered[key])


@contextmanager
def shared_support_context(wiki: object) -> Iterator[StudySupportContext]:
    context = StudySupportContext(wiki)
    token = _ACTIVE.set(context)
    try:
        yield context
    finally:
        _ACTIVE.reset(token)


def resolve_support(wiki, support, manifest=None, *, context=None):
    context = context or StudySupportContext(wiki)
    ref = support["ref"]
    unit = context.unit(ref)
    projection = unit["snapshot"]["payload"]["projection"]
    scope = support.get("scope") or {}
    if ref.get("fact_id"):
        fact = context.fact(unit, ref["fact_id"])
        citations = context.citations(unit, fact.get("evidence_refs", []))
        return {
            "kind": "study_unit_fact",
            "ref": deepcopy(ref),
            "fact": deepcopy(fact),
            "citations": deepcopy(citations),
            "use_status": "unreviewed_candidate",
            "scientific_validation": False,
            "human_expert_review": False,
        }
    entry_kind = scope.get("snapshot_entry_kind")
    entry_id = scope.get("snapshot_entry_id")
    if entry_kind in {"citation", "gap"}:
        entries = projection["citations" if entry_kind == "citation" else "gaps"]
        selected = next((e for e in entries if e["id"] == entry_id), None)
        if selected is None:
            raise StateConflict("STUDY_ENTRY_SCOPE")
        return {
            "kind": "study_unit_" + entry_kind,
            "ref": deepcopy(ref),
            "entry": context.registered(unit, selected) if entry_kind == "citation" else deepcopy(selected),
            "use_status": "unreviewed_candidate",
            "scientific_validation": False,
        }
    if entry_kind == "source_block":
        binding = (manifest or {}).get("study_unit", {})
        blocks = binding.get("generation_manifest", {}).get("source_blocks", [])
        block = scope.get("source_block")
        if block not in blocks or block.get("block_id") != entry_id:
            raise StateConflict("STUDY_ENTRY_SCOPE")
        return {
            "kind": "study_unit_source_block",
            "ref": deepcopy(ref),
            "entry": deepcopy(block),
            "use_status": "unreviewed_candidate",
            "scientific_validation": False,
        }
    if entry_kind is not None:
        raise StateConflict("STUDY_ENTRY_SCOPE")
    return {
        "kind": "study_unit_overview",
        "ref": deepcopy(ref),
        "study_label": projection.get("study_label"),
        "context": deepcopy(projection["context"]),
        "use_status": "unreviewed_candidate",
        "scientific_validation": False,
    }


def support_navigation(wiki, support, *, context=None):
    """Fixed identity labels only; a support remains unread numeric evidence."""
    if support["ref"].get("kind") != "study_unit_snapshot":
        return None
    context = context or StudySupportContext(wiki)
    unit = context.unit(support["ref"])
    projection = unit["snapshot"]["payload"]["projection"]
    fid = support["ref"].get("fact_id")
    if fid:
        fact = context.fact(unit, fid)
        pointer = fact.get("source_pointer", "")
        owner = fact.get("owner_pointer", "")
        ancestors = [
            entry
            for entry in projection["context"]
            if entry.get("candidate_id") == fact.get("candidate_id")
            and entry.get("study_ref") == fact.get("study_ref")
            and (pointer.startswith(entry.get("source_pointer", "") + "/") or entry.get("source_pointer") == owner)
        ]
        sample = next(
            (e for e in ancestors if "/samples/" in e["source_pointer"] and "/tests/" not in e["source_pointer"]), {}
        )
        owned = next((e for e in ancestors if e["source_pointer"] == owner), {})
        label = fact.get("object_label") or sample.get("label")
        owner_label = fact.get("owner_label") or owned.get("label")
        nav = {"field_name": fact.get("name"), "object_label": label, "source_pointer": pointer}
        if owner_label and owner_label != label:
            nav["owner_label"] = owner_label
        return {k: v for k, v in nav.items() if isinstance(v, str) and v}
    scope = support.get("scope") or {}
    kind = scope.get("snapshot_entry_kind")
    if kind in {"citation", "gap"}:
        rows = projection["citations" if kind == "citation" else "gaps"]
        entry = next((e for e in rows if e["id"] == scope.get("snapshot_entry_id")), None)
        if entry is None:
            raise StateConflict("STUDY_ENTRY_SCOPE")
        return {k: v for k, v in {"entry_kind": kind, "source_pointer": entry.get("source_pointer")}.items() if v}
    if kind == "source_block":
        return {"entry_kind": "source_block", "source_pointer": scope.get("snapshot_entry_id")}
    return {"entry_kind": "study_unit_overview", "object_label": projection.get("study_label")}


def read_support(wiki, support, selector, manifest):
    payload = resolve_support(wiki, support, manifest)
    if selector.evidence_index is not None:
        citations = payload.get("citations", [])
        if selector.evidence_index >= len(citations):
            raise ValueError("invalid_request")
        payload = {
            "kind": "study_unit_citation",
            "ref": payload["ref"],
            "entry": citations[selector.evidence_index],
            "use_status": "unreviewed_candidate",
            "scientific_validation": False,
        }
    elif payload["kind"] == "study_unit_fact":
        payload["citations"] = [
            {
                "id": c["id"],
                "evidence_ref": c.get("evidence_ref"),
                "location": deepcopy(c.get("location")),
                "resolved_registered_location": deepcopy(c.get("resolved_registered_location")),
            }
            for c in payload["citations"]
        ]
    entry = payload.get("entry", {})
    quote = (
        (entry.get("evidence") or {}).get("quote") if payload["kind"] == "study_unit_citation" else entry.get("text")
    )
    if quote is not None:
        parts = text_parts(quote)
        if selector.part_index >= len(parts):
            raise ValueError("invalid_request")
        if payload["kind"] == "study_unit_citation":
            entry["evidence"]["quote"] = parts[selector.part_index]
        else:
            entry["text"] = parts[selector.part_index]
        payload["quote_coverage"] = {
            "part_index": selector.part_index,
            "part_count": len(parts),
            "complete": len(parts) == 1,
        }
    elif selector.part_index:
        raise ValueError("invalid_request")
    return payload, "record_fact" if payload[
        "kind"
    ] == "study_unit_fact" else "document_evidence" if "citation" in payload["kind"] or "source_block" in payload[
        "kind"
    ] else "study_unit_context"
