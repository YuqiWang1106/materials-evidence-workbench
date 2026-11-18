"""Conservative descriptive comparison of existing exact contexts."""

from .contract import strict_equal
from .core import fingerprint
from .knowledge_contracts import (
    AssertionRef,
    BlockedOperation,
    CheckedCondition,
    ComparabilityResult,
    ConditionValue,
    MissingCondition,
)


def compare_pair(left, right, la, ra, profile, requested, evidence_builder):
    checked = []
    missing = []
    evidence = []

    def value(matches, owner):
        if len(matches) != 1:
            return None
        a = matches[0]
        v = a["value"]
        from .ingest.tables.table_quality import NON_VALUES

        if v.get("raw_value") in (None, "", []) or a.get("source_context", {}).get("value_status") in NON_VALUES:
            return None
        refs = evidence_builder(owner, a["evidence_refs"])
        for ref in refs:
            if ref not in evidence:
                evidence.append(ref)
        return ConditionValue(
            raw_value=v.get("raw_value"),
            unit=v.get("raw_unit"),
            basis=v.get("raw_basis"),
            assertion_ref=AssertionRef.model_validate(
                {
                    **owner.model_dump(exclude={"sample_ref", "test_ref"}),
                    "kind": "assertion",
                    "assertion_id": a["assertion_id"],
                }
            ),
            evidence_refs=refs,
        )

    for rule in profile.required_conditions:
        sel = rule.selector

        def matching(assertions):
            return [
                a
                for a in assertions
                if a["field_id"] == sel.field_id
                and all(a.get("qualifiers", {}).get(k) == v for (k, v) in sel.qualifiers.items())
            ]

        (lm, rm) = (matching(la), matching(ra))
        (lv, rv) = (value(lm, left), value(rm, right))
        verdict = "same"
        reason = "exact_source_values_equal"
        if lv is None or rv is None:
            verdict = "unknown" if len(lm) > 1 or len(rm) > 1 else "missing"
            reason = "ambiguous_condition" if verdict == "unknown" else "condition_not_reported"
        elif rule.rule == "source_explicit_relation":
            verdict = "unknown"
            reason = "explicit_relation_not_proven"
        elif rule.rule == "same_unit_basis":
            from decimal import Decimal, InvalidOperation

            def scalar(v):
                try:
                    return Decimal(str(v.raw_value)).is_finite() and (not isinstance(v.raw_value, bool))
                except (InvalidOperation, ValueError):
                    return False

            if not scalar(lv) or not scalar(rv):
                checked.append(
                    CheckedCondition(
                        condition=sel, left=lv, right=rv, verdict="unknown", reason="exact_scalar_condition_required"
                    )
                )
                missing.append(MissingCondition(condition=sel, side="both", reason="exact_scalar_condition_required"))
                continue
            if not strict_equal(lv.raw_value, rv.raw_value):
                verdict = "different"
                reason = "condition_value_differs"
            elif lv.unit is not None and rv.unit is not None and (lv.unit != rv.unit):
                verdict = "different"
                reason = "condition_unit_differs"
            elif lv.basis is not None and rv.basis is not None and (lv.basis != rv.basis):
                verdict = "different"
                reason = "condition_basis_differs"
            elif (
                lv.unit is None
                or rv.unit is None
                or (sel.field_id == "composition.amount" and (lv.basis is None or rv.basis is None))
            ):
                verdict = "unknown"
                reason = "unit_or_basis_not_reported"
        elif not strict_equal(lv.raw_value, rv.raw_value):
            verdict = "different"
            reason = "condition_value_differs"
        condition = CheckedCondition(condition=sel, left=lv, right=rv, verdict=verdict, reason=reason)
        checked.append(condition)
        if verdict in {"missing", "unknown"}:
            side = (
                "both"
                if lv is None and rv is None or (lv is not None and rv is not None)
                else "left"
                if lv is None
                else "right"
            )
            missing.append(MissingCondition(condition=sel, side=side, reason=reason))
    differences = [c for c in checked if c.verdict == "different"]
    status = "not_comparable" if differences else "insufficient_conditions" if missing else "comparable"
    allowed = []
    blocked = []
    for operation in requested:
        if operation not in profile.allowed_operations:
            reason = "operation_not_declared_by_profile"
        elif operation in {"side_by_side", "condition_diff"}:
            allowed.append(operation)
            continue
        elif status != "comparable":
            reason = "conditions_not_comparable"
        elif operation == "numeric_delta":
            reason = "profile_target_metric_not_declared"
        elif operation == "category_compare":
            reason = "profile_category_order_not_declared"
        else:
            reason = "profile_rank_not_declared"
        blocked.append(BlockedOperation(operation=operation, reason=reason))
    payload = dict(
        left=left.model_dump(mode="json"),
        right=right.model_dump(mode="json"),
        left_assertions=la,
        right_assertions=ra,
        profile=profile.model_dump(mode="json"),
    )
    from .knowledge_contracts import ProfileRef

    return ComparabilityResult(
        comparison_id=fingerprint(payload),
        left_ref=left,
        right_ref=right,
        profile=ProfileRef(id=profile.id, version=profile.version, sha256=profile.sha256),
        status=status,
        checked_conditions=checked,
        differences=differences,
        missing_conditions=missing,
        evidence_refs=evidence,
        allowed_operations=allowed,
        blocked_operations=blocked,
        policy_version="knowledge-comparison-1",
        input_manifest_sha256=fingerprint(payload),
    )
