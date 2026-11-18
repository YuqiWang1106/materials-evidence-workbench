"""The one entry for reading registered originals into document IR.

Photos, and PDFs marked as handwritten, are read by the local readers. Any other PDF is read by
MinerU and its scanned pages are then read again locally. Both OCR paths stay; callers do not
choose between them.
"""

from __future__ import annotations

import os
from pathlib import Path

from kb_ingest.core import fingerprint

from . import reading_timeline as _timeline
from .document_ir import read_document_ir
from .readers.handwriting import HANDWRITING_SUFFIXES, IMAGE_SUFFIXES
from .readers.handwriting_apply import apply_local_ocr, scanned_pages
from .readers.handwriting_compare import read_handwriting_ir
from .readers.page_triage import mark_blank_pages


def local_readers(options):
    """The local OCR readers' settings (the handwriting_* options name them for scans and photos alike)."""
    return {
        "command": options.get("handwriting_command"),
        "model_root": options.get("handwriting_model_root")
        or (str(Path(os.environ["MATERIALS_MINERU_HOME"]) / "models") if os.environ.get("MATERIALS_MINERU_HOME") else None),
        "second_command": options.get("handwriting_second_command"),
        "second_model": options.get("handwriting_second_model") or os.environ.get("MATERIALS_HANDWRITING_SECOND_MODEL"),
        "second_seconds_per_item": options.get("handwriting_second_seconds_per_item")
        or os.environ.get("MATERIALS_HANDWRITING_SECOND_SECONDS_PER_ITEM"),
        "cancelled": options.get("cancel_check"),
    }


def read_documents(assets, source_id, options, processing_run_id, shared):
    if options.get("documents") is not None:
        return list(options["documents"]), []
    documents: list[dict] = []
    gaps: list[dict] = []
    # Photos always go to the local readers; a PDF marked handwritten is read page by page by them too (they
    # turn and straighten each page), any other PDF is read by MinerU and its scanned pages again locally.
    handwriting = (options.get("reader_options") or {}).get("handwriting") is True
    readers = local_readers(options)
    root = options.get("_observation_root")
    for number, asset in enumerate(assets, 1):
        if options.get("progress_callback"):
            options["progress_callback"](
                "reading",
                document_index=number,
                documents_total=len(assets),
                asset_id=asset["asset_id"],
                completed=number - 1,
                total=len(assets),
                unit="份原件",
            )
        suffix = Path(asset["original_name"]).suffix.lower()
        cache = Path(options.get("reader_cache_dir", shared / "reading")) / asset["sha256"]
        if suffix in IMAGE_SUFFIXES or (handwriting and suffix in HANDWRITING_SUFFIXES):
            step = _timeline.start(root, "ocr_first", number)
            ir = read_handwriting_ir(asset, cache / "handwriting", **readers)
            _timeline.photographed(step, ir)
            documents.append(ir)
            gaps.extend({"asset_ref": asset["asset_id"], **gap} for gap in ir.get("gaps", []))
            continue
        if suffix != ".pdf":
            gaps.append({"asset_ref": asset["asset_id"], "reason": "unsupported non-PDF attachment"})
            continue
        step = _timeline.start(root, "mineru", number)
        ir = read_document_ir(
            asset,
            cache,
            mineru_command=options.get("mineru_command"),
            cancelled=options.get("cancel_check"),
            tier=options.get("document_reader_tier", "standard"),
            reread_state_dir=shared / "reading-state",
            reread_task_binding=fingerprint([source_id, processing_run_id.rsplit(":attempt", 1)[0]]),
            reread_findings=options.get("document_reading_findings"),
        )
        _timeline.parsed(step, ir)
        # Scanned pages: MinerU keeps the layout, the local readers give every line and cell with its score.
        step = _timeline.start(root if scanned_pages(ir) else None, "ocr_first", number)
        ir = apply_local_ocr(ir, asset, cache / "local-ocr", **readers)
        _timeline.local_ocr(step, ir)
        mark_blank_pages(ir, asset)
        documents.append(ir)
        if options.get("progress_callback"):
            options["progress_callback"](
                "reading",
                document_index=number,
                documents_total=len(assets),
                completed=number,
                total=len(assets),
                unit="份原件",
                failed=sum(bool(d.get("gaps")) for d in documents),
            )
        # Native text is present on every page we index; a missing parser is reported once, not per page.
        gaps.extend(
            {"asset_ref": asset["asset_id"], **gap}
            for gap in ir.get("gaps", [])
            if gap.get("kind") != "visual_read_not_done" or not _page_has_text(ir, gap.get("page"))
        )
    return documents, gaps


def _page_has_text(ir, page):
    return any(region.get("page") == page and (region.get("text") or "").strip() for region in ir.get("regions", []))
