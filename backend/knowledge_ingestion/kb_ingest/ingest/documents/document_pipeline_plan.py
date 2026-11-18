"""Plan, review and correction helpers for document v2 orchestration."""

from __future__ import annotations

from copy import deepcopy

from kb_ingest.contract import FIELDS
from kb_ingest.core import fingerprint

from .document_pipeline_schemas import CORRECTION_SCHEMA, EXTRACT_SCHEMA, PLAN_SCHEMA, REVIEW_SCHEMA
from .document_quality_verify import requires_image_review, review_targets


def scoped_extraction_schema(unit, *, correction=False):
    schema = deepcopy(CORRECTION_SCHEMA if correction else EXTRACT_SCHEMA)
    if unit.get("kind") == "source_metadata":
        schema["properties"]["facts"]["items"]["properties"]["field_id"]["enum"] = sorted(
            f for f in FIELDS if f.startswith("source.")
        )
    return schema


def source_scope_payload(primary, context):
    ids = {b["block_id"] for b in primary}
    return {
        "target_block_ids": [b["block_id"] for b in primary],
        "context_only_block_ids": [b["block_id"] for b in context if b["block_id"] not in ids],
    }


def apply_correction(candidate, correction, rejected_targets):
    """Apply named replacements, never silently drop unmentioned valid records."""
    result = deepcopy(candidate)
    withdrawals = correction.get("withdrawals", [])
    targets = review_targets(candidate)
    remove = {x["target"] for x in withdrawals}
    if len(remove) != len(withdrawals) or not remove <= set(rejected_targets) or not remove <= set(targets):
        raise ValueError("only explicitly rejected targets may be withdrawn")
    if any(not x.get("reason", "").strip() for x in withdrawals):
        raise ValueError("withdrawal requires reason")
    for section, prefix in (("objects", "o:"), ("facts", "f:")):
        rows = correction[section]
        keys = [x["key"] for x in rows]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate correction identity")
        index = {x["key"]: x for x in result[section] if prefix + x["key"] not in remove}
        index.update({x["key"]: deepcopy(x) for x in rows})
        result[section] = list(index.values())
    old = [x for i, x in enumerate(result["relations"]) if "r:" + str(i) not in remove]
    # Endpoints plus predicate identify a relation; changed endpoints require
    # an explicit withdrawal of the old edge and independent verification.
    key = lambda x: (x["from_key"], x["predicate"], x["to_key"])
    rows = correction["relations"]
    keys = [key(x) for x in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate correction relationship")
    index = {key(x): x for x in old}
    index.update({key(x): deepcopy(x) for x in rows})
    result["relations"] = list(index.values())
    result["issues"] += deepcopy([i for i in correction["issues"] if i not in result["issues"]])
    return result, deepcopy(withdrawals)


def plan_schema(block_ids):
    schema = deepcopy(PLAN_SCHEMA)
    # Reference one enum definition instead of repeating hundreds of IDs in
    # three schema branches. Large documents still undergo registry validation.
    ids = (
        {"type": "string", "enum": list(block_ids)}
        if 0 < len(block_ids) <= 750
        else {"type": "string", "pattern": "^b[1-9][0-9]*$"}
    )
    schema["$defs"] = {"SourceRegionId": ids}
    for key in ("block_ids", "context_block_ids"):
        schema["properties"]["units"]["items"]["properties"][key] = {
            "type": "array",
            "items": {"$ref": "#/$defs/SourceRegionId"},
        }
    schema["properties"]["excluded"]["items"]["properties"]["block_id"] = {"$ref": "#/$defs/SourceRegionId"}
    return schema


def review_schema(target_keys):
    schema = deepcopy(REVIEW_SCHEMA)
    checks = schema["properties"]["checks"]
    checks["items"]["properties"]["target"] = {"type": "string", "enum": list(target_keys)}
    checks["minItems"] = checks["maxItems"] = len(target_keys)
    return schema


def review_packets(targets, blocks):
    registry = {b["block_id"]: b for b in blocks}
    packets = []
    current = []
    images = set()
    for key, item in targets.items():
        required = {
            fingerprint([b["asset_ref"], b["page"]])
            for e in item.get("evidence", [])
            if (b := registry.get(e.get("block_id"))) and requires_image_review(b)
        }
        if current and (len(current) >= 50 or len(images | required) > 4):
            packets.append(current)
            current = []
            images = set()
        current.append(key)
        images |= required
    if current:
        packets.append(current)
    return packets


def validate_plan(plan, blocks):
    ids = {b["block_id"] for b in blocks}
    assigned = set()
    keys = [u["key"] for u in plan["units"]]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate unit key")
    index = {u["key"]: u for u in plan["units"]}
    for u in plan["units"]:
        chosen = set(u["block_ids"] + u.get("context_block_ids", []))
        assigned |= chosen
        if not chosen <= ids:
            raise ValueError("unknown source region: " + ",".join(sorted(chosen - ids)))
        if not set(u.get("depends_on", [])) <= set(keys):
            raise ValueError("unknown unit dependency")
    visiting = set()
    done = set()

    def visit(key):
        if key in visiting:
            raise ValueError("cyclic document dependency")
        if key in done:
            return
        visiting.add(key)
        for child in index[key].get("depends_on", []):
            visit(child)
        visiting.remove(key)
        done.add(key)

    for key in keys:
        visit(key)
    for e in plan.get("excluded", []):
        if e.get("block_id") not in ids or not e.get("reason"):
            raise ValueError("exclusion lacks source or reason")
    excluded = {e["block_id"] for e in plan.get("excluded", [])}
    # A contradictory scope annotation must not discard readable independent
    # content. Keep the region in extraction and retain an unresolved scope flag.
    return {
        "unresolved": sorted(ids - assigned - excluded),
        "assigned": sorted(assigned),
        "scope_conflicts": sorted(assigned & excluded),
        "excluded": plan.get("excluded", []),
    }
