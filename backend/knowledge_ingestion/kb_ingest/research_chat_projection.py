"""Presentation-only read projections; authoritative references stay in the registry."""

from copy import deepcopy

from .core.serialization import canonical
from .research_chat_contracts import ResearchError


class ReadProjectionLimit(ResearchError):
    def __init__(self):
        super().__init__("invalid_request")
        self.next_read = None

    def public(self):
        result = {
            "code": "read_projection_limit",
            "message": (
                "本批内容超出模型读取限额，整批未登记为已读。请缩小页或读取断言叶项；这不表示资料不存在。"
                "单个叶项仍超限时明确报告读取限制。"
            ),
        }
        if self.next_read is not None:
            result["next_read"] = self.next_read
        return result


def bounded(view, max_bytes=12000):
    if len(canonical(view).encode()) > max_bytes:
        raise ReadProjectionLimit()
    return view


def identity(ref):
    return {
        k: deepcopy(ref[k])
        for k in (
            "kind",
            "source_id",
            "sample_ref",
            "test_ref",
            "assertion_id",
            "evidence_id",
            "physical_page",
            "segment_id",
            "wiki_id",
            "revision_id",
        )
        if k in ref
    }


def value_view(value):
    value = deepcopy(value)
    evidence = value.pop("evidence_refs", [])
    value["evidence_count"] = len(evidence)
    for path in value.get("context_paths", []):
        for relation in path["relations"]:
            relation["evidence_count"] = len(relation.pop("evidence_refs", []))
    # Typed nulls carry no additional value. Raw null, unit and basis remain explicit.
    return {k: v for k, v in value.items() if v is not None or k in ("raw_value", "unit", "basis")}


def item_view(entry, operations, *, comparison_text=None):
    kind = entry["kind"]
    view = {k: deepcopy(entry[k]) for k in ("id", "kind", "layer")}
    view["operations"] = operations
    if kind == "unified_support":
        return {**view, "ref_kind": entry["ref"]["kind"], "navigation_only": True}
    if kind.startswith("wiki_"):
        binding = entry["wiki_binding"]
        view.update({k: deepcopy(v) for k, v in binding.items() if k in ("page_id", "revision_id")})
        view.update(
            {
                k: deepcopy(entry[k])
                for k in (
                    "title",
                    "heading",
                    "section_id",
                    "claim_id",
                    "part_index",
                    "part_count",
                    "body",
                    "claim_type",
                    "snippet",
                    "relation_depth",
                    "status",
                    "evidence_type",
                    "payload",
                    "conditions",
                    "uncertainties",
                    "context_status",
                )
                if k in entry
            }
        )
        if kind == "wiki_navigation":
            view["hit_id"] = entry["id"]
        if kind == "wiki_support":
            view.update(
                handle=entry["id"],
                ref_kind=entry["support"]["ref"]["kind"],
                support_type=entry["support"]["support_type"],
            )
        return view
    if kind == "comparison":
        return {**view, "summary": comparison_text}
    if kind in ("user_target", "task_result"):
        return {**view, **{k: deepcopy(entry[k]) for k in ("text", "task_kind", "dependency_ids") if k in entry}}
    if kind in ("query_receipt", "comparison_receipt"):
        return {**view, **{k: deepcopy(entry[k]) for k in ("comparison_ids",) if k in entry}}
    ref = entry.get("context_ref") or entry.get("ref")
    if ref:
        view["identity"] = identity(ref)
    for key in (
        "label",
        "status",
        "storage",
        "integrity",
        "title",
        "body",
        "body_clipped",
        "subject_ref",
        "subject_kind",
        "field_id",
        "qualifiers",
        "requires_authoritative_read",
        "snippet",
        "snippet_truncated",
        "indexed_text_truncated",
        "source_label",
        "content_kind",
        "source_content_kind",
        "scientific_validation",
        "score",
        "readiness",
        "availability",
        "attribution",
        "quote",
    ):
        if key in entry:
            view[key] = deepcopy(entry[key])
    if kind == "navigation":
        if view.get("subject_ref") in view.get("identity", {}).values():
            view.pop("subject_ref", None)
        for key in ("label", "field_id", "qualifiers"):
            if view.get(key) is None or view.get(key) == {}:
                view.pop(key, None)
    evidence_owner = entry.get("value", {}) if kind == "fact" else entry
    if "evidence_refs" in evidence_owner:
        view["evidence_indices"] = list(range(min(20, len(evidence_owner["evidence_refs"]))))
        view["evidence_refs_complete"] = len(evidence_owner["evidence_refs"]) <= 20 and entry.get(
            "evidence_refs_complete", True
        )
    if kind == "fact":
        view["value"] = value_view(entry["value"])
        context = deepcopy(entry.get("test_context"))
        if context:
            context["conditions"] = [value_view(v) for v in context["conditions"]]
        view["test_context"] = context
    if "provenance" in entry:
        provenance = entry.get("provenance")
        view["provenance"] = (
            None
            if provenance is None
            else {
                k: deepcopy(provenance[k])
                for k in (
                    "reader",
                    "block_id",
                    "ir_region",
                    "verification_state",
                    "requires_image_verification",
                    "quote_relation",
                )
                if k in provenance
            }
        )
    if kind == "guide":
        view["dependency_count"] = len(entry.get("dependency_refs", []))
    if kind == "count":
        view["count"] = entry["count"]
        view["query"] = {k: deepcopy(entry["query"][k]) for k in ("filters", "include_history") if k in entry["query"]}
        if entry["query"].get("selection"):
            view["query"]["selection"] = [identity(r) for r in entry["query"]["selection"]]
        view["completeness"] = {
            k: v for k, v in entry["completeness"].items() if k not in ("result_set_id", "next_cursor")
        }
    return view


def shared_contexts(view):
    """Expose each complete test condition set once, with explicit local references."""
    view = deepcopy(view)
    keys = {}
    contexts = {}
    status_keys = {}
    statuses = {}
    for item in view.get("items", []):
        if "status" in item:
            status = item.pop("status")
            raw = canonical(status)
            key = status_keys.setdefault(raw, "status_" + str(len(status_keys) + 1))
            statuses[key] = status
            item["status_ref"] = key
        context = item.get("test_context")
        if not context:
            continue
        raw = canonical(context)
        key = keys.setdefault(raw, "test_context_" + str(len(keys) + 1))
        contexts[key] = context
        item["test_context"] = {"conditions_ref": key}
    if contexts:
        view["test_contexts"] = contexts
    if statuses:
        view["statuses"] = statuses
    return view
