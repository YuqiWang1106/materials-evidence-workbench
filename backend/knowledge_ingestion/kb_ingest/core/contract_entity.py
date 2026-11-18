"""Bottom-layer entity persistence contract: name keys, section vocabulary, readings.

Repositories and extraction both import these primitives from core. The module is
stdlib-only and must not import extraction, storage, or llm.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from functools import lru_cache

DESCRIPTION_SECTION = "说明"
COMPOSITION_SECTION = "原料"
PROCESS_SECTION = "制备过程"
PROPERTY_SECTION = "性能"
METHOD_SECTION = "测试方法"
CONCLUSION_SECTION = "结论"
EXPLANATION_SECTION = "解释"
# Author statements compared across papers (同一结论也见于).
CLAIM_SECTIONS = (CONCLUSION_SECTION, EXPLANATION_SECTION)
FORMULATION_SECTIONS = (
    COMPOSITION_SECTION,
    PROCESS_SECTION,
    PROPERTY_SECTION,
    METHOD_SECTION,
    CONCLUSION_SECTION,
    EXPLANATION_SECTION,
)
_PAREN = re.compile(r"^(.+?)\s*[\(（]\s*([^()（）]+?)\s*[\)）]\s*$")
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "·": "", "•": ""})


def normalize_name(name: object) -> str:
    """One key for names that differ only by width, case, or spacing: 「样品 A」, 「样品A」, 「ＡＰＰ」."""
    text = unicodedata.normalize("NFKC", str(name)).casefold().translate(_DASHES)
    return "".join(text.split())


_GRADE = re.compile(r"^(型号|牌号|规格|等级|grade|model|type)\s*[:：]?", re.I)


_EVALUATION = re.compile(r"(较好|很好|良好|优异|优良|较高|较低|显著|好$)")


def present_aliases(name: object, aliases: Iterable[object] | None) -> list[str]:
    """Keep an abbreviation by itself. 「极限氧指数(LOI)」 stored beside 极限氧指数 becomes LOI. A phrase that
    judges (阻燃性能好) is a statement about the entity, not another name for it."""
    base = normalize_name(name)
    result = []
    seen = {base}
    for alias in aliases or []:
        text = " ".join(str(alias).split())
        if _EVALUATION.search(text) or _GRADE.match(text):
            continue
        match = _PAREN.match(text)
        if match and normalize_name(match.group(1)) == base:
            text = match.group(2).strip()
        key = normalize_name(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


_UNIT = (
    r"(?:MPa|GPa|kPa|Pa|[kK]J/m[²2]|J/m|kW/m[²2]|MJ/m[²2]|W/\(?m·?K\)?|g/cm[³3]|g/10\s?min|wt\s?%|%|％|℃|°C|mm|min|s)"
)
_TRAILING_UNIT = re.compile(r"[\s,，/]*[(（]?\s*" + _UNIT + r"\s*[)）]?\s*$")
_TEST_MODE = re.compile(r"^(Izod|Charpy|IZOD|CHARPY|悬臂梁|简支梁)\s*")


def bare_property_name(name: object) -> tuple[str, str | None]:
    """A property's own name, without its unit or test mode: 「缺口冲击强度 KJ/m²」 and 「Izod缺口冲击强度」 are both
    缺口冲击强度. Returns (name, alias): the alias keeps a test-mode spelling (Izod缺口冲击强度), a unit is dropped."""
    text = " ".join(str(name).split())
    bare = text
    while True:
        stripped = _TRAILING_UNIT.sub("", bare).strip()
        if stripped == bare or not stripped:
            break
        bare = stripped
    alias = None
    mode = _TEST_MODE.match(bare)
    if mode and bare[mode.end() :].strip():
        alias, bare = bare, bare[mode.end() :].strip()
    return bare or text, alias


def split_parenthetical(name: object) -> tuple[str, list[str]]:
    """「聚磷酸铵(APP)」 -> ('聚磷酸铵', ['APP']). Other names are returned unchanged; a grade in brackets
    (「ABS（型号:750A）」) is part of the name, not another name for ABS."""
    text = " ".join(str(name).split())
    match = _PAREN.match(text)
    if match and _GRADE.match(match.group(2)):
        return text, []
    if match and len(match.group(2)) < len(match.group(1)) + 8:
        return match.group(1).strip(), [match.group(2).strip()]
    return text, []


def _loose(text: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def contains_name(short: str, long: str) -> bool:
    """CJK names may sit inside a longer name; Latin abbreviations must stand as a word (PP is not inside APP)."""
    if len(normalize_name(short)) < 2 or normalize_name(short) == normalize_name(long):
        return False
    if re.search(r"[㐀-鿿]", short):
        return normalize_name(short) in normalize_name(long)
    return re.search(r"(?<![a-z0-9])" + re.escape(_loose(short)) + r"(?![a-z0-9])", _loose(long)) is not None


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


# Sample markers (3#), local ids (f3), and references to a figure, table or equation (图5b, 表2, Table 2,
# 式(1)). 图 and 表 inside another word (列表 3 项, 配图 2 张) do not start a reference.
_NOT_VALUES = re.compile(
    r"\d+(?:\.\d+)?\s*[#号]|(?<![a-z0-9])[a-z]\d+(?![a-z0-9.])"
    r"|(?:(?<![列报配插代发外仪])[图表]|公式|(?<![a-z])(?:fig(?:ure|\.)?|table|eq(?:uation|\.)?|scheme))"
    r"\s*\d+[a-z]?(?![a-z0-9.])"
    r"|式\s*\(\s*\d+\s*\)"
)


_WORDLESS = re.compile(r"[^a-z\u3400-\u9fff]+")


def _name_pattern(name: str) -> str | None:
    """How one entity name is cut out of a line before its values are read.

    A name that is only a number (sample 5) is never cut: it cannot be told from a value, and cutting it
    erased the 5 in 53.59. A name made of digits and marks only (the ratio 78/22 naming a sample) is cut as a
    whole, never out of a longer number (178/22, 78/22.5). A name with letters is cut wherever it stands: a
    grade is often printed against its amount (B3387份 is B338, 7份).
    """
    if _NUMBER.fullmatch(name):
        return None
    if _WORDLESS.fullmatch(name):
        return r"(?<![0-9.])" + re.escape(name) + r"(?![0-9]|\.\d)"
    return re.escape(name)


@lru_cache(maxsize=64)
def _names_pattern(names: frozenset[str]) -> re.Pattern[str] | None:
    # Longest first, so a name is cut before any shorter name it contains.
    parts = [part for name in sorted(names, key=lambda item: (-len(item), item)) if (part := _name_pattern(name))]
    return re.compile("|".join(parts)) if parts else None


def numbers(text: object, names: Iterable[object] = ()) -> set[str]:
    """Values written in the text. Sample markers (3#), local ids (f3) and digits inside entity
    names (Ti3C2Tx, 0#样品) are not values."""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    pattern = _names_pattern(frozenset(unicodedata.normalize("NFKC", str(item)).casefold() for item in names if item))
    if pattern is not None:
        text = pattern.sub(" ", text)
    return set(_NUMBER.findall(_NOT_VALUES.sub(" ", text)))


_BETWEEN_DIGITS = re.compile(r"(?<=\d)\s+(?=\d)")
_CLOSING_MARKS = "。.;；,，、"


def reading_key(text: object) -> str:
    """One key for lines that say the same words: width, case, spacing and a closing full stop aside.

    A space between two numbers stays (1 5 is not 15). Lines that only share their values or most of
    their wording have different keys and stay separate lines.
    """
    spaced = _BETWEEN_DIGITS.sub("\0", unicodedata.normalize("NFKC", str(text or "")))
    return normalize_name(spaced).replace("\0", " ").rstrip(_CLOSING_MARKS)


def same_reading(first: object, second: object) -> bool:
    return reading_key(first) == reading_key(second)


def cite(row: Mapping[str, object]) -> dict[str, object]:
    """The place of an attribute, as one cite of another attribute."""
    return {
        key: row[key]
        for key in ("block_id", "quote", "block_ids", "asset_ref", "page", "source_id", "highlight_regions")
        if row.get(key) not in (None, [], "")
    }
