"""Rebuild the located blocks used by entity extraction from documents that were already read."""

from __future__ import annotations

from kb_ingest.extraction.entity_windows import reading_order
from kb_ingest.ingest.documents.document_pipeline_blocks import ir_blocks
from kb_ingest.ingest.documents.readers.page_triage import image_key, read_image_keys

# Providers shrink high-detail images to about 2048 px, so larger renders only cost upload time.
SCAN_RENDER_PIXELS = 2048
IMAGE_MIN_AREA = 0.02
IMAGE_MIN_WIDTH = 0.25


def image_region_blocks(documents, assets, blocks):
    """Embedded table and figure images on pages read only from the PDF text layer.

    Without the MinerU parser the native reader returns text only, so a table pasted into a patent as an image
    is invisible. Each large image becomes an `image` block at its place on the page; `read_visual_regions`
    then transcribes it. Pages that already have parser regions are left to the parser; blank pages and images
    the local reader already read as a table (page_triage) are left out.
    """
    import pymupdf

    paths = {asset["asset_id"]: asset.get("local_path") for asset in assets}
    by_page = {}
    for block in blocks:
        by_page.setdefault((block.get("asset_ref"), block.get("page")), []).append(block)
    parsed = {
        (document["asset_ref"], region.get("page"))
        for document in documents
        for region in document.get("regions", [])
        if not str(region.get("source", "")).startswith("pymupdf") and not region.get("embedded_image")
    }
    read_images = read_image_keys(documents)
    next_id = 1 + max((int(block["block_id"][1:]) for block in blocks if block["block_id"][1:].isdigit()), default=0)
    result = []
    for document in documents:
        path = paths.get(document["asset_ref"])
        if not path:
            continue
        with pymupdf.open(path) as pdf:
            for page in document["pages"]:
                key = (document["asset_ref"], page["page"])
                if key in parsed or page.get("blank") or page["page"] > len(pdf):
                    continue
                if any(block["kind"] == "unread_page" for block in by_page.get(key, [])):
                    continue
                pdf_page = pdf[page["page"] - 1]
                width, height = pdf_page.rect.width, pdf_page.rect.height
                for info in sorted(pdf_page.get_image_info(), key=lambda item: (item["bbox"][1], item["bbox"][0])):
                    x0, y0, x1, y1 = info["bbox"]
                    if (x1 - x0) * (y1 - y0) < IMAGE_MIN_AREA * width * height or (x1 - x0) < IMAGE_MIN_WIDTH * width:
                        continue
                    if (*key, tuple(image_key(info["bbox"]))) in read_images:
                        continue
                    result.append(
                        {
                            "block_id": "b" + str(next_id),
                            "asset_ref": document["asset_ref"],
                            "page": page["page"],
                            "kind": "image",
                            "source": "pymupdf_image",
                            "text": "",
                            "requires_image_verification": True,
                            "anchor": {
                                "kind": "pdf",
                                "page_numbers": [page["page"]],
                                "coordinate_region": {
                                    "bbox": [x0, y0, x1, y1],
                                    "width": page["width_pt"],
                                    "height": page["height_pt"],
                                    "unit": "pt",
                                    "origin": "top_left",
                                    "page": page["page"],
                                },
                            },
                        }
                    )
                    next_id += 1
    return result


def caption_blocks(blocks):
    """A table's caption and footnotes as text blocks right before and right after the table.

    MinerU keeps them only as fields of the table region, and they carry what names the table: 「表2：原料组成」
    above it, and often the next example's heading 「实施例2：……如下表2所示」 as a footnote below it. As blocks,
    the windows and the table program read them in their place."""

    def texts(block, many, one):
        values = block.get(many) or ([block[one]] if block.get(one) else [])
        return [" ".join(str(value).split()) for value in values if str(value or "").strip()]

    def text_block(table, suffix, kind, lines):
        return {
            **{key: table.get(key) for key in ("asset_ref", "page", "anchor", "source")},
            "block_id": table["block_id"] + suffix,
            "kind": kind,
            "text": "\n".join(lines),
            "table_context_for": table["block_id"],
        }

    last = {}
    for index, block in enumerate(blocks):
        owner = block["block_id"] if block.get("kind") == "table" else block.get("table_region")
        if owner:
            last[owner] = index
    by_id = {block["block_id"]: block for block in blocks if block.get("kind") == "table"}
    result = []
    for index, block in enumerate(blocks):
        if block.get("kind") == "table":
            captions = texts(block, "captions", "caption")
            if captions:
                result.append(text_block(block, "c", "caption", captions))
        result.append(block)
        for table_id, end in last.items():
            if end == index and table_id in by_id:
                notes = texts(by_id[table_id], "footnotes", "footnote")
                if notes:
                    result.append(text_block(by_id[table_id], "f", "footnote", notes))
    return result


def place_blocks(blocks, extra):
    """Insert blocks at their vertical place on their page, before the first block that starts below them."""

    def top(block):
        bbox = ((block.get("anchor") or {}).get("coordinate_region") or {}).get("bbox")
        return bbox[1] if bbox else None

    result = list(blocks)
    for block in extra:
        index = None
        for position, other in enumerate(result):
            same = (other.get("asset_ref"), other.get("page")) == (block["asset_ref"], block["page"])
            if same and top(other) is not None and top(other) > top(block):
                index = position
                break
            if same:
                index = position + 1
        result.insert(len(result) if index is None else index, block)
    return result


def segment_blocks(page_block, segments):
    """One block per transcribed segment of a scanned page, each with its own box, so a quote is highlighted where it
    is written. Boxes come back normalized to 0-1000; an unusable box falls back to the whole page."""
    region = (page_block.get("anchor") or {}).get("coordinate_region") or {}
    width, height = float(region.get("width") or 0), float(region.get("height") or 0)
    result = []
    for index, segment in enumerate(segments):
        box = segment.get("box") or {}
        values = [max(0, min(1000, int(box.get(key, -1)))) for key in ("x0", "y0", "x1", "y1")]
        usable = width and height and values[2] > values[0] and values[3] > values[1]
        bbox = (
            [values[0] * width / 1000, values[1] * height / 1000, values[2] * width / 1000, values[3] * height / 1000]
            if usable
            else [0, 0, width, height]
        )
        result.append(
            {
                **{key: page_block.get(key) for key in ("asset_ref", "page")},
                "block_id": page_block["block_id"] + "s" + str(index + 1),
                "kind": "visual_transcription",
                "source": "visual",
                "text": segment["text"],
                "segment_kind": segment.get("kind"),
                "anchor": {
                    "kind": "pdf",
                    "page_numbers": [page_block.get("page")],
                    "coordinate_region": {
                        "bbox": bbox,
                        "width": width,
                        "height": height,
                        "unit": "pt",
                        "origin": "top_left",
                        "page": page_block.get("page"),
                        "precision": "model_segment" if usable else "page_bbox",
                    },
                },
            }
        )
    return result


def apply_page_readings(blocks, readings):
    """Replace each transcribed scanned page by its segment blocks, in place in the reading order."""
    by_page = {row["block_id"]: row for row in readings if row.get("segments")}
    result = []
    for block in blocks:
        reading = by_page.get(block["block_id"])
        if block["kind"] == "unread_page" and reading:
            result.extend(segment_blocks(block, reading["segments"]))
        else:
            result.append(block)
    return result


def rebuild_blocks(documents, assets, readings):
    """The blocks extraction worked on, rebuilt from document-ir.json (for drafts saved before
    blocks kept their text)."""
    blocks = ir_blocks(documents)
    blocks = place_blocks(blocks, image_region_blocks(documents, assets, blocks))
    blocks = apply_page_readings(blocks, readings)
    images = {row["block_id"]: row for row in readings if not row.get("segments") and row.get("transcript")}
    for block in blocks:
        if block["block_id"] in images:
            previous = block.get("text") or ""
            transcript = images[block["block_id"]]["transcript"]
            block["text"] = previous + "\n[Original image transcription]\n" + transcript if previous else transcript
    return caption_blocks(reading_order(blocks))
