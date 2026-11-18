"""Exact decimal spelling and source measurement notation."""

import re
from decimal import Decimal, InvalidOperation
from typing import NotRequired, TypedDict


class _TypedValue(TypedDict):
    """Fields stored by the parser; source raw and formula values retain their type."""

    version: str
    raw: object
    kind: str
    status: str
    reason: NotRequired[str]
    formula: NotRequired[object]
    cached: NotRequired["_TypedValue | None"]
    canonical: NotRequired[str]
    decimal: NotRequired[str]
    center: NotRequired[str]
    uncertainty: NotRequired[str]
    uncertainty_kind: NotRequired[str]
    lower: NotRequired[str | None]
    upper: NotRequired[str | None]
    lower_inclusive: NotRequired[bool]
    upper_inclusive: NotRequired[bool]


def _localized_number(text: str, fmt: str) -> str:
    decimal, group = (".", ",") if fmt == "decimal_dot" else (",", ".")
    # Mixed grouping or incomplete groups cannot be interpreted reliably.
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    mantissa, *exponent = re.split("[eE]", text)
    if len(exponent) > 1 or (group in mantissa and " " in mantissa):
        raise ValueError("invalid")
    sep = " " if " " in mantissa else group
    if sep in mantissa:
        pattern = rf"[+-]?\d{{1,3}}(?:{re.escape(sep)}\d{{3}})+(?:{re.escape(decimal)}\d+)?"
        if not re.fullmatch(pattern, mantissa):
            raise ValueError("invalid")
        mantissa = mantissa.replace(sep, "")
    return mantissa.replace(decimal, ".") + ("E" + exponent[0] if exponent else "")


def _number(raw: object, fmt: str) -> str:
    # Native numerics already carry magnitude, regardless of the text locale.
    # Boolean spellings fail the decimal grammar as well.
    if isinstance(raw, (int, float, Decimal)):
        fmt = "source"
    text = str(raw).strip().replace("\u2212", "-")
    if fmt == "source":
        if "," in text or re.search(r"\d[ \u00a0\u202f]\d", text):
            raise ValueError("ambiguous")
    else:
        text = _localized_number(text, fmt)
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        raise ValueError("invalid")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid") from None
    if not value.is_finite():
        raise ValueError("invalid")
    return str(value)


def _uncertainty_value(match: re.Match[str], fmt: str, result: _TypedValue) -> _TypedValue:
    center, error = (_number(part, fmt) for part in match.groups())
    if Decimal(error) < 0:
        raise ValueError("invalid")
    return {
        **result,
        "kind": "uncertainty",
        "status": "valid",
        "center": center,
        "uncertainty": error,
        "uncertainty_kind": "unspecified",
    }


def _bound_value(match: re.Match[str], fmt: str, result: _TypedValue) -> _TypedValue:
    op, number = match.groups()
    endpoint = _number(number, fmt)
    below = op in {"<", "<=", "≤"}
    inclusive = op in {"<=", ">=", "≤", "≥"}
    return {
        **result,
        "kind": "bound",
        "status": "valid",
        "lower": None if below else endpoint,
        "upper": endpoint if below else None,
        "lower_inclusive": inclusive if not below else False,
        "upper_inclusive": inclusive if below else False,
    }


def _interval_value(text: str, fmt: str, result: _TypedValue) -> _TypedValue | None:
    interval = re.fullmatch(r"([\[(])\s*(.+?)\s*,\s*(.+?)\s*([\])])", text) if fmt != "decimal_comma" else None
    if interval:
        left, lower, upper, right = interval.groups()
        li, ui = left == "[", right == "]"
    else:
        # A hyphen in the exponent is not a range separator.
        interval = re.fullmatch(r"(.+?)\s*(?:–|—|\bto\b|(?<![eE])-)\s*([+-]?[\d.,].*)", text)
        if not interval:
            return None
        lower, upper = interval.groups()
        li = ui = True
    lower, upper = _number(lower, fmt), _number(upper, fmt)
    if Decimal(lower) > Decimal(upper) or (Decimal(lower) == Decimal(upper) and not (li and ui)):
        raise ValueError("invalid")
    return {
        **result,
        "kind": "interval",
        "status": "valid",
        "lower": lower,
        "upper": upper,
        "lower_inclusive": li,
        "upper_inclusive": ui,
    }


def _parse_numeric(raw: object, text: str, fmt: str, result: _TypedValue) -> _TypedValue:
    try:
        if uncertainty := re.fullmatch(r"(.+?)\s*(?:±|\+/-)\s*(.+)", text):
            parsed = _uncertainty_value(uncertainty, fmt, result)
        elif bound := re.fullmatch(r"(<=|>=|<|>|≤|≥)\s*(.+)", text):
            parsed = _bound_value(bound, fmt, result)
        else:
            parsed = _interval_value(text, fmt, result) or {
                **result,
                "kind": "decimal",
                "status": "valid",
                "decimal": _number(raw, fmt),
            }
    except ValueError as exc:
        return {**result, "status": str(exc)}
    return parsed
