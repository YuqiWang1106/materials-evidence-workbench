"""Versioned subject roles shared by guards, queries and source projections.

Material, formulation and a source-defined record are not physical samples.
They nevertheless have an explicit identity to which an activity can belong.
No relationship is inferred by these roles; an admitted source edge is needed.
"""


def subject_kinds(package):
    if package.get("header", {}).get("table_quality_version") == "tabular-quality-2":
        return frozenset({"sample", "material", "formulation", "content_unit"})
    return frozenset({"sample"})


ACTIVITY_KINDS = frozenset({"reported_test", "instrument_run"})


def ownership_parents(package, relations=None):
    """Return child -> (parent, source relation index), using scientific roles.

    Definitions (methods, ingredients) are intentionally not exclusive owners.
    Typed edges have at most three upward levels, not arbitrary graph traversal.
    """
    from collections import defaultdict

    objects = {o["object_id"]: o for o in package["objects"]}
    subjects = subject_kinds(package)
    parents = defaultdict(list)
    for index, r in enumerate(package["relations"]) if relations is None else relations:
        left, right = r["from_ref"], r["to_ref"]
        predicate = r["predicate"]
        lk, rk = objects.get(left, {}).get("kind"), objects.get(right, {}).get("kind")
        if (
            (predicate == "tested_by" and lk in subjects and rk in ACTIVITY_KINDS)
            or (predicate == "has_series" and lk in subjects | ACTIVITY_KINDS and rk == "series")
            or (predicate == "produces_observation" and lk in ACTIVITY_KINDS and rk == "observation")
        ):
            parents[right].append((left, index))
        elif (predicate in ("tests_subject", "characterizes") and lk in ACTIVITY_KINDS and rk in subjects) or (
            predicate == "observes_subject"
            and lk in ("observation", "series")
            and rk in subjects | ACTIVITY_KINDS | ({"series"} if lk == "observation" else set())
        ):
            parents[left].append((right, index))
    return parents


def enforce_exclusive_ownership(package, active_relations):
    """One measurement context; repeated proofs of the same owner are harmless."""
    from .contract import add_issue

    objects = {o["object_id"]: o for o in package["objects"]}
    subjects = subject_kinds(package)
    parents = ownership_parents(package, active_relations)
    memo = {}

    def context(ref):
        if ref in memo:
            return memo[ref]
        kind = objects[ref]["kind"]
        result = [
            {ref} if kind in subjects else set(),
            {ref} if kind in ACTIVITY_KINDS else set(),
            {ref} if kind == "series" else set(),
        ]
        for parent, _ in parents.get(ref, []):
            for axis, ids in enumerate(context(parent)):
                # Two distinct identities already prove a conflict. Bound the
                # diagnostic footprint even for a malicious many-parent plan.
                result[axis] = set(sorted(result[axis] | ids)[:2])
        memo[ref] = result
        return result

    for ref, obj in objects.items():
        if obj["kind"] not in ACTIVITY_KINDS | {"series", "observation"}:
            continue
        identities = context(ref)
        if not any(len(ids) > 1 for ids in identities):
            continue
        # The legacy sample/test message is retained by the shared direct guard.
        if obj["kind"] in ACTIVITY_KINDS and len(identities[0]) > 1:
            continue
        issue = add_issue(
            package,
            "One measurement context has incompatible subject, activity or series identities",
            [ref],
            severity="error",
            code="ambiguous_" + obj["kind"] + "_owner",
        )
        obj["use_status"] = "blocked"
        if issue not in obj.setdefault("issue_refs", []):
            obj["issue_refs"].append(issue)
    for _, r in active_relations:
        if r["predicate"] == "contains_component":
            component, role, other = r["to_ref"], "owner_ref", r["from_ref"]
        elif r["predicate"] == "refers_to_material":
            component, role, other = r["from_ref"], "material_ref", r["to_ref"]
        else:
            continue
        obj = objects[component]
        if obj["kind"] != "component" or obj.get(role) == other:
            continue
        issue = add_issue(
            package,
            "Component reference disagrees with its explicit relationship",
            [component],
            r.get("evidence_refs", []),
            severity="error",
            code="conflicting_component_" + role,
        )
        obj["use_status"] = "blocked"
        if issue not in obj.setdefault("issue_refs", []):
            obj["issue_refs"].append(issue)
