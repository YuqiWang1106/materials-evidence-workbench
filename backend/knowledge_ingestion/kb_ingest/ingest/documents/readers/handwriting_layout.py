"""Vote between MinerU and the local readings on a scanned page."""

from __future__ import annotations

import difflib
import re
import unicodedata

from .handwriting import _NOT_A_READING, LOW_SCORE, PICTURE_KINDS, READER_VERSION, TEXT_KINDS
from .handwriting_compare import _delatex, _normal, _reading, _same
from .page_triage import is_large_picture


def _layout(ir, numbers, zoom):
    """{page: [reader regions]} from MinerU's regions on the scanned pages; only pages with a region worth reading
    again appear. Which scanned pages MinerU left without any region is decided by the caller."""
    sizes = {p["page"]: (p["width_pt"], p["height_pt"]) for p in ir["pages"]}
    layout = {}
    for region in ir["regions"]:
        page, box = region.get("page"), (region.get("anchor") or {}).get("bbox")
        if page not in numbers or region.get("source") != "mineru" or not isinstance(box, list) or len(box) != 4:
            continue
        width, height = sizes[page]
        if not (box[2] > box[0] and box[3] > box[1]):
            continue  # a degenerate parser box has nothing to read
        if region["kind"] == "table":
            kind = "table"
        elif region["kind"] in TEXT_KINDS:
            kind = "text"
        elif region["kind"] in PICTURE_KINDS and is_large_picture(box, width, height):
            kind = "probe"
        else:
            continue
        layout.setdefault(page, []).append(
            {"id": region["id"], "kind": kind, "bbox": [round(float(v) * zoom[page], 2) for v in box]}
        )
    return layout


_LATEX = re.compile(r"\\[A-Za-z]+|[{}^_$]")


_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")


def _plain(text):
    """Text written alike for both readers: LaTeX markup removed, subscripts and full-width forms normalised."""
    return _LATEX.sub("", unicodedata.normalize("NFKC", text or ""))


def _flat(text):
    return re.sub(r"\s", "", _plain(text))


def _parser_pieces(texts, parser_text):
    """MinerU's paragraph cut into the pieces that line up with the local reader's lines (character alignment of
    the two readings, spaces and LaTeX markup removed); None for every line when MinerU read nothing there."""
    local = [_flat(text) for text in texts]
    whole, parser = "".join(local), _flat(parser_text)
    if not parser or not whole:
        return [None] * len(texts)
    at = [0] * (len(whole) + 1)
    for _, i1, i2, j1, j2 in difflib.SequenceMatcher(None, whole, parser, autojunk=False).get_opcodes():
        for k in range(i1, i2 + 1):
            at[k] = j1 + (round((k - i1) * (j2 - j1) / (i2 - i1)) if i2 > i1 else 0)
    at[0], at[-1] = 0, len(parser)
    pieces, start = [], 0
    for text in local:
        end = start + len(text)
        pieces.append(parser[at[start] : max(at[start], at[end])])
        start = end
    return pieces


def _attach_parser(output, ir, region_pages):
    """Put MinerU's own reading beside each local line and cell (`parser`) on the pages read by region: a cell
    gets MinerU's cell at the same grid position when both grids have the same shape, a line its piece of
    MinerU's paragraph. Items MinerU did not read get none."""
    by_id = {region["id"]: region for region in ir["regions"]}

    def shape(cells, column):
        return (
            1 + max((c["row"] + (c.get("row_span") or 1) - 1 for c in cells), default=-1),
            1 + max((c[column] + (c.get("col_span") or 1) - 1 for c in cells), default=-1),
        )

    for page in output["pages"]:
        if page.get("error") or int(page["page"]) not in region_pages:
            continue
        for table in page.get("tables", []):
            region = by_id.get(table.get("region")) or {}
            parser = [
                c
                for c in region.get("cells") or []
                if isinstance(c.get("row"), int) and isinstance(c.get("column", c.get("col")), int)
            ]
            parser = [{**c, "column": c.get("column", c.get("col"))} for c in parser]
            if not parser or table.get("probe") or shape(parser, "column") != shape(table["cells"], "col"):
                continue
            texts = {(c["row"], c["column"]): c.get("text") or "" for c in parser}
            for cell in table["cells"]:
                cell["parser"] = texts.get((cell["row"], cell["col"]), "")
        lines_of = {}
        for line in page.get("lines", []):
            lines_of.setdefault(line.get("region"), []).append(line)
        for region_id, lines in lines_of.items():
            region = by_id.get(region_id)
            if region is None:
                continue
            for line, piece in zip(lines, _parser_pieces([line["text"] for line in lines], region.get("text", ""))):
                if piece is not None:
                    line["parser"] = piece
    return output


def _agrees(reading, second):
    """The second reader's text supports `reading`: the same, or, on a long line, the same followed by a little
    more (it sometimes reads on past the end of the line's box)."""
    if _same(reading, second):
        return True
    a, b = _normal(reading), _normal(second)
    return len(a) >= 8 and b.startswith(a) and len(b) <= 1.3 * len(a)


def _vote(item, readings):
    """(text, reading) of one line or cell read by MinerU, the local reader and, where they disagreed or the cell
    holds a number, the second reader. Two readings that agree win; when all three differ the local reading stays
    unless its score is low (then the second reader's text is used), marked for review with all three kept. A cell
    flagged decimal_point_missing takes a reading that is the same number with its point (715 → 71.5): the
    commonest misreading of a scan, which two readers can share."""
    reading = _reading(item, readings)
    local, parser = item.get("text") or "", item.get("parser")
    if item.get("empty"):
        return local, reading
    if _dash_confirmed(local, reading):
        kept = [f for f in reading["flags"] if f not in ("readers_disagree", "low_score")]
        return local, {**reading, "state": "both_readers_agree", "flags": kept, "needs_review": bool(kept)}
    if parser is None:
        return local, reading
    return _vote_with_parser(local, parser, reading)


def _dash_confirmed(local: str, reading: dict) -> bool:
    """A dash the local reader found from the ink (it has no recognition score) that the second reader also
    reads as a dash. The vote cannot use the second reading here (it holds no letter or digit), and a picture
    table has no MinerU reading to confirm it."""
    second = (reading.get("second") or {}).get("text")
    return bool(_normal(local) == "-" and _normal(second) == "-")


def _vote_with_parser(local: str, parser: str, reading: dict) -> tuple[str, dict]:
    """The vote of one line or cell that MinerU read too."""
    reading["parser"] = parser
    # On a printed line the second reader writes formulas as LaTeX; their plain text is a reading here (on
    # handwriting, LaTeX in a near-empty box is invented and stays unusable).
    second = _delatex((reading.get("second") or {}).get("text"))
    if not local.strip() or not second or _NOT_A_READING.search(second) or not any(c.isalnum() for c in second):
        second = None
    if second and _same(local, second) and reading["state"] != "both_readers_agree":
        kept = [f for f in reading["flags"] if f not in ("readers_disagree", "low_score")]
        reading = {**reading, "state": "both_readers_agree", "flags": kept, "needs_review": bool(kept)}
    flags = list(reading["flags"])
    for candidate in (second, parser):
        if (
            candidate
            and "decimal_point_missing" in flags
            and "." in _normal(candidate)
            and _normal(candidate).replace(".", "", 1) == _normal(local)
        ):
            kept = [f for f in flags if f not in ("decimal_point_missing", "readers_disagree", "low_score")]
            return candidate, {
                **reading,
                "state": "corrected_by_pattern",
                "local": local,
                "flags": kept,
                "needs_review": bool(kept),
            }
    if _same(local, parser):
        kept = [f for f in flags if f not in ("readers_disagree", "low_score")]
        state = "both_readers_agree" if reading["state"] == "both_readers_agree" else "local_and_parser_agree"
        return local, {**reading, "state": state, "flags": kept, "needs_review": bool(kept)}
    if reading["state"] == "both_readers_agree":
        return local, reading
    if second and _agrees(parser, second):
        kept = [f for f in flags if f not in ("readers_disagree", "low_score")]
        return second, {
            **reading,
            "state": "corrected_by_majority",
            "local": local,
            "flags": kept,
            "needs_review": bool(kept),
        }
    if second is None:
        flags.append("parser_reads_differently")
    # No majority: a low local score makes the local reading the least likely to be right, so the second
    # reader's text is used instead (still marked for review, the local reading kept beside it).
    if second and reading.get("score") is not None and reading["score"] < LOW_SCORE:
        chosen = {**reading, "flags": sorted(set(flags)), "needs_review": True, "local": local, "chosen": "second"}
        return second, chosen
    return local, {**reading, "flags": sorted(set(flags)), "needs_review": True}


def _summary(lines):
    """The reading of a text region from the readings of its lines."""
    flags = sorted({flag for line in lines for flag in line["reading"].get("flags", [])})
    states = [line["reading"].get("state") for line in lines]
    agreed = ("both_readers_agree", "local_and_parser_agree", "corrected_by_majority", "corrected_by_pattern")
    state = (
        "readers_disagree"
        if any(line["reading"].get("needs_review") for line in lines)
        else "both_readers_agree"
        if states and all(s in agreed for s in states)
        else "local_ocr_not_human_verified"
    )
    scores = [line["reading"]["score"] for line in lines if line["reading"].get("score") is not None]
    return {
        "reader": READER_VERSION,
        "state": state,
        "flags": flags,
        "score": min(scores) if scores else None,
        "needs_review": any(line["reading"].get("needs_review") for line in lines),
    }
