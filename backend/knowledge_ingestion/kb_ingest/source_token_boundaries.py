"""Conservative lexical boundaries at original source character positions.

This gate preserves a selected numeric expression or unit token; it does not
prove its scientific meaning, attribution, or dimensional correctness. Unknown
numeric notation and ambiguous unit boundaries return False for caller review.
No source spelling, magnitude, unit, or whitespace inside a number is converted.
"""

from __future__ import annotations

import re
import unicodedata

from .typed_values import parse_typed_value

_POWERS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺₀₁₂₃₄₅₆₇₈₉"
_SIGNS = "+-−–—±"
_PRODUCTS = "*/⁄÷·⋅×"
_NUMERIC_JOIN = _SIGNS + _PRODUCTS + "^~～〜至到<>≤≥"
_UNIT_JOIN = _SIGNS + _PRODUCTS + "^_."
_OPEN = "([{（［"
_CLOSE = ")]}）］"


def _han_letter(char: str) -> bool:
    return unicodedata.name(char, "").startswith(("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-"))


def _unit_letter(char: str, *, allow_han=False) -> bool:
    if char in "µμ°℃℉%‰ΩΩÅÅ":
        return True
    name = unicodedata.name(char, "")
    return char.isalpha() and ("LATIN" in name or "GREEK" in name or (allow_han and _han_letter(char)))


def _numeric_boundary(source: str, start: int, end: int) -> bool:
    before, after = source[:start], source[end:]
    left, right = before.rstrip(), after.lstrip()
    if before and (before[-1].isdigit() or before[-1] in "._" or _unit_letter(before[-1])):
        return False
    # A following unit is allowed (12MPa, 20℃). e/E followed by a numeric
    # exponent, or a dangling e/E, remains part of the numeric expression.
    if after and (after[0].isdigit() or after[0] in "._" + _POWERS):
        return False
    if re.match(r"[eE](?:[+\-−]?\d|[+\-−]?$)", right):
        return False
    if (
        left
        and (left[-1].isdigit() or left[-1] in _NUMERIC_JOIN + _POWERS)
        or right
        and (right[0].isdigit() or right[0] in _NUMERIC_JOIN + _POWERS)
    ):
        return False
    if re.search(r"[<>]=\s*$", before):
        return False
    if re.search(r"\d\s*[:：]\s*$", before) or re.match(r"[:：]\s*\d", right):
        return False
    # Commas and periods can be prose punctuation or numeric separators. A
    # neighboring digit makes the separator unsafe; a directly cut decimal dot
    # is always unsafe, including its retained trailing precision marker.
    if left.endswith((".", ",")) and re.search(r"\d[.,]$", left):
        return False
    if after.startswith(".") or re.match(r",\s*\d", right):
        return False
    if re.search(r"\d\s+to\s*$", before, flags=re.IGNORECASE):
        return False
    if re.match(r"to\s*[+\-−]?\d", right, flags=re.IGNORECASE):
        return False
    if re.match(r"[xX]\s*10(?:\b|[\^" + _POWERS + "])", right):
        return False
    outer_left, outer_right = left, right
    while outer_left and outer_left[-1] in _OPEN:
        outer_left = outer_left[:-1].rstrip()
    while outer_right and outer_right[0] in _CLOSE:
        outer_right = outer_right[1:].lstrip()
    if (
        outer_left != left
        and outer_left
        and outer_left[-1] in _NUMERIC_JOIN + _POWERS
        or outer_right != right
        and outer_right
        and outer_right[0] in _NUMERIC_JOIN + _POWERS
    ):
        return False
    return True


def _unit_shape(value: str, *, allow_han=False) -> bool:
    if not any(_unit_letter(c, allow_han=allow_han) for c in value):
        return False
    if any(
        not (
            c.isspace()
            or c.isdigit()
            or _unit_letter(c, allow_han=allow_han)
            or c in _UNIT_JOIN + _POWERS + _OPEN + _CLOSE
        )
        for c in value
    ):
        return False
    if value[0] in _UNIT_JOIN + _POWERS or value[-1] in _UNIT_JOIN:
        return False
    stack = []
    for char in value:
        if char in _OPEN:
            stack.append(_CLOSE[_OPEN.index(char)])
        elif char in _CLOSE:
            if not stack or stack.pop() != char:
                return False
    return not stack


def _measurement_prefix(source: str, end: int) -> bool:
    """Use the full numeric grammar, never reinterpret a suffix as a scalar."""
    while end and source[end - 1].isspace():
        end -= 1
    start = end
    numeric_chars = "0123456789.,eEtoTOxX" + _SIGNS + _PRODUCTS + "^<>=≤≥" + _OPEN + _CLOSE + _POWERS
    while start and (source[start - 1].isspace() or source[start - 1] in numeric_chars):
        start -= 1
    # The lexical window is only a candidate search bound. Admission still
    # requires the existing typed parser AND the original numeric boundaries,
    # so the final 20 in 10–20 or the 1 in m-1 cannot certify itself.
    for offset in range(start, end):
        if source[offset].isdigit() or source[offset] in ".+-−<>=≤≥[(":
            if typed_span_supported(source, offset, end, kind="number"):
                return True
    return False


def _possible_prose_continuation(after: str, *, allow_han=False) -> bool:
    """Lexical ambiguity only: no unit vocabulary or prose-word guessing.

    Whitespace before a bare word could be prose or an implicit unit product.
    An attached sentence period followed by whitespace has the same ambiguity.
    Explicit powers/products attached to that word remain definite truncation.
    """
    if after and after[0].isspace():
        right = after.lstrip()
    elif len(after) > 1 and after[0] == "." and after[1].isspace():
        right = after[1:].lstrip()
    else:
        return False
    end = 0
    while end < len(right) and right[end].isalpha() and _unit_letter(right[end], allow_han=allow_han):
        end += 1
    if not end:
        return False
    tail = right[end:]
    if tail and (tail[0].isdigit() or tail[0] in _POWERS or _unit_letter(tail[0], allow_han=allow_han)):
        return False
    suffix = tail.lstrip()
    if suffix and suffix[0] in _UNIT_JOIN + _POWERS:
        # A period only stays ambiguous at a sentence boundary, never N.m.
        if suffix[0] != "." or (len(suffix) > 1 and not suffix[1].isspace()):
            return False
    return True


def _unit_boundary(source: str, start: int, end: int, *, allow_prose_context=False, allow_han=False) -> bool:
    before, after = source[:start], source[end:]
    left, right = before.rstrip(), after.lstrip()
    # Strip only source wrappers around the selected unit. A product, quotient
    # or exponent outside them still belongs to the same unit expression.
    left_wrapped = bool(left and left[-1] in _OPEN)
    right_wrapped = bool(right and right[0] in _CLOSE)
    if left_wrapped:
        while left and left[-1] in _OPEN:
            left = left[:-1].rstrip()
    if right_wrapped:
        while right and right[0] in _CLOSE:
            right = right[1:].lstrip()
    if left and left[-1] in _UNIT_JOIN + _POWERS:
        return False
    terminal_period = right == "."
    ambiguous_prose = allow_prose_context is True and _possible_prose_continuation(after, allow_han=allow_han)
    if (
        right
        and not terminal_period
        and not ambiguous_prose
        and (right[0] in _UNIT_JOIN + _POWERS or right[0].isdigit())
    ):
        return False
    if right and not ambiguous_prose and (_unit_letter(right[0], allow_han=allow_han) or right[0] in _OPEN):
        return False
    if left and _unit_letter(left[-1], allow_han=allow_han) and not left_wrapped:
        return False
    if left_wrapped:
        wrapper = before.rstrip()
        while wrapper and wrapper[-1] in _OPEN:
            wrapper = wrapper[:-1]
        if wrapper and _unit_letter(wrapper[-1], allow_han=allow_han):
            return False
    if left and (left[-1].isdigit() or left[-1] in _CLOSE) and not left_wrapped:
        # Complete ranges, bounds and uncertainty values can precede a unit;
        # selecting only the last decimal would lose the source grammar.
        if not _measurement_prefix(source, start):
            return False
    return True


def typed_span_supported(source: str, start: int, end: int, *, kind: str) -> bool:
    """Check an exact half-open source span; unsupported/ambiguous returns False.

    ``number`` uses the existing source-format typed parser (no locale guessing).
    ``unit`` checks lexical completeness, including prefix/power/product edges,
    without claiming the unit name or expression is scientifically recognized.
    A bare adjacent ASCII word is ambiguous with an implicit product: e.g.
    selecting MPa from "MPa were recorded" is deliberately unsupported. Retain
    that source and report an unresolved boundary; do not guess prose stopwords.
    Parenthesized units, Chinese prose and unambiguous terminal punctuation are
    supported. This lexical gate is not a general mathematical/unit parser.
    """
    if (
        not isinstance(source, str)
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(source)
        or kind not in ("number", "unit")
    ):
        return False
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    if start == end:
        return False
    value = source[start:end]
    if kind == "number":
        return parse_typed_value(value, expected="number")["status"] == "valid" and _numeric_boundary(
            source, start, end
        )
    return _unit_shape(value) and _unit_boundary(source, start, end)


def _literal_matches(source: str, literal: str):
    # Match layout whitespace with original character offsets. Never delete
    # whitespace: '1 2' must not become '12', nor can 'mm min' become 'mmmin'.
    # The existing review typography equivalence for minus glyphs is retained;
    # it changes matching only, never the source text or persisted raw value.
    if not isinstance(source, str) or not isinstance(literal, str) or not literal.strip():
        return
    pattern = "".join(
        r"\s+" if part.isspace() else "".join(r"[-−–]" if char in "-−–" else re.escape(char) for char in part)
        for part in re.split(r"(\s+)", literal)
        if part
    )
    yield from re.finditer("(?=(" + pattern + "))", source)


def typed_quote_supported(source: str, quote: str, *, kind: str) -> bool:
    """Whether a literal quote has a complete typed occurrence in its source."""
    return any(
        typed_span_supported(source, match.start(1), match.end(1), kind=kind)
        for match in _literal_matches(source, quote)
    )


def unit_evidence_status(source: str, quote: str, literal: str) -> str:
    """Classify a quoted unit without guessing whether adjacent words are prose.

    ``ambiguous`` needs independent semantic confirmation of the complete unit
    for this exact fact. ``invalid`` cannot be overridden by that confirmation.
    A valid occurrence outside the selected source quote never rescues a cut.
    Whole Han spellings also need that explicit review, with Han characters
    participating in word boundaries; this does not segment Chinese prose or
    prove a spelling is a scientifically recognized unit.
    """
    status = "invalid"
    for evidence in _literal_matches(source, quote):
        for match in _literal_matches(evidence.group(1), literal):
            start, end = evidence.start(1) + match.start(1), evidence.start(1) + match.end(1)
            if typed_span_supported(source, start, end, kind="unit"):
                return "complete"
            while start < end and source[start].isspace():
                start += 1
            while end > start and source[end - 1].isspace():
                end -= 1
            allow_han = any(_han_letter(c) for c in source[start:end])
            if (
                start < end
                and _unit_shape(source[start:end], allow_han=allow_han)
                and _unit_boundary(source, start, end, allow_prose_context=True, allow_han=allow_han)
            ):
                status = "ambiguous"
    return status


def typed_evidence_supported(source: str, quote: str, literal: str, *, kind: str, allow_prose_context=False) -> bool:
    """Bind a typed literal to an occurrence inside the evidence's source quote.

    A complete occurrence elsewhere in the block cannot rescue a fragment in
    this quote. With repeated identical quotes and no offset, at least one exact
    source placement must support the full expression; identity is reviewed by
    the caller. Layout whitespace and minus typography are flexible, with
    offsets preserved; numeric precision and unit spellings are not converted.
    The optional unit-only path allows lexical prose ambiguity solely after an
    independent caller confirms the full unit. It never relaxes numeric checks.
    """
    if kind == "unit" and allow_prose_context is True:
        return unit_evidence_status(source, quote, literal) in ("complete", "ambiguous")
    for evidence in _literal_matches(source, quote):
        for match in _literal_matches(evidence.group(1), literal):
            if typed_span_supported(
                source, evidence.start(1) + match.start(1), evidence.start(1) + match.end(1), kind=kind
            ):
                return True
    return False


def reviewed_table_header_unit_supported(source: str, quote: str, literal: str, label: str) -> bool:
    """Check a complete ``metric / unit`` cell after explicit semantic review.

    The caller must establish a table-cell anchor and an independently reviewed
    metric label/unit for the exact fact. Only the first slash can be a header
    separator: the label must cover its whole prefix, and the literal must cover
    its whole suffix. Unit operators in the label are never reinterpreted, nor
    can a partial quote manufacture a boundary. This is lexical completeness,
    not recognition of a physical unit or a metric vocabulary.
    """
    if not all(isinstance(x, str) for x in (source, quote, literal, label)):
        return False
    header, label = source.strip(), label.strip()
    if not label or not any(c.isalpha() for c in label) or any(c in _UNIT_JOIN + _POWERS for c in label):
        return False
    prefix, separator, suffix = header.partition("/")
    if not separator or prefix.strip() != label:
        return False
    if not any(
        not source[: m.start(1)].strip() and not source[m.end(1) :].strip() for m in _literal_matches(source, quote)
    ):
        return False
    unit = suffix.strip()
    if not unit or not any(m.start(1) == 0 and m.end(1) == len(unit) for m in _literal_matches(unit, literal)):
        return False
    allow_han = any(_han_letter(c) for c in unit)
    return _unit_shape(unit, allow_han=allow_han) and _unit_boundary(unit, 0, len(unit), allow_han=allow_han)
