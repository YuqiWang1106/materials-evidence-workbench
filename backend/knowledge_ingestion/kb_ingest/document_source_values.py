"""Exact, reproducible projections from one original document table cell.

The source text remains evidence. A projection only separates a complete
numeric expression from a complete unit written in that same cell. Unknown
syntax fails closed; it does not turn a number into a scientifically verified
measurement or infer a unit from a field name.
"""

from __future__ import annotations

import re
from typing import Any

from .core import fingerprint
from .source_token_boundaries import typed_span_supported
from .typed_values import parse_typed_value

_TEXT_KINDS = frozenset({"text", "category"})
_NUMBER_KINDS = frozenset(
    {"scalar", "number", "interval", "numeric_notation", "ratio", "statistics", "constraint", "integer"}
)


def _same_source_spelling(proposed: Any, source: str) -> bool:
    # Layout whitespace may move between OCR/native readers. No sign, precision,
    # separator, exponent, value or unit is normalized for source admission.
    return isinstance(proposed, str) and " ".join(proposed.split()) == " ".join(source.split())


def _kind_matches(value: str, value_kind: str) -> bool:
    parsed = parse_typed_value(value, expected="number")
    if parsed["status"] != "valid":
        return False
    if value_kind in {"scalar", "integer"}:
        return parsed["kind"] == "decimal"
    if value_kind == "interval":
        return parsed["kind"] == "interval"
    return True


def _unit_expression_is_unambiguous(unit: str) -> bool:
    """Reject a second measurement or prose that lexical unit shape can admit."""
    # Explicit product/quotient/power operators disambiguate whitespace; this
    # syntax-only view never replaces the original unit or its source offsets.
    expression = re.sub(r"\s*([*/⁄÷·⋅×^])\s*", r"\1", unit)
    if re.search(r"\d[.,]\d|\s+[+\-−]\s*\d|\s+\d|[%‰]\d|[*/⁄÷·⋅×][+\-−]?\d", expression):
        return False
    # A spaced bare token might be a second unit, an implicit product or prose.
    # A power attached to it (kg m−3) is an explicit expression; a bare token
    # after % (e.g. "% of") cannot be admitted as a complete unit by syntax.
    parts = expression.split()
    return all(re.search(r"(?:[−\-^]\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)$", part) for part in parts[1:])


def project_source_cell(text: str, value_kind: str, proposed_value=None, proposed_unit=None) -> dict:
    """Project exact raw value/unit spans; reject conflicting model proposals.

    Spans are half-open offsets into ``text``. A unit quoted by a header or
    another source stays as ``proposed_unit`` when this cell has no unit; callers
    must separately verify that external unit evidence and its meaning.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("selected source cell is blank or unreadable")
    if value_kind not in _TEXT_KINDS | _NUMBER_KINDS:
        raise ValueError("unsupported selected cell value kind")
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if proposed_unit is not None and (not isinstance(proposed_unit, str) or not proposed_unit.strip()):
        raise ValueError("proposed unit is not a source string")
    if value_kind in _TEXT_KINDS:
        projection = {"raw_value": text, "raw_unit": proposed_unit, "value_span": [0, len(text)], "unit_span": None}
    else:
        candidates = []
        for value_end in range(start + 1, end + 1):
            if (
                text[value_end - 1].isspace()
                or not typed_span_supported(text, start, value_end, kind="number")
                or not _kind_matches(text[start:value_end], value_kind)
            ):
                continue
            if value_end == end:
                candidates.append(
                    {
                        "raw_value": text[start:end],
                        "raw_unit": proposed_unit,
                        "value_span": [start, end],
                        "unit_span": None,
                    }
                )
                continue
            unit_start = value_end
            while unit_start < end and text[unit_start].isspace():
                unit_start += 1
            unit = text[unit_start:end]
            if (
                unit_start == end
                or not _unit_expression_is_unambiguous(unit)
                or not typed_span_supported(text, unit_start, end, kind="unit")
            ):
                continue
            if proposed_unit is not None and not _same_source_spelling(proposed_unit, unit):
                continue
            candidates.append(
                {
                    "raw_value": text[start:value_end],
                    "raw_unit": unit,
                    "value_span": [start, value_end],
                    "unit_span": [unit_start, end],
                }
            )
        if len(candidates) != 1:
            raise ValueError("selected source cell has no unique complete numeric value/unit projection")
        projection = candidates[0]
    if proposed_value is not None and not _same_source_spelling(proposed_value, projection["raw_value"]):
        raise ValueError("proposed raw value conflicts with selected source cell")
    return projection


def program_cell_source(block_id: str, source_text: str, projection: dict) -> dict:
    """Generate a marker bound to both full original text and its projection."""
    # A unit supplied by a header belongs to separate evidence, not this cell.
    # Its later correction must be checked against that evidence without making
    # the immutable cell marker appear forged.
    intrinsic = {**projection, "raw_unit": projection["raw_unit"] if projection["unit_span"] else None}
    return {
        "kind": "table_cell",
        "block_id": block_id,
        # Historical key name; this hashes the full unmodified cell text.
        "value_sha256": fingerprint(source_text),
        "value_span": projection["value_span"],
        "unit_span": projection["unit_span"],
        "projection_sha256": fingerprint(intrinsic),
    }


def source_cell_provenance_matches(fact: dict, cell: dict) -> bool:
    """Recompute a fact's cell provenance without trusting its stored marker."""
    if not isinstance(cell, dict) or cell.get("kind") != "table_cell" or fact.get("value_cell") != cell.get("block_id"):
        return False
    try:
        projection = project_source_cell(
            cell.get("text"), fact.get("value_kind"), fact.get("raw_value"), fact.get("raw_unit")
        )
    except ValueError:
        return False
    return (
        fact.get("raw_value") == projection["raw_value"]
        and fact.get("raw_unit") == projection["raw_unit"]
        and fact.get("program_value_source") == program_cell_source(cell["block_id"], cell["text"], projection)
        and {"block_id": cell["block_id"], "quote": cell["text"]} in fact.get("evidence", [])
    )
