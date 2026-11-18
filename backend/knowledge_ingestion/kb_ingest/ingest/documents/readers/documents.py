"""Portable, read-only PDF indexing. No scientific reference answers are used."""

from __future__ import annotations

import base64
import hashlib
import unicodedata
from pathlib import Path

import pymupdf


class SourceReadError(ValueError):
    pass


def _text_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _block_key(item) -> tuple:
    return (*item[:4], _text_key(str(item[4])))


def _has_nonvisible_text(item, spans: list[dict]) -> bool:
    box = pymupdf.Rect(item[:4])
    text = _text_key(str(item[4]))
    for span in spans:
        span_text = _text_key("".join(chr(character[0]) for character in span["chars"]))
        span_box = pymupdf.Rect(span["bbox"])
        area = span_box.get_area()
        if span_text and span_text in text and area and (box & span_box).get_area() / area >= 0.9:
            return True
    return False


def read_pdf_asset(asset: dict, *, visible_only: bool = False) -> dict:
    """Read PDF blocks; the optional policy excludes clipped/invisible text.

    The legacy caller's flags=0 behavior remains unchanged. New document IR
    uses the visible policy and retains other native readings separately.
    Neither policy is a scientific or image-verification assertion.
    """
    path = Path(asset["local_path"])
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_hash != asset["sha256"]:
        raise SourceReadError("source hash mismatch")
    blocks, pages, alternatives = [], [], []
    flags = pymupdf.TEXTFLAGS_BLOCKS if visible_only else 0
    try:
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise SourceReadError("encrypted PDF requires a supplied decrypted asset")
            for index, page in enumerate(document):
                page_number = index + 1
                images = page.get_image_info()
                drawings = (
                    sum(entry[0] in {"fill-path", "stroke-path", "fill-shade"} for entry in page.get_bboxlog())
                    if visible_only
                    else 0
                )
                native = page.get_text("blocks", flags=flags, sort=True)
                hidden = (
                    [span for span in page.get_texttrace() if span.get("type") == 3 or span.get("opacity", 1) <= 0]
                    if visible_only
                    else []
                )
                selected = [item for item in native if not _has_nonvisible_text(item, hidden)]
                excluded = [
                    (item, flags, "contains_nonvisible_text") for item in native if _has_nonvisible_text(item, hidden)
                ]
                if visible_only:
                    known = {_block_key(item) for item in native}
                    excluded.extend(
                        (item, 0, "not_in_clipped_reading")
                        for item in page.get_text("blocks", flags=0, sort=True)
                        if _block_key(item) not in known
                    )
                text_count = 0
                for ordinal, item in enumerate(selected):
                    text = str(item[4])
                    if not text.strip():
                        continue
                    text_count += len(text)
                    block_id = f"{asset['asset_id']}:p{page_number}:b{ordinal}"
                    anchor = {
                        "kind": "pdf",
                        "page_numbers": [page_number],
                        "block_id": block_id,
                        "locator_text": f"PDF page {page_number}, block {ordinal}",
                        "coordinate_region": {
                            "bbox": list(item[:4]),
                            "unit": "pt",
                            "origin": "top_left",
                            "page_width": page.rect.width,
                            "page_height": page.rect.height,
                            "rotation": page.rotation,
                        },
                    }
                    blocks.append(
                        {
                            "block_id": block_id,
                            "asset_ref": asset["asset_id"],
                            "page": page_number,
                            "text": text,
                            "kind": "native_text",
                            "anchor": anchor,
                            "reader_flags": flags,
                        }
                    )
                for ordinal, (item, alternate_flags, reason) in enumerate(excluded):
                    text = str(item[4])
                    if not text.strip():
                        continue
                    block_id = f"{asset['asset_id']}:p{page_number}:unverified{ordinal}"
                    alternatives.append(
                        {
                            "block_id": block_id,
                            "asset_ref": asset["asset_id"],
                            "page": page_number,
                            "text": text,
                            "kind": "native_alternative",
                            "reader_flags": alternate_flags,
                            "visibility_reason": reason,
                            "verification_state": "unverified",
                            "eligible_for_extraction": False,
                            "anchor": {
                                "kind": "pdf",
                                "page_numbers": [page_number],
                                "block_id": block_id,
                                "coordinate_region": {
                                    "bbox": list(item[:4]),
                                    "unit": "pt",
                                    "origin": "top_left",
                                    "page_width": page.rect.width,
                                    "page_height": page.rect.height,
                                    "rotation": page.rotation,
                                },
                            },
                        }
                    )
                pages.append(
                    {
                        "page": page_number,
                        "asset_ref": asset["asset_id"],
                        "text_characters": text_count,
                        "image_count": len(images),
                        "width": page.rect.width,
                        "height": page.rect.height,
                        "rotation": page.rotation,
                        "needs_visual_read": bool(images) or bool(drawings) or text_count < 100,
                        **({"drawing_count": drawings} if visible_only else {}),
                    }
                )
    except SourceReadError:
        raise
    except Exception as exc:
        raise SourceReadError(f"PDF read failed: {type(exc).__name__}") from exc
    return {
        "asset_ref": asset["asset_id"],
        "source_sha256": actual_hash,
        "reader": "pymupdf",
        "reader_version": pymupdf.VersionBind,
        "reader_flags": flags,
        "visibility_policy": "clipped_visible_text" if visible_only else "legacy_unclipped",
        "pages": pages,
        "blocks": blocks,
        "alternative_blocks": alternatives,
    }


def chunk_blocks(blocks: list[dict], max_chars: int = 18000) -> list[list[dict]]:
    """Split oversized text blocks with offsets; never silently drop trailing text."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks, current, size = [], [], 0
    for block in blocks:
        text = block["text"]
        for start in range(0, max(1, len(text)), max_chars):
            part = dict(block)
            part["source_block_id"] = block.get("source_block_id", block["block_id"])
            part["text"] = text[start : start + max_chars]
            part["text_offset"] = start
            if len(text) > max_chars:
                part["block_id"] = f"{block['block_id']}:chars{start}-{start + len(part['text'])}"
            if current and size + len(part["text"]) > max_chars:
                chunks.append(current)
                current, size = [], 0
            current.append(part)
            size += len(part["text"])
    if current:
        chunks.append(current)
    return chunks


def render_page(asset: dict, page_number: int, *, max_dimension: int = 1800) -> tuple[dict, str]:
    """Create an in-memory page image and a source anchor, without touching the PDF."""
    if hashlib.sha256(Path(asset["local_path"]).read_bytes()).hexdigest() != asset["sha256"]:
        raise SourceReadError("source hash mismatch")
    try:
        with pymupdf.open(asset["local_path"], filetype="pdf") as document:
            if document.needs_pass:
                raise SourceReadError("encrypted PDF requires a supplied decrypted asset")
            if not 1 <= page_number <= len(document):
                raise SourceReadError("page outside source")
            page = document[page_number - 1]
            scale = min(2.0, max_dimension / max(page.rect.width, page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            uri = "data:image/png;base64," + base64.b64encode(pixmap.tobytes("png")).decode("ascii")
            block_id = f"{asset['asset_id']}:p{page_number}:visual"
            anchor = {
                "kind": "pdf",
                "page_numbers": [page_number],
                "block_id": block_id,
                "locator_text": f"PDF page {page_number}, rendered whole page",
                "coordinate_region": {
                    "bbox": list(page.rect),
                    "unit": "pt",
                    "origin": "top_left",
                    "page_width": page.rect.width,
                    "page_height": page.rect.height,
                    "rotation": page.rotation,
                },
            }
            return {
                "block_id": block_id,
                "asset_ref": asset["asset_id"],
                "page": page_number,
                "text": "[Page image supplied separately; visual transcriptions require source review]",
                "kind": "visual_page",
                "anchor": anchor,
            }, uri
    except SourceReadError:
        raise
    except Exception as exc:
        raise SourceReadError(f"PDF page render failed: {type(exc).__name__}") from exc
