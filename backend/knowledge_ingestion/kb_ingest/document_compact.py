"""Compact, source-local transport; the direct contract remains authoritative.

No persisted seventh category is introduced. IDs identify declarations, never
names. Evidence failures on facts are local; ownership failures abort with a
complete rejection trace attached to CompactError.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy

from .contract import FIELDS
from .document_direct_contract import DIRECT_SCHEMA, _nodes, _validate, convert_direct, validate_direct
from .document_source_values import project_source_cell
from .document_value_contract import VALUE_CONTRACT_INSTRUCTIONS
from .ingest.documents.document_quality import resolve_cell_values
from .ingest.documents.extractors.documents import NULLABLE_STRING, STRING, _object_schema
from .llm.prompts import load_prompt


def _compact_schema(schema):
    result = deepcopy(schema)
    if "properties" in result:
        props = result["properties"]
        is_node = "label" in props
        is_fact = "raw_value" in props
        result["properties"] = {
            (
                {
                    "fields": "facts",
                    "input_field_paths": "input_fact_ids",
                    "method_path": "method_id",
                    "study_path": "study_id",
                    "object_path": "object_id",
                    "object_paths": "object_ids",
                }.get(k, k)
            ): (
                {"type": "array", "items": STRING, **({"minItems": 1} if v.get("minItems") else {})}
                if k == "evidence"
                else _compact_schema(v)
            )
            for k, v in props.items()
        }
        if is_node or is_fact:
            result["properties"] = {"id": STRING, **result["properties"]}
        result["required"] = list(result["properties"])
    if "items" in result:
        result["items"] = _compact_schema(result["items"])
    if "anyOf" in result:
        result["anyOf"] = [_compact_schema(s) for s in result["anyOf"]]
    return result


COMPACT_SCHEMA = _compact_schema(DIRECT_SCHEMA)
COMPACT_SCHEMA["properties"]["evidence"] = {
    "type": "array",
    "items": _object_schema(
        {"id": STRING, "block_id": NULLABLE_STRING, "page": {"type": ["integer", "null"]}, "quote": NULLABLE_STRING}
    ),
}
COMPACT_SCHEMA["required"] = list(COMPACT_SCHEMA["properties"])
COMPACT_INSTRUCTIONS = load_prompt("document-compact-v1") + VALUE_CONTRACT_INSTRUCTIONS


class CompactError(ValueError):
    """Invalid structure/ownership; trace retains every discoverable input fact."""

    def __init__(self, message, trace):
        super().__init__(message)
        self.trace = trace


def _literal_quote(text, quote):
    """Only whitespace runs may differ; retain the actual original substring."""
    if not isinstance(text, str) or not text.strip():
        return None
    if quote is None:
        return text
    pattern = r"\s+".join(re.escape(token) for token in quote.split())
    match = re.search(pattern, text)
    return match.group(0) if match else None


def _same_physical_reading(candidates):
    # Source alternatives, never table cells whose anchor is only the enclosing
    # table bbox. Require conservative full text equality and pairwise IoU.
    if len(candidates) != 2 or {b.get("source") for b in candidates} != {"pymupdf", "mineru"}:
        return False
    if any(b.get("kind") in ("table_cell", "visual_page") for b in candidates):
        return False
    a, b = candidates
    if not a.get("asset_ref") or a.get("asset_ref") != b.get("asset_ref"):
        return False
    if " ".join(a["text"].split()) != " ".join(b["text"].split()):
        return False
    boxes = []
    for block in candidates:
        anchor = block.get("anchor", {}).get("coordinate_region", {})
        box = anchor.get("bbox")
        if anchor.get("unit") != "pt" or not isinstance(box, (list, tuple)) or len(box) != 4:
            return False
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in box):
            return False
        if box[2] <= box[0] or box[3] <= box[1]:
            return False
        boxes.append(box)
    a, b = boxes
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union >= 0.9


def _input_facts(payload):
    result = []

    def visit(value):
        if isinstance(value, dict):
            if "field_id" in value and "raw_value" in value:
                result.append(
                    {
                        "fact_id": value.get("id"),
                        "direct_field_path": None,
                        "candidate_field_key": None,
                        "status": "pending",
                        "reason": None,
                    }
                )
            for v in value.values():
                visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)

    visit(payload)
    return result


def expand_compact(payload, blocks):
    """Return a strict direct payload and loss trace; invalid ownership raises CompactError.

    Strict schema validation is identical for offline and model callers. No
    missing keys are silently defaulted. Fact evidence errors produce issues and
    rejected trace entries, while independent facts can still be converted.
    """
    trace = {"facts": _input_facts(payload), "evidence_errors": [], "evidence_resolutions": []}
    try:
        return _expand(payload, blocks, trace), trace
    except ValueError as exc:
        for entry in trace["facts"]:
            entry.update(status="rejected", candidate_field_key=None, reason=str(exc))
        raise CompactError(str(exc), trace) from exc


def _expand(payload, blocks, trace):
    _validate(payload, COMPACT_SCHEMA)
    block_index = {}
    for block in blocks:
        if block["block_id"] in block_index:
            raise ValueError("duplicate source block identity")
        block_index[block["block_id"]] = block
    evidence = {}
    errors = {}
    used_ids = set()

    def declare(id):
        if id in used_ids:
            raise ValueError("duplicate compact declaration id: " + id)
        used_ids.add(id)

    for item in payload["evidence"]:
        id = item["id"]
        declare(id)
        block_id, page, quote = item["block_id"], item["page"], item["quote"]
        candidates = []
        if block_id is not None and page is None:
            b = block_index.get(block_id)
            if b and _literal_quote(b.get("text"), quote) is not None:
                candidates = [b]
        elif block_id is None and type(page) is int and page > 0 and quote:
            candidates = [
                b
                for b in blocks
                if b.get("page") == page
                and isinstance(b.get("text"), str)
                and _literal_quote(b["text"], quote) is not None
                and b.get("kind") != "visual_page"
            ]
        alternatives = []
        if len(candidates) > 1 and _same_physical_reading(candidates):
            alternatives = [b["block_id"] for b in candidates if b["source"] != "pymupdf"]
            candidates = [b for b in candidates if b["source"] == "pymupdf"]
        if len(candidates) != 1:
            errors[id] = "missing, ambiguous or illegal evidence locator: " + id
            trace["evidence_errors"].append({"evidence_id": id, "reason": errors[id]})
        else:
            b = candidates[0]
            evidence[id] = {"block_id": b["block_id"], "quote": _literal_quote(b["text"], quote)}
            trace["evidence_resolutions"].append(
                {"evidence_id": id, "block_id": b["block_id"], "alternative_block_ids": alternatives}
            )

    def resolve(ids):
        for id in ids:
            if id not in evidence:
                raise ValueError(errors.get(id, "unknown evidence reference: " + id))
        return [deepcopy(evidence[id]) for id in ids]

    result = deepcopy(payload)
    result.pop("evidence")
    node_ids = {}
    fact_ids = {}
    fact_inputs = {}

    # Translate vocabulary first so the authoritative hierarchy walk is reusable.
    def translate(value):
        if isinstance(value, list):
            return [translate(v) for v in value]
        if not isinstance(value, dict):
            return value
        return {
            (
                {
                    "facts": "fields",
                    "input_fact_ids": "input_field_paths",
                    "method_id": "method_path",
                    "study_id": "study_path",
                    "object_id": "object_path",
                    "object_ids": "object_paths",
                }.get(k, k)
            ): translate(v)
            for k, v in value.items()
        }

    result = translate(result)
    # Collect original fields before localized pruning, preserving full trace.
    entry_by_id = {}
    for entry in trace["facts"]:
        id = entry["fact_id"]
        declare(id)
        entry_by_id[id] = entry
    for path, kind, node, parent, claimed in _nodes(result):
        id = node.pop("id")
        declare(id)
        node_ids[id] = path
        try:
            node["evidence"] = resolve(node["evidence"])
        except ValueError as exc:
            raise ValueError("unresolved object ownership " + id + ": " + str(exc)) from exc
        for slot in ("fields", "conditions"):
            if slot not in node:
                continue
            retained = []
            for field in node[slot]:
                id = field.pop("id")
                entry = entry_by_id[id]
                # Validate roles even when evidence is broken; malformed contracts
                # cannot hide behind a localized missing evidence rejection.
                definition = FIELDS[field["field_id"]]
                if kind not in definition["owner_kinds"] or field["value_kind"] not in definition["value_kinds"]:
                    raise ValueError("illegal field owner or value kind: " + id)
                try:
                    field["evidence"] = resolve(field["evidence"])
                    cell = field["value_cell"]
                    if cell is not None:
                        b = block_index.get(cell)
                        if (
                            not b
                            or b.get("kind") != "table_cell"
                            or not isinstance(b.get("text"), str)
                            or not b["text"].strip()
                        ):
                            raise ValueError("missing or illegal value_cell: " + str(cell))
                        proposed = field["raw_value"]
                        entry.update(proposed_raw_value=proposed, source_value=b["text"])
                        projection = project_source_cell(
                            b["text"], field["value_kind"], proposed_value=proposed, proposed_unit=field.get("raw_unit")
                        )
                        field["raw_value"] = projection["raw_value"]
                        field["raw_unit"] = projection["raw_unit"]
                        entry["source_projection"] = deepcopy(projection)
                        proof = {"block_id": cell, "quote": b["text"]}
                        if proof not in field["evidence"]:
                            field["evidence"].append(proof)
                except ValueError as exc:
                    entry.update(status="rejected", reason=str(exc))
                    result["issues"].append(
                        {
                            "description": str(exc),
                            "object_paths": [path],
                            "severity": "error",
                            "category": "extraction_uncertain",
                            "evidence": [],
                        }
                    )
                    continue
                fpath = f"{path}/{slot}/{len(retained)}"
                fact_ids[id] = fpath
                entry.update(direct_field_path=fpath, status="expanded")
                fact_inputs[fpath] = field
                retained.append(field)
            node[slot] = retained
    for field in fact_inputs.values():
        field["input_field_paths"] = [fact_ids.get(id, "/unresolved-fact/" + id) for id in field["input_field_paths"]]
    for path, kind, node, parent, claimed in _nodes(result):
        if kind == "reported_test" and node["method_ref"]:
            ref = node["method_ref"]
            ref["method_path"] = node_ids.get(ref["method_path"], "/unresolved-method")
            try:
                ref["evidence"] = resolve(ref["evidence"])
            except ValueError as exc:
                raise ValueError("unresolved method applicability: " + str(exc)) from exc
        if kind == "content_unit":
            for ref in node["inherits_from"]:
                ref["study_path"] = node_ids.get(ref["study_path"], "/unresolved-study")
                try:
                    ref["evidence"] = resolve(ref["evidence"])
                except ValueError as exc:
                    raise ValueError("unresolved inheritance ownership: " + str(exc)) from exc
    original_issue_count = len(payload["issues"])
    for item in result["issues"][:original_issue_count]:
        item["object_paths"] = [node_ids.get(id, "/unresolved-object/" + id) for id in item["object_paths"]]
        item["evidence"] = resolve(item["evidence"])
    for item in result["unreported_fields"]:
        item["object_path"] = node_ids.get(item["object_path"], "/unresolved-object")
        item["evidence"] = resolve(item["evidence"])
    validate_direct(result)
    return result


def convert_compact(payload, blocks):
    """Return document candidate and per-input-fact disposition; never certify truth."""
    direct, trace = expand_compact(payload, blocks)
    candidate = resolve_cell_values(convert_direct(direct), blocks)
    # Expansion-generated locator/value diagnostics are program errors, not
    # immutable qualifications reported by the original source. Preserve their
    # origin so correction can retire them without erasing source conflicts.
    source_issue_index = 0
    for issue in candidate["issues"]:
        if issue.get("origin") == "source_reported":
            if source_issue_index >= len(payload["issues"]):
                issue["origin"] = "direct_conversion"
            source_issue_index += 1
    accepted = {f["key"] for f in candidate["facts"]}
    for entry in trace["facts"]:
        if entry["status"] == "rejected":
            continue
        key = "direct:" + entry["direct_field_path"]
        if key in accepted:
            entry.update(status="converted", candidate_field_key=key)
        else:
            # Converter errors are preserved verbatim in the candidate. Attach
            # owner-scoped reasons rather than claiming a rejected fact survived.
            owner = "direct:" + entry["direct_field_path"].rsplit("/", 2)[0]
            reasons = [i["description"] for i in candidate["issues"] if owner in i["object_keys"]]
            entry.update(status="rejected", reason="; ".join(reasons) or "direct contract withheld unresolved field")
    return candidate, trace
