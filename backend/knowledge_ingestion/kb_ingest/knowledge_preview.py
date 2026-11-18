"""Bounded previews from already authorized immutable bytes, without new joins."""

from __future__ import annotations

import base64
import math
from hashlib import sha256

import pymupdf

from .document_evidence_location import EvidenceLocator
from .knowledge_contracts import VERSION, EvidencePreviewResult, PreviewBox, PreviewImage
from .knowledge_snapshot import KnowledgeIntegrityError

MAX_EDGE = 1600
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024


def validate_bytes(data, expected):
    if not isinstance(data, bytes) or not data or len(data) > MAX_ASSET_BYTES or sha256(data).hexdigest() != expected:
        raise KnowledgeIntegrityError("INTEGRITY_FAILURE")


def evidence_pages(anchor):
    if not isinstance(anchor, dict) or anchor.get("kind") not in {None, "pdf"}:
        raise KnowledgeIntegrityError("PREVIEW_UNSUPPORTED")
    pages = anchor.get("page_numbers")
    if pages is None:
        pages = [anchor.get("physical_page", anchor.get("page"))]
    if not isinstance(pages, list) or not pages or any(type(p) is not int or p < 1 for p in pages):
        raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
    return sorted(set(pages))


def choose_page(pages, page):
    if page is None:
        if len(pages) != 1:
            raise KnowledgeIntegrityError("INVALID_QUERY")
        return pages[0]
    if type(page) is not int or page not in pages:
        raise KnowledgeIntegrityError("INVALID_QUERY")
    return page


def _image(pixmap):
    if pixmap.width > MAX_EDGE or pixmap.height > MAX_EDGE:
        raise KnowledgeIntegrityError("INTEGRITY_FAILURE")
    data = pixmap.tobytes("png")
    if len(data) > MAX_IMAGE_BYTES:
        raise KnowledgeIntegrityError("PREVIEW_UNSUPPORTED")
    return PreviewImage(
        data_uri="data:image/png;base64," + base64.b64encode(data).decode("ascii"),
        width=pixmap.width,
        height=pixmap.height,
        sha256=sha256(data).hexdigest(),
    )


def _result(ref, asset, page, image, boxes, quote, status, location):
    out = EvidencePreviewResult(
        schema_version=VERSION,
        ref=ref,
        asset_sha256=asset,
        physical_page=page,
        image=image,
        location_status="located" if boxes else location,
        boxes=boxes,
        quote=quote,
        status=status,
    )
    if len(out.model_dump_json().encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise KnowledgeIntegrityError("PREVIEW_UNSUPPORTED")
    return out


def render_pdf(ref, data, anchor, page, quote, status):
    asset = ref.asset_sha256 if ref.kind == "evidence" else ref.source_snapshot_ref.source_asset_sha256
    validate_bytes(data, asset)
    pages = evidence_pages(anchor)
    number = choose_page(pages, page)
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or number > len(document):
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            source = document[number - 1]
            if not all(math.isfinite(v) and v > 0 for v in (source.rect.width, source.rect.height)):
                raise KnowledgeIntegrityError("INTEGRITY_FAILURE")
            scale = min(1.5, MAX_EDGE / max(source.rect.width, source.rect.height))
            image = _image(source.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False))
            boxes = []
            unreliable = any(
                key in anchor and anchor[key] not in {"located", "page_only"}
                for key in ("location_status", "localization_status")
            )
            location = "unresolved" if unreliable else "page_only"
            coordinates = anchor.get("coordinate_region")
            if location != "unresolved" and isinstance(coordinates, dict):
                bound = coordinates.get("page")
                explicit_page = (bound == number and type(bound) is int) or (bound is None and len(pages) == 1)
                if explicit_page and coordinates.get("precision") not in {"unknown", "page_bbox"}:
                    region = EvidenceLocator._region(coordinates, source, number)
                    if region is not None:
                        x0, y0, x1, y1 = region["bbox"]
                        boxes = [
                            PreviewBox(
                                x0=x0 / source.rect.width,
                                y0=y0 / source.rect.height,
                                x1=x1 / source.rect.width,
                                y1=y1 / source.rect.height,
                            )
                        ]
            return _result(ref, asset, number, image, boxes, quote, status, location)
    except KnowledgeIntegrityError:
        raise
    except Exception:
        raise KnowledgeIntegrityError("PREVIEW_UNSUPPORTED") from None


def render_passage_image(ref, original, pinned_image, page, quote, status):
    validate_bytes(original, ref.source_snapshot_ref.source_asset_sha256)
    number = choose_page([ref.physical_page], page)
    bbox = ref.bbox
    if bbox is None or ref.location_status != "located":
        raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
    validate_bytes(pinned_image, bbox.page_image_sha256)
    try:
        # The coordinate space belongs to this exact hash-pinned page image.
        original_pixmap = pymupdf.Pixmap(pinned_image)
        width, height = original_pixmap.width, original_pixmap.height
        values = [bbox.x0, bbox.y0, bbox.x1, bbox.y1]
        if bbox.coordinate_space == "page_pixels":
            if values[2] > width or values[3] > height:
                raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH")
            values = [values[0] / width, values[1] / height, values[2] / width, values[3] / height]
        box = PreviewBox(x0=values[0], y0=values[1], x1=values[2], y1=values[3])
        if max(width, height) > MAX_EDGE:
            with pymupdf.open(stream=pinned_image, filetype="png") as document:
                source = document[0]
                scale = MAX_EDGE / max(source.rect.width, source.rect.height)
                image = _image(source.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False))
        else:
            image = _image(original_pixmap)
        return _result(ref, ref.source_snapshot_ref.source_asset_sha256, number, image, [box], quote, status, "located")
    except KnowledgeIntegrityError:
        raise
    except Exception:
        raise KnowledgeIntegrityError("SOURCE_INDEX_MISMATCH") from None
