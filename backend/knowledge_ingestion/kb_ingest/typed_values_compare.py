"""Conservative filters on saved typed values and explicitly admitted formula caches."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from .typed_values_number import _number
from .typed_values_parse import QUERY_VALUE_TYPES, TYPED_VALUE_VERSION, parse_typed_value

type _Record = Mapping[str, object]
type _Result = tuple[bool, str | None]

_NUMERIC_KINDS = frozenset({"decimal", "interval", "bound", "uncertainty"})
_ISO_TYPES: dict[object, type[date] | type[time]] = {"date": date, "datetime": datetime, "time": time}
_TEMPORAL_KINDS = frozenset({*_ISO_TYPES, "duration"})
_SOURCE_KINDS = _TEMPORAL_KINDS | {"text", "category"}


@dataclass(frozen=True, slots=True)
class _Filter:
    query: _Record
    explicit_type: bool
    requested: object
    op: object
    expected: object


def _outcome(matched: bool, reason: str = "not_definitely_satisfied") -> _Result:
    return matched, None if matched else reason


def _source_decimal(raw: object) -> Decimal:
    return Decimal(_number(raw, "source"))


@dataclass(frozen=True, slots=True)
class _Bounds:
    lower: Decimal | None
    upper: Decimal | None
    lower_inclusive: bool
    upper_inclusive: bool

    def satisfies(self, op: object, right: Decimal) -> bool:
        """Every possible value in the interval must satisfy the filter."""
        if op == "eq":
            return self.lower == self.upper == right and self.lower_inclusive and self.upper_inclusive
        if op == "gte":
            return self.lower is not None and self.lower >= right
        if op == "gt":
            return self.lower is not None and (self.lower > right or self.lower == right and not self.lower_inclusive)
        if op == "lte":
            return self.upper is not None and self.upper <= right
        return (
            op == "lt"
            and self.upper is not None
            and (self.upper < right or self.upper == right and not self.upper_inclusive)
        )


def _bounds(value: _Record, kind: object) -> _Bounds | None:
    lower = _source_decimal(value["lower"]) if value.get("lower") is not None else None
    upper = _source_decimal(value["upper"]) if value.get("upper") is not None else None
    lower_inclusive, upper_inclusive = value["lower_inclusive"], value["upper_inclusive"]
    if type(lower_inclusive) is not bool or type(upper_inclusive) is not bool:
        return None
    if lower is None or upper is None:
        if (lower is None and upper is None) or kind == "interval":
            return None
    elif lower > upper or lower == upper and not (lower_inclusive and upper_inclusive):
        return None
    return _Bounds(lower, upper, lower_inclusive, upper_inclusive)


def _valid_explicit_value(value: _Record) -> bool:
    """An explicit exists query requires valid saved evidence for the requested type."""
    kind = value["kind"]
    try:
        if kind == "decimal":
            _number(value["decimal"], "source")
        elif kind == "uncertainty":
            _number(value["center"], "source")
            return _source_decimal(value["uncertainty"]) >= 0
        elif kind in {"interval", "bound"}:
            return _bounds(value, kind) is not None
        elif kind in _ISO_TYPES:
            canonical = value["canonical"]
            if not isinstance(canonical, str):
                return False
            _ISO_TYPES[kind].fromisoformat(canonical)
        elif kind == "duration":
            _number(value["canonical"], "source")
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False
    return True


def _ordered[T: (Decimal, date, time)](left: T, right: T, op: object) -> _Result:
    comparisons: dict[object, bool] = {
        "eq": left == right,
        "gte": left >= right,
        "gt": left > right,
        "lte": left <= right,
        "lt": left < right,
    }
    return _outcome(comparisons[op])


def _temporal_order[T: (Decimal, date, time)](left: T, right: T, op: object) -> _Result:
    if (
        isinstance(left, (time, datetime))
        and isinstance(right, (time, datetime))
        and (left.utcoffset() is None) != (right.utcoffset() is None)
    ):
        return False, "timezone_not_comparable"
    if op == "contains":
        return False, "contains_requires_text"
    return _ordered(left, right, op)


def _compare_temporal(value: _Record, kind: str, op: object, expected: object) -> _Result:
    parsed = parse_typed_value(expected, expected=kind)
    if parsed.get("status") != "valid" or parsed.get("kind") != kind:
        return False, "invalid_temporal_query"
    try:
        left, right = value["canonical"], parsed["canonical"]
        if kind == "duration":
            if not isinstance(left, (Decimal, str, int, float)) or not isinstance(right, str):
                return False, "invalid_temporal_value"
            return _temporal_order(Decimal(left), Decimal(right), op)
        if not isinstance(left, str) or not isinstance(right, str):
            return False, "invalid_temporal_value"
        if kind == "datetime":
            return _temporal_order(datetime.fromisoformat(left), datetime.fromisoformat(right), op)
        if kind == "time":
            return _temporal_order(time.fromisoformat(left), time.fromisoformat(right), op)
        return _temporal_order(date.fromisoformat(left), date.fromisoformat(right), op)
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return False, "invalid_temporal_value"


def _compare_source(raw: object, kind: str, op: object, expected: object) -> _Result:
    if op == "eq" and type(raw) is type(expected):
        return _outcome(raw == expected, "not_equal")
    if op == "contains" and isinstance(raw, str) and isinstance(expected, str):
        return _outcome(expected.casefold() in raw.casefold(), "not_contained")
    return False, "non_numeric_kind:" + kind


def _compare_number(value: _Record, kind: object, criteria: _Filter) -> _Result:
    op = criteria.op
    if op == "contains":
        return False, "contains_requires_text"
    if kind == "uncertainty" and criteria.query.get("allow_uncertainty_center") is not True:
        return False, "uncertainty_center_not_enabled"
    if kind not in _NUMERIC_KINDS:
        return False, "invalid_typed_value"
    try:
        right = _source_decimal(criteria.expected)
    except (ValueError, InvalidOperation):
        return False, "invalid_query_number"
    try:
        if kind in {"decimal", "uncertainty"}:
            left = _source_decimal(value.get("decimal" if kind == "decimal" else "center"))
            return _ordered(left, right, op)
        bounds = _bounds(value, kind)
        if bounds is None:
            return False, "invalid_typed_value"
        matched = bounds.satisfies(op, right)
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False, "invalid_typed_value"
    return _outcome(matched)


def _compare_valid(value: _Record, kind: str | None, criteria: _Filter) -> _Result:
    requested, op = criteria.requested, criteria.op
    if criteria.explicit_type:
        if not (requested == kind or requested == "number" and kind in _NUMERIC_KINDS):
            return False, "value_type_mismatch"
        if not _valid_explicit_value(value):
            return False, "invalid_typed_value"
        if op == "exists":
            return _outcome(value.get("raw") not in (None, "", []), "missing:blank")
    if kind is not None and kind in _TEMPORAL_KINDS and requested == kind:
        return _compare_temporal(value, kind, op, criteria.expected)
    if kind is not None and kind in _SOURCE_KINDS:
        return _compare_source(value.get("raw"), kind, op, criteria.expected)
    return _compare_number(value, kind, criteria)


def _compare_interpreted(value: _Record, kind: str | None, status: object, criteria: _Filter) -> _Result:
    if status == "ambiguous":
        return False, "ambiguous_number_format"
    if status != "valid":
        return False, "invalid_typed_value"
    return _compare_valid(value, kind, criteria)


def compare_typed_value(value: object, query: _Record) -> tuple[bool, str | None]:
    """Conservative comparison. A range must satisfy a filter at every point.

    Query numbers use ungrouped decimal-dot spelling, independent of the source
    locale. Formula caches and uncertainty centers require separate explicit
    booleans. Optional value_type constrains all operations, including exists.
    With no value_type, historical presence and source-string behavior stays
    unchanged. Typed uncertainty presence does not use its center; typed formula
    presence requires an explicitly admitted cache of the requested type.
    """
    if not isinstance(value, dict) or value.get("version") != TYPED_VALUE_VERSION:
        return False, "unsupported_typed_value_version"
    explicit_type = "value_type" in query
    requested = query.get("value_type")
    if explicit_type and (not isinstance(requested, str) or requested not in QUERY_VALUE_TYPES):
        return False, "invalid_query_value_type"
    kind: str | None = value.get("kind")
    status: object = value.get("status")
    op, expected = query.get("op", "eq"), query.get("value")
    if op not in {"eq", "contains", "exists", "gte", "gt", "lte", "lt"}:
        msg = "Unsupported filter operator"
        raise ValueError(msg)
    if kind == "missing":
        return False, "missing:" + str(status)
    if op == "exists" and not explicit_type:
        return _outcome(value.get("raw") not in (None, "", []), "missing:blank")
    if kind == "formula_cached":
        if query.get("allow_formula_cached") is not True:
            return False, "formula_cache_not_enabled"
        if value.get("cached") is None:
            return False, "formula_cache_missing"
        return compare_typed_value(value["cached"], query)
    return _compare_interpreted(value, kind, status, _Filter(query, explicit_type, requested, op, expected))
