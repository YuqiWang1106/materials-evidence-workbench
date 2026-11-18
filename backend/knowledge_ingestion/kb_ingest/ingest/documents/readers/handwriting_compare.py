"""Adapt local reader output into document IR and compare the two readings."""

from __future__ import annotations

import re
import shutil
import statistics
import unicodedata
from pathlib import Path
from uuid import uuid4

from kb_ingest.core import fingerprint

from ..document_ir import SCHEMA, SCHEMA_VERSION, _native
from .handwriting import (
    _NOT_A_READING,
    _NUMBER,
    LOW_SCORE,
    MAX_SECOND_ITEMS,
    OUTLIER_RATIO,
    READER_VERSION,
    SECOND_READER_VERSION,
)
from .handwriting_runtime import (
    ReaderCancelled,
    ReaderError,
    render_pages,
    run_reader,
    second_pass,
    seconds_per_item,
)


def _region_id(asset, page, kind, bbox, text):

    return "region:" + fingerprint([asset["sha256"], "handwriting", page, kind, bbox, text])[:24]


def _anchor(page, bbox, precision):
    return {
        "page": page,
        "bbox": bbox,
        "unit": "pt",
        "origin": "top_left",
        "coordinate_space": "rendered_page",
        "precision": precision,
    }


def _decimal_places(text):
    return len(text.split(".", 1)[1]) if "." in text else 0


def flag_cells(cells):
    """Add `flags` to every non-empty cell: low_score, numeric_outlier, decimal_point_missing. Returns the cells.

    decimal_point_missing: an integer of two or more digits where most other numbers of both its row and its
    column have a decimal point (`715` among `71.5`, `73.5`; a lost point is the commonest misreading of a
    scanned table). Never corrected, only flagged."""
    by_column, by_row = {}, {}
    for cell in cells:
        cell["flags"] = list(cell.get("flags") or [])
        if cell.get("empty"):
            continue
        if cell.get("score") is None or cell["score"] < LOW_SCORE:
            cell["flags"].append("low_score")
        if _NUMBER.match(cell.get("text") or ""):
            by_column.setdefault(cell["col"], []).append(cell)
            by_row.setdefault(cell["row"], []).append(cell)
    for column in by_column.values():
        values = [abs(float(cell["text"])) for cell in column]
        if len(values) < 3:
            continue
        median = statistics.median(values)
        if median <= 0:
            continue
        for cell, value in zip(column, values):
            if value > median * OUTLIER_RATIO or (value > 0 and value < median / OUTLIER_RATIO):
                cell["flags"].append("numeric_outlier")

    def mostly_decimal(peers, cell):
        others = [c for c in peers if c is not cell]
        return len(others) >= 2 and sum(1 for c in others if _decimal_places(c["text"])) >= 2 * len(others) / 3

    for line in by_row.values():
        for cell in line:
            text = cell["text"].lstrip("+-")
            if (
                "." not in text
                and len(text) >= 2
                and mostly_decimal(line, cell)
                and mostly_decimal(by_column[cell["col"]], cell)
            ):
                cell["flags"].append("decimal_point_missing")
    return cells


def _union(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def merge_collisions(cells):
    """Two reader cells that snapped to one (row, col) become one cell: union box, joined text, flagged."""
    merged = {}
    for cell in cells:
        key = (cell["row"], cell["col"])
        if key not in merged:
            merged[key] = dict(cell)
            continue
        kept = merged[key]
        kept["bbox"] = _union(kept["bbox"], cell["bbox"])
        if kept.get("upright_bbox") and cell.get("upright_bbox"):
            kept["upright_bbox"] = _union(kept["upright_bbox"], cell["upright_bbox"])
        else:
            kept.pop("upright_bbox", None)
        kept["text"] = " ".join(t for t in (kept.get("text"), cell.get("text")) if t)
        kept["empty"] = kept.get("empty") and cell.get("empty")
        scores = [s for s in (kept.get("score"), cell.get("score")) if s is not None]
        kept["score"] = min(scores) if scores else None
        kept["flags"] = sorted(set(kept.get("flags") or []) | {"merged_cells"})
    return list(merged.values())


def _table_text(cells):
    rows = {}
    for cell in cells:
        rows.setdefault(cell["row"], []).append(cell)
    return "\n".join(
        " | ".join(c["text"] for c in sorted(row, key=lambda c: c["col"])) for _, row in sorted(rows.items())
    )


def _checkable(item):
    """On a printed scan: what a misreading changes and the second reader can confirm, a number or a code
    (digits, Latin letters), or what was flagged. Plain Chinese labels and prose are left to the first reading."""
    return bool(re.search(r"[0-9A-Za-z]", item.get("text") or "") or item.get("flags"))


def _differs(item):
    """MinerU read this line or cell, and read it differently from the local reader."""
    return item.get("parser") is not None and not _same(item.get("text"), item["parser"])


def cap_items(items, reader_output, limit=MAX_SECOND_ITEMS):
    """(items to read, number left out): flagged items first, then cells, then lines, in page order."""
    if len(items) <= limit:
        return items, 0
    flagged = {
        source["second_id"]
        for page in reader_output["pages"]
        if not page.get("error")
        for source in [*(c for t in page.get("tables", []) for c in t["cells"]), *page.get("lines", [])]
        if source.get("second_id") and source.get("flags")
    }
    ranked = sorted(range(len(items)), key=lambda i: (items[i]["id"] not in flagged, items[i]["kind"] != "cell", i))
    keep = sorted(ranked[:limit])
    kept = {items[i]["id"] for i in keep}
    for page in reader_output["pages"]:
        for source in [*(c for t in page.get("tables", []) for c in t["cells"]), *page.get("lines", [])]:
            if source.get("second_id") and source["second_id"] not in kept:
                source.pop("second_id")
    return [items[i] for i in keep], len(items) - limit


def second_items(reader_output, rendered, printed_pages=()):
    """What the second reader reads: every non-empty cell and every line, with its page image, the first
    reader's rotation and deskew, and its box in that turned, straightened image (so skewed tables crop cleanly).
    On the pages in `printed_pages` (printed scans with MinerU's reading attached, see _attach_parser) the second
    reader breaks ties: it reads the lines and cells where MinerU and the local reader disagree, and every cell
    holding a number or code; what both already read alike is not read a third time."""
    images = {p["page"]: p["image"] for p in rendered}
    items = []
    for page in reader_output["pages"]:
        if page.get("error") or int(page["page"]) not in images:
            continue
        number = int(page["page"])
        view = {
            "image": images[number],
            "rotation": page.get("rotation") or 0,
            "deskew": page.get("deskew_degrees") or 0.0,
        }

        def item(item_id, kind, source, scale):
            box = {"upright_bbox": source["upright_bbox"]} if source.get("upright_bbox") else {"bbox": source["bbox"]}
            # The second reader sees the crop at the size the first reader read it (halftone scans).
            return {"id": item_id, "kind": kind, **view, **box, **({"scale": scale} if scale and scale < 1 else {})}

        for t, table in enumerate(page.get("tables", [])):
            for cell in table["cells"]:
                if cell.get("empty") or (number in printed_pages and not (_checkable(cell) or _differs(cell))):
                    continue
                cell["second_id"] = f"{number}:t{t}:{cell['row']}:{cell['col']}"
                items.append(item(cell["second_id"], "cell", cell, table.get("text_scale")))
        for index, line in enumerate(page.get("lines", [])):
            if number in printed_pages and not _differs(line):
                continue
            line["second_id"] = f"{number}:l{index}"
            items.append(item(line["second_id"], "line", line, page.get("text_scale")))
    return items


def prepare(reader_output):
    """Merge colliding cells and flag cells and lines, in reader pixels; updates and returns reader_output."""
    for page in reader_output["pages"]:
        if page.get("error"):
            continue
        for table in page.get("tables", []):
            table["cells"] = flag_cells(merge_collisions(table["cells"]))
        for line in page.get("lines", []):
            line["flags"] = [] if (line.get("score") or 0) >= LOW_SCORE else ["low_score"]
    return reader_output


def _normal(text):
    """Writing differences that are not reading differences: 口/☐/□ boxes, ℃/°, 0·02/0.02, dash forms, sentence
    punctuation (，。；：、 and a final full stop), spaces except between digits (1 2 is not 12). Digits, letters
    and a decimal point inside a number stay as written."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("口", "□").replace("☐", "□").replace("°C", "°")
    text = re.sub(r"[‐‑–—−]", "-", text)
    text = re.sub(r"(?<!\d)[,;:]|[,;:](?!\d)", "", text)  # a separator between digits (1:2, 1,5) stays
    text = re.sub(r'[，。；：、!?“”"‘’「」]', "", text).rstrip(".")
    text = re.sub(r"(?<=\d)[·・](?=\d)", ".", text)
    text = re.sub(r"(?<=\d)\s+(?=\d)", "\u2423", text)
    return re.sub(r"\s+", "", text)


def _same(local, second):
    """Equal after _normal; the second reader writes a decimal point as a hyphen (0-02), so a hyphen between
    digits matches a point only where the first reading has that point (12-5 and 12.5 still differ). A letter O
    where the first reading is a number is its zero."""
    local, second = _normal(local), _normal(second)
    if _NUMBER.match(local):
        second = second.replace("O", "0").replace("o", "0")
    if local == second:
        return True
    return len(local) == len(second) and all(
        a == b or (a, b) == (".", "-") and 0 < i < len(local) - 1 and local[i - 1].isdigit() and local[i + 1].isdigit()
        for i, (a, b) in enumerate(zip(local, second))
    )


_INLINE_MATH = re.compile(r"\\\((.*?)\\\)")


def _delatex(text):
    """The second reader writes formulas in a printed line as inline LaTeX (\\(100-120^{\\circ}C\\), N_{2}): the plain
    text they stand for (100-120°C, N2), so the reading can be compared. Other markup is left as it is."""
    if not text:
        return text

    def plain(match):
        body = match.group(1).replace("^{\\circ}", "°").replace("\\circ", "°")
        body = re.sub(r"[_^]\{([^{}]*)\}", r"\1", body)
        return re.sub(r"\\[A-Za-z]+\s*|[{}]", "", body)

    text = _INLINE_MATH.sub(plain, text)
    return re.sub(r"(?<=[A-Za-z0-9)])[_^]\{([0-9A-Za-z+\-]{1,4})\}", r"\1", text)  # a bare N_{2}, Zn^{2+}


def compare(local, second):
    """(state, flags to add, flags to drop) for one item read by both readers."""
    if not (local or "").strip():
        return "local_blank", [], []  # the second reader invents text in near-empty boxes; the first stays
    if not second or _NOT_A_READING.search(second) or not any(ch.isalnum() for ch in second):
        return "second_reading_unusable", [], []
    if _same(local, second):
        return "both_readers_agree", [], ["low_score"]
    return "readers_disagree", ["readers_disagree"], []


def _reading(item, readings):
    """The reading record of one cell or line; `readings` is None when the second reader did not run."""
    reading = {
        "reader": READER_VERSION,
        "score": item.get("score"),
        "flags": list(item.get("flags") or []),
        "state": "empty_cell" if item.get("empty") else "local_ocr_not_human_verified",
    }
    if item.get("empty"):
        return reading
    if readings is None or item.get("second_id") not in readings:
        return {**reading, "needs_review": bool(reading["flags"])}
    second = readings[item["second_id"]]
    state, add, drop = compare(item.get("text"), second)
    flags = [flag for flag in reading["flags"] if flag not in drop] + add
    return {
        **reading,
        "flags": flags,
        "state": state,
        "needs_review": bool(flags),
        "second": {"reader": SECOND_READER_VERSION, "text": second},
    }


def adapt(asset, pages, reader_output, rendered, readings=None, *, source="handwriting", risks=("handwriting",)):
    """Prepared reader pixels → rendered-page points; returns (IR regions, page gaps)."""
    zoom = {p["page"]: p["zoom"] for p in rendered}
    sizes = {p["page"]: (p["width_pt"], p["height_pt"]) for p in pages}
    regions, gaps = [], []
    for page in reader_output["pages"]:
        number = int(page["page"])
        if number not in zoom:
            raise ReaderError("handwriting_reader_unknown_page")
        if page.get("error"):
            gaps.append({"kind": "handwriting_page_failed", "page": number, "reason": str(page["error"])[:80]})
            continue
        width, height = sizes[number]

        def points(box):
            x0, y0, x1, y1 = (float(v) / zoom[number] for v in box)
            return [round(max(0.0, x0), 2), round(max(0.0, y0), 2), round(min(width, x1), 2), round(min(height, y1), 2)]

        page_regions = []
        for table in page.get("tables", []):
            ir_cells = [
                {
                    "row": c["row"],
                    "col": c["col"],
                    "row_span": c["row_span"],
                    "col_span": c["col_span"],
                    "text": c["text"],
                    "bbox": points(c["bbox"]),
                    "structure_reliable": True,
                    "reading": _reading(c, readings),
                }
                for c in table["cells"]
            ]
            bbox = points(table["bbox"])
            text = _table_text(ir_cells)
            page_regions.append(
                {
                    "id": _region_id(asset, number, "table", bbox, text),
                    "asset_ref": asset["asset_id"],
                    "source": source,
                    "kind": "table",
                    "page": number,
                    "text": text,
                    "anchor": _anchor(number, bbox, "handwriting_table"),
                    "cells": ir_cells,
                    "caption": "",
                    "footnote": "",
                    "image_refs": [],
                    "diagnostics": [],
                    "reading_risks": list(risks),
                    "reading": {
                        "reader": READER_VERSION,
                        "rotation": page.get("rotation"),
                        "skew_degrees": page.get("skew_degrees"),
                        "style": table.get("style"),
                    },
                }
            )
        for line in page.get("lines", []):
            bbox = points(line["bbox"])
            page_regions.append(
                {
                    "id": _region_id(asset, number, "text", bbox, line["text"]),
                    "asset_ref": asset["asset_id"],
                    "source": source,
                    "kind": "text",
                    "page": number,
                    "text": line["text"],
                    "anchor": _anchor(number, bbox, "handwriting_line"),
                    "cells": [],
                    "caption": "",
                    "footnote": "",
                    "image_refs": [],
                    "diagnostics": [],
                    "reading_risks": list(risks),
                    "reading": _reading(line, readings),
                }
            )
        # A title written above a table is read before it.
        regions.extend(sorted(page_regions, key=lambda r: (r["anchor"]["bbox"][1], r["anchor"]["bbox"][0])))
    return regions, gaps


def read_handwriting_ir(
    asset,
    work_dir,
    *,
    command=None,
    model_root=None,
    second_command=None,
    second_model=None,
    second_seconds_per_item=None,
    cancelled=None,
):
    """Shared IR for one handwritten original (PDF, JPG or PNG); never raises for reader problems (an unreadable
    or changed source file raises, as in read_document_ir)."""
    pages, _, alternatives, native_reader = _native(asset)
    result = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "asset_ref": asset["asset_id"],
        "source_sha256": asset["sha256"],
        "pages": pages,
        "regions": [],
        "alternative_readings": alternatives,
        "native_reader": native_reader,
        "read_status": "partial",
        "gaps": [],
        "image_assets": [],
        "reading_disagreements": [],
        "reading_policy": {"reader": "handwriting"},
        "runtime": {"status": "not_ready", "reader": READER_VERSION, "second_reader": None},
        "handwriting_review": {"readers_disagree": 0, "needs_review": 0},
    }
    if not command or not model_root:
        result["gaps"].append({"kind": "handwriting_reader_not_configured"})
        return result
    work, run = Path(work_dir).resolve(), uuid4().hex  # the readers run with the work directory as cwd
    succeeded, readings, second_gap = False, None, None
    allowance, allowance_gap = seconds_per_item(second_seconds_per_item)
    try:
        work.mkdir(parents=True, exist_ok=True)
        rendered = render_pages(asset, work, run)
        output = prepare(run_reader(command, rendered, model_root, work, run, cancelled=cancelled))
        readings, second_gap = second_pass(
            second_items(output, rendered),
            second_command,
            second_model,
            work,
            run,
            cancelled=cancelled,
            allowance=allowance,
        )
        result["regions"], page_gaps = adapt(asset, pages, output, rendered, readings)
        succeeded = True
    except ReaderCancelled:
        result["gaps"].append({"kind": "reading_cancelled"})
        return result
    except ReaderError as exc:
        result["gaps"].append({"kind": "handwriting_reader_failed", "reason": exc.code})
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError, IndexError) as exc:
        result["gaps"].append({"kind": "handwriting_reader_failed", "reason": type(exc).__name__})
        return result
    finally:
        # Page images and exchange files are per run; only a failed run's logs are kept for diagnosis.
        keep_logs = (
            not succeeded or second_gap is not None and second_gap["kind"] != "handwriting_second_reader_not_configured"
        )
        for leftover in work.glob(f"*-{run}*"):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            elif not (keep_logs and leftover.suffix == ".log"):
                leftover.unlink(missing_ok=True)
    result["runtime"] = {
        "status": "available",
        "reader": READER_VERSION,
        "second_reader": SECOND_READER_VERSION if readings is not None else None,
    }
    readings_of = [cell["reading"] for region in result["regions"] for cell in region["cells"]]
    readings_of += [region["reading"] for region in result["regions"] if region["kind"] == "text"]
    result["handwriting_review"] = {
        "readers_disagree": sum(1 for r in readings_of if r.get("state") == "readers_disagree"),
        "needs_review": sum(1 for r in readings_of if r.get("needs_review")),
    }
    result["gaps"].extend(page_gaps)
    if allowance_gap:
        result["gaps"].append(allowance_gap)
    if second_gap:
        result["gaps"].append(second_gap)
    read = {region["page"] for region in result["regions"]}
    failed = {gap["page"] for gap in page_gaps}
    result["gaps"].extend(
        {"kind": "handwriting_page_empty", "page": p["page"]}
        for p in pages
        if p["page"] not in read and p["page"] not in failed
    )
    result["read_status"] = "complete" if not result["gaps"] else "partial"
    return result
