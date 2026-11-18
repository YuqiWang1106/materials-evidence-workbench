"""Versioned source values: exact decimals, explicit ambiguity, no evaluation.

``source`` accepts decimal-dot numbers without grouping. Separators requiring a
regional choice are refused until ``decimal_dot`` or ``decimal_comma`` is chosen.
Units and bases deliberately live outside this parser and are never converted.
"""

import math
import re
from collections.abc import Mapping as _Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from .typed_values_compare import compare_typed_value
from .typed_values_number import _number
from .typed_values_parse import QUERY_VALUE_TYPES, TYPED_VALUE_VERSION, _json_raw, parse_typed_value
from .typed_values_parse import _numeric_notation as _numeric_notation

# Existing imports and wildcard imports expose these names.
__all__ = [
    "QUERY_VALUE_TYPES",
    "TYPED_VALUE_VERSION",
    "Decimal",
    "InvalidOperation",
    "compare_typed_value",
    "date",
    "datetime",
    "legacy_query_value",
    "math",
    "normalize_unit",
    "parse_typed_value",
    "re",
    "time",
    "timedelta",
]


def _legacy_interval(interval: object, result: dict[str, object]) -> dict[str, object] | None:
    if not isinstance(interval, dict):
        return None
    try:
        lower, upper = (_number(interval[key], "source") for key in ("lower", "upper"))
        if Decimal(lower) > Decimal(upper):
            return None
    except (KeyError, ValueError):
        return None
    # v1 saved intervals originate from the closed `a to b` interpretation.
    return {
        **result,
        "kind": "interval",
        "lower": lower,
        "upper": upper,
        "lower_inclusive": True,
        "upper_inclusive": True,
    }


def legacy_query_value(value: _Mapping[str, object]) -> dict[str, object] | None:
    """Project only saved v1 interpretations; never classify numeric-looking text.

    Historical kind is a representation rather than a temporal type. Only its
    declared text/category or numerical forms with saved numeric evidence can
    support an explicit query type. The temporary projection is never persisted.
    """
    raw, kind = value.get("raw_value"), value.get("kind")
    result: dict[str, object] = {"version": TYPED_VALUE_VERSION, "raw": raw, "status": "valid"}
    if raw is None or isinstance(raw, str) and not raw.strip():
        return {**result, "kind": "missing", "status": "blank"}
    if kind in {"text", "category"}:
        return {**result, "kind": kind}
    if kind not in {"scalar", "number", "integer", "ratio", "numeric_notation", "interval"}:
        return None
    interval = value.get("parsed_decimal_interval", value.get("parsed_interval"))
    if interval is not None:
        return _legacy_interval(interval, result)
    if kind == "interval":
        return None
    number = value.get("parsed_decimal")
    if number is None:
        parsed = value.get("parsed")
        number = parsed if type(parsed) in (int, float) else raw if type(raw) in (int, float) else None
    if number is None:
        return None
    try:
        decimal = _number(number, "source")
    except ValueError:
        return None
    return {**result, "kind": "decimal", "decimal": decimal}


# Finite, case-sensitive spelling aliases only. Each recognized name explicitly
# states the same unit; no inferred prefix algebra, dimensional conversion,
# temperature offset or composition-basis conversion is performed here.
_UNIT_SPELLINGS = {
    "°C": "°C",
    "degC": "°C",
    "celsius": "°C",
    "Celsius": "°C",
    "MPa": "MPa",
    "megapascal": "MPa",
    "megapascals": "MPa",
    "Pa": "Pa",
    "pascal": "Pa",
    "pA": "pA",
    "picoampere": "pA",
    "s": "s",
    "second": "s",
    "seconds": "s",
    "min": "min",
    "minutes": "min",
    "h": "h",
    "hours": "h",
    "K": "K",
    "kelvin": "K",
    "wt%": "wt%",
    "vol%": "vol%",
    "phr": "phr",
}


def normalize_unit(raw_unit: object) -> dict[str, object]:
    """Retain source spelling and optionally annotate an exact known unit name.

    Unknown units remain usable as source labels, never guessed from case,
    surrounding text or a bare C. V2 queries may match these exact spelling
    aliases; raw_unit, numeric magnitude and composition basis stay unchanged.
    """
    result = {"raw": _json_raw(raw_unit), "canonical": None, "status": "unknown"}
    if raw_unit is None or isinstance(raw_unit, str) and not raw_unit.strip():
        return {**result, "status": "missing"}
    if isinstance(raw_unit, str) and raw_unit in _UNIT_SPELLINGS:
        return {**result, "canonical": _UNIT_SPELLINGS[raw_unit], "status": "recognized"}
    return result
