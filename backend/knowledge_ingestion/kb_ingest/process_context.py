"""Finite, evidence-bound process applicability; no inferred execution order."""

from collections import defaultdict


def process_index(package, *, objects=None, blocked=None, relations=None):
    """Build once per query/projection, then visit only an owner's edges."""
    from .ingest.tables.table_quality import blocked_objects
    from .query import eligible_relations

    objects = objects if objects is not None else {o["object_id"]: o for o in package["objects"]}
    blocked = blocked if blocked is not None else blocked_objects(package)
    relations = relations if relations is not None else eligible_relations(package)
    valid = {ref: o for ref, o in objects.items() if ref not in blocked}
    evidence = {e["evidence_id"] for e in package["evidence"]}
    edges = [
        r
        for r in relations
        if r["predicate"] in {"has_formulation", "recipe_for", "process_applicability", "defines_step"}
        and r["from_ref"] in valid
        and r["to_ref"] in valid
        and r.get("evidence_refs")
        and set(r["evidence_refs"]) <= evidence
    ]
    outgoing = defaultdict(list)
    recipes = defaultdict(list)
    for edge in edges:
        outgoing[edge["from_ref"]].append(edge)
        if edge["predicate"] == "recipe_for":
            recipes[edge["to_ref"]].append(edge)
    return valid, outgoing, recipes


def process_paths(package, sample_ref, *, objects=None, blocked=None, relations=None, index=None):
    valid, outgoing, recipes = (
        index if index is not None else process_index(package, objects=objects, blocked=blocked, relations=relations)
    )
    if valid.get(sample_ref, {}).get("kind") != "sample":
        return {}
    owners = [(sample_ref, None, [])]
    for edge in [*outgoing.get(sample_ref, ()), *recipes.get(sample_ref, ())]:
        form = None
        if edge["predicate"] == "has_formulation" and edge["from_ref"] == sample_ref:
            form = edge["to_ref"]
        elif edge["predicate"] == "recipe_for" and edge["to_ref"] == sample_ref:
            form = edge["from_ref"]
        if form and valid[form]["kind"] == "formulation":
            owners.append((form, form, [edge]))
    paths = defaultdict(list)
    for owner, form, prefix in owners:
        for edge in outgoing.get(owner, ()):
            process = edge["to_ref"]
            if (
                edge["predicate"] != "process_applicability"
                or edge["from_ref"] != owner
                or valid[process]["kind"] != "process_definition"
            ):
                continue
            path = dict(
                sample_ref=sample_ref,
                formulation_ref=form,
                process_ref=process,
                step_ref=None,
                relations=prefix + [edge],
            )
            paths[process].append(path)
            for step_edge in outgoing.get(process, ()):
                step = step_edge["to_ref"]
                if (
                    step_edge["predicate"] == "defines_step"
                    and step_edge["from_ref"] == process
                    and valid[step]["kind"] == "step_definition"
                ):
                    paths[step].append({**path, "step_ref": step, "relations": path["relations"] + [step_edge]})
    return {
        subject: sorted(values, key=lambda p: tuple(r["relation_id"] for r in p["relations"]))
        for subject, values in paths.items()
    }


def process_candidates(candidates, subjects):
    """Reaching a process never admits unrelated observation/claim fields."""
    return [a for a in candidates if a["subject_ref"] not in subjects or a["field_id"].startswith("process.")]
