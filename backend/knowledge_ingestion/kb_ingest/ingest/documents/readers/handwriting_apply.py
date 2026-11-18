"""Apply local OCR to the scanned pages of a MinerU document IR."""

from __future__ import annotations

import json
import logging
import shutil
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pymupdf
from kb_ingest.core import fingerprint

from ..document_ir import _verified_path
from .documents import SourceReadError
from .handwriting import (
    _CJK,
    LOCAL_OCR_STAGE_VERSION,
    MAX_SECOND_ITEMS,
    OCR_RENDER_LONG_SIDE,
    READER_VERSION,
    SCANNED_TEXT_CHARACTERS,
    SECOND_ITEM_POLICY,
    SECOND_READER_VERSION,
)
from .handwriting_compare import adapt, cap_items, prepare, second_items
from .handwriting_layout import _attach_parser, _layout, _summary, _vote
from .handwriting_runtime import (
    ReaderCancelled,
    ReaderError,
    render_pages,
    run_reader,
    second_pass,
    seconds_per_item,
)
from .local_ocr_identity import PRIMARY_WEIGHTS, reader_identity, second_reader_identity
from .page_triage import picture_regions

logger = logging.getLogger("kb_ingest.readers.handwriting")


def scanned_pages(ir):
    """Page numbers of the PDF pages without a text layer that hold a picture (scans)."""
    return [
        p["page"]
        for p in ir["pages"]
        if (p.get("native_text_characters") or 0) < SCANNED_TEXT_CHARACTERS and p.get("image_count")
    ]


def _apply(ir, asset, output, rendered, readings, region_pages):
    """Write the local readings into the IR; returns the gaps."""
    zoom = {p["page"]: p["zoom"] for p in rendered}
    sizes = {p["page"]: (p["width_pt"], p["height_pt"]) for p in ir["pages"]}
    by_id = {region["id"]: region for region in ir["regions"]}
    gaps, whole = [], {"pages": []}
    for page in output["pages"]:
        number = int(page["page"])
        if number not in zoom:
            raise ReaderError("local_ocr_unknown_page")
        if page.get("error"):
            gaps.append({"kind": "local_ocr_page_failed", "page": number, "reason": str(page["error"])[:80]})
            continue
        if number not in region_pages:
            whole["pages"].append(page)
            continue
        width, height = sizes[number]

        def points(box):
            x0, y0, x1, y1 = (float(v) / zoom[number] for v in box)
            return [round(max(0.0, x0), 2), round(max(0.0, y0), 2), round(min(width, x1), 2), round(min(height, y1), 2)]

        gaps.extend(
            {
                "kind": "local_ocr_region_failed",
                "page": number,
                "region_id": item["region"],
                "reason": str(item["error"])[:80],
            }
            for item in page.get("region_errors", [])
        )
        for table in page.get("tables", []):
            region = by_id.get(table["region"])
            cells = []
            for c in table["cells"]:
                text, reading = _vote(c, readings)
                cells.append(
                    {
                        "row": c["row"],
                        "col": c["col"],
                        "row_span": c["row_span"],
                        "col_span": c["col_span"],
                        "text": text,
                        "bbox": points(c["bbox"]),
                        "structure_reliable": True,
                        "reading": reading,
                    }
                )
            if region is None or not any(cell["text"] for cell in cells):
                continue
            if table.get("probe"):
                region["parser_kind"], region["kind"] = region["kind"], "table"
            region["parser_cells"] = region.get("cells") or []
            # Joined like MinerU's table text: the windows build the grid from the cells themselves.
            region.update(
                cells=cells,
                source="local_ocr",
                text=" ".join(c["text"] for c in cells if c["text"]),
                reading={
                    "reader": READER_VERSION,
                    "style": table.get("style"),
                    "skew_degrees": table.get("skew_degrees"),
                },
                reading_risks=[r for r in region.get("reading_risks") or [] if r != "table_structure_unreliable"],
            )
        lines_of = {}
        for line in page.get("lines", []):
            lines_of.setdefault(line.get("region"), []).append(line)
        for region_id, lines in lines_of.items():
            region = by_id.get(region_id)
            if region is None:
                continue
            records = []
            for line in lines:
                text, reading = _vote(line, readings)
                records.append({"text": text, "bbox": points(line["bbox"]), "reading": reading})
            from_lines = _join_lines([record["text"] for record in records])
            if not from_lines:
                continue
            region["parser_text"] = region.get("text", "")
            region.update(text=from_lines, source="local_ocr", lines=records, reading=_summary(records))
    if whole["pages"]:
        regions, page_gaps = adapt(asset, ir["pages"], whole, rendered, readings, source="local_ocr", risks=())
        ir["regions"].extend(regions)
        gaps.extend(page_gaps)
        read = {region["page"] for region in regions}
        # A scanned page read here no longer waits for the page-by-page visual reading.
        ir["gaps"] = [
            gap for gap in ir["gaps"] if not (gap.get("kind") == "visual_read_not_done" and gap.get("page") in read)
        ]
    return gaps


def _join_lines(texts):
    """The lines of one paragraph as one text: no space between Chinese characters, one space otherwise."""
    text = ""
    for part in (t.strip() for t in texts):
        if not part:
            continue
        if text and not (_CJK.search(text[-1]) and _CJK.search(part[0])):
            text += " "
        text += part
    return text


def _picture_layout(pictures: dict[int, list[dict]], zoom: dict[int, float]) -> dict[int, list[dict]]:
    """Reader regions (probes, in rendered pixels) for the large pictures on pages with a text layer."""
    return {
        page: [
            {
                "id": region["id"],
                "kind": "probe",
                "bbox": [round(float(v) * zoom[page], 2) for v in region["anchor"]["bbox"]],
            }
            for region in regions
        ]
        for page, regions in pictures.items()
    }


def _new_pictures(pictures: dict[int, list[dict]]) -> list[dict]:
    """Copies of the picture regions found by PyMuPDF on pages MinerU did not read (not yet in the IR)."""
    return [deepcopy(region) for regions in pictures.values() for region in regions if region.get("embedded_image")]


def _without_unread_images(regions: list[dict]) -> list[dict]:
    """An embedded image joins the IR only as the table read in it; any other picture stays with the vision model."""
    return [region for region in regions if not region.get("embedded_image") or region.get("source") == "local_ocr"]


def apply_local_ocr(
    ir,
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
    """Read the scanned pages of a PDF's IR (from read_document_ir) with the local readers; updates `ir` in place and
    returns it. Never raises for reader problems (they become gaps); an unreadable or changed source raises.

    MinerU's table and text regions on scanned pages get the local reading (cells with boxes, lines with scores,
    the second reader's agreement, flags); a page without any MinerU region is read page by page. Large pictures
    on pages with a text layer are checked for a ruled table (page_triage.picture_regions). The reading is
    cached in `work_dir` per source, reader, second reader and layout, so a retry does not read again."""
    from kb_ingest.core import atomic_json

    numbers = scanned_pages(ir)
    summary = {
        "status": "not_needed",
        "reader": None,
        "second_reader": None,
        "pages": numbers,
        "review": {"readers_disagree": 0, "needs_review": 0},
    }
    ir["local_ocr"] = summary
    # Large pictures on pages with a text layer are checked only when the reader is configured; otherwise they
    # go to the vision model as before, without a gap.
    pictures = picture_regions(ir, asset, set(numbers)) if command and model_root else {}
    if not numbers and not pictures:
        return ir
    if not command or not model_root:
        summary["status"] = "not_configured"
        ir["gaps"].append({"kind": "local_ocr_not_configured"})
        return ir
    work, run = Path(work_dir).resolve(), uuid4().hex
    allowance, allowance_gap = seconds_per_item(second_seconds_per_item)
    gaps = [allowance_gap] if allowance_gap else []
    succeeded, second_gap = False, None
    try:
        work.mkdir(parents=True, exist_ok=True)
        _verified_path(asset)
        with pymupdf.open(asset["local_path"]) as document:
            zoom = {
                n: min(4.0, OCR_RENDER_LONG_SIDE / max(document[n - 1].rect.width, document[n - 1].rect.height))
                for n in [*numbers, *pictures]
            }
        layout = _layout(ir, set(numbers), zoom)
        # Pages with regions to read again, and scanned pages MinerU left without any region (read page by page);
        # a page whose MinerU regions are only headers, formulas or small pictures keeps MinerU's reading.
        parsed = {r.get("page") for r in ir["regions"] if r.get("source") == "mineru"}
        numbers = [n for n in numbers if n in layout or n not in parsed]
        layout.update(_picture_layout(pictures, zoom))
        numbers = sorted({*numbers, *pictures})
        summary["pages"] = numbers
        if not numbers:
            summary["status"] = "not_needed"
            return ir
        try:
            reader = reader_identity(command, READER_VERSION, model_root, PRIMARY_WEIGHTS)
            second = second_reader_identity(second_command, SECOND_READER_VERSION, second_model)
        except OSError:
            # A cache lookup must not suppress the primary reading if secondary weights are unreadable.
            logger.warning("Local OCR model identity unreadable; bypassing cache")
            cached_path = None
        else:
            key = fingerprint(
                {
                    "source_sha256": asset["sha256"],
                    "stage_version": LOCAL_OCR_STAGE_VERSION,
                    "reader": reader,
                    "second_reader": second,
                    "pages": [[n, zoom[n], layout.get(n)] for n in numbers],
                    "render_long_side": OCR_RENDER_LONG_SIDE,
                    "parser_readings": [
                        [r["id"], r.get("text"), r.get("cells")] for r in ir["regions"] if r.get("page") in layout
                    ],
                    "max_second_items": MAX_SECOND_ITEMS,
                    "second_item_policy": SECOND_ITEM_POLICY,
                }
            )
            cached_path = work / f"local-ocr-{key[:32]}.json"
        cached = None
        if cached_path is not None and cached_path.is_file():
            try:
                cached = json.loads(cached_path.read_text(encoding="utf-8"))
            except ValueError:
                cached = None
        if cached and cached.get("version") == READER_VERSION:
            output, readings, second_gap = cached["output"], cached["readings"], cached["second_gap"]
            gaps.extend(cached.get("gaps") or [])
            rendered = [{"page": n, "zoom": zoom[n]} for n in numbers]
        else:
            rendered = render_pages(asset, work, run, set(numbers), OCR_RENDER_LONG_SIDE)
            for page in rendered:
                if page["page"] in layout:
                    page["regions"] = layout[page["page"]]
            output = _attach_parser(
                prepare(run_reader(command, rendered, model_root, work, run, cancelled=cancelled)), ir, set(layout)
            )
            items, left_out = cap_items(second_items(output, rendered, printed_pages=set(layout)), output)
            readings, second_gap = second_pass(
                items,
                second_command,
                second_model,
                work,
                run,
                cancelled=cancelled,
                allowance=allowance,
                prefix="local_ocr",
            )
            if left_out:
                gaps.append({"kind": "local_ocr_second_reader_capped", "items": left_out})
            # A run with a failed page or region (memory, a stray image) is read again next time, not cached.
            failed = any(page.get("error") or page.get("region_errors") for page in output["pages"])
            complete = not failed and (
                second_gap is None or second_gap["kind"] == "local_ocr_second_reader_not_configured"
            )
            if complete and cached_path is not None:
                atomic_json(
                    cached_path,
                    {
                        "version": READER_VERSION,
                        "output": output,
                        "readings": readings,
                        "second_gap": second_gap,
                        "gaps": [g for g in gaps if g is not allowance_gap],
                    },
                )
        # Written to a copy first: a failure part-way leaves MinerU's reading whole.
        trial = {**ir, "regions": deepcopy(ir["regions"]) + _new_pictures(pictures), "gaps": list(ir["gaps"])}
        gaps.extend(_apply(trial, asset, output, rendered, readings, set(layout)))
        ir["regions"], ir["gaps"] = _without_unread_images(trial["regions"]), trial["gaps"]
        succeeded = True
    except ReaderCancelled:
        summary["status"] = "cancelled"
        ir["gaps"].append({"kind": "reading_cancelled"})
        return ir
    except ReaderError as exc:
        summary["status"] = "failed"
        ir["gaps"].append({"kind": "local_ocr_failed", "reason": exc.code})
        return ir
    except SourceReadError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError, IndexError) as exc:
        summary["status"] = "failed"
        ir["gaps"].append({"kind": "local_ocr_failed", "reason": type(exc).__name__})
        return ir
    finally:
        keep_logs = (
            not succeeded or second_gap is not None and second_gap["kind"] != "local_ocr_second_reader_not_configured"
        )
        for leftover in work.glob(f"*-{run}*"):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            elif not (keep_logs and leftover.suffix == ".log"):
                leftover.unlink(missing_ok=True)
    if second_gap:
        gaps.append(second_gap)
    ir["gaps"].extend(gaps)
    read = {region["page"] for region in ir["regions"] if region.get("source") == "local_ocr"}
    for page in ir["pages"]:
        if page["page"] in read and page.get("read_status") == "unread_visual":
            page["read_status"], page["local_ocr"] = "parsed", "page"  # read here, page by page
    if gaps and ir.get("read_status") == "complete":
        ir["read_status"] = "partial"
    readings_of = [
        cell["reading"]
        for region in ir["regions"]
        if region.get("source") == "local_ocr"
        for cell in region.get("cells", [])
        if cell.get("reading")
    ]
    readings_of += [
        line["reading"]
        for region in ir["regions"]
        if region.get("source") == "local_ocr"
        for line in region.get("lines", [])
    ]
    readings_of += [
        region["reading"]
        for region in ir["regions"]
        if region.get("source") == "local_ocr" and region["kind"] == "text" and not region.get("lines")
    ]
    summary.update(
        status="complete" if not gaps else "partial",
        reader=READER_VERSION,
        second_reader=SECOND_READER_VERSION if readings is not None else None,
        review={
            "readers_disagree": sum(1 for r in readings_of if r.get("state") == "readers_disagree"),
            "needs_review": sum(1 for r in readings_of if r.get("needs_review")),
        },
    )
    return ir
