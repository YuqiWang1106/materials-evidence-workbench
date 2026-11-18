"""Curve previews and per-point issues for table result projections."""

import math
from collections import Counter
from copy import deepcopy

from kb_ingest.contract import strict_equal


def curve_preview(series, max_points=2000):
    xs, ys = series.get("x_values", []), series.get("values", [])
    typed_series = "typed_values" in series
    index_axis = typed_series and "x_typed_values" not in series
    if typed_series:

        def plotted(value):
            if value.get("kind") != "decimal" or value.get("status") != "valid":
                return None
            try:
                number = float(value["decimal"])
                return number if math.isfinite(number) else None
            except (ValueError, OverflowError, KeyError):
                return None

        ys = [plotted(v) for v in series["typed_values"]]
        xs = list(range(1, len(ys) + 1)) if index_axis else [plotted(v) for v in series["x_typed_values"]]
    count = max(len(xs), len(ys))
    if not count:
        return {"points": [], "total_points": 0, "sampled": False}
    # Bucket extrema and explicit gap sentinels avoid connecting across missing data.
    stride = max(1, math.ceil(count / max(1, max_points // 4)))
    indices = set()
    for start in range(0, count, stride):
        end = min(count, start + stride)
        valid = [
            i
            for i in range(start, end)
            if i < len(xs) and i < len(ys) and type(xs[i]) in (int, float) and type(ys[i]) in (int, float)
        ]
        if valid:
            indices.add(min(valid, key=lambda i: ys[i]))
            indices.add(max(valid, key=lambda i: ys[i]))
        indices.add(start)
        indices.add(end - 1)
    points = [[xs[i] if i < len(xs) else None, ys[i] if i < len(ys) else None, i] for i in sorted(indices)]
    # Every displayed segment records whether an omitted source gap interrupts it.
    for previous, current in zip(points, points[1:]):
        if any(
            i >= len(xs) or i >= len(ys) or xs[i] is None or ys[i] is None for i in range(previous[2] + 1, current[2])
        ):
            current.append("gap_before")
    result = {
        "points": points,
        "total_points": count,
        "sampled": len(points) < count,
        "x_label": series.get("x_label"),
        "x_unit": series.get("x_unit"),
        "y_label": series.get("y_label"),
        "y_unit": series.get("y_unit"),
    }
    if typed_series:
        result.update(
            x_is_point_index=index_axis,
            unplotted_points=sum(i >= len(xs) or i >= len(ys) or xs[i] is None or ys[i] is None for i in range(count)),
            projection="finite_exact_numbers_only",
        )
    return result


def _bound_curve_value(assertion, role, value):
    """Display a source label/unit only when its retained cell proof agrees."""
    binding = assertion.get("source_context", {}).get("bindings", {}).get(role)
    if (
        value in (None, "")
        or not isinstance(binding, dict)
        or binding.get("not_source_fact")
        or any(type(binding.get(key)) is not int or binding[key] < 1 for key in ("row", "column"))
        or not binding.get("asset_ref")
        or not strict_equal(binding.get("raw"), value)
    ):
        return None
    return deepcopy(value)


def _curve_point_issues(assertion):
    """Yield unresolved point interpretations, keeping known absences as gaps."""
    series = assertion["series_data"]
    bindings = assertion.get("source_context", {}).get("bindings", {})
    known_missing = {"blank", "not_tested", "not_detected", "not_applicable", "not_reported", "not_available"}
    for axis, key, role in (("x", "x_typed_values", "x_value"), ("y", "typed_values", "value")):
        sources = bindings.get(role, [])
        for index, typed in enumerate(series.get(key, [])):
            if (
                typed.get("kind") == "decimal"
                and typed.get("status") == "valid"
                or typed.get("kind") == "missing"
                and typed.get("status") in known_missing
            ):
                continue
            proof = sources[index] if isinstance(sources, list) and index < len(sources) else {}
            yield axis, index, typed, proof if isinstance(proof, dict) else {}


def _curve_result(assertion):
    series = assertion["series_data"]
    preview = curve_preview(series)
    labels = {}
    if assertion["value"].get("typed_value_version") == "2":
        qualifiers = assertion.get("qualifiers", {})
        for display, qualifier in (
            ("x_label", "axis:x_label"),
            ("y_label", "metric_label"),
            ("material_label", "material_label"),
            ("stage_label", "condition:stage"),
        ):
            labels[display] = _bound_curve_value(assertion, "qualifier:" + qualifier, qualifiers.get(qualifier))
        preview.update(
            x_label=labels.pop("x_label"),
            y_label=labels.pop("y_label"),
            x_unit=_bound_curve_value(assertion, "x_unit", series.get("x_unit")),
            y_unit=_bound_curve_value(assertion, "unit", assertion["value"].get("raw_unit")),
        )
    counts = Counter()
    issues = []
    for axis, index, typed, proof in _curve_point_issues(assertion):
        counts[axis] += 1
        if len(issues) < 20:
            raw = deepcopy(typed.get("raw"))
            truncated = isinstance(raw, str) and len(raw) > 512
            issues.append(
                {
                    "axis": axis,
                    "point_index": index,
                    "kind": typed.get("kind"),
                    "status": typed.get("status"),
                    "raw": raw[:512] if truncated else raw,
                    **({"raw_truncated": True} if truncated else {}),
                    **{key: proof[key] for key in ("asset_ref", "sheet", "row", "column") if key in proof},
                }
            )
    preview.update(
        unparsed_x_point_count=counts["x"],
        unparsed_point_count=counts["y"],
        point_issues=issues,
        point_issue_total=sum(counts.values()),
    )
    return {"series_preview": preview, **labels}


def _curve_counts(entries):
    """Count complete source rows once within a scope shared by several series."""
    series = [entry for entry in entries if "series_data" in entry]
    rows = set()
    unbound_rows = 0
    unparsed_x = set()
    unparsed_y = 0
    for entry in series:
        context = entry.get("source_context", {})
        scope = context.get("record_scope", {})
        bindings = context.get("bindings", {}).get("value")
        if isinstance(bindings, list) and bindings:
            for index, binding in enumerate(bindings):
                if isinstance(binding, dict) and type(binding.get("row")) is int:
                    rows.add(
                        (
                            scope.get("scope"),
                            scope.get("block"),
                            binding.get("asset_ref"),
                            binding.get("sheet"),
                            binding["row"],
                        )
                    )
                else:
                    rows.add((entry["assertion_id"], index))
        else:
            raw = entry["value"].get("raw_value")
            unbound_rows += len(raw) if isinstance(raw, list) else entry["series_data"].get("point_count", 0)
        for axis, index, _, proof in _curve_point_issues(entry):
            if axis == "y":
                unparsed_y += 1
            elif type(proof.get("row")) is int and type(proof.get("column")) is int:
                unparsed_x.add(
                    (
                        scope.get("scope"),
                        scope.get("block"),
                        proof.get("asset_ref"),
                        proof.get("sheet"),
                        proof["row"],
                        proof["column"],
                    )
                )
            else:
                unparsed_x.add((entry["assertion_id"], index))
    return {
        "series_count": len({entry["subject_ref"] for entry in series}),
        "point_row_count": len(rows) + unbound_rows,
        "unparsed_x_point_count": len(unparsed_x),
        "unparsed_point_count": unparsed_y,
    }
