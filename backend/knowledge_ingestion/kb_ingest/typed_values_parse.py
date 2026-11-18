"""Parse versioned source values without guessing the locale or converting units."""

import math
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TypedDict, TypeGuard

from .typed_values_number import _parse_numeric, _TypedValue

TYPED_VALUE_VERSION = "2"
QUERY_VALUE_TYPES = frozenset({"number", "text", "category", "date", "time", "datetime", "duration"})
_FORMATS = {
    "source": "source",
    "decimal_dot": "decimal_dot",
    "decimal_comma": "decimal_comma",
    "en_US": "decimal_dot",
    "de_DE": "decimal_comma",
}
_MISSING = {
    "": "blank",
    "nt": "not_tested",
    "not tested": "not_tested",
    "未测": "not_tested",
    "未测试": "not_tested",
    "未检出": "not_detected",
    "不适用": "not_applicable",
    "not applicable": "not_applicable",
    "未报告": "not_reported",
    "not reported": "not_reported",
    "nd": "not_detected",
    "not detected": "not_detected",
    "nan": "nan",
    "n/a": "not_available",
    "na": "not_available",
    "null": "not_available",
    **dict.fromkeys(("inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"), "non_finite"),
}
_ERRORS = {"#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#NULL!", "#SPILL!", "#CALC!"}
_ISO_TYPES: dict[str, tuple[str, type[date] | type[time]]] = {
    "date": (r"\d{4}-\d{2}-\d{2}", date),
    "time": (r"\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", time),
    "datetime": (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", datetime),
}


def _json_raw(raw: object) -> object:
    if isinstance(raw, (datetime, date, time)):
        return raw.isoformat()
    if isinstance(raw, (timedelta, Decimal)) or isinstance(raw, float) and not math.isfinite(raw):
        return str(raw)
    if isinstance(raw, dict):
        return {str(k): _json_raw(v) for k, v in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_json_raw(v) for v in raw]
    return raw


def _seconds_text(whole_seconds: int, fractional_seconds: str = "0", negative: bool = False) -> str:
    """Add integer elapsed units without Decimal context rounding.

    ISO/clock fractions occur only in seconds. Retaining the fractional digits
    verbatim lets duration conversion stay exact at any caller precision.
    """
    whole, dot, fraction = fractional_seconds.partition(".")
    total = whole_seconds + int(whole)
    return ("-" if negative else "") + str(total) + (dot + fraction if dot else "")


def _microseconds_text(micros: int) -> str:
    whole, remainder = divmod(abs(micros), 1000000)
    fraction = str(remainder).zfill(6).rstrip("0")
    return ("-" if micros < 0 else "") + str(whole) + ("." + fraction if fraction else "")


class _DurationParts(TypedDict):
    days: int
    seconds: int
    microseconds: int


def _has_duration_parts(raw: dict[object, object]) -> TypeGuard[_DurationParts]:
    return all(type(raw.get(key)) is int for key in ("days", "seconds", "microseconds"))


def _duration(raw: object) -> str:
    if isinstance(raw, dict) and raw.get("kind") == "duration":
        if not _has_duration_parts(raw):
            raise ValueError("invalid")
        micros = (raw["days"] * 86400 + raw["seconds"]) * 1000000 + raw["microseconds"]
        return _microseconds_text(micros)
    if isinstance(raw, timedelta):
        return _microseconds_text((raw.days * 86400 + raw.seconds) * 1000000 + raw.microseconds)
    text = str(raw).strip()
    match = re.fullmatch(r"(-)?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", text)
    if match and any(match.group(i) is not None for i in range(2, 6)):
        if text.endswith("T"):
            raise ValueError("invalid")
        whole = sum(int(match[i] or "0") * factor for i, factor in zip(range(2, 5), (86400, 3600, 60), strict=True))
        return _seconds_text(whole, match[5] or "0", bool(match[1]))
    match = re.fullmatch(r"(-)?(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)", text)
    if match:
        return _seconds_text(int(match[2]) * 3600 + int(match[3]) * 60, match[4], bool(match[1]))
    raise ValueError("invalid")


def _numeric_notation(text: str) -> bool:
    """Check whether source text belongs to the numeric notation language.

    Incomplete numeric forms still need validation. Explicit bound/error
    operators reserve numeric meaning even if an operand is malformed.
    Material codes and unparsed unit-bearing text remain exact text.
    """
    if re.match(r"^[<>≤≥]", text) or "±" in text or "+/-" in text:
        return True
    tokens = r"(?:[\dEe+\-−.,\s\u00a0\u202f–—<>=≤≥±/()\[\]]|\bto\b)+"
    return bool(re.search(r"\d", text) and re.fullmatch(tokens, text))


def _is_formula(raw: object, formula: object, metadata: dict[object, object]) -> bool:
    return (
        formula is not None
        or metadata.get("value_type") in {"f", "formula", "array_formula"}
        or (isinstance(raw, str) and raw.lstrip().startswith("="))
    )


def _text_or_missing(
    text: str, expected: str, metadata: dict[object, object], result: _TypedValue
) -> _TypedValue | None:
    # A confirmed name/category is not a measurement missing-value vocabulary.
    # Physical blanks and native error cells remain absent/error in every role.
    if expected in {"text", "category"} and text and metadata.get("value_type") not in {"e", "error"}:
        return {**result, "kind": expected, "status": "valid"}
    status = _MISSING.get(text.casefold())
    if metadata.get("value_type") in {"e", "error"} or text.upper() in _ERRORS:
        status = "error"
    if status:
        return {**result, "kind": "missing", "status": status}
    if expected in {"text", "category"}:
        return {**result, "kind": expected, "status": "valid"}
    return None


def _temporal_kind(raw: object, text: str, expected: str, metadata: dict[object, object]) -> str:
    for cls, kind in ((datetime, "datetime"), (date, "date"), (time, "time"), (timedelta, "duration")):
        if isinstance(raw, cls):
            return kind
    if expected in {"auto", "number", "scalar"}:
        source_type = metadata.get("value_type")
        if source_type == "duration" or isinstance(raw, dict) and raw.get("kind") == "duration":
            return "duration"
        if source_type in {"d", "date", "datetime", "time"}:
            if re.match(r"\d{4}-\d{2}-\d{2}[T ]", text):
                return "datetime"
            return "time" if re.match(r"\d{2}:", text) else "date"
    return expected


def _parse_temporal(raw: object, text: str, kind: str, result: _TypedValue) -> _TypedValue:
    try:
        if kind == "duration":
            canonical = _duration(raw)
        else:
            pattern, cls = _ISO_TYPES[kind]
            if not re.fullmatch(pattern, text):
                return result
            canonical = cls.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except (ValueError, InvalidOperation):
        return result
    return {**result, "kind": kind, "status": "valid", "canonical": canonical}


def parse_typed_value(cell: object, expected: str = "number", number_format: str = "source") -> _TypedValue:
    """Parse a scalar or source-cell mapping without guessing locale or units.

    Cell mappings use raw/raw_value and may carry formula, cached_value and
    value_type. Date strings require an explicit expected type and ISO spelling;
    calendar months/years are not fixed elapsed durations.
    """
    if number_format not in _FORMATS:
        message = "Unsupported number_format"
        raise ValueError(message)
    if expected not in {"auto", "number", "scalar", "category", "text", "date", "time", "datetime", "duration"}:
        message = "Unsupported expected value type"
        raise ValueError(message)
    fmt = _FORMATS[number_format]
    metadata: dict[object, object] = cell if isinstance(cell, dict) else {}
    raw = metadata.get("raw", metadata.get("raw_value", metadata.get("value"))) if metadata else cell
    result: _TypedValue = {
        "version": TYPED_VALUE_VERSION,
        "raw": _json_raw(raw),
        "kind": "unparsed",
        "status": "invalid",
    }
    if raw is None and metadata.get("missing_kind") == "formula_cache_unavailable":
        return {**result, "kind": "missing", "status": "not_available", "reason": "formula_cache_unavailable"}
    formula = metadata.get("formula")
    if _is_formula(raw, formula, metadata):
        cache = metadata.get("cached_value")
        return {
            **result,
            "kind": "formula_cached",
            "formula": _json_raw(formula if formula is not None else raw),
            "status": "unverified_cache" if cache is not None else "cache_missing",
            "cached": parse_typed_value(cache, expected, number_format) if cache is not None else None,
        }
    text = str(raw).strip() if raw is not None else ""
    classified = _text_or_missing(text, expected, metadata, result)
    if classified is not None:
        return classified
    temporal = _temporal_kind(raw, text, expected, metadata)
    if temporal in {"date", "time", "datetime", "duration"}:
        return _parse_temporal(raw, text, temporal, result)
    if expected == "auto" and isinstance(raw, str) and not _numeric_notation(text):
        return {**result, "kind": "text", "status": "valid"}
    return _parse_numeric(raw, text, fmt, result)
