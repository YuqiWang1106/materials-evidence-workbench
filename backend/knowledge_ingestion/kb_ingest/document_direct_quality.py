"""Dependency closure for direct nested extraction; legacy gates stay unchanged."""

from __future__ import annotations

from copy import deepcopy

from .core import fingerprint

# Direct nesting gives these edges an ownership direction. In particular a
# formulation/process is owned by its sample, not the reverse. Shared method
# and material references instead invalidate only their consuming objects.
_CHILD_DEPENDS_ON_PARENT = {
    "contains_study",
    "contains_claim",
    "describes_test",
    "contains_sample",
    "has_formulation",
    "process_applicability",
    "tested_by",
    "produces_observation",
    "contains_component",
}
_REFERENCE_DEPENDENCIES = {
    "uses_method",
    "refers_to_material",
    "observes_subject",
    "about_sample",
    "about_study",
    "inherits_from",
}
_CONTEXT_FIELDS = {
    "material.identity",
    "condition.parameter",
    "method.name",
    "method.standard",
    "method.instrument",
    "observation.statement_kind",
    "activity.identifier",
}


def filter_direct_dependencies(original, accepted, report):
    """Withhold dependents of rejected ownership, identity, and input facts.

    Inspect the original edges: the common verifier has already discarded the
    failed relations. Looking only at accepted relations would lose the reason
    a nested fact no longer has a supported subject. Inputs and audit artifacts
    are immutable; rejected targets keep their original candidate indices.
    """
    result, report = deepcopy(accepted), deepcopy(report)
    rejected = {item["target"] for item in report.get("rejected", [])}
    objects = {item["key"]: item for item in original["objects"]}
    facts = {item["key"]: item for item in original["facts"]}
    alive_objects = {o["key"] for o in accepted["objects"] if o["key"] in objects and "o:" + o["key"] not in rejected}
    alive_facts = {f["key"] for f in accepted["facts"] if f["key"] in facts and "f:" + f["key"] not in rejected}
    accepted_relations = {fingerprint(r) for r in accepted["relations"]}
    dependencies = []
    failed_relations = set()
    for index, relation in enumerate(original["relations"]):
        signature = fingerprint(relation)
        failed = signature not in accepted_relations or "r:" + str(index) in rejected
        if failed:
            failed_relations.add(signature)
        if relation["predicate"] in _CHILD_DEPENDS_ON_PARENT:
            support, consumer = relation["from_key"], relation["to_key"]
        elif relation["predicate"] in _REFERENCE_DEPENDENCIES:
            support, consumer = relation["to_key"], relation["from_key"]
        else:
            continue
        dependencies.append((support, consumer))
        if failed:
            alive_objects.discard(consumer)

    while True:
        previous = (set(alive_objects), set(alive_facts))
        for support, consumer in dependencies:
            if support not in alive_objects:
                alive_objects.discard(consumer)
        for key in tuple(alive_objects):
            if any(
                objects[key].get(ref) and objects[key][ref] not in alive_objects
                for ref in ("owner_key", "material_key")
            ):
                alive_objects.discard(key)
        alive_facts = {key for key in alive_facts if facts[key]["object_key"] in alive_objects}
        # Start with independent facts, so an input cycle cannot certify itself.
        resolved = {key for key in alive_facts if not facts[key].get("input_fact_keys")}
        while True:
            additions = {
                key for key in alive_facts - resolved if set(facts[key].get("input_fact_keys", [])) <= resolved
            }
            if not additions:
                break
            resolved.update(additions)
        alive_facts = resolved
        for key, fact in facts.items():
            if key not in alive_facts and fact["field_id"] in _CONTEXT_FIELDS:
                alive_objects.discard(fact["object_key"])
        if previous == (alive_objects, alive_facts):
            break

    result["objects"] = [o for o in result["objects"] if o["key"] in alive_objects]
    result["facts"] = [f for f in result["facts"] if f["key"] in alive_facts]
    result["relations"] = [
        r
        for r in result["relations"]
        if r["from_key"] in alive_objects and r["to_key"] in alive_objects and fingerprint(r) not in failed_relations
    ]
    retained_relations = {fingerprint(r) for r in result["relations"]}
    removed = [
        *("o:" + key for key in objects if key not in alive_objects),
        *("f:" + key for key in facts if key not in alive_facts),
        *("r:" + str(i) for i, r in enumerate(original["relations"]) if fingerprint(r) not in retained_relations),
    ]
    for target in removed:
        if target not in rejected:
            report.setdefault("rejected", []).append(
                {"target": target, "reason": "direct ownership, identity or input dependency is unverified"}
            )
            rejected.add(target)
    return result, report
