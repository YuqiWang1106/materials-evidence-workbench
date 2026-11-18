"""Source-derived, reproducible input strategies; no answer-key access.

The registry is an offline artifact. PDF-only prompts deliberately contain no
parser text. Visual selection records conservative full-page fallbacks when
caption/footnote geometry cannot establish a complete crop.
"""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path

import pymupdf

from .contract import FIELDS
from .core import fingerprint
from .ingest.documents.document_pipeline_blocks import ir_blocks

VERSION = "controlled-source-input-v1"
_VISUAL = {"table", "image", "chart", "figure", "equation", "unread_page"}
_TASK_KEYS = {"doc_id", "scope", "source_type", "source_sha256", "target_pages", "context_pages"}


def _page_visuals(document, pdf):
    disputed = {rid for x in document.get("reading_disagreements", []) for rid in x.get("affected_region_ids", [])}
    gap_pages = {
        p
        for x in document.get("gaps", [])
        for p in ([x["page"]] if isinstance(x.get("page"), int) else x.get("pages", []))
    }
    result = []
    for page in document["pages"]:
        number = page["page"]
        regions = [r for r in document["regions"] if r["page"] == number]
        visual = [r for r in regions if r.get("source") == "mineru" and r["kind"] in _VISUAL]
        anomaly = (
            number in gap_pages
            or page.get("read_status") not in (None, "parsed", "complete")
            or any(r["id"] in disputed for r in regions)
            or not regions
        )
        if not visual and not anomaly:
            continue
        bounds = pdf[number - 1].rect
        # These parser versions attach caption text but do not consistently
        # expose its geometry or continuation links. A body-only crop would be
        # false completeness; use the page until the IR proves the enclosure.
        enclosing = [r.get("complete_context_bbox") for r in visual]
        complete = (
            not anomaly
            and visual
            and all(
                isinstance(b, list)
                and len(b) == 4
                and bounds.contains(pymupdf.Rect(b))
                and not pymupdf.Rect(b).is_empty
                for b in enclosing
            )
        )
        if complete:
            for r, box in zip(visual, enclosing):
                result.append(
                    {"page": number, "bbox": box, "region_ids": [r["id"]], "reason": "IR complete context enclosure"}
                )
        else:
            result.append(
                {
                    "page": number,
                    "bbox": list(bounds),
                    "region_ids": [r["id"] for r in visual],
                    "reason": "reading disagreement or unresolved context boundary: full original page",
                }
            )
    return result


def build_controlled_inputs(original: Path, ir: dict, task: dict, arm: str, output: Path, *, max_images=128):
    if arm not in {"A", "B", "C", "D"}:
        raise ValueError("unknown controlled input arm")
    if set(task) - _TASK_KEYS:
        raise ValueError("unexpected public task keys; answers belong only in offline evaluation")
    if len(ir.get("documents", [])) != 1:
        raise ValueError("controlled comparison requires one original")
    document = ir["documents"][0]
    raw = original.read_bytes()
    digest = sha256(raw).hexdigest()
    if digest != document["source_sha256"] or digest != task["source_sha256"]:
        raise ValueError("original / IR / public task source hash mismatch")
    with pymupdf.open(original) as pdf:
        pages = list(range(1, pdf.page_count + 1))
        if sorted(p["page"] for p in document["pages"]) != pages:
            raise ValueError("incomplete or duplicated page inventory")
        if any(r.get("page") not in pages for r in document["regions"]):
            raise ValueError("source region outside original pages")
        registry = ir_blocks([document])
        originals = {r["id"]: r for r in document["regions"]}
        source_blocks = []
        for b in registry:
            if b.get("source") != "mineru":
                continue
            item = {k: b[k] for k in ("block_id", "page", "kind", "text")}
            item.update(
                {
                    k: b[k]
                    for k in ("reading_sha256", "requires_image_verification", "reading_risks", "diagnostics")
                    if k in b
                }
            )
            if b["kind"] == "table":
                region = originals[b["ir_region"]]
                item["html"] = region.get("html")
                item.update({k: region[k] for k in ("caption", "captions", "footnote", "footnotes") if region.get(k)})
            if b["kind"] == "table_cell":
                item.update(table_region=b["table_region"], cell=b["anchor"]["table_cell"])
            source_blocks.append(item)
        files, images, image_rows = [], [], []
        if arm in {"A", "B", "C"}:
            files = [
                {
                    "filename": "source.pdf",
                    "file_data": "data:application/pdf;base64," + base64.b64encode(raw).decode(),
                    "detail": "high",
                }
            ]
        if arm == "D":
            image_rows = _page_visuals(document, pdf)
            if len(image_rows) > max_images:
                raise ValueError("visual region limit exceeded; input is not silently truncated")
            output.mkdir(parents=True, exist_ok=True)
            for index, row in enumerate(image_rows):
                page = pdf[row["page"] - 1]
                box = pymupdf.Rect(row["bbox"])
                zoom = min(3.0, 2400 / max(box.width, box.height))
                binary = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=box, alpha=False).tobytes("png")
                name = f"original-region-{index + 1:03d}.png"
                (output / name).write_bytes(binary)
                row.update(image_sha256=sha256(binary).hexdigest(), asset=name, detail="high")
                images.append("data:image/png;base64," + base64.b64encode(binary).decode())
        page_map = {str(p): p for p in pages} if files else {}
        payload = {
            "scope": task["scope"],
            "source_type": task.get("source_type", "paper"),
            "page_number_rule": "Use 1-based ORIGINAL PHYSICAL PDF pages; never printed page labels.",
            "pdf_page_map": page_map,
            "field_catalogue": [
                {k: f[k] for k in ("field_id", "definition", "owner_kinds", "value_kinds")} for f in FIELDS.values()
            ],
            "source_blocks": [] if arm == "C" else source_blocks,
            "image_sources": image_rows,
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        manifest = {
            "version": VERSION,
            "arm": arm,
            "source_sha256": digest,
            "source_pages": pages,
            "ir_sha256": fingerprint(ir),
            "task_sha256": fingerprint(task),
            "input_sha256": fingerprint(
                {"text": text, "pdf": digest if files else None, "images": [r["image_sha256"] for r in image_rows]}
            ),
            "text_bytes": len(text.encode()),
            "pdf_bytes": len(raw) if files else 0,
            "visual_regions": image_rows,
            "parse_mode": "versioned parse cache reused",
            "parser": document.get("parser"),
            "reading_gaps": document.get("gaps", []),
            "review_policy": (
                "common source text plus required original-page images for every arm; arm C is PDF-only at extraction"
            ),
            "page_coverage": [
                {
                    "page": p["page"],
                    "parser_status": p.get("parser_status"),
                    "read_status": p.get("read_status"),
                    "text_regions": sum(b["page"] == p["page"] for b in source_blocks),
                    "original_image_supplied": bool(files) or any(r["page"] == p["page"] for r in image_rows),
                }
                for p in document["pages"]
            ],
        }
    return {"text": text, "files": files, "images": images, "registry": registry, "manifest": manifest}
