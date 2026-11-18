"""Read-only display locations for original candidate evidence.

Locations establish source position only: they neither approve a quotation nor
validate a scientific claim. One session owns verified original PDF bytes and
lazily builds each requested page's character index once. No OCR/model calls.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pymupdf


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th", "tr", "p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th", "tr", "p", "div", "li"}:
            self.parts.append(" ")


def _plain(value):
    if not isinstance(value, str):
        return ""
    if re.search(r"<(?:table|tr|td|th|p|div|span|br|sub|sup|b|i|strong|em)\b", value, re.I):
        parser = _HTMLText()
        parser.feed(value)
        return "".join(parser.parts)
    return unescape(value)


def _normalize(text, positions=None):
    """Ignore layout whitespace and compatibility glyphs, never fuzzy numbers.

    A separator between two digits remains significant: ``1 23`` is not ``123``.
    The map retains original character positions through ligature expansion.
    """
    expanded = []
    for index, char in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", char):
            if normalized == "\u00ad":  # discretionary, invisible hyphen
                continue
            expanded.append((normalized.replace("\u2212", "-"), positions[index] if positions is not None else None))
    result, mapping = [], []
    for index, (char, position) in enumerate(expanded):
        if char.isspace():
            if index and expanded[index - 1][0].isspace():
                continue
            next_char = next((c for c, _ in expanded[index + 1 :] if not c.isspace()), "")
            if result and result[-1].isdigit() and next_char.isdigit():
                result.append(" ")
                mapping.append(None)
            continue
        result.append(char)
        mapping.append(position)
    return "".join(result), mapping


def _matches(haystack, needle):
    result, start = [], 0
    while needle:
        index = haystack.find(needle, start)
        if index < 0:
            break
        end = index + len(needle)
        # Prevent a quote's numeric endpoint selecting a different larger value.
        left_ok = not (
            needle[0].isdigit() and index and (haystack[index - 1].isdigit() or haystack[index - 1] in ".+-")
        )
        right_ok = not (
            needle[-1].isdigit() and end < len(haystack) and (haystack[end].isdigit() or haystack[end] == ".")
        )
        if left_ok and right_ok:
            result.append((index, end))
        start = index + 1
    return result


def _short(quote):
    return len([c for c in quote if c.isalnum()]) < 4 or not any(c.isalpha() for c in quote)


class EvidenceLocator:
    """Context manager for all evidence in one candidate GET.

    ``assets`` is an authorized asset-ref mapping (including registered aliases,
    or a registered asset list), ``registry``
    contains complete ``ir_blocks`` records, and ``page_inventory`` maps asset
    refs to known one-based physical pages. ``locate`` never mutates inputs.
    """

    def __init__(self, assets, registry, page_inventory):
        self.assets = (
            assets
            if isinstance(assets, dict)
            else {a["asset_id"]: a for a in assets if isinstance(a, dict) and a.get("asset_id")}
        )
        self.registry = (
            registry
            if isinstance(registry, dict)
            else {b["block_id"]: b for b in registry if isinstance(b, dict) and b.get("block_id")}
        )
        self.page_inventory = page_inventory
        self._documents = {}
        self._page_indexes = {}
        self._blocks = defaultdict(list)
        for block in self.registry.values():
            if (
                isinstance(block, dict)
                and isinstance(block.get("asset_ref"), str)
                and type(block.get("page")) is int
                and block["page"] > 0
            ):
                self._blocks[(block.get("asset_ref"), block.get("page"))].append(block)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for document, _ in self._documents.values():
            if document is not None:
                document.close()
        self._documents.clear()
        self._page_indexes.clear()

    def _document(self, asset_ref):
        if asset_ref not in self._documents:
            asset = self.assets.get(asset_ref)
            document, reason = None, "source_asset_unavailable"
            if isinstance(asset, dict):
                try:
                    content = Path(asset["local_path"]).read_bytes()
                    expected = asset.get("sha256")
                    if not isinstance(expected, str) or not expected:
                        reason = "source_hash_unavailable"
                    elif hashlib.sha256(content).hexdigest() != expected:
                        reason = "source_hash_mismatch"
                    else:
                        reason = "source_pdf_unreadable"
                        document = pymupdf.open(stream=content, filetype="pdf")
                        if document.needs_pass:
                            document.close()
                            document = None
                        else:
                            reason = None
                except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                    pass
            self._documents[asset_ref] = (document, reason)
        return self._documents[asset_ref]

    def _page_index(self, asset_ref, page):
        key = (asset_ref, page.number)
        if key not in self._page_indexes:
            chars, positions = [], []
            for block in page.get_text("rawdict", sort=True).get("blocks", []):
                for line_index, line in enumerate(block.get("lines", [])):
                    line_key = (len(chars), line_index)
                    for span in line.get("spans", []):
                        for char in span.get("chars", []):
                            rect = pymupdf.Rect(char["bbox"]) * page.rotation_matrix
                            if rect.is_empty or not page.rect.contains(rect):
                                continue
                            value = char.get("c", "")
                            chars.extend(value)
                            positions.extend([(line_key, list(rect))] * len(value))
                    chars.append("\n")
                    positions.append(None)
            self._page_indexes[key] = _normalize("".join(chars), positions)
        return self._page_indexes[key]

    @staticmethod
    def _region(coordinates, page, number):
        if not isinstance(coordinates, dict) or coordinates.get("unit") != "pt":
            return None
        if coordinates.get("origin", "top_left") != "top_left":
            return None
        if coordinates.get("coordinate_space", "rendered_page") != "rendered_page":
            return None
        bbox = coordinates.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return None
        try:
            if any(isinstance(value, bool) for value in bbox):
                return None
            values = [float(value) for value in bbox]
            if not all(math.isfinite(value) for value in values):
                return None
            if values[0] >= values[2] or values[1] >= values[3]:
                return None
            rect = pymupdf.Rect(values)
            if not page.rect.contains(rect):
                return None
            for dimension, actual in (("width", page.rect.width), ("height", page.rect.height)):
                if dimension in coordinates and (
                    not math.isfinite(float(coordinates[dimension]))
                    or abs(float(coordinates[dimension]) - actual) > 0.1
                ):
                    return None
        except (TypeError, ValueError, OverflowError):
            return None
        return {
            "bbox": values,
            "unit": "pt",
            "origin": "top_left",
            "coordinate_space": "rendered_page",
            "width": page.rect.width,
            "height": page.rect.height,
            "page": number,
        }

    def _block_region(self, block, page, number):
        anchor = block.get("anchor")
        if not isinstance(anchor, dict) or anchor.get("kind") != "pdf":
            return None
        pages = anchor.get("page_numbers")
        if not isinstance(pages, (list, tuple)) or number not in pages:
            return None
        coordinates = anchor.get("coordinate_region")
        if isinstance(coordinates, dict) and (
            coordinates.get("precision") in {"page_bbox", "unknown"}
            or ("page" in coordinates and coordinates["page"] != number)
        ):
            return None
        return self._region(coordinates, page, number)

    @staticmethod
    def _anchor(number, regions, original=None):
        anchor = deepcopy(original) if isinstance(original, dict) else {"kind": "pdf", "page_numbers": [number]}
        anchor["kind"] = "pdf"
        anchor["page_numbers"] = [number]
        boxes = [region["bbox"] for region in regions]
        union = [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ]
        anchor["coordinate_region"] = {**regions[0], "bbox": union, "precision": "display_highlight_union"}
        return anchor

    def _text_regions(self, positions, start, end, page, number):
        lines = {}
        for position in positions[start:end]:
            if position is None:
                continue
            line_key, bbox = position
            rect = pymupdf.Rect(bbox)
            lines[line_key] = lines[line_key] | rect if line_key in lines else rect
        regions = [self._region({"bbox": list(rect), "unit": "pt"}, page, number) for rect in lines.values()]
        return [region for region in regions if region]

    def _native_fragment(self, fragment, native_text, positions, page, number):
        matches = _matches(native_text, fragment)
        if len(matches) != 1:
            return [], "ambiguous_fragment" if matches else "quote_not_found"
        regions = self._text_regions(positions, *matches[0], page, number)
        return regions, None if regions else "invalid_text_coordinates"

    @staticmethod
    def _mineru_text(candidate):
        # Attached captions/footnotes may lie outside this body's bbox. Only
        # independently registered text blocks can bind their text to geometry.
        return _plain(candidate.get("text", ""))

    def _mineru_fragment(self, fragment, mineru_blocks, page, number):
        regions = {}
        invalid = False
        for candidate, normalized in mineru_blocks:
            matches = _matches(normalized, fragment)
            if len(matches) > 1:
                return [], "ambiguous_fragment"
            if not matches:
                continue
            region = self._block_region(candidate, page, number)
            if region is None:
                invalid = True
            else:
                # Parent table and its cells point to the same enclosing box.
                # This deduplicates positions, never invents per-cell geometry.
                regions[tuple(region["bbox"])] = region
        if invalid:
            return [], "invalid_region_coordinates"
        if len(regions) > 1:
            return [], "ambiguous_fragment"
        return list(regions.values()), None if regions else "quote_not_found"

    def _segments(self, raw_quote, find_fragment, number, *, precision, method):
        """Locate long exact fragments; never imply the whole quote was found.

        No commas/dashes or decimal points are split. A repeated fragment (in
        the quote or original page) cannot select a location. Short labels and
        unmatched numeric statements remain visible in unmatched metadata.
        """
        fragments = [
            text.strip() for text in re.split(r"[;；。！？!?：:\n]+|(?<!\d)\.(?=\s|$)", raw_quote) if text.strip()
        ]
        if len(fragments) < 2:
            return None
        normalized = [_normalize(fragment)[0] for fragment in fragments]
        counts = defaultdict(int)
        for fragment in normalized:
            counts[fragment] += 1
        matched, unmatched, regions = [], [], []
        region_indexes = {}
        for raw, fragment in zip(fragments, normalized):
            reason = None
            if sum(char.isalnum() for char in fragment) < 12 or not any(char.isalpha() for char in fragment):
                reason = "short_fragment"
            elif counts[fragment] > 1:
                reason = "repeated_quote_fragment"
            else:
                boxes, reason = find_fragment(fragment)
                if boxes:
                    indexes = []
                    for region in boxes:
                        key = tuple(region["bbox"])
                        if key not in region_indexes:
                            region_indexes[key] = len(regions)
                            regions.append(region)
                        indexes.append(region_indexes[key])
                    matched.append({"quote": raw, "highlight_region_indexes": indexes})
            if reason:
                unmatched.append({"quote": raw, "reason": reason})
        if not matched:
            return None
        return {
            "status": "located",
            "precision": precision,
            "highlight_regions": regions,
            "anchor": self._anchor(number, regions),
            "location_method": method,
            "location_reason": "partial_quote_segments",
            "partial_match": True,
            "matched_fragments": matched,
            "unmatched_fragments": unmatched,
        }

    def _table_row_segments(self, raw_quote, asset_ref, page, number):
        """Match serialized rows to complete registered cells, never fuzzy values.

        This is only a display fallback: the enclosing table box is returned as
        a partial match, not a cell bbox or a validation of the quoted claim.
        """
        if "|" not in raw_quote:
            return None
        numeric = re.compile(r"[<>≤≥~≈]?[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?")
        rows = []
        blocks = self._blocks.get((asset_ref, number), [])
        for table in blocks:
            if table.get("source") != "mineru" or table.get("kind") != "table":
                continue
            region = self._block_region(table, page, number)
            if region is None:
                continue
            grouped, valid = defaultdict(list), True
            for cell in blocks:
                if cell.get("kind") != "table_cell" or cell.get("table_region") != table.get("block_id"):
                    continue
                anchor = cell.get("anchor")
                structure = anchor.get("table_cell") if isinstance(anchor, dict) else None
                if not isinstance(structure, dict) or cell.get("source") != "mineru":
                    valid = False
                    break
                row, column = structure.get("row"), structure.get("column")
                span = structure.get("col_span", 1)
                if (
                    type(row) is not int
                    or row < 0
                    or type(column) is not int
                    or column < 0
                    or type(span) is not int
                    or span < 1
                    or self._block_region(cell, page, number) != region
                ):
                    valid = False
                    break
                value = _normalize(self._mineru_text(cell))[0]
                if not value:
                    valid = False
                    break
                grouped[row].append((column, span, value))
            if not valid or not grouped:
                continue
            ordered = [sorted(cells) for _, cells in sorted(grouped.items())]
            # A missing/duplicated cell must not create an apparently complete
            # source row. Parent text and registered cells must agree exactly.
            joined = " ".join(value for cells in ordered for _, _, value in cells)
            if _normalize(joined)[0] != _normalize(self._mineru_text(table))[0]:
                continue
            width = max(column + span for cells in ordered for column, span, _ in cells)
            for cells in ordered:
                expected, values = 0, []
                for column, span, value in cells:
                    if column != expected:
                        break
                    expected += span
                    values.append(value)
                else:
                    if expected == width:
                        rows.append((values, region))
        fragments = [
            part.strip() for part in re.split(r"[;；。！？!?：:\n]+|(?<!\d)\.(?=\s|$)", raw_quote) if part.strip()
        ]
        counts = defaultdict(int)
        for fragment in fragments:
            counts[_normalize(fragment)[0]] += 1
        matched, unmatched, regions, indexes = [], [], [], {}
        for fragment in fragments:
            if counts[_normalize(fragment)[0]] > 1:
                unmatched.append({"quote": fragment, "reason": "repeated_quote_fragment"})
                continue
            values = [_normalize(cell.strip())[0] for cell in fragment.split("|")]
            # A final sentence period is layout punctuation only when the last
            # cell is not numeric; numeric representation remains exact.
            if values and not numeric.fullmatch(values[-1]) and values[-1].endswith("."):
                values[-1] = values[-1][:-1]
            candidates = []
            if len(values) >= 3 and all(values) and sum(bool(numeric.fullmatch(v)) for v in values) >= 2:
                for source, region in rows:
                    complete = values == source
                    label_omitted = (
                        len(source) == len(values) + 1
                        and not numeric.fullmatch(source[0])
                        and all(numeric.fullmatch(value) for value in values)
                        and values == source[1:]
                    )
                    if complete or label_omitted:
                        candidates.append(region)
            if len(candidates) != 1:
                unmatched.append(
                    {"quote": fragment, "reason": "ambiguous_table_row" if candidates else "table_row_not_found"}
                )
                continue
            region = candidates[0]
            key = tuple(region["bbox"])
            if key not in indexes:
                indexes[key] = len(regions)
                regions.append(region)
            matched.append({"quote": fragment, "highlight_region_indexes": [indexes[key]]})
        if not matched:
            return None
        return {
            "status": "located",
            "precision": "region",
            "highlight_regions": regions,
            "anchor": self._anchor(number, regions),
            "location_method": "mineru_table_rows",
            "location_reason": "partial_table_rows",
            "partial_match": True,
            "matched_fragments": matched,
            "unmatched_fragments": unmatched,
        }

    def locate(self, evidence):
        evidence = evidence if isinstance(evidence, dict) else {}
        block_id = evidence.get("block_id")
        block = self.registry.get(block_id) if isinstance(block_id, str) else None
        block = block if isinstance(block, dict) else None
        declared_asset = evidence.get("asset_ref")
        declared_page = evidence.get("page")
        # Older strict output schemas serialize unknown identity fields as null.
        # Null/missing can inherit a registered block; malformed values cannot.
        asset_ref = declared_asset if declared_asset is not None else (block or {}).get("asset_ref")
        number = declared_page if declared_page is not None else (block or {}).get("page")
        valid_page = (
            isinstance(asset_ref, str)
            and type(number) is int
            and number > 0
            and number in self.page_inventory.get(asset_ref, set())
        )
        result = {
            "status": "page_only" if valid_page else "not_found",
            "asset_ref": asset_ref if isinstance(asset_ref, str) else None,
            "page": number if type(number) is int else None,
            "block_id": block_id if isinstance(block_id, str) else None,
            "anchor": None,
            "highlight_regions": [],
            "precision": "page",
            "location_method": "declared_page" if valid_page else "unresolved_source",
            "location_reason": "quote_not_found" if valid_page else "source_page_unavailable",
            "partial_match": False,
            "matched_fragments": [],
            "unmatched_fragments": [],
        }

        def degrade(reason):
            result["location_reason"] = reason
            return result

        if block and (
            (declared_asset is not None and asset_ref != block.get("asset_ref"))
            or (declared_page is not None and number != block.get("page"))
        ):
            return degrade("block_source_conflict")
        # A damaged/missing derived IR does not erase the real original's pages.
        if (
            not valid_page
            and isinstance(asset_ref, str)
            and asset_ref in self.assets
            and type(number) is int
            and number > 0
        ):
            original, _ = self._document(asset_ref)
            if original is not None and number <= len(original):
                valid_page = True
                result.update(status="page_only", location_method="declared_page", location_reason="quote_not_found")
        if not valid_page:
            return result
        document, reason = self._document(asset_ref)
        if document is None:
            return degrade(reason)
        if number > len(document):
            result["status"] = "not_found"
            return degrade("source_page_out_of_range")
        page = document[number - 1]
        raw_quote = evidence.get("quote") if isinstance(evidence.get("quote"), str) else ""
        quote, _ = _normalize(raw_quote)
        if not quote:
            region = self._block_region(block, page, number) if block else None
            if region:
                result.update(
                    status="located",
                    precision="region",
                    highlight_regions=[region],
                    anchor=self._anchor(number, [region], block["anchor"]),
                    location_method="legacy_block_anchor",
                    location_reason="empty_quote_block_anchor",
                )
                return result
            return degrade("empty_quote")
        table_segments = self._table_row_segments(raw_quote, asset_ref, page, number)
        if _short(quote):
            if table_segments:
                result.update(table_segments)
                return result
            return degrade("short_quote")
        try:
            native_text, positions = self._page_index(asset_ref, page)
        except (ValueError, RuntimeError):
            return degrade("source_page_text_unreadable")
        matches = _matches(native_text, quote)
        if len(matches) > 1:
            return degrade("ambiguous_native_text_match")
        if matches:
            start, end = matches[0]
            regions = self._text_regions(positions, start, end, page, number)
            if regions:
                result.update(
                    status="located",
                    precision="text",
                    highlight_regions=regions,
                    anchor=self._anchor(number, regions),
                    location_method="native_text_match",
                    location_reason="unique_quote_on_declared_page",
                )
                return result
        candidates = {}
        invalid = False
        mineru_blocks = []
        for candidate in self._blocks.get((asset_ref, number), []):
            if candidate.get("source") != "mineru":
                continue
            normalized, _ = _normalize(self._mineru_text(candidate))
            mineru_blocks.append((candidate, normalized))
            found = _matches(normalized, quote)
            if len(found) > 1:
                return degrade("ambiguous_mineru_region_match")
            if not found:
                continue
            region = self._block_region(candidate, page, number)
            if region is None:
                invalid = True
                continue
            key = tuple(region["bbox"])
            previous = candidates.get(key)
            if previous is None or candidate.get("kind") == "table":
                candidates[key] = (candidate, region)
        if invalid:
            return degrade("invalid_region_coordinates")
        if len(candidates) > 1:
            return degrade("ambiguous_mineru_region_match")
        if candidates:
            candidate, region = next(iter(candidates.values()))
            table = candidate.get("kind") in {"table", "table_cell"} or candidate.get("table_region")
            result.update(
                status="located",
                precision="region",
                highlight_regions=[region],
                anchor=self._anchor(number, [region], candidate["anchor"]),
                location_method="mineru_region_match",
                location_reason="table_region" if table else "unique_mineru_region",
            )
        elif not matches:
            if table_segments:
                result.update(table_segments)
                return result
            segments = self._segments(
                raw_quote,
                lambda fragment: self._native_fragment(fragment, native_text, positions, page, number),
                number,
                precision="text",
                method="native_text_segments",
            )
            if not segments:
                segments = self._segments(
                    raw_quote,
                    lambda fragment: self._mineru_fragment(fragment, mineru_blocks, page, number),
                    number,
                    precision="region",
                    method="mineru_region_segments",
                )
            if segments:
                result.update(segments)
        return result


EvidenceLocationSession = EvidenceLocator
