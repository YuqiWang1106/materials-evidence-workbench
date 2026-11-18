"""Pure, bounded research task computation; no model, runtime, storage, or scope widening.

Inputs are authoritative read results supplied by the host. Revalidating their
freshness and persisting output are the chat integration's responsibility.
"""

from .core.serialization import canonical, fingerprint
from .knowledge_contracts import AssertionRef, KnowledgeResult, QueryRequest, RecordResult
from .research_observations import matches_selector, observation_reason, scalar, semantic_inputs
from .research_tasks_contracts import (
    POLICY_VERSION,
    CandidateEvaluation,
    ConstraintCheck,
    Coverage,
    PerformanceReference,
    PlanParameter,
    ResearchPlan,
    TargetFragment,
    TargetSelection,
    TaskResult,
    TaskScope,
    UserMessage,
    UserTarget,
    parse_fragment,
)
from .typed_values import compare_typed_value

OWNER = (
    "workspace_id",
    "corpus_snapshot_id",
    "task_id",
    "source_id",
    "source_revision_id",
    "record_revision_id",
    "record_snapshot_id",
    "package_sha256",
)


def bind_user_target(message, selections):
    message = UserMessage.model_validate(message)
    fragments = []
    for raw in selections:
        part = TargetSelection.model_validate(raw)
        fragments.append(TargetFragment(**part.model_dump(), text=message.text[part.start : part.end]))
    data = {
        "origin": "user_target",
        "message": message.model_dump(mode="json"),
        "message_sha256": message.text_sha256,
        "fragments": [f.model_dump(mode="json") for f in fragments],
        "constraints": [parse_fragment(f).model_dump(mode="json") for f in fragments],
        "policy_version": POLICY_VERSION,
        "scientific_validation": False,
    }
    return UserTarget(id="target-" + fingerprint(data), **data)


def _rows(candidates, scope):
    scope = TaskScope.model_validate(scope)
    rows = []
    seen = set()
    if len(candidates) > 100:
        raise ValueError("Too many candidate contexts")
    for raw in candidates:
        row = RecordResult.model_validate(raw.model_dump() if isinstance(raw, RecordResult) else raw)
        if row.kind != "sample_test_context" or any(
            getattr(row.ref, k) != getattr(scope, k) for k in ("workspace_id", "corpus_snapshot_id")
        ):
            raise ValueError("Candidate outside exact sample/test scope")
        if (
            row.integrity != "verified"
            or row.storage not in {"local_verified", "archive_verified"}
            or not row.status.currently_eligible
        ):
            raise ValueError("Unverified candidate")
        key = fingerprint(row.ref.model_dump())
        if key in seen:
            raise ValueError("Duplicate candidate context")
        seen.add(key)
        rows.append(row)
    if len(canonical([r.model_dump(mode="json") for r in rows]).encode()) > 2 * 1024 * 1024:
        raise ValueError("Candidate input too large")
    return scope, rows


def _scope_result(result, scope):
    if (
        result.workspace_id != scope.workspace_id
        or result.versions.corpus_snapshot_id != scope.corpus_snapshot_id
        or result.scope.collection_id != scope.collection_id
        or result.scope.use_policy != scope.use_policy
    ):
        raise ValueError("Receipt scope differs")


def _coverage(rows, scope, pages):
    if not pages:
        return Coverage(
            mode="checked_candidates", exhaustive=False, checked=len(rows), reason="no_complete_query_chain"
        )
    if len(pages) > 20:
        raise ValueError("Too many query pages")
    next_cursor = None
    initial = None
    resultset = None
    total = None
    seen = set()
    hashes = []
    complete = True
    for raw in pages:
        if set(raw) != {"request", "response"}:
            raise ValueError("Expected query receipt pair")
        request = QueryRequest.model_validate(raw["request"])
        result = KnowledgeResult.model_validate(raw["response"])
        _scope_result(result, scope)
        if result.operation != "query" or any(
            getattr(request, k) != getattr(scope, k) for k in ("collection_id", "corpus_snapshot_id", "use_policy")
        ):
            raise ValueError("Not an exact query for this scope")
        if request.page.cursor != next_cursor:
            raise ValueError("Broken query cursor chain")
        identity = request.model_dump(mode="json", exclude={"request_id", "page"}) | {"page_size": request.page.size}
        if initial is None:
            initial = identity
        elif identity != initial:
            raise ValueError("Query selection changed within chain")
        c = result.completeness
        if resultset is None:
            resultset = c.result_set_id
            total = c.total_matches
        elif c.result_set_id != resultset or c.total_matches != total:
            raise ValueError("Query set changed within chain")
        if not c.result_set_id:
            raise ValueError("Missing query result set identity")
        complete = complete and result.status == "ok" and c.matching_complete and c.exhaustive_within_scope
        if c.returned != len(result.results):
            raise ValueError("Query receipt count differs")
        for row in result.results:
            if row.kind != "sample_test_context":
                raise ValueError("Query did not return candidate contexts")
            key = fingerprint(row.ref.model_dump())
            if key in seen:
                raise ValueError("Duplicate context across query pages")
            seen.add(key)
        next_cursor = c.next_cursor
        hashes.append(fingerprint(raw))
    candidates = {fingerprint(r.ref.model_dump()) for r in rows}
    if not candidates <= seen:
        raise ValueError("Candidates not proved by query chain")
    exhaustive = bool(complete and next_cursor is None and total == len(seen) == len(candidates))
    return Coverage(
        mode="exact_query" if exhaustive else "checked_candidates",
        exhaustive=exhaustive,
        checked=len(rows),
        total=total if exhaustive else None,
        query_receipt_hashes=hashes,
        query_definition=initial,
        population="query_matches" if exhaustive else "checked_candidates",
        reason="complete_within_declared_query" if exhaustive else "partial_query_chain_or_candidates",
    )


def _fact_ref(row, value):
    return AssertionRef(kind="assertion", **{k: getattr(row.ref, k) for k in OWNER}, assertion_id=value.assertion_id)


def _scalar(value, constraint):
    typed, reason = scalar(value, constraint.value_type)
    if reason:
        return None, reason
    if constraint.value_type == "number" and not constraint.unit:
        return None, "target_unit_missing"
    if constraint.value_type == "number" and not value.unit:
        return None, "unit_missing"
    if value.unit != constraint.unit:
        return None, "unit_mismatch"
    if constraint.field_id == "composition.amount" and (not constraint.basis or not value.basis):
        return None, "basis_missing"
    if constraint.basis is not None and value.basis is None:
        return None, "basis_missing"
    if value.basis != constraint.basis:
        return None, "basis_mismatch"
    return typed, None


def _check(row, constraint):
    values = [v for v in row.values if matches_selector(v, constraint.field_id, constraint.qualifiers)]

    def result(status, reason=None):
        return ConstraintCheck(
            constraint_id=constraint.constraint_id,
            status=status,
            reason=reason,
            fact_refs=[_fact_ref(row, v) for v in values],
        )

    if not values:
        return result("unknown", "not_read_or_not_reported")
    for value in values:
        reason = observation_reason(row, value)
        if reason:
            return result("unknown", reason)
    typed = []
    for value in values:
        scalar, reason = _scalar(value, constraint)
        if reason:
            return result("unknown", reason)
        typed.append(scalar)
    # Multiple observations do not permit existential cherry-picking. Even when
    # all pass the threshold, unequal reported values remain separate/unknown.
    identities = {(t.get("decimal") if t["kind"] == "decimal" else t.get("raw")) for t in typed}
    if len(identities) != 1:
        return result("unknown", "conflicting_values")
    matched, reason = compare_typed_value(
        typed[0], {"op": constraint.op, "value": constraint.value, "value_type": constraint.value_type}
    )
    if reason not in (None, "not_definitely_satisfied", "not_equal"):
        return result("unknown", reason)
    return result("satisfied" if matched else "not_satisfied")


def evaluate_constraints(target, candidates, *, scope, query_pages=None):
    target = UserTarget.model_validate(target.model_dump() if isinstance(target, UserTarget) else target)
    scope, rows = _rows(candidates, scope)
    coverage = _coverage(rows, scope, query_pages)
    out = []
    for row in rows:
        checks = [_check(row, c) for c in target.constraints]
        status = (
            "not_satisfied"
            if any(c.status == "not_satisfied" for c in checks)
            else "unknown"
            if any(c.status == "unknown" for c in checks)
            else "satisfied"
        )
        out.append(
            CandidateEvaluation(
                candidate_id="candidate-" + fingerprint(row.ref.model_dump()),
                ref=row.ref,
                label=row.label,
                status=status,
                checks=checks,
            )
        )
    inputs = {
        "target": target.model_dump(mode="json"),
        "candidates": [r.model_dump(mode="json") for r in rows],
        "scope": scope.model_dump(),
        "query_pages": query_pages or [],
        "policy_version": POLICY_VERSION,
        "observation_semantics": semantic_inputs(rows),
    }
    return TaskResult(
        goal_id=target.id, scope=scope, coverage=coverage, candidates=out, input_hash=fingerprint(inputs), inputs=inputs
    )


def performance_reference(candidates, *, scope, comparison_results):
    scope, rows = _rows(candidates, scope)
    refs = {fingerprint(r.ref.model_dump()) for r in rows}
    comparisons = []
    if len(comparison_results) > 20:
        raise ValueError("Too many comparisons")
    for raw in comparison_results:
        result = KnowledgeResult.model_validate(raw)
        _scope_result(result, scope)
        if result.operation != "compare" or result.status != "ok":
            raise ValueError("Not a verified comparison receipt")
        for pair in result.comparisons:
            if any(fingerprint(r.model_dump()) not in refs for r in (pair.left_ref, pair.right_ref)):
                raise ValueError("Comparison outside candidates")
            comparisons.append(pair.model_dump(mode="json"))
    inputs = {
        "candidates": [r.model_dump(mode="json") for r in rows],
        "scope": scope.model_dump(),
        "comparisons": comparisons,
        "policy_version": POLICY_VERSION,
        "observation_semantics": semantic_inputs(rows),
    }
    return PerformanceReference(
        scope=scope,
        references=[r.ref for r in rows],
        comparisons=comparisons,
        missing_readiness=[
            "training_examples",
            "target_definition",
            "condition_coverage",
            "independent_validation",
            "applicability_domain",
        ],
        input_hash=fingerprint(inputs),
        inputs=inputs,
    )


def research_plan(target, candidates, *, scope):
    target = UserTarget.model_validate(target.model_dump() if isinstance(target, UserTarget) else target)
    scope, rows = _rows(candidates, scope)
    parameters = []
    seen = set()
    for row in rows:
        for value in row.values:
            if not value.field_id.startswith("process.") or not value.evidence_refs:
                continue
            for path in value.context_paths:
                if path.sample_ref != row.ref.sample_ref or value.subject_ref != (path.step_ref or path.process_ref):
                    raise ValueError("Process path does not own this parameter")
                if path.relation_ids != [r.relation_id for r in path.relations] or len(set(path.relation_ids)) != len(
                    path.relation_ids
                ):
                    raise ValueError("Process relation identities differ")
                expected = []
                owner = path.sample_ref
                if path.formulation_ref:
                    first = path.relations[0]
                    form = path.formulation_ref
                    if first.predicate == "has_formulation":
                        expected.append(("has_formulation", owner, form))
                    elif first.predicate == "recipe_for":
                        expected.append(("recipe_for", form, owner))
                    else:
                        raise ValueError("Missing formulation owner edge")
                    owner = form
                expected.append(("process_applicability", owner, path.process_ref))
                if path.step_ref:
                    expected.append(("defines_step", path.process_ref, path.step_ref))
                if [(r.predicate, r.from_ref, r.to_ref) for r in path.relations] != expected:
                    raise ValueError("Broken process ownership path")
                if any(
                    not r.evidence_refs
                    or r.basis_kind
                    not in {"source_explicit", "structural_expansion", "explicit_inheritance", "explicit_override"}
                    or r.applicability
                    in {"unknown", "not_applicable", "superseded", "requires_review", "candidate", "unresolved"}
                    for r in path.relations
                ):
                    continue
                identity = fingerprint([row.ref.model_dump(), value.assertion_id, path.model_dump()])
                if identity in seen:
                    continue
                seen.add(identity)
                parameters.append(
                    PlanParameter(
                        ref=_fact_ref(row, value), value=value, process_ref=path.process_ref, step_ref=path.step_ref
                    )
                )
    inputs = {
        "target": target.model_dump(mode="json"),
        "candidates": [r.model_dump(mode="json") for r in rows],
        "scope": scope.model_dump(),
        "policy_version": POLICY_VERSION,
        "observation_semantics": semantic_inputs(rows),
    }
    # Missing categories remain per-experiment decisions; one reported process
    # parameter cannot fill a different step or a proposed experiment's settings.
    return ResearchPlan(
        goal_id=target.id,
        scope=scope,
        candidate_refs=[r.ref for r in rows],
        parameters=parameters,
        pending_parameters=["temperature", "duration", "mixing_ratio"],
        next_steps=[
            "confirm_process_order_and_parameters",
            "choose_control_and_replicates",
            "confirm_test_method_and_conditions",
            "compare_observations_with_user_targets",
        ],
        input_hash=fingerprint(inputs),
        inputs=inputs,
    )
