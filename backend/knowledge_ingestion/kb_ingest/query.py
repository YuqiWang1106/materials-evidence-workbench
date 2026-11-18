"""Conservative business filtering inside one sample and one reported test.

No vector similarity or source-level co-occurrence can establish a scientific join.
"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .contract import FIELDS, strict_equal


def _legacy_matches(assertion: dict, query: dict) -> bool:
    from .ingest.tables.table_quality import NON_VALUES, inactive

    if assertion.get("source_context", {}).get("value_status") in NON_VALUES:
        return False
    if assertion.get("field_id") != query.get("field_id"):
        return False
    if any(assertion.get("qualifiers", {}).get(k) != v for k, v in query.get("qualifiers", {}).items()):
        return False
    value = assertion["value"]
    if "unit" in query and value.get("raw_unit") != query["unit"]:
        return False
    if "basis" in query and value.get("raw_basis") != query["basis"]:
        return False
    if inactive(assertion):
        return False
    raw, expected, op = value.get("raw_value"), query.get("value"), query.get("op", "eq")
    if op == "exists":
        return raw not in (None, "", []) and (not isinstance(raw, list) or any(v not in (None, "") for v in raw))
    if op == "eq":
        parsed = value.get("parsed")
        if type(expected) in (int, float) and type(parsed) in (int, float):
            try:
                left, right = Decimal(str(parsed)), Decimal(str(expected))
                return left.is_finite() and right.is_finite() and left == right
            except InvalidOperation:
                return False
        return strict_equal(raw, expected)
    if op == "contains":
        return isinstance(raw, str) and isinstance(expected, str) and expected.casefold() in raw.casefold()
    if op not in ("gte", "gt", "lte", "lt"):
        raise ValueError("Unsupported filter operator")
    if set(FIELDS.get(assertion["field_id"], {}).get("value_kinds", [])) == {"text"}:
        return False
    if raw is None or isinstance(raw, bool) or isinstance(expected, bool):
        return False
    try:
        left, right = Decimal(str(raw)), Decimal(str(expected))
        if not left.is_finite() or not right.is_finite():
            return False
    except (InvalidOperation, ValueError):
        return False
    return {"gte": left >= right, "gt": left > right, "lte": left <= right, "lt": left < right}[op]


def _legacy_explicit_match(assertion: dict, query: dict) -> tuple[bool, str | None]:
    """Apply a requested type without changing legacy facts or default queries."""
    from .ingest.tables.table_quality import NON_VALUES, inactive
    from .typed_values import QUERY_VALUE_TYPES, compare_typed_value, legacy_query_value

    if assertion.get("field_id") != query.get("field_id"):
        return False, None
    if any(assertion.get("qualifiers", {}).get(k) != v for k, v in query.get("qualifiers", {}).items()):
        return False, None
    requested = query.get("value_type")
    if not isinstance(requested, str) or requested not in QUERY_VALUE_TYPES:
        return False, "invalid_query_value_type"
    if inactive(assertion):
        return False, "inactive_assertion"
    status = assertion.get("source_context", {}).get("value_status")
    if status in NON_VALUES:
        return False, "source_status:" + status
    value = assertion.get("value", {})
    for key in ("unit", "basis"):
        if key in query and value.get("raw_" + key) != query[key]:
            return False, key + "_mismatch"
    if requested in {"date", "time", "datetime", "duration"}:
        return False, "legacy_type_evidence_missing"
    if requested == "number" and set(FIELDS.get(assertion["field_id"], {}).get("value_kinds", [])) == {"text"}:
        return False, "value_type_mismatch"
    typed = legacy_query_value(value)
    if typed is None:
        return False, "legacy_type_evidence_missing"
    return compare_typed_value(typed, query)


def _match_result(assertion: dict, query: dict) -> tuple[bool, str | None]:
    """Typed metadata is opt-in; historical records retain their exact policy."""
    from .ingest.tables.table_quality import inactive
    from .typed_values import compare_typed_value, normalize_unit

    value = assertion.get("value", {})
    if "typed_value_version" not in value:
        if "value_type" in query:
            return _legacy_explicit_match(assertion, query)
        return _legacy_matches(assertion, query), None
    if assertion.get("field_id") != query.get("field_id"):
        return False, None
    if any(assertion.get("qualifiers", {}).get(k) != v for k, v in query.get("qualifiers", {}).items()):
        return False, None
    if inactive(assertion):
        return False, "inactive_assertion"
    source_status = assertion.get("source_context", {}).get("value_status")
    if source_status in {"ambiguous_units", "unreadable", "extraction_failed"}:
        return False, "source_status:" + source_status
    if "unit" in query and value.get("raw_unit") != query["unit"]:
        original, requested = normalize_unit(value.get("raw_unit")), normalize_unit(query["unit"])
        if (
            original["status"] != "recognized"
            or requested["status"] != "recognized"
            or original["canonical"] != requested["canonical"]
        ):
            return False, "unit_mismatch"
    if "basis" in query and value.get("raw_basis") != query["basis"]:
        return False, "basis_mismatch"
    if value.get("typed_value_version") != "2":
        return False, "unsupported_typed_value_version"
    if "typed_values" in value and "typed_value" not in value:
        if query.get("op") != "exists":
            return False, "series_requires_point_selection"
        points = value["typed_values"]
        if not isinstance(points, list):
            return False, "invalid_typed_value"
        if "value_type" in query:
            reasons = set()
            for point in points:
                matched, reason = compare_typed_value(point, query)
                if matched:
                    return True, None
                reasons.add(reason)
            return False, next(iter(reasons)) if len(reasons) == 1 else "no_values_of_requested_type"
        matched = any(compare_typed_value(point, query)[0] for point in points)
        return matched, None if matched else "no_present_series_values"
    return compare_typed_value(value.get("typed_value"), query)


def _matches(assertion: dict, query: dict) -> bool:
    return _match_result(assertion, query)[0]


def _matched_assertions(candidates: list[dict], filters: list[dict], diagnostics=None) -> dict | None:
    """Bind compatible constraints on one field to the same source assertion."""
    selected = []
    for index, query in enumerate(filters):
        matches = []
        for assertion in candidates:
            matched, reason = _match_result(assertion, query)
            if matched:
                matches.append(assertion)
            elif diagnostics is not None and reason:
                entry = {"assertion_id": assertion["assertion_id"], "filter_index": index, "reason": reason}
                diagnostics[(assertion["assertion_id"], index, reason)] = entry
        selected.append(matches)
    if not all(selected):
        return None

    def compatible(left, right):
        if left.get("field_id") != right.get("field_id"):
            return False
        lq, rq = left.get("qualifiers", {}), right.get("qualifiers", {})
        return all(lq[k] == rq[k] for k in lq.keys() & rq.keys())

    matched = {}
    remaining = set(range(len(filters)))
    while remaining:
        component = {remaining.pop()}
        while True:
            neighbors = {j for j in remaining if any(compatible(filters[i], filters[j]) for i in component)}
            if not neighbors:
                break
            component |= neighbors
            remaining -= neighbors
        # A broad selector connected to two contradictory metric IDs is
        # ambiguous; do not silently use different assertions to satisfy it.
        qualifiers = {}
        for i in component:
            for key, value in filters[i].get("qualifiers", {}).items():
                if key in qualifiers and qualifiers[key] != value:
                    return None
                qualifiers[key] = value
        common = set(a["assertion_id"] for a in selected[next(iter(component))])
        for i in component:
            common &= {a["assertion_id"] for a in selected[i]}
        if not common:
            return None
        matched.update({a["assertion_id"]: a for a in candidates if a["assertion_id"] in common})
    return matched


def eligible_relations(package: dict) -> list[dict]:
    """Scientific joins admitted by the existing query policy, in source order."""
    from .ingest.tables.table_quality import inactive

    return [
        r
        for r in package["relations"]
        if not inactive(r)
        and r.get("applicability") not in ("unknown", "not_applicable", "superseded")
        and r.get("basis_kind")
        in ("source_explicit", "structural_expansion", "explicit_inheritance", "explicit_override")
        and r.get("applicability") not in ("requires_review", "candidate", "unresolved")
    ]


def _query_package(package: dict, filters: list[dict], diagnostics=None, context_collector=None) -> list[dict]:
    if not filters:
        return []
    objs = {o["object_id"]: o for o in package["objects"]}
    from .ingest.tables.table_quality import (
        SAMPLE_TEST_FORWARD,
        SAMPLE_TEST_REVERSE,
        blocked_objects,
        inactive,
        verify_table_bindings,
    )

    if package["header"].get("table_quality_version"):
        verify_table_bindings(package)
    blocked = blocked_objects(package)
    relations = eligible_relations(package)
    if package["header"].get("table_quality_version") == "tabular-quality-2":
        from .scientific_query import query_contexts

        return query_contexts(
            package, filters, objs, blocked, relations, _matched_assertions, diagnostics, context_collector
        )
    # A source header grouping establishes a test-to-curve context, but never
    # establishes physical sample identity or joins separate header groups.
    header_series = [
        r
        for r in package["relations"]
        if r.get("predicate") == "has_series"
        and r.get("basis_kind") == "source_header_group"
        and r.get("applicability") == "as_reported"
        and not inactive(r)
        and objs.get(r.get("from_ref"), {}).get("kind") == "reported_test"
        and objs.get(r.get("to_ref"), {}).get("kind") == "series"
    ]
    outgoing, incoming = defaultdict(list), defaultdict(list)
    for order, relation in enumerate(relations):
        outgoing[(relation["from_ref"], relation["predicate"])].append((order, relation["to_ref"]))
        incoming[(relation["to_ref"], relation["predicate"])].append((order, relation["from_ref"]))
    assertions_by_subject = defaultdict(list)
    for order, assertion in enumerate(package["assertions"]):
        assertions_by_subject[assertion["subject_ref"]].append((order, assertion))
    components_by_owner = defaultdict(set)
    for oid, obj in objs.items():
        if obj["kind"] == "component":
            components_by_owner[obj.get("owner_ref")].add(oid)
    header_series_by_test = defaultdict(set)
    for relation in header_series:
        header_series_by_test[relation["from_ref"]].add(relation["to_ref"])

    def connected(index, node, predicates):
        # Preserve the prior relation insertion order when building sets so the
        # ordering of result contexts stays identical to the pre-indexed query.
        entries = [entry for predicate in predicates for entry in index.get((node, predicate), ())]
        return {target for _, target in sorted(entries, key=lambda entry: entry[0])}

    def targets(node, predicates):
        return connected(outgoing, node, predicates)

    def origins(node, predicates):
        return connected(incoming, node, predicates)

    def context_assertions(context):
        selected = [entry for subject in context for entry in assertions_by_subject.get(subject, ())]
        return [assertion for _, assertion in sorted(selected, key=lambda entry: entry[0])]

    def append_match(context, *, kind, record_ref, sample_ref=None, test_ref=None, run_ref=None, series_ref=None):
        if record_ref in blocked or context & blocked:
            return
        candidates = context_assertions(context)
        if context_collector is not None:
            context_collector.append(
                dict(
                    record_ref=record_ref,
                    sample_ref=sample_ref,
                    test_ref=test_ref,
                    run_ref=run_ref,
                    series_ref=series_ref,
                    candidates=candidates,
                )
            )
        matched = _matched_assertions(candidates, filters, diagnostics)
        if matched is None:
            return
        results.append(
            {
                "source_id": package["header"]["source_id"],
                "record_revision_id": package["header"]["record_revision_id"],
                "record_kind": kind,
                "record_ref": record_ref,
                "sample_ref": sample_ref,
                "sample_label": objs[sample_ref]["label"] if sample_ref else None,
                "test_ref": test_ref,
                "run_ref": run_ref,
                "series_ref": series_ref,
                "assertion_refs": sorted(matched),
                "evidence_refs": sorted({e for a in matched.values() for e in a["evidence_refs"]}),
                "use_status": "not_evaluated",
            }
        )

    results = []
    from .process_context import process_candidates, process_index, process_paths

    process_lookup = process_index(package, objects=objs, blocked=blocked, relations=relations)
    subject_kinds = {"sample"}
    for sample, obj in objs.items():
        if obj["kind"] not in subject_kinds or sample in blocked:
            continue
        formulations = (
            {sample}
            if obj["kind"] == "formulation"
            else targets(sample, {"has_formulation"}) | origins(sample, {"recipe_for"})
        )
        # Inner formulations are retained as distinct levels; do not mix their dosages.
        composition = set(formulations)
        for formulation in formulations:
            composition |= targets(formulation, {"contains_component"})
            composition |= components_by_owner.get(formulation, set())
        tests = targets(sample, SAMPLE_TEST_FORWARD) | origins(sample, SAMPLE_TEST_REVERSE)
        processes = set(process_paths(package, sample, index=process_lookup))
        contexts = list(tests) + [None]
        for test in contexts:
            if test in blocked:
                continue
            context = {sample} | composition | processes
            direct_observations = origins(sample, {"observes_subject"})
            # A result may point to the sample AND to a particular test. A sample
            # link never licenses joining it with a different run's conditions.
            context |= {
                o
                for o in direct_observations
                if (
                    test is None
                    and not origins(o, {"produces_observation"})
                    and not targets(o, {"observes_subject"}) & tests
                )
                or (
                    test is not None
                    and (test in origins(o, {"produces_observation"}) or test in targets(o, {"observes_subject"}))
                )
            }
            if test:
                context.add(test)
                context |= targets(test, {"uses_method", "produces_observation", "has_series"})
                context |= header_series_by_test.get(test, set())
                context |= origins(test, {"observes_subject"})
            else:
                context |= targets(sample, {"has_series"})
            candidates = process_candidates(context_assertions(context - blocked), processes)
            if test:
                # Older records may place a result directly on the sample. They
                # carry no run identity and cannot borrow any selected test's context.
                candidates = [
                    a
                    for a in candidates
                    if not (a["subject_ref"] == sample and a["field_id"].startswith("observation."))
                ]

                def semantic_key(a):
                    q = a.get("qualifiers", {})
                    return (a["field_id"], q.get("parameter_id", q.get("metric_id", a.get("profile_id"))))

                local_conditions = {
                    semantic_key(a)
                    for a in candidates
                    if a["subject_ref"] == test and a["field_id"].startswith("condition.")
                }
                methods = targets(test, {"uses_method"})
                candidates = [
                    a for a in candidates if not (a["subject_ref"] in methods and semantic_key(a) in local_conditions)
                ]
            if context_collector is not None:
                context_collector.append(
                    dict(
                        record_ref=sample,
                        sample_ref=sample,
                        test_ref=test,
                        run_ref=None,
                        series_ref=None,
                        candidates=candidates,
                    )
                )
            matched = _matched_assertions(candidates, filters, diagnostics)
            if matched is not None:
                if test and all(a["subject_ref"] in {sample} | composition | processes for a in matched.values()):
                    continue
                results.append(
                    {
                        "source_id": package["header"]["source_id"],
                        "record_revision_id": package["header"]["record_revision_id"],
                        "record_kind": obj["kind"],
                        "record_ref": sample,
                        "sample_ref": sample if obj["kind"] == "sample" else None,
                        "sample_label": obj["label"] if obj["kind"] == "sample" else None,
                        "test_ref": test,
                        "run_ref": None,
                        "series_ref": None,
                        "assertion_refs": sorted(matched),
                        "evidence_refs": sorted({e for a in matched.values() for e in a["evidence_refs"]}),
                        "use_status": "not_evaluated",
                    }
                )
    # Source tables often report a run or test without identifying a physical
    # sample. Such records remain queryable only inside their own proven scope.
    attached_tests = {
        r["to_ref"]
        for r in relations
        if r["predicate"] in SAMPLE_TEST_FORWARD and objs.get(r["from_ref"], {}).get("kind") in subject_kinds
    }
    attached_tests |= {
        r["from_ref"]
        for r in relations
        if r["predicate"] in SAMPLE_TEST_REVERSE and objs.get(r["to_ref"], {}).get("kind") in subject_kinds
    }
    attached_series = {r["to_ref"] for r in relations + header_series if r["predicate"] == "has_series"}
    attached_series |= {
        r["from_ref"]
        for r in relations
        if r["predicate"] == "observes_subject" and objs.get(r["from_ref"], {}).get("kind") == "series"
    }
    for oid, obj in objs.items():
        if obj["kind"] == "reported_test" and oid not in attached_tests:
            context = {oid} | targets(oid, {"produces_observation", "has_series"})
            context |= header_series_by_test.get(oid, set())
            append_match(context, kind="reported_test", record_ref=oid, test_ref=oid)
        elif obj["kind"] == "instrument_run":
            context = {oid} | origins(oid, {"observes_subject"}) | targets(oid, {"has_series"})
            append_match(context, kind="instrument_run", record_ref=oid, run_ref=oid)
        elif obj["kind"] == "series" and oid not in attached_series:
            append_match({oid}, kind="series", record_ref=oid, series_ref=oid)
    return results


def query_package(package: dict, filters: list[dict]) -> list[dict]:
    """Compatibility API: results only; typed v2 values opt in per assertion."""
    return _query_package(package, filters)


def query_package_detailed(package: dict, filters: list[dict]) -> dict:
    """Results plus v2 comparison exclusions for considered assertion/filter pairs.

    Exclusions do not assert absence of a scientific fact. Structural relation
    and blocked-object policies remain those of query_package.
    """
    diagnostics = {}
    results = _query_package(package, filters, diagnostics)
    return {"results": results, "exclusions": list(diagnostics.values())}


def query_context_candidates(package):
    """Expose exactly the candidate grains used by query, without new joins."""
    contexts = []
    _query_package(package, [{"field_id": "observation.value", "op": "exists"}], context_collector=contexts)
    return contexts
