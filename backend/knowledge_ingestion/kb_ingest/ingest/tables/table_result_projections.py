"""Ownership, structural and relationship projections for table results."""

from collections import defaultdict
from copy import deepcopy

from kb_ingest.query import eligible_relations
from kb_ingest.scientific_subjects import ownership_parents

from .table_quality import SAMPLE_TEST_FORWARD, SAMPLE_TEST_REVERSE, inactive


def _ownership_projection(package, objects, blocked):
    """Index only typed, admitted ownership edges within this source revision.

    No generic graph traversal: a sample cannot lend sibling tests to an
    observation, and document/header grouping cannot establish sample identity.
    """
    header = package["header"]
    valid = {
        oid: obj
        for oid, obj in objects.items()
        if oid not in blocked
        and obj.get("source_id") == header["source_id"]
        and obj.get("record_revision_id") == header["record_revision_id"]
    }
    samples = defaultdict(dict)
    observed = defaultdict(dict)
    series_owners = defaultdict(dict)
    for relation in eligible_relations(package):
        left, right = relation["from_ref"], relation["to_ref"]
        if (
            left not in valid
            or right not in valid
            or relation.get("source_revision_id") != header["source_revision_id"]
        ):
            continue
        lk, rk, predicate = valid[left]["kind"], valid[right]["kind"], relation["predicate"]
        if predicate in SAMPLE_TEST_FORWARD and lk == "sample" and rk == "reported_test":
            samples[right][left] = None
        elif predicate in SAMPLE_TEST_REVERSE and lk in ("reported_test", "instrument_run") and rk == "sample":
            samples[left][right] = None
        elif predicate == "produces_observation" and lk == "reported_test" and rk == "observation":
            observed[right][left] = None
        elif (
            predicate == "observes_subject"
            and lk == "observation"
            and rk in ("sample", "reported_test", "instrument_run", "series")
        ):
            observed[left][right] = None
        elif predicate == "has_series" and lk in ("sample", "reported_test", "instrument_run") and rk == "series":
            series_owners[right][left] = None

    def activity_contexts(oid, series=None):
        kind = valid[oid]["kind"]
        if kind == "sample":
            yield (oid, None, None, series)
        else:
            for sample in samples.get(oid) or (None,):
                yield (
                    sample,
                    oid if kind == "reported_test" else None,
                    oid if kind == "instrument_run" else None,
                    series,
                )

    def series_contexts(oid):
        owners = series_owners.get(oid)
        if owners:
            for parent in owners:
                yield from activity_contexts(parent, oid)
        else:
            yield (None, None, None, oid)

    def project(assertion):
        oid = assertion["subject_ref"]
        contexts = {}
        if oid in valid and not inactive(assertion):
            kind = valid[oid]["kind"]
            if kind in ("sample", "reported_test", "instrument_run"):
                contexts.update(dict.fromkeys(activity_contexts(oid)))
            elif kind == "series":
                contexts.update(dict.fromkeys(series_contexts(oid)))
            elif kind == "observation":
                direct_samples = []
                for subject in observed.get(oid, {}):
                    subject_kind = valid[subject]["kind"]
                    if subject_kind == "sample":
                        direct_samples.append(subject)
                    elif subject_kind == "series":
                        contexts.update(dict.fromkeys(series_contexts(subject)))
                    else:
                        contexts.update(dict.fromkeys(activity_contexts(subject)))
                # A direct link corroborating a test's sample is one context;
                # another direct sample remains separate, never cross-joined.
                represented = {context[0] for context in contexts}
                for sample in direct_samples:
                    if sample not in represented:
                        contexts[(sample, None, None, None)] = None
        total = len(contexts)
        bounded = []
        for context in list(contexts)[:8]:
            row = {}
            for key, ref in zip(("sample", "test", "run", "series"), context):
                row[key + "_ref"] = ref
                row[key + "_label"] = valid[ref]["label"] if ref else None
            bounded.append(row)
        return {"contexts": bounded, "context_total": total, "contexts_truncated": total > len(bounded)}

    return project


def _structural_projection(package, subjects, objects, blocked):
    """Inspect stored dependencies without turning them into relation facts.

    Only each page subject's two direct reference fields are displayed. Following
    dependencies is a bounded validity check, never a reverse ingredient lookup
    or a source of additional display edges. A failed dependency invalidates the
    subject's context, including its other otherwise healthy direct reference.
    """
    fields = ("owner_ref", "material_ref")
    header = package["header"]
    checks = {}

    def reasons(ref):
        if ref in checks:
            return checks[ref]
        problems, visiting, visited = set(), set(), set()

        def inspect(current, depth):
            if not isinstance(current, str):
                problems.add("invalid_structural_reference")
                return
            if current in visiting:
                problems.add("structural_cycle")
                return
            if current in visited:
                return
            if depth > 32 or len(visited) >= 256:
                problems.add("structural_check_limit")
                return
            visited.add(current)
            obj = objects.get(current)
            if obj is None:
                problems.add("missing_endpoint")
                return
            if current in blocked:
                problems.add("blocked_object")
            if (
                obj.get("source_id") != header["source_id"]
                or obj.get("record_revision_id") != header["record_revision_id"]
            ):
                problems.add("revision_mismatch")
            visiting.add(current)
            for field in fields:
                target = obj.get(field)
                if not target:
                    if obj["kind"] == "component":
                        problems.add("missing_structural_reference")
                    continue
                endpoint = objects.get(target) if isinstance(target, str) else None
                if obj["kind"] == "component" and endpoint is not None:
                    allowed = ("formulation", "patent_claim") if field == "owner_ref" else ("material", "formulation")
                    if endpoint["kind"] not in allowed:
                        problems.add("invalid_structural_endpoint_kind")
                inspect(target, depth + 1)
            visiting.remove(current)

        inspect(ref, 0)
        checks[ref] = sorted(problems)
        return checks[ref]

    links = []
    for ref in subjects:
        obj = objects.get(ref, {})
        for field in fields:
            if obj.get(field):
                links.append(
                    {
                        "from_ref": ref,
                        "reference_field": field,
                        "to_ref": deepcopy(obj[field]),
                        "reference_basis": "stored_object_reference",
                        "usable": not reasons(ref),
                        "unusable_reasons": reasons(ref)[:],
                    }
                )
    return links[:200], len(links), reasons


def _relationship_view(package, rows, objects, blocked):
    """Bounded explicit edges near this row page, with source identity proofs.

    Direct edges belong to the displayed assertion subjects. Further traversal
    is restricted to declared observation/series -> activity -> sample ownership;
    it never fans out through a sample's other tests or a formula's components.
    Displaying a blocked attempted edge does not admit it to scientific queries.
    """
    subjects = list(dict.fromkeys(r["subject_ref"] for r in rows))
    seeds = set(subjects)
    relations = package["relations"]
    indices = set()
    parents = ownership_parents(package)
    header = package["header"]
    admitted = {r["relation_id"] for r in eligible_relations(package)}
    for index, relation in enumerate(relations):
        left, right, _predicate = relation["from_ref"], relation["to_ref"], relation["predicate"]
        if left in seeds or right in seeds:
            indices.add(index)
    pending, seen = set(seeds), set(seeds)
    while pending:
        following = set()
        for child in pending:
            for parent, index in parents.get(child, []):
                indices.add(index)
                if parent not in seen:
                    seen.add(parent)
                    following.add(parent)
        pending = following
    ordered = sorted(indices)
    selected = [relations[index] for index in ordered[:100]]
    structural_links, structural_total, structural_reasons = _structural_projection(package, subjects, objects, blocked)
    structural_refs = [link["to_ref"] for link in structural_links if isinstance(link["to_ref"], str)]
    all_refs = (
        seeds
        | set(structural_refs)
        | {r[k] for index in ordered for r in (relations[index],) for k in ("from_ref", "to_ref")}
    )
    visible_refs = list(
        dict.fromkeys(subjects + structural_refs + [r[k] for r in selected for k in ("from_ref", "to_ref")])
    )[:300]

    def binding(source):
        item = {
            k: deepcopy(source[k])
            for k in (
                "asset_ref",
                "sheet",
                "row",
                "column",
                "metadata",
                "span",
                "merged_target",
                "raw",
                "kind",
                "operation",
                "selector",
            )
            if k in source
        }
        if isinstance(item.get("raw"), str) and len(item["raw"]) > 512:
            item["raw"] = item["raw"][:512]
            item["raw_truncated"] = True
        if isinstance(source.get("binding"), dict):
            item["binding"] = binding(source["binding"])
        return item

    def binding_list(target, name, sources):
        target[name] = [binding(s) for s in sources[:8]]
        stem = name[:-1] if name.endswith("s") else name
        target[stem + "_total"] = len(sources)
        target[name + "_truncated"] = len(sources) > 8

    evidence_ids = {e["evidence_id"] for e in package["evidence"]}

    def evidence(item, source):
        item["source_evidence_available"] = any(ref in evidence_ids for ref in source.get("evidence_refs", []))
        for name in ("evidence_refs", "issue_refs"):
            values = source.get(name, [])
            item[name] = deepcopy(values[:20])
            item[name + "_total"] = len(values)
            item[name + "_truncated"] = len(values) > 20

    def object_reasons(obj):
        return structural_reasons(obj["object_id"])

    asset_ids = {asset["asset_id"] for asset in package["assets"]}

    def has_identity_source(source, depth=0):
        if (
            not isinstance(source, dict)
            or depth > 4
            or source.get("not_source_fact")
            or source.get("kind") == "declared_semantic_mapping"
        ):
            return False
        if isinstance(source.get("binding"), dict):
            return has_identity_source(source["binding"], depth + 1)
        if source.get("asset_ref") not in asset_ids:
            return False
        return (
            type(source.get("row")) is int
            and source["row"] > 0
            and type(source.get("column")) is int
            and source["column"] > 0
        ) or (source.get("metadata") == "sheet_name" and isinstance(source.get("sheet"), str))

    projected_objects = []
    for ref in visible_refs:
        obj = objects.get(ref)
        if obj is None:
            continue
        context = obj.get("source_context", {})
        occurrences = context.get("occurrences", [])
        reasons = object_reasons(obj)
        item = {
            k: deepcopy(obj[k])
            for k in ("object_id", "kind", "label", "owner_ref", "material_ref", "use_status", "lifecycle_status")
            if k in obj
        }
        if len(str(item.get("label", ""))) > 512:
            item["label"] = str(item["label"])[:512]
            item["label_truncated"] = True
        item.update(
            identity_scope=context.get("identity_scope"),
            identity_status=context.get("identity_status"),
            identity_occurrences=[],
            identity_occurrence_total=len(occurrences),
            identity_occurrences_truncated=len(occurrences) > 20,
            usable=not reasons,
            unusable_reasons=reasons,
            identity_source_available=any(
                has_identity_source(source) for occurrence in occurrences for source in occurrence.get("bindings", [])
            ),
        )
        for occurrence in occurrences[:20]:
            entry = {"record_scope": deepcopy(occurrence.get("record_scope"))}
            binding_list(entry, "bindings", occurrence.get("bindings", []))
            item["identity_occurrences"].append(entry)
        evidence(item, obj)
        projected_objects.append(item)

    projected_relations = []
    for relation in selected:
        left, right = objects.get(relation["from_ref"]), objects.get(relation["to_ref"])
        reasons = []
        if relation["relation_id"] not in admitted:
            reasons.append("relation_not_admitted")
        if relation.get("source_revision_id") != header["source_revision_id"]:
            reasons.append("revision_mismatch")
        if left is None or right is None:
            reasons.append("missing_endpoint")
        elif object_reasons(left) or object_reasons(right):
            reasons.append("endpoint_unusable")
        item = {
            k: deepcopy(relation[k])
            for k in (
                "relation_id",
                "from_ref",
                "predicate",
                "to_ref",
                "basis_kind",
                "applicability",
                "use_status",
                "lifecycle_status",
                "source_revision_id",
            )
            if k in relation
        }
        for name, obj in (("from", left), ("to", right)):
            item[name + "_label"] = str(obj["label"])[:512] if obj else None
            item[name + "_kind"] = obj["kind"] if obj else None
            item[name + "_label_truncated"] = bool(obj and len(str(obj["label"])) > 512)
        proof = relation.get("proof", {})
        item["proof"] = {
            k: deepcopy(proof[k])
            for k in ("kind", "record_scope", "left_record", "right_record", "join_index", "cardinality")
            if k in proof
        }
        for name in ("left_bindings", "right_bindings"):
            if name in proof:
                binding_list(item["proof"], name, proof[name])
        item.update(usable=not reasons, unusable_reasons=reasons)
        evidence(item, relation)
        projected_relations.append(item)
    for link in structural_links:
        source = objects[link["from_ref"]]
        for name in ("from", "to"):
            ref = link[name + "_ref"]
            obj = objects.get(ref) if isinstance(ref, str) else None
            link[name + "_label"] = str(obj["label"])[:512] if obj else None
            link[name + "_kind"] = obj["kind"] if obj else None
            link[name + "_label_truncated"] = bool(obj and len(str(obj["label"])) > 512)
        evidence(link, source)
    return {
        "version": "1",
        "selection": "page_subjects_and_explicit_ownership",
        "subject_refs": subjects,
        "objects": projected_objects,
        "object_total": len(all_refs),
        "objects_truncated": len(projected_objects) < len(all_refs),
        "relations": projected_relations,
        "relation_total": len(ordered),
        "relations_truncated": len(selected) < len(ordered),
        "structural_links": structural_links,
        "structural_link_total": structural_total,
        "structural_links_truncated": len(structural_links) < structural_total,
        "limits": {
            "relations": 100,
            "structural_links": 200,
            "objects": 300,
            "identity_occurrences": 20,
            "bindings_per_occurrence": 8,
            "evidence_refs": 20,
            "structural_check_depth": 32,
            "structural_check_objects": 256,
        },
    }
