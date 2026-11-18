"""Query v2 records at one proven subject/activity/series grain.

Only the shared ownership graph lends context. Sibling traces and activities
never lend assertions to each other. Shared definitions remain definitions.
"""

from collections import defaultdict

from .core import fingerprint
from .ingest.tables.table_quality import _semantic_qualifiers
from .scientific_subjects import ACTIVITY_KINDS, ownership_parents, subject_kinds


def query_contexts(package, filters, objects, blocked, relations, match, diagnostics, context_collector=None):
    subjects = subject_kinds(package)
    valid = {ref: o for ref, o in objects.items() if ref not in blocked}
    admitted = [(i, r) for i, r in enumerate(relations) if r["from_ref"] in valid and r["to_ref"] in valid]
    parents = ownership_parents(package, admitted)
    memo = {}

    def ancestors(ref):
        if ref not in memo:
            memo[ref] = {ref}
            for parent, _ in parents.get(ref, []):
                memo[ref].update(ancestors(parent))
        return memo[ref]

    def rank(ref):
        kind = valid[ref]["kind"]
        return 2 if kind == "series" else 1 if kind in ACTIVITY_KINDS else 0 if kind in subjects else -1

    observed = defaultdict(set)
    for ref, obj in valid.items():
        if obj["kind"] != "observation":
            continue
        owners = ancestors(ref) - {ref}
        candidates = [o for o in owners if rank(o) >= 0]
        if candidates:
            closest = [o for o in candidates if rank(o) == max(map(rank, candidates))]
            if len(closest) == 1:
                observed[closest[0]].add(ref)
    outgoing = defaultdict(set)
    incoming = defaultdict(set)
    for _, r in admitted:
        outgoing[(r["from_ref"], r["predicate"])].add(r["to_ref"])
        incoming[(r["to_ref"], r["predicate"])].add(r["from_ref"])
    components = defaultdict(set)
    for ref, obj in valid.items():
        if obj["kind"] == "component":
            components[obj.get("owner_ref")].add(ref)

    def composition(ref):
        forms = (
            {ref}
            if valid[ref]["kind"] == "formulation"
            else outgoing[(ref, "has_formulation")] | incoming[(ref, "recipe_for")]
        )
        result = set(forms)
        for form in forms:
            result.update(outgoing[(form, "contains_component")] | components[form])
        return result & valid.keys()

    by_subject = defaultdict(list)
    for order, a in enumerate(package["assertions"]):
        by_subject[a["subject_ref"]].append((order, a))
    results = []
    from .process_context import process_candidates, process_index, process_paths

    process_lookup = process_index(package, objects=objects, blocked=blocked, relations=relations)
    for root, obj in valid.items():
        if rank(root) < 0:
            continue
        context = set(ancestors(root))
        intrinsic = {root} | observed[root]
        context.update(observed[root])
        subject_refs = [r for r in context if valid[r]["kind"] in subjects]
        activities = [r for r in context if valid[r]["kind"] in ACTIVITY_KINDS]
        if len(subject_refs) > 1 or len(activities) > 1:
            continue
        processes = set()
        for ref in subject_refs:
            owned = composition(ref)
            context.update(owned)
            if ref == root:
                intrinsic.update(owned)
            linked = set(process_paths(package, ref, index=process_lookup))
            processes.update(linked)
            context.update(linked)
            if ref == root:
                intrinsic.update(linked)
        methods = set()
        for activity in activities:
            methods.update(outgoing[(activity, "uses_method")])
        context.update(methods)
        if obj["kind"] in ACTIVITY_KINDS:
            intrinsic.update(methods)
        candidates = [
            a for _, a in sorted((item for ref in context for item in by_subject[ref]), key=lambda item: item[0])
        ]
        candidates = process_candidates(candidates, processes)
        if activities:
            # Subject-level results do not acquire an activity merely because a
            # query also asks for that activity's conditions.
            candidates = [
                a
                for a in candidates
                if not (a["subject_ref"] in subject_refs and a["field_id"].startswith("observation."))
            ]

            def condition_key(a):
                return a["field_id"], fingerprint(_semantic_qualifiers(a))

            local = {
                condition_key(a)
                for a in candidates
                if a["subject_ref"] in activities and a["field_id"].startswith("condition.")
            }
            candidates = [a for a in candidates if not (a["subject_ref"] in methods and condition_key(a) in local)]
        record = subject_refs[0] if subject_refs else activities[0] if activities else root
        sample = record if valid[record]["kind"] == "sample" else None
        activity = activities[0] if activities else None
        if context_collector is not None:
            context_collector.append(
                dict(
                    record_ref=record,
                    sample_ref=sample,
                    test_ref=activity if activity and valid[activity]["kind"] == "reported_test" else None,
                    run_ref=activity if activity and valid[activity]["kind"] == "instrument_run" else None,
                    series_ref=root if obj["kind"] == "series" else None,
                    candidates=candidates,
                )
            )
        matched = match(candidates, filters, diagnostics)
        if matched is None or not any(a["subject_ref"] in intrinsic for a in matched.values()):
            continue
        record = subject_refs[0] if subject_refs else activities[0] if activities else root
        sample = record if valid[record]["kind"] == "sample" else None
        activity = activities[0] if activities else None
        results.append(
            {
                "source_id": package["header"]["source_id"],
                "record_revision_id": package["header"]["record_revision_id"],
                "record_kind": valid[record]["kind"],
                "record_ref": record,
                "record_label": valid[record]["label"],
                "sample_ref": sample,
                "sample_label": valid[sample]["label"] if sample else None,
                "test_ref": activity if activity and valid[activity]["kind"] == "reported_test" else None,
                "run_ref": activity if activity and valid[activity]["kind"] == "instrument_run" else None,
                "series_ref": root if obj["kind"] == "series" else None,
                "assertion_refs": sorted(matched),
                "evidence_refs": sorted({e for a in matched.values() for e in a["evidence_refs"]}),
                "use_status": "not_evaluated",
            }
        )
    return results
