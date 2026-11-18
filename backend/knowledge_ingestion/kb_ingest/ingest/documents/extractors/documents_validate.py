"""Validate a model candidate against supplied source blocks."""

from __future__ import annotations

import re

from kb_ingest.contract import FIELDS, RELATIONS


def _comparison_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def validate_candidate(
    candidate: dict, blocks: list[dict], existing_objects: list[dict] | None = None
) -> tuple[dict, list[str]]:
    registry = {b["block_id"]: b for b in blocks}
    accepted = {"objects": [], "facts": [], "relations": [], "issues": []}
    problems = []

    def supported(item: dict) -> bool:
        evidence = item.get("evidence") or []
        if not evidence:
            problems.append("missing evidence")
            return False
        for evidence_item in evidence:
            block = registry.get(evidence_item.get("block_id"))
            if block is None:
                problems.append("unknown evidence block")
                return False
            quote = evidence_item.get("quote", "")
            if not quote or (
                block.get("kind") != "visual_page" and _comparison_text(quote) not in _comparison_text(block["text"])
            ):
                problems.append("quote not supported by supplied source block")
                return False
        return True

    existing = {o["key"]: o for o in existing_objects or []}
    keys = set(existing)
    for item in candidate.get("objects", []):
        if item.get("key") in keys or not item.get("key") or not item.get("scope"):
            problems.append("duplicate or missing scoped object identity")
            continue
        if supported(item):
            accepted["objects"].append(item)
            keys.add(item["key"])
    object_index = {**existing, **{o["key"]: o for o in accepted["objects"]}}
    for obj in list(accepted["objects"]):
        if obj["kind"] == "component" and (
            object_index.get(obj.get("owner_key"), {}).get("kind") not in {"formulation", "patent_claim"}
            or object_index.get(obj.get("material_key"), {}).get("kind") != "material"
        ):
            accepted["objects"].remove(obj)
            keys.remove(obj["key"])
            problems.append("component owner/material identity or kind not supported")
    for item in candidate.get("facts", []):
        if item.get("object_key") not in keys:
            problems.append("fact subject not supported")
            continue
        obj = object_index[item["object_key"]]
        field = FIELDS.get(item.get("field_id"))
        if not field or obj["kind"] not in field["owner_kinds"]:
            problems.append("fact field or owner is not registered")
        elif (
            obj["kind"] == "patent_claim" or object_index.get(obj.get("owner_key"), {}).get("kind") == "patent_claim"
        ) and item.get("statement_kind") == "source_reported_measurement":
            problems.append("patent scope cannot be an observed measurement")
        elif item.get("raw_value") is None:
            problems.append("missing value retained as gap, not assertion")
        elif supported(item):
            accepted["facts"].append(item)
    for item in candidate.get("relations", []):
        if item.get("from_key") not in keys or item.get("to_key") not in keys:
            problems.append("relation endpoint not supported")
        else:
            rule = RELATIONS.get(item.get("predicate"))
            left = object_index[item["from_key"]]
            right = object_index[item["to_key"]]
            if (
                not rule
                or left["kind"] not in rule["allowed_from_kinds"]
                or right["kind"] not in rule["allowed_to_kinds"]
            ):
                problems.append("relation kind is not registered for these endpoints")
            elif supported(item):
                accepted["relations"].append(item)
    for item in candidate.get("issues", []):
        if not item.get("evidence") or supported(item):
            accepted["issues"].append(item)
    return accepted, problems
