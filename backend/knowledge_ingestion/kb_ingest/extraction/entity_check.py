"""Keep only entities, attributes, and related links that point at real blocks and listed entities."""

import re
import unicodedata
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from typing import NotRequired, Required, TypedDict

import kb_ingest.core.contract_entity as name_keys
from kb_ingest.core.conservation import require_conserved
from kb_ingest.core.contract_entity import normalize_name, present_aliases, split_parenthetical

SPAN_BLOCKS = 4
SKIP_BLOCKS = 3
_LAYOUT_NOISE = {"header", "footer", "page_header", "page_footer", "page_number", "page_numbering"}
_EMPTY_TEXT_KINDS = {None, "", "text", "paragraph", "native_text"}
_CHINESE_DIGITS = str.maketrans("〇一二两三四五六七八九", "01223456789")


_CELL = re.compile(r"[|｜\t\u3000]| {2,}")


def _cells(text: object) -> list[str]:
    return [_flat(cell) for cell in _CELL.split(str(text or "")) if _flat(cell)]


def table_row_match(block_text: object, quote: object) -> bool:
    """A quote of table cells is in the source when the
    same cells occur, in this order, as whole cells of one row. A column-per-sample table cannot
    be quoted contiguously."""
    wanted = _cells(quote)
    if len(wanted) < 2 or not _CELL.search(str(quote)):
        return False
    for line in str(block_text or "").splitlines():
        row = _cells(line)
        position = 0
        for cell in row:
            if position < len(wanted) and cell == wanted[position]:
                position += 1
        if position == len(wanted) and row and row[0] == wanted[0]:
            return True
    return False


_SENTENCE_MARKS = str.maketrans("", "", ",;!?、。“”‘’\"'「」『』")
_NUMERIC_SEPARATOR = re.compile(r'((?<=\d)[\s,;!?、。“”‘’"\'「」『』]+(?=\d))')


def _flat(text: object) -> str:
    """Ignore layout whitespace, width, case and sentence punctuation when matching quotations.

    Preserve separators between numeric values; only a PDF line break inside a number may disappear.
    Decimal points, signs, ratios and brackets also stay.
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    # A PDF line break inside 13\n0 may be removed; 1 5 or 1、5 must never become 15.
    pieces = _NUMERIC_SEPARATOR.split(normalized)
    flattened = []
    for index, part in enumerate(pieces):
        if index % 2:
            if part.isspace():
                flattened.append("" if "\n" in part or "\r" in part else " ")
            else:
                flattened.append("".join(part.split()))
        else:
            flattened.append("".join(part.translate(_SENTENCE_MARKS).split()))
    return "".join(flattened)


type Blocks[T] = dict[str, T] | Sequence[Mapping[str, object]]
type Placement = tuple[object, list[object]]


class BlockText:
    """Ordered block texts. A quote starts in its block and may continue into the next few blocks."""

    def __init__[T](self, blocks: Blocks[T]) -> None:
        rows: Sequence[Mapping[str, object]] = (
            [{"block_id": key, "text": value} for key, value in blocks.items()] if isinstance(blocks, dict) else blocks
        )
        self.order = [block["block_id"] for block in rows]
        self.position = {block_id: index for index, block_id in enumerate(self.order)}
        self.flat = {block["block_id"]: _flat(block.get("text")) for block in rows}
        self.raw = {block["block_id"]: str(block.get("text") or "") for block in rows}
        self.assets = {block["block_id"]: block.get("asset_ref") for block in rows}
        self.kinds = {block["block_id"]: block.get("kind") for block in rows}

    def _empty_text(self, block_id: object) -> bool:
        # Window construction omits empty text. An unread page/image is not proof of an empty source.
        return not self.raw[block_id].strip() and self.kinds[block_id] in _EMPTY_TEXT_KINDS

    def _following(self, start: int, limit: int, allowed: Collection[object] | None) -> Iterator[object]:
        for item in self.order[start + 1 : start + 1 + limit]:
            if self.assets[item] != self.assets[self.order[start]]:
                break
            if allowed is None or item in allowed:
                yield item
            elif not self._empty_text(item):
                break

    def _covered(self, ids: list[object], start: int, end: int) -> list[object]:
        covered, offset = [], 0
        for item in ids:
            width = len(self.flat[item])
            if offset < end and offset + width > start:
                covered.append(item)
            offset += width
        return covered

    def locate(self, block_id: object, quote: object, allowed: Collection[object] | None = None) -> list[object] | None:
        """Block ids covered by the quote, or None when the quote is not in the source."""
        needle = _flat(quote)
        if not needle or block_id not in self.position or (allowed is not None and block_id not in allowed):
            return None
        start = self.position[block_id]
        first = self.flat[block_id]
        if needle in first or table_row_match(self.raw[block_id], quote):
            return [block_id]
        joined, ids = first, [block_id]
        for item in self._following(start, SPAN_BLOCKS, allowed):
            ids.append(item)
            joined += self.flat[item]
            found = joined.find(needle)
            if found != -1:
                # The quote must begin inside the cited block, not only in a later one.
                return self._covered(ids, found, found + len(needle)) if found < len(first) else None
        return self._locate_skipping(start, needle, allowed)

    def _locate_skipping(
        self, start: int, needle: str, allowed: Collection[object] | None = None
    ) -> list[object] | None:
        """A sentence may cross explicitly typed layout noise, never an unquoted body or sample heading."""
        first = self.flat[self.order[start]]
        head = next((i for i in range(len(first)) if needle.startswith(first[i:])), None)
        if head is None:
            return None
        rest, ids, skipped = needle[len(first) - head :], [self.order[start]], 0
        for item in self._following(start, SPAN_BLOCKS + SKIP_BLOCKS, allowed):
            text = self.flat[item]
            if text and rest.startswith(text):
                rest = rest[len(text) :]
                ids.append(item)
            elif text and text.startswith(rest):
                return [*ids, item]
            else:
                skipped += 1
                if self.kinds[item] not in _LAYOUT_NOISE or skipped > SKIP_BLOCKS:
                    return None
            if not rest:
                return ids
        return None

    def relocate(self, block_id: object, quote: object, block_ids: Sequence[object]) -> Placement | None:
        """(block_id, covered) for a quote cited at a wrong block: the window block nearest the
        cited one that contains it."""
        if len(_flat(quote)) < 2:
            return None
        near = self.position.get(block_id, 0)
        allowed = set(block_ids)
        for candidate in sorted(block_ids, key=lambda item: abs(self.position.get(item, 0) - near)):
            if candidate == block_id:
                continue
            if block_id in self.assets and self.assets.get(candidate) != self.assets[block_id]:
                continue
            covered = self.locate(candidate, quote, allowed)
            if covered:
                return candidate, covered
        return None

    def place(self, block_id: object, quote: object, block_ids: Sequence[object]) -> Placement | None:
        """Where the quote really is: its cited block, or the nearest block of this window that contains it."""
        covered = self.locate(block_id, quote, set(block_ids))
        if covered:
            return block_id, covered
        return self.relocate(block_id, quote, block_ids)

    def find_name(self, names: Sequence[str], block_ids: Sequence[object]) -> tuple[object, str] | None:
        """First block among block_ids whose text contains one of the names, with the matched name."""
        for block_id in block_ids:
            text = self.flat.get(block_id, "")
            for name in names:
                if len(_flat(name)) >= 2 and _flat(name) in text:
                    return block_id, name
        return None


def _loose(text: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


class _Entity[ID, Kind](TypedDict):
    local_id: ID
    type: Kind
    name: str
    aliases: NotRequired[list[str]]
    mentions: NotRequired[list[dict[str, object]]]
    known: NotRequired[bool]


class Entity(_Entity[str, str]):
    """Entity proposed by the extraction schema."""


def _name_pattern(needle: str, sample: bool) -> str | None:
    if len(needle.replace(" ", "")) < 2 and not (sample and re.fullmatch(r"[a-z]", needle)):
        return None
    if sample:
        # Sample labels tolerate PDF spacing, but B must not match B1 or ABS.
        pattern = r"\s*".join(re.escape(char) for char in needle if not char.isspace())
        if needle[0].isascii() and needle[0].isalnum():
            pattern = r"(?<![a-z0-9])" + pattern
        if needle[-1].isascii() and needle[-1].isalnum():
            pattern += r"(?![a-z0-9])"
        return pattern
    if re.search(r"[\u3400-\u9fff]", needle):
        return re.escape(needle)
    return r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"


def named_in[ID, Kind](
    quote: object, entities: Iterable[_Entity[ID, Kind]], owner: object, *, include_formulations: bool = False
) -> list[ID]:
    """List entities named in the quote, excluding matches inside another longer name or Latin word."""
    text = _loose(quote)
    spans: list[tuple[int, int, ID]] = []
    for entity in entities:
        if entity["local_id"] == owner:
            continue
        sample = entity["type"] == "formulation"
        if sample and not include_formulations:
            continue
        for name in [entity["name"], *entity.get("aliases", [])]:
            pattern = _name_pattern(_loose(name), sample)
            if pattern:
                spans.extend((match.start(), match.end(), entity["local_id"]) for match in re.finditer(pattern, text))
    found = []
    for start, end, local_id in spans:
        inside = any(s <= start and end <= e and (e - s) > (end - start) and other != local_id for s, e, other in spans)
        if not inside and local_id not in found:
            found.append(local_id)
    return found


def quote_in_block[T](block_id: object, quote: object, blocks: Blocks[T]) -> bool:
    return BlockText(blocks).locate(block_id, quote) is not None


type _Record = Mapping[str, object]


def _values(value: object) -> Iterable[object]:
    if isinstance(value, Iterable):
        return value
    msg = "Expected iterable graph values"
    raise TypeError(msg)


def _records(value: object) -> Iterator[_Record]:
    for row in _values(value):
        if not isinstance(row, Mapping):
            msg = "Expected a graph record"
            raise TypeError(msg)
        yield row


def _name_and_aliases(entity: _Record) -> tuple[str, list[str]]:
    name, extra = split_parenthetical(entity.get("name") or "")
    return name, present_aliases(name, [*extra, *_values(entity.get("aliases") or [])])


def entity_keys(entity: _Record) -> set[str]:
    """Name keys used to merge temporary entities inside one paper."""
    keys = {normalize_name(entity["name"])}
    if entity["type"] != "formulation":
        keys |= {normalize_name(alias) for alias in _values(entity.get("aliases") or [])}
    return {key for key in keys if key}


class AttributeProposal(TypedDict, total=False):
    local_id: str
    section: str
    text: str
    block_id: object
    quote: str
    covered: list[object] | None
    refs: list[str]


class GraphProposal(TypedDict, total=False):
    entities: list[Entity]
    attributes: list[AttributeProposal]
    related: list[dict[str, object]]


class _Dropped(TypedDict, total=False):
    kind: Required[str]
    reason: Required[str]
    local_id: object
    name: object
    section: object
    text: object
    a: object
    b: object
    numbers: list[str]
    block_id: object
    quote: object


def _attribute_drop(attribute: _Record, section: object, reason: str) -> _Dropped:
    return {
        "kind": "attribute",
        "local_id": attribute.get("local_id"),
        "section": section,
        "text": attribute.get("text"),
        "reason": reason,
        "block_id": attribute.get("block_id"),
        "quote": attribute.get("quote"),
    }


class _CheckedGraph(TypedDict):
    entities: list[_Entity[object, object]]
    attributes: list[dict[str, object]]
    related: list[dict[str, object]]
    dropped: list[_Dropped]


class _Window:
    def __init__(self, text: BlockText, scope: list[object]) -> None:
        self.text = text
        self.scope = scope

    def mentions(self, entity: _Record, names: list[str]) -> list[dict[str, object]]:
        mentions: list[dict[str, object]] = []
        for mention in _records(entity.get("mentions", [])):
            found = self.text.place(mention.get("block_id"), mention.get("quote"), self.scope)
            if found:
                row = {"block_id": found[0], "quote": mention["quote"]}
                if row not in mentions:
                    mentions.append(row)
        if not mentions:
            match = self.text.find_name(names, self.scope)
            if match:
                mentions.append({"block_id": match[0], "quote": match[1]})
        return mentions

    def attribute_quote(self, attribute: _Record) -> tuple[object, tuple[object, Sequence[object]] | None]:
        quote = attribute.get("quote")
        covered = attribute.get("covered")
        if covered is not None:
            # Cited segment IDs already establish the quote; all segments must still be in scope.
            if not covered:
                return quote, None
            if not isinstance(covered, Sequence):
                msg = "Expected ordered citation block IDs"
                raise TypeError(msg)
            return quote, (covered[0], covered) if set(covered) <= set(self.scope) else None
        found = self.text.place(attribute.get("block_id"), quote, self.scope)
        if found is None and attribute.get("text"):
            found = self.text.place(attribute.get("block_id"), attribute["text"], self.scope)
            if found:
                quote = attribute["text"]
        return quote, found


def _source_values(quote: object, names: list[str]) -> set[str]:
    # Only normalize the matched quotation, never search surrounding blocks for missing numbers.
    quoted = unicodedata.normalize("NFKC", str(quote))
    line_joined = re.sub(r"(?<=\d)[ \t]*[\r\n]+[ \t]*(?=\d)", "", quoted)
    # A PDF may space a decimal point off a zero integer part (0 .2-1份 for 0.2-1份).
    spaced_point = re.sub(r"(?<![\d.])0\s+\.(?=\d)", "0.", quoted)
    nv = name_keys.numbers
    return (
        nv(quoted, names)
        | nv(line_joined, names)
        | nv(spaced_point, names)
        | nv(quoted.translate(_CHINESE_DIGITS), [n.translate(_CHINESE_DIGITS) for n in names])
    )


class _GraphCheck:
    def __init__(self, window: _Window) -> None:
        self.window = window
        self.result: _CheckedGraph = {"entities": [], "attributes": [], "related": [], "dropped": []}
        self.ids: set[object] = set()

    def add_entity(self, entity: _Record) -> None:
        name, aliases = _name_and_aliases(entity)
        mentions = self.window.mentions(entity, [name, *aliases])
        if not name or entity.get("local_id") in self.ids or (not mentions and not entity.get("known")):
            self.result["dropped"].append(
                {
                    "kind": "entity",
                    "local_id": entity.get("local_id"),
                    "name": entity.get("name"),
                    "reason": "mention is not in the block",
                }
            )
            return
        row: _Entity[object, object] = {
            "local_id": entity["local_id"],
            "type": entity["type"],
            "name": name,
            "aliases": aliases,
            "mentions": mentions,
        }
        if not mentions:
            # Earlier windows supply the mentions; keep this entity to carry attributes and links.
            row["known"] = True
        self.result["entities"].append(row)
        self.ids.add(row["local_id"])

    def add_link(self, link: _Record) -> None:
        a, b = link.get("a"), link.get("b")
        if a == b or a not in self.ids or b not in self.ids:
            self.result["dropped"].append(
                {"kind": "related", "a": a, "b": b, "reason": "ends must be two different listed entities"}
            )
            return
        found = self.window.text.place(link.get("block_id"), link.get("quote"), self.window.scope)
        if found is None:
            self.result["dropped"].append({"kind": "related", "a": a, "b": b, "reason": "quote is not in the block"})
            return
        self.result["related"].append(
            {"a": a, "b": b, "block_id": found[0], "quote": link["quote"], "block_ids": found[1]}
        )

    def add_attributes(self, attributes: Iterable[_Record]) -> None:
        entities = self.result["entities"]
        names = [name for entity in entities for name in [entity["name"], *entity["aliases"]]]
        formulations = [entity for entity in entities if entity["type"] == "formulation"]
        formulation_ids = {entity["local_id"] for entity in formulations}
        for attribute in attributes:
            self._add_attribute(attribute, names, formulations, formulation_ids)

    def _add_attribute(
        self,
        attribute: _Record,
        names: list[str],
        formulations: list[_Entity[object, object]],
        formulation_ids: set[object],
    ) -> None:
        owner = attribute.get("local_id")
        dropped = self.result["dropped"]
        if owner not in self.ids:
            dropped.append(_attribute_drop(attribute, attribute.get("section"), "entity is not in this paper"))
            return
        quote, found = self.window.attribute_quote(attribute)
        if found is None:
            dropped.append(_attribute_drop(attribute, attribute.get("section"), "quote is not in the block"))
            return
        section = " ".join(str(attribute.get("section") or "").split()) or "说明"
        refs = list(
            dict.fromkeys(ref for ref in _values(attribute.get("refs") or []) if ref in self.ids and ref != owner)
        )
        values = name_keys.numbers(attribute.get("text"), names)
        missing = values - _source_values(quote, names)
        if missing:
            drop = _attribute_drop(attribute, section, "number is not in the source")
            drop["numbers"] = sorted(missing)
            dropped.append(drop)
            return
        if values and owner in formulation_ids:
            quoted_samples = named_in(quote, formulations, None, include_formulations=True)
            if quoted_samples and owner not in quoted_samples:
                dropped.append(_attribute_drop(attribute, section, "quote names another formulation"))
                return
        location = {"block_id": found[0], "quote": quote, "block_ids": found[1]}
        self.result["attributes"].append(
            {
                "local_id": owner,
                "section": section,
                "text": attribute.get("text") or quote,
                **location,
                "refs": refs,
                "numbers": "in_quote" if values else "none",
            }
        )
        self.result["related"].extend({"a": owner, "b": ref, **location, "derived_from": "attribute"} for ref in refs)


def check_graph[T](
    graph: _Record, blocks: Blocks[T] | BlockText, *, window_block_ids: Iterable[object] | None = None
) -> _CheckedGraph:
    """Check entities first so attributes and links can reference only the kept entities in this window."""
    text = blocks if isinstance(blocks, BlockText) else BlockText(blocks)
    check = _GraphCheck(_Window(text, list(text.order if window_block_ids is None else window_block_ids)))
    proposed = [list(_records(graph.get(key, []))) for key in ("entities", "attributes", "related")]
    for entity in proposed[0]:
        check.add_entity(entity)
    check.add_attributes(proposed[1])
    for link in proposed[2]:
        check.add_link(link)
    result = check.result
    # Links derived from attribute refs were never proposed; only proposed links count as kept proposals.
    kept = [len(result["entities"]), len(result["attributes"]), sum("derived_from" not in x for x in result["related"])]
    for kind, rows, stayed in zip(("entity", "attribute", "related"), proposed, kept, strict=True):
        dropped = sum(1 for item in result["dropped"] if item["kind"] == kind)
        require_conserved("check " + kind, entered=len(rows), kept=stayed, set_aside=dropped)
    return result
