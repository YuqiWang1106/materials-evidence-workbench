"""Page triage before the paid visual reading: blank pages, and large pictures the local reader checks for a table.

- `picture_regions`: large pictures on pages with a text layer. A picture that holds a ruled table is read by
  the local reader (apply_local_ocr) instead of the vision model; any other picture still goes to the model.
- `mark_blank_pages`: pages that nothing read and that hold almost no ink. They are left out of the page-by-page
  visual reading, so they cost no model call and report no gap.
"""

from __future__ import annotations

from collections.abc import Sequence

import pymupdf

from ..document_ir import _verified_path
from .handwriting import PICTURE_KINDS, PROBE_PAGE_SHARE

# Blank check: pages are rendered in grey at this resolution; a pixel darker than INK_LEVEL is ink, and a page
# whose ink covers less than BLANK_INK_SHARE of its pixels holds nothing to read (scanner specks stay below it,
# a single line of text does not).
BLANK_CHECK_DPI = 72
INK_LEVEL = 128
BLANK_INK_SHARE = 0.001
LIGHT_LEVELS = bytes(range(INK_LEVEL, 256))
BLANK_GAP_KINDS = {"visual_read_not_done", "parser_page_content_missing"}


def is_large_picture(box: Sequence[float], width: float, height: float) -> bool:
    """Whether a picture box (pt) covers enough of its page to be checked for a table."""
    return (box[2] - box[0]) * (box[3] - box[1]) >= PROBE_PAGE_SHARE * width * height


def picture_regions(ir: dict, asset: dict, scanned: set[int]) -> dict[int, list[dict]]:
    """{page: IR regions} of the large pictures on the pages with a text layer.

    MinerU's own picture regions where MinerU read the page; on a page MinerU did not read, the embedded images
    PyMuPDF finds become new regions (`embedded_image`, not yet in the IR). Scanned pages are left out: their
    pictures are already part of the scanned-page layout.
    """
    sizes = {page["page"]: (page["width_pt"], page["height_pt"]) for page in ir["pages"]}
    parsed = {region.get("page") for region in ir["regions"] if region.get("source") == "mineru"}
    found: dict[int, list[dict]] = {}
    for region in ir["regions"]:
        page = region.get("page")
        if page not in scanned and _is_mineru_picture(region) and is_large_picture(_box(region), *sizes[page]):
            found.setdefault(page, []).append(region)
    unparsed = [number for number in sizes if number not in parsed and number not in scanned]
    if unparsed:
        found.update(_embedded_images(ir, asset, unparsed))
    return found


def _box(region: dict) -> list[float]:
    box: list[float] = region["anchor"]["bbox"]
    return box


def _is_mineru_picture(region: dict) -> bool:
    box = (region.get("anchor") or {}).get("bbox")
    return region.get("source") == "mineru" and region.get("kind") in PICTURE_KINDS and _valid_box(box)


def _embedded_images(ir: dict, asset: dict, numbers: list[int]) -> dict[int, list[dict]]:
    """New picture regions for the large embedded images of the given pages, in page order."""
    found: dict[int, list[dict]] = {}
    with pymupdf.open(_verified_path(asset)) as document:
        for number in numbers:
            page = document[number - 1]
            width, height = page.rect.width, page.rect.height
            infos = sorted(page.get_image_info(), key=lambda item: (item["bbox"][1], item["bbox"][0]))
            for index, info in enumerate(infos):
                # Image boxes are in unrotated page space; the regions are in the displayed page's points.
                rect = pymupdf.Rect(info["bbox"]) * page.rotation_matrix
                box = [round(v, 2) for v in (rect.x0, rect.y0, rect.x1, rect.y1)]
                if _valid_box(box) and is_large_picture(box, width, height):
                    found.setdefault(number, []).append(_image_region(ir, number, index, box, info["bbox"]))
    return found


def _image_region(ir: dict, page: int, index: int, box: list[float], image_bbox: Sequence[float]) -> dict:
    return {
        "id": f"region:embedded-image:{page}:{index}",
        "asset_ref": ir["asset_ref"],
        "source": "pymupdf_image",
        "kind": "image",
        "page": page,
        "text": "",
        "embedded_image": True,
        "image_bbox": image_key(image_bbox),
        "anchor": {"page": page, "bbox": box, "unit": "pt", "precision": "image_bbox"},
        "cells": [],
        "caption": "",
        "footnote": "",
        "image_refs": [],
        "diagnostics": [],
        "reading_risks": [],
    }


def image_key(bbox: Sequence[float]) -> list[float]:
    """An embedded image's PyMuPDF box, rounded so that a region and a later image listing match."""
    return [round(float(v), 1) for v in bbox]


def read_image_keys(documents: Sequence[dict]) -> set[tuple[str, int, tuple[float, ...]]]:
    """(asset, page, image key) of the embedded images the local reader already read as a table."""
    return {
        (document["asset_ref"], region["page"], tuple(region["image_bbox"]))
        for document in documents
        for region in document.get("regions", [])
        if region.get("embedded_image")
    }


def _valid_box(box: object) -> bool:
    return isinstance(box, list) and len(box) == 4 and box[2] > box[0] and box[3] > box[1]


def mark_blank_pages(ir: dict, asset: dict) -> list[int]:
    """Mark the pages without any region and almost no ink as blank (`blank`); returns their numbers.

    ir_blocks gives a blank page no unread-page block, and its reading gaps are dropped: there is nothing on it.
    """
    read = {region.get("page") for region in ir["regions"]}
    candidates = [page for page in ir["pages"] if page["page"] not in read]
    if not candidates:
        return []
    blank = []
    with pymupdf.open(_verified_path(asset)) as document:
        for page in candidates:
            if _ink_share(document[page["page"] - 1]) < BLANK_INK_SHARE:
                page["blank"] = True
                blank.append(page["page"])
    if blank:
        ir["gaps"] = [
            gap for gap in ir["gaps"] if not (gap.get("kind") in BLANK_GAP_KINDS and gap.get("page") in blank)
        ]
        ir["blank_pages"] = blank
    return blank


def _ink_share(page: pymupdf.Page) -> float:
    pixmap = page.get_pixmap(dpi=BLANK_CHECK_DPI, colorspace=pymupdf.csGRAY, alpha=False)
    samples = pixmap.samples
    # Deleting the light bytes leaves one byte per ink pixel.
    return len(samples.translate(None, LIGHT_LEVELS)) / max(1, len(samples))
