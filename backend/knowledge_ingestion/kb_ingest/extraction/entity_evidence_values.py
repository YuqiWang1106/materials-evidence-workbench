"""Conservative, deterministic checks for quantities in an edited claim.

This is evidence consistency, not scientific validation. Units are never converted;
unknown units retain their spelling. Qualifiers and intervals stay attached to values.
The caller must separately verify the quotation and the edited wording's support.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, DecimalException

_SUPER = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+|\s*[×x*]\s*10\s*\^\s*[+-]?\d+)?")
_ATOM = r"(?:min|rpm|phr|hr|[fpnumkcdMGTμµ]?(?:mol|Hz|Pa|J|W|N|g|m|s|V|A|L)|°[CF]|K|h|份)"
_POWER = r"(?:\s*\^\s*[+-]?\d{1,2}|[23])?"
_FACTOR = _ATOM + _POWER + r"(?![A-Za-z0-9])"
_UNIT = re.compile(
    r"\s*(?P<unit>(?:wt\.?\s*%|vol\.?\s*%|%|质量份)|"
    + _FACTOR
    + r"(?:\s*[/·⋅*]\s*(?:\("
    + _FACTOR
    + r"(?:(?:\s*[·⋅*]\s*|\s+)"
    + _FACTOR
    + r")*\)|"
    + _FACTOR
    + r")|\s+"
    + _FACTOR
    + r")*)"
)
_UNKNOWN_UNIT = re.compile(r"\s*([A-Za-zμµ][A-Za-z0-9μµ/^·⋅*.-]*)(?![A-Za-z0-9])")
_COMPARISON = re.compile(
    (
        "(>=|<=|>|<|≈|~|约|大于|小于|不少于|不低于|不超过|不高于|至少|至多|最多|超过|低于|at least|at "
        "most|more than|less than|approximately|about)\\s*$"
    ),
    re.I,
)
_QUALIFIERS = {
    "大于": ">",
    "小于": "<",
    "不少于": ">=",
    "不低于": ">=",
    "不超过": "<=",
    "不高于": "<=",
    "至少": ">=",
    "至多": "<=",
    "最多": "<=",
    "超过": ">",
    "低于": "<",
    "约": "~",
    "≈": "~",
    "at least": ">=",
    "at most": "<=",
    "more than": ">",
    "less than": "<",
    "approximately": "~",
    "about": "~",
}
_POST_COMPARISON = re.compile(r"\s*(以上|以下|左右)")
_CONNECTORS = {"and", "or", "to", "with", "at", "in", "is", "of", "was", "were"}
_FOLD: dict[str, str | int | None] = {
    "−": "-",
    "–": "-",
    "—": "-",
    "〜": "~",
    "～": "~",
    "≥": ">=",
    "≤": "<=",
    "μ": "µ",
}


@dataclass(frozen=True)
class _Quantity:
    start: int
    end: int
    values: tuple[Decimal, ...]
    unit: str
    kind: str = ""
    comparison: str = ""

    @property
    def key(self) -> tuple[tuple[Decimal, ...], str, str, str]:
        return self.values, self.unit, self.kind, self.comparison


def _normalize(text: str | None) -> str:
    # Preserve powers BEFORE NFKC would flatten m² to m2 and 10⁻³ to 10−3.
    text = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+", lambda match: "^" + match[0].translate(_SUPER), str(text or ""))
    text = unicodedata.normalize("NFKC", text)
    return text.translate(str.maketrans(_FOLD))


def _decimal(raw: str) -> Decimal:
    raw = re.sub(r"\s+", "", raw)
    raw = re.sub(r"[×x*]10\^", "e", raw)
    return Decimal(raw)


def _unit(text: str, position: int) -> tuple[str, int]:
    match = _UNIT.match(text, position)
    if match:
        raw_unit = match["unit"]
        if re.fullmatch(r"(?:wt|vol)\.?\s*%", raw_unit):
            unit = re.sub(r"[.\s]", "", raw_unit)
        else:
            # Space between unit factors is multiplication, not an SI prefix: m s != ms.
            unit = re.sub(r"\s*([/·⋅*^()])\s*", r"\1", raw_unit)
            unit = re.sub(r"\s+", "·", unit).replace("⋅", "·").replace("*", "·")
        unit = re.sub(r"(?<=[A-Za-zµ])[23](?=$|[/·*)])", lambda value: "^" + value[0], unit)
        return unit, match.end()
    match = _UNKNOWN_UNIT.match(text, position)
    if match and match[1] == "x" and _NUMBER.match(text[match.end() :].lstrip()):
        return "", position  # The x in 5 x 10 mm separates dimensions, not a unit named x.
    if match and match[1].casefold() not in _CONNECTORS:
        return "literal:" + match[1], match.end()
    return "", position


def _quantities(text: str) -> list[_Quantity]:
    atoms, consumed = [], 0
    for match in _NUMBER.finditer(text):
        if match.start() < consumed:
            continue
        unit, end = _unit(text, match.end())
        # Standards and formula/sample identifiers are not measured quantities. A unit
        # next to an identifier's digits still takes precedence, so LOI30% is measured.
        prefix = re.search(r"[A-Za-z]+$", text[: match.start()])
        if (
            prefix
            and (not unit or unit.startswith("literal:"))
            and (len(prefix[0]) == 1 or prefix[0].upper() in {"ISO", "ASTM", "UL", "DIN", "GB"})
        ):
            continue
        if prefix and unit.startswith("literal:") and not text[match.end() : end].startswith(" "):
            continue
        comparison = _COMPARISON.search(text[: match.start()])
        start = comparison.start() if comparison else match.start()
        qualifier = _QUALIFIERS.get(comparison[1].casefold(), comparison[1]) if comparison else ""
        postfix = _POST_COMPARISON.match(text, end)
        if postfix:
            qualifier += {"以上": ">=", "以下": "<=", "左右": "~"}[postfix[1]]
            end = postfix.end()
        atoms.append(_Quantity(start, end, (_decimal(match[0]),), unit, comparison=qualifier))
        consumed = end
    return _joined(text, atoms)


def _joined(text: str, atoms: list[_Quantity]) -> list[_Quantity]:
    """Join adjacent values that one separator makes a range, uncertainty, ratio, fraction or dimensions."""
    result = []
    index = 0
    while index < len(atoms):
        first = atoms[index]
        consumed_count = 1
        while index + consumed_count < len(atoms):
            second = atoms[index + consumed_count]
            between = text[first.end : second.start].strip()
            second_values = second.values
            # The scanner treats the minus in 20-30 as a sign; here it is an interval separator.
            if not between and text[second.start : second.end].startswith("-"):
                between, second_values = "-", (-second.values[0],)
            kind = (
                "range"
                if between in ("-", "~", "至", "到", "to")
                else "uncertainty"
                if between == "±"
                else "ratio"
                if between == ":"
                else "fraction"
                if between == "/"
                else "dimensions"
                if between in ("×", "x", "*")
                else None
            )
            if (
                kind
                and not second.comparison
                and (not first.kind or first.kind == kind)
                and (not first.unit or not second.unit or first.unit == second.unit)
            ):
                first = _Quantity(
                    first.start,
                    second.end,
                    (*first.values, *second_values),
                    second.unit or first.unit,
                    kind,
                    first.comparison,
                )
                consumed_count += 1
            else:
                break
        result.append(first)
        index += consumed_count
    return result


def _safe_names(text: str, quote: str, names: Iterable[str | None]) -> tuple[str, str]:
    """Only common literal non-measurement identifiers can be ignored; aliases cannot hide values."""
    for name in sorted({_normalize(value) for value in names if value}, key=len, reverse=True):
        if (
            not re.search(r"[A-Za-z]", name)
            or name not in text
            or name not in quote
            or any(quantity.unit and not quantity.unit.startswith("literal:") for quantity in _quantities(name))
        ):
            continue
        text, quote = text.replace(name, " " * len(name)), quote.replace(name, " " * len(name))
    return text, quote


def value_evidence_issues(
    text: str | None, quote: str | None, *, names: Iterable[str | None] = ()
) -> list[dict[str, str]]:
    """Return unsupported quantities in source order; never borrow a unit from another value.

    A quotation may include extra context. Claim quantities must be a subsequence of its
    complete quantities, so changing a bound, uncertainty or value/unit pairing is rejected.
    """
    try:
        edited, source = _safe_names(_normalize(text), _normalize(quote), names)
        expected, available = _quantities(edited), _quantities(source)
    except (DecimalException, ValueError, OverflowError):
        return [
            {"code": "unsupported_quantity", "message": "数值格式无法可靠核验，请保留原文中的数值写法", "value": ""}
        ]
    cursor, issues = 0, []
    for quantity in expected:
        found = next((index for index in range(cursor, len(available)) if available[index].key == quantity.key), None)
        if found is None:
            value = edited[quantity.start : quantity.end].strip()
            issues.append(
                {
                    "code": "unsupported_quantity",
                    "message": "修改后的数值、单位或限定条件无法由原文引文确认",
                    "value": value,
                }
            )
        else:
            cursor = found + 1
    return issues


def _number_key(value):
    if not value:
        return "0"
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return ("-" if sign else "") + "".join(map(str, digits)) + "e" + str(exponent)


def normalized_evidence_text(text: str | None) -> str:
    """Canonical layout/quantity spelling while retaining words, order, unit scale and powers.

    Used only for support comparison. Never persist this instead of the source evidence.
    """
    text = _normalize(text)
    try:
        quantities = _quantities(text)
    except (DecimalException, ValueError, OverflowError):
        return text  # value_evidence_issues reports and blocks this unsupported spelling.
    parts, position = [], 0
    for quantity in quantities:
        parts.append(re.sub(r"\s+", "", text[position : quantity.start]))
        parts.append(
            "⟦"
            + "|".join(
                [
                    ",".join(_number_key(value) for value in quantity.values),
                    quantity.unit,
                    quantity.kind,
                    quantity.comparison,
                ]
            )
            + "⟧"
        )
        position = quantity.end
    parts.append(re.sub(r"\s+", "", text[position:]))
    # A property-label colon is layout; quantity-ratio colons remain inside quantity tokens.
    return re.sub(r"(?<=[^\W\d_]):(?=⟦)", "", "".join(parts))
