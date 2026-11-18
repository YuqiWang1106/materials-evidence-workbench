"""One entry point for a handwritten original: read it, give the page view a frontend can render, and
optionally extract entities whose evidence points back to boxes in the original image.

``process_handwriting(file, name, description=None, *, readers, extraction=None)`` returns a
``handwriting-view-v1`` dict:

- ``file``: name, description, file name, SHA-256, media type, and every page's size in ``unit``
  (``px`` of the image as displayed, i.e. with its EXIF orientation applied, for JPG/PNG; ``pt`` of the
  displayed, /Rotate-applied page for PDF).
- ``reading``: read status, reader versions, gaps, and how many items need review.
- ``regions``: tables (with every cell) and text lines in reading order. Each carries ``bbox`` in the
  page's unit, ``bbox_norm`` (0-1 of the page), its text, reading state, ``needs_review`` and both readers'
  texts, so a viewer can draw boxes on the original image without knowing the reader internals.
- ``document``: the shared document IR. ``extract_handwriting`` passes it to entity extraction as it is,
  so the file is not read twice.
- ``extraction`` (only with ``extraction=...``): the entity draft (entities, attributes, related links)
  where every piece of evidence has ``locations`` on the original image (page, bbox, bbox_norm, and the
  region / cell it came from). The draft stays in the extraction work directory; nothing is written to
  the knowledge base, and confirming such a draft is not wired to any task or page yet.

Reading never calls a model API. Extraction does (it is paid) and only runs when asked for.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pymupdf

from kb_ingest.core import fingerprint
from kb_ingest.core.paths import HANDWRITING_RUNTIME as RUNTIME

from .readers.handwriting import HANDWRITING_SUFFIXES
from .readers.handwriting_compare import read_handwriting_ir

VIEW_VERSION = "handwriting-view-v1"


@dataclass(frozen=True)
class HandwritingReaders:
    """Where the two local readers live. Paths are absolute; the second reader is optional."""

    first_python: str
    mineru_models: str
    second_python: str | None = None
    second_model: str | None = None
    backend: str = "auto"
    dtype: str = "auto"
    seconds_per_item: float | None = None
    first_script: str = str(RUNTIME / "handwriting_reader.py")
    second_script: str = str(RUNTIME / "second_reader.py")

    def options(self):
        """The reader options of entity extraction (kb_ingest/extraction/entity_pipeline.py) for these readers."""
        result = {
            "handwriting_command": [self.first_python, "-I", self.first_script],
            "handwriting_model_root": self.mineru_models,
        }
        if self.second_python and self.second_model:
            result.update(
                handwriting_second_command=[
                    self.second_python,
                    "-I",
                    self.second_script,
                    "--backend",
                    self.backend,
                    "--dtype",
                    self.dtype,
                ],
                handwriting_second_model=self.second_model,
                handwriting_second_seconds_per_item=self.seconds_per_item,
            )
        return result


@dataclass(frozen=True)
class HandwritingExtraction:
    """What entity extraction needs: the documents model configuration (with its API key) and a work directory.

    ``model_check`` also sends the cells the readers flagged to the vision model API (more paid requests).
    """

    model_config: dict
    work_dir: str
    source_type: str = "paper"
    model_check: bool = False
    entity_store: object = None
    extra_options: dict = field(default_factory=dict)
    workspace_id: str | None = None


def _asset(path, name):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "asset_id": "handwriting-" + digest[:16],
        "original_name": path.name,
        "local_path": str(path),
        "sha256": digest,
        "title": name,
    }


def _page_scale(path, ir):
    """Page unit per IR point: original image pixels for JPG/PNG (one page), points for PDF."""
    if path.suffix.lower() == ".pdf":
        return "pt", {page["page"]: 1.0 for page in ir["pages"]}
    pixmap = pymupdf.Pixmap(str(path))
    page = ir["pages"][0]
    width, height = pixmap.width, pixmap.height
    # PyMuPDF opens a photo upright (EXIF orientation applied); the stored pixels may be turned a quarter.
    if width != height and (width > height) != (float(page["width_pt"]) > float(page["height_pt"])):
        width, height = height, width
    return "px", {page["page"]: width / float(page["width_pt"])}


def _box(bbox, scale, size):
    box = [round(float(value) * scale, 2) for value in bbox]
    width, height = size
    return box, [
        round(box[0] / width, 5),
        round(box[1] / height, 5),
        round(box[2] / width, 5),
        round(box[3] / height, 5),
    ]


def _reading_view(reading):
    reading = reading or {}
    return {
        "state": reading.get("state"),
        "needs_review": bool(reading.get("needs_review")),
        "flags": list(reading.get("flags") or []),
        "score": reading.get("score"),
        "second_reading": (reading.get("second") or {}).get("text"),
    }


def build_view(path, name, description, ir):
    """The frontend view of a read handwritten original (see the module docstring)."""
    unit, scales = _page_scale(path, ir)
    pages = {
        page["page"]: (
            round(page["width_pt"] * scales[page["page"]], 2),
            round(page["height_pt"] * scales[page["page"]], 2),
        )
        for page in ir["pages"]
    }
    regions = []
    for region in ir["regions"]:
        number, scale = region["page"], scales[region["page"]]
        bbox, norm = _box(region["anchor"]["bbox"], scale, pages[number])
        item = {
            "region_id": region["id"],
            "page": number,
            "kind": region["kind"],
            "bbox": bbox,
            "bbox_norm": norm,
            "text": region.get("text", ""),
        }
        if region["kind"] == "table":
            cells = []
            for cell in region["cells"]:
                cell_box, cell_norm = _box(cell["bbox"], scale, pages[number])
                cells.append(
                    {
                        "cell_id": f"{region['id']}:r{cell['row']}c{cell['col']}",
                        "row": cell["row"],
                        "col": cell["col"],
                        "row_span": cell["row_span"],
                        "col_span": cell["col_span"],
                        "bbox": cell_box,
                        "bbox_norm": cell_norm,
                        "text": cell["text"],
                        **_reading_view(cell.get("reading")),
                    }
                )
            item["table"] = {
                "rows": 1 + max((c["row"] + c["row_span"] - 1 for c in cells), default=-1),
                "cols": 1 + max((c["col"] + c["col_span"] - 1 for c in cells), default=-1),
                "rotation": (region.get("reading") or {}).get("rotation"),
                "cells": cells,
            }
            item["needs_review_count"] = sum(1 for c in cells if c["needs_review"])
        else:
            item.update(_reading_view(region.get("reading")))
        regions.append(item)
    return {
        "version": VIEW_VERSION,
        "file": {
            "name": name,
            "description": description,
            "file_name": path.name,
            "sha256": ir["source_sha256"],
            "media_type": mimetypes.guess_type(path.name)[0],
            "unit": unit,
            "pages": [{"page": number, "width": size[0], "height": size[1]} for number, size in sorted(pages.items())],
        },
        "reading": {
            "status": ir["read_status"],
            "runtime": ir["runtime"],
            "gaps": ir["gaps"],
            "review": ir.get("handwriting_review"),
        },
        "regions": regions,
        "document": ir,
        "extraction": None,
    }


def read_handwriting(file, name, description=None, *, readers, work_dir=None):
    """Read a handwritten PDF/JPG/PNG with the two local readers; no model API call."""
    path = Path(file).resolve()
    if path.suffix.lower() not in HANDWRITING_SUFFIXES:
        raise ValueError("handwritten file must be PDF, JPG or PNG")
    if not str(name or "").strip():
        raise ValueError("name is required")
    options = readers.options()
    asset = _asset(path, name)
    with tempfile.TemporaryDirectory() as temporary:
        ir = read_handwriting_ir(
            asset,
            Path(work_dir or temporary) / "reading",
            command=options["handwriting_command"],
            model_root=options["handwriting_model_root"],
            second_command=options.get("handwriting_second_command"),
            second_model=options.get("handwriting_second_model"),
            second_seconds_per_item=options.get("handwriting_second_seconds_per_item"),
        )
    ir["description"] = description
    view = build_view(path, name, description, ir)
    view["_asset"] = asset
    return view


def locate_extraction(view, graph, blocks):
    """Evidence of an entity draft → boxes on the original. `blocks` is entity-blocks.json (block id → block)."""
    unit, scales = _page_scale(Path(view["_asset"]["local_path"]), view["document"])
    sizes = {page["page"]: (page["width"], page["height"]) for page in view["file"]["pages"]}

    def where(block_id, quote=None):
        block = blocks.get(block_id) or {}
        region = (block.get("anchor") or {}).get("coordinate_region") or {}
        page = block.get("page") or region.get("page")
        location = {"block_id": block_id, "quote": quote, "page": page}
        if region.get("bbox") and page in scales:
            location["bbox"], location["bbox_norm"] = _box(region["bbox"], scales[page], sizes[page])
        if block.get("ir_region"):
            location["region_id"] = block["ir_region"]
        cell = (block.get("anchor") or {}).get("table_cell")
        if cell:
            region_id = block.get("ir_region")
            location["cell"] = {
                "row": cell["row"],
                "col": cell["column"],
                "cell_id": f"{region_id}:r{cell['row']}c{cell['column']}" if region_id else None,
            }
        return location

    entities = [
        {**entity, "locations": [where(m.get("block_id"), m.get("quote")) for m in entity.get("mentions") or []]}
        for entity in graph.get("entities", [])
    ]
    attributes = [
        {
            **attribute,
            "locations": [
                where(block_id, attribute.get("quote"))
                for block_id in attribute.get("block_ids") or [attribute.get("block_id")]
                if block_id
            ],
        }
        for attribute in graph.get("attributes", [])
    ]
    related = [
        {**link, "locations": [where(item.get("block_id"), item.get("quote")) for item in link.get("locations") or []]}
        for link in graph.get("related", [])
    ]
    return {"unit": unit, "entities": entities, "attributes": attributes, "related": related}


def extract_handwriting(view, extraction):
    """Entity extraction on an already read handwritten original (paid model calls); returns the located draft."""
    from kb_ingest.extraction.entity_pipeline import extract_entity_document
    from kb_ingest.storage.engine import file_engine

    work = Path(extraction.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    asset = view["_asset"]
    run = "handwriting:" + uuid4().hex
    # The draft step does not read or write the knowledge base (that happens when a draft is confirmed);
    # without a workspace store a private empty one keeps the pipeline's contract.
    store = extraction.entity_store or file_engine(work / "draft-entities.sqlite3")
    options = {
        **extraction.extra_options,
        "documents": [view["document"]],
        "work_dir": str(work),
        "workspace_id": extraction.workspace_id or extraction.extra_options.get("workspace_id"),
        "model_config": extraction.model_config,
        "source_type": extraction.source_type,
        "entity_store": store,
        "reader_options": {"handwriting": True, "handwriting_model_check": bool(extraction.model_check)},
    }
    package = extract_entity_document([asset], "handwriting-" + asset["sha256"][:16], options, run)
    artifacts = work / "artifacts" / fingerprint(run)
    graph_path, blocks_path = artifacts / "entity-graph.json", artifacts / "entity-blocks.json"
    # Gaps and unresolved steps (e.g. missing model credentials, failed windows) are the package's issues;
    # reading gaps stay in view['reading']['gaps']. The work path is private (not sent to a frontend).
    view["_extraction_artifacts"] = str(artifacts)
    result = {"status": "not_extracted", "issues": [issue["description"] for issue in package.get("issues", [])]}
    if graph_path.is_file() and blocks_path.is_file():
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
        result.update(
            status=(graph.get("summary") or {}).get("status", "awaiting_confirmation"),
            summary=graph.get("summary"),
            **locate_extraction(view, graph, blocks),
        )
    return result


def process_handwriting(file, name, description=None, *, readers, extraction=None, work_dir=None):
    """Read a handwritten original and, when `extraction` is given, extract a located entity draft.

    `readers` is a HandwritingReaders; `extraction` a HandwritingExtraction (paid) or None (reading only).
    The returned view is JSON-serialisable; private keys (`_asset`, `_extraction_artifacts`: local paths) and
    the IR are dropped by `public_view` before it goes to a frontend. With `work_dir`, the readers' work files
    (and their logs when a reader fails) are kept there.
    """
    view = read_handwriting(file, name, description, readers=readers, work_dir=work_dir)
    if extraction is not None:
        view["extraction"] = extract_handwriting(view, extraction)
        # The optional model check rewrites checked cells in the IR; show the same texts the evidence quotes.
        fresh = build_view(Path(view["_asset"]["local_path"]), name, description, view["document"])
        view.update(regions=fresh["regions"], reading=fresh["reading"])
    return view


def public_view(view, *, include_document=False):
    """The view without private keys (local paths) and without the IR unless asked for."""
    return {
        key: value for key, value in view.items() if not key.startswith("_") and (include_document or key != "document")
    }


def readers_from_environment(environ=None):
    """HandwritingReaders from MATERIALS_HANDWRITING_* variables, for scripts and local services."""
    env = os.environ if environ is None else environ
    missing = [key for key in ("MATERIALS_HANDWRITING_FIRST_PYTHON", "MATERIALS_HANDWRITING_MINERU_MODELS") if not env.get(key)]
    if missing:
        raise ValueError("missing " + ", ".join(missing))
    return HandwritingReaders(
        first_python=env["MATERIALS_HANDWRITING_FIRST_PYTHON"],
        mineru_models=env["MATERIALS_HANDWRITING_MINERU_MODELS"],
        second_python=env.get("MATERIALS_HANDWRITING_SECOND_PYTHON") or None,
        second_model=env.get("MATERIALS_HANDWRITING_SECOND_MODEL") or None,
        backend=env.get("MATERIALS_HANDWRITING_SECOND_BACKEND", "auto"),
        dtype=env.get("MATERIALS_HANDWRITING_SECOND_DTYPE", "auto"),
        seconds_per_item=env.get("MATERIALS_HANDWRITING_SECOND_SECONDS_PER_ITEM") or None,
    )
