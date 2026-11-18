"""Restrict direct output to target facts and their explicit source dependencies."""

from __future__ import annotations

from copy import deepcopy

from .core import fingerprint
from .document_direct_quality import _CHILD_DEPENDS_ON_PARENT, _CONTEXT_FIELDS, _REFERENCE_DEPENDENCIES

_SUPPORT_FIELDS = _CONTEXT_FIELDS | {"source.identifier", "source.title", "study.scope"}


def filter_direct_scope(candidate, target_ids):
    """Keep target content plus prerequisite identity/method/condition/input facts.

    Context alone cannot seed a result. Following dependencies backwards does
    not pull siblings or other measurements into the batch. Explicit inheritance
    input fields are an exception: their original values and evidence must stay
    available even when those values came entirely from auxiliary source blocks.
    """
    target_ids = set(target_ids)
    result = deepcopy(candidate)
    objects = {o["key"]: o for o in candidate["objects"]}
    facts = {f["key"]: f for f in candidate["facts"]}

    def target_evidence(item):
        return bool({e.get("block_id") for e in item.get("evidence", [])} & target_ids)

    direct_facts = {key for key, item in facts.items() if target_evidence(item)}
    kept_facts = set(direct_facts)
    kept_objects = {key for key, item in objects.items() if target_evidence(item)}
    kept_missing = [item for item in candidate.get("unreported_fields", []) if target_evidence(item)]
    kept_objects.update(item["object_key"] for item in kept_missing if item["object_key"] in objects)
    dependencies = []
    for relation in candidate["relations"]:
        if relation["predicate"] in _CHILD_DEPENDS_ON_PARENT:
            support, consumer = relation["from_key"], relation["to_key"]
        elif relation["predicate"] in _REFERENCE_DEPENDENCIES:
            support, consumer = relation["to_key"], relation["from_key"]
        else:
            continue
        dependencies.append((support, consumer))
    while True:
        previous = (set(kept_objects), set(kept_facts))
        for key in tuple(kept_facts):
            if facts[key]["object_key"] in objects:
                kept_objects.add(facts[key]["object_key"])
            kept_facts.update(ref for ref in facts[key].get("input_fact_keys", []) if ref in facts)
        for support, consumer in dependencies:
            if consumer in kept_objects and support in objects:
                kept_objects.add(support)
        for key in tuple(kept_objects):
            kept_objects.update(
                objects[key][ref] for ref in ("owner_key", "material_key") if objects[key].get(ref) in objects
            )
        kept_facts.update(
            key
            for key, fact in facts.items()
            if fact["object_key"] in kept_objects and fact["field_id"] in _SUPPORT_FIELDS
        )
        if previous == (kept_objects, kept_facts):
            break
    result["objects"] = [o for o in result["objects"] if o["key"] in kept_objects]
    result["facts"] = [f for f in result["facts"] if f["key"] in kept_facts]
    result["relations"] = [
        r for r in result["relations"] if r["from_key"] in kept_objects and r["to_key"] in kept_objects
    ]
    result["unreported_fields"] = kept_missing
    retained_relations = {fingerprint(r) for r in result["relations"]}
    excluded = [
        *("o:" + key for key in objects if key not in kept_objects),
        *("f:" + key for key in facts if key not in kept_facts),
        *("r:" + str(i) for i, r in enumerate(candidate["relations"]) if fingerprint(r) not in retained_relations),
    ]
    return result, {
        "target_block_ids": sorted(target_ids),
        "excluded_context_targets": excluded,
        "omissions": ["No extracted fact is supported by the target source blocks; context is not target coverage"]
        if facts and not direct_facts
        else [],
    }
