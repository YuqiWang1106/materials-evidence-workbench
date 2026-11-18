"""Source-preserving interpretation of simple reported measurements.

The raw cell never changes. An embedded unit is accepted only after a plain
numeric prefix and a whitespace boundary; its exact source span is retained.
"""

import re

from kb_ingest.typed_values import _numeric_notation, normalize_unit, parse_typed_value

_INLINE = re.compile(r"^\s*([+\-−]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)\s+([^\d\s].*?)\s*$")
# Finite scientific unit grammar, not a suffix catch-all. Unknown unit labels
# remain possible through explicit unit-source selection, never inferred here.
_SYMBOL = (
    "(?:[yzafpnumkMGTPEZYµμ]?(?:mol|kat|Wb|Hz|Pa|Bq|Gy|Sv|lm|lx|cd|eV|m|g|s|A|K|N|J|W|C|V|F|Ω|S|T|H|L|l)|kg"
    "|min|h|rpm|wt%|vol%|phr|%|°C|°F|℃)"
)
_ATOM = _SYMBOL + r"(?:\^?[+−-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+)?"
_UNIT = re.compile(_ATOM + r"(?:(?:[*/·]| +)" + _ATOM + r")*")


def inline_unit_supported(token):
    return normalize_unit(token).get("status") == "recognized" or bool(_UNIT.fullmatch(token))


_ALLOWED_MISSING = {"blank", "not_tested", "not_detected", "not_applicable", "not_reported", "not_available", "nan"}


def simple_measurement(cell, expected, unit=None, *, number_format="source"):
    raw = cell.get("raw_value")
    numeric = parse_typed_value(cell, expected="number", number_format=number_format)
    if numeric["kind"] == "missing":
        return numeric, None, None if numeric["status"] in _ALLOWED_MISSING else "unsupported_result"
    if numeric["kind"] == "formula_cached" or numeric["kind"] in (
        "interval",
        "bound",
        "uncertainty",
        "date",
        "time",
        "datetime",
        "duration",
    ):
        return numeric, None, "unsupported_result"
    inline = None
    if isinstance(raw, str) and (
        any(op in raw for op in ("±", "+/-", "≤", "≥", "<", ">"))
        or re.match(r"^\s*[+−-]?[\d.]+\s*(?:–|—|\bto\b|-)\s*[+−-]?[\d.]", raw)
    ):
        return numeric, None, "unsupported_result"
    if expected == "number":
        typed = numeric
        if typed.get("kind") != "decimal" and isinstance(raw, str):
            match = _INLINE.fullmatch(raw)
            if match:
                # Numeric punctuation/range operators cannot become a unit.
                token = match[2]
                if inline_unit_supported(token):
                    value = parse_typed_value(match[1], expected="number", number_format=number_format)
                    if value.get("kind") == "decimal" and value.get("status") == "valid":
                        typed = {**value, "raw": raw}
                        inline = {"raw": token, "span": list(match.span(2))}
        if typed.get("kind") != "decimal" or typed.get("status") != "valid":
            return typed, inline, "unsupported_result"
    elif expected == "category":
        if isinstance(raw, str):
            numeric_prefix = re.match(r"^\s*[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+−-]?\d+)?", raw)
            if numeric_prefix:
                tail = raw[numeric_prefix.end() :]
                # Preserve compact source grades such as 5VA; punctuation,
                # another expression, or a physical unit is not a grade.
                if tail and (tail[0].isspace() or tail[0] in ".,+−-–—*/" or inline_unit_supported(tail)):
                    return numeric, None, "unsupported_result"
        if isinstance(raw, str) and _numeric_notation(raw.strip()) and numeric.get("status") != "valid":
            return numeric, None, "unsupported_result"
        typed = parse_typed_value(cell, expected="category", number_format=number_format)
        if typed.get("status") != "valid" or typed.get("kind") != "category":
            return typed, None, "unsupported_result"
    else:
        return numeric, None, "result_type_required"
    if inline and unit not in (None, ""):
        left, right = normalize_unit(inline["raw"]), normalize_unit(unit)
        if (left.get("canonical") or left["raw"]) != (right.get("canonical") or right["raw"]):
            return typed, inline, "unit_conflict"
    return typed, inline, None
