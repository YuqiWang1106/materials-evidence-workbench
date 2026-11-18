"""Versioned PDF representation with explicit native and parser reading status."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pymupdf

from .readers.documents import SourceReadError, read_pdf_asset
from .readers.mineru import adapt_middle_json, resolve_mineru_command, run_mineru
from .readers.mineru_runtime import probe_mineru_runtime

SCHEMA = "materials.document_ir"
SCHEMA_VERSION = "1.0"
PARAMETERS = {"tier": "standard", "pages": "all", "ocr_mode": "auto", "format": "middle_json", "network_models": False}


def document_reading_policy(*, legacy_diagnostics: bool = False) -> dict:
    """Processing policy is independent of the reusable first parser output."""
    return {
        "version": "mineru-primary-v1",
        "primary_parser": "mineru",
        "native_reader_role": "source_geometry_and_page_inventory",
        "compare_native_readings": legacy_diagnostics,
        "automatic_rereads": legacy_diagnostics,
        "source_review_rereads": "explicit_bound_findings",
        "initial_ocr_mode": "auto",
    }


def compare_readings(regions: list[dict]) -> dict:
    """Report geometrically grounded candidate disagreements, never correctness.

    Native table text can be split by column or row. The containing parser
    region must cover each native block and their collective envelope; words
    merely sharing a page cannot be treated as alternative readings.
    """

    def box(region):
        anchor = region.get("anchor") or {}
        raw = anchor.get("bbox")
        if anchor.get("unit") != "pt" or not isinstance(raw, (list, tuple)) or len(raw) != 4:
            return None
        try:
            coordinates = [float(x) for x in raw]
            rect = pymupdf.Rect(coordinates)
            return rect if all(math.isfinite(x) for x in coordinates) and not rect.is_empty else None
        except (TypeError, ValueError):
            return None

    def tokens(text):
        normalized = unicodedata.normalize("NFKC", text).replace("−", "-")
        # Preserve signs, precision and order; Chinese adjacency is not a number boundary.
        numbers = re.findall(r"(?<![\d.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", normalized)
        words = Counter(re.findall(r"[^\W\d_][\w/+-]*|[%°µμΩ]", normalized, re.UNICODE))
        return numbers, words

    native_by_page = defaultdict(list)
    for region in regions:
        native_box = box(region)
        if region.get("source") == "pymupdf" and region.get("text", "").strip() and native_box:
            native_by_page[region.get("page")].append((region, native_box))
    differences, unpaired = [], []
    paired_count = 0
    for parser in regions:
        if parser.get("source") != "mineru" or not parser.get("text", "").strip():
            continue
        parser_box = box(parser)
        candidates = []
        if parser_box and parser.get("page") is not None:
            for native, native_box in native_by_page.get(parser["page"], []):
                intersection = (native_box & parser_box).get_area()
                if intersection / native_box.get_area() >= 0.9:
                    candidates.append((native, native_box))
        if candidates:
            envelope = pymupdf.Rect(candidates[0][1])
            for _, candidate_box in candidates[1:]:
                envelope |= candidate_box
            intersection = (envelope & parser_box).get_area()
            union = envelope.get_area() + parser_box.get_area() - intersection
            if not union or intersection / union < 0.65:
                candidates = []
        if not candidates:
            unpaired.append(parser["id"])
            continue
        paired_count += 1
        native_text = "\n".join(region["text"] for region, _ in candidates)
        native_numbers, native_words = tokens(native_text)
        parser_numbers, parser_words = tokens(parser["text"])
        reasons = []
        # Table native text may be column-major; it cannot prove cell alignment.
        numeric_mismatch = (
            Counter(native_numbers) != Counter(parser_numbers)
            if parser.get("kind") == "table" or len(candidates) > 1
            else native_numbers != parser_numbers
        )
        if numeric_mismatch:
            reasons.append("numeric_reading_mismatch")
        elif native_numbers != parser_numbers:
            reasons.append("numeric_order_or_layout_disagreement")
        if native_words != parser_words:
            reasons.append("text_or_unit_reading_mismatch")
        # A small unit vocabulary is a diagnostic, never an automatic correction.
        from kb_ingest.typed_values import _UNIT_SPELLINGS

        unit_words = sorted(set(_UNIT_SPELLINGS) | {"°", "%", "µ", "μ", "Ω"}, key=len, reverse=True)
        pattern = r"(?<![A-Za-z])(?:" + "|".join(re.escape(x) for x in unit_words) + r")(?![A-Za-z])"

        def units(text):
            return Counter(re.findall(pattern, unicodedata.normalize("NFKC", text)))

        if units(native_text) != units(parser["text"]):
            reasons.append("unit_reading_mismatch")
        if parser.get("kind") == "table" and (native_words - parser_words):
            reasons.append("table_text_or_header_gap")
        if reasons:
            differences.append(
                {
                    "kind": "reading_disagreement",
                    "page": parser["page"],
                    "reasons": reasons,
                    "pairing": "native_blocks_contained_with_matching_region_envelope",
                    "affected_region_ids": [*(r["id"] for r, _ in candidates), parser["id"]],
                    "native_readings": [
                        {"region_id": r["id"], "text": r["text"], "anchor": r["anchor"]} for r, _ in candidates
                    ],
                    "parser_reading": {"region_id": parser["id"], "text": parser["text"], "anchor": parser["anchor"]},
                    "numeric_sequences": {
                        "native": native_numbers,
                        "parser": parser_numbers,
                        "cell_alignment_confirmed": False,
                    },
                    "resolution": "requires_original_image_check",
                }
            )
    return {
        "version": 2,
        "disagreements": differences,
        "paired_region_count": paired_count,
        "unpaired_region_ids": unpaired,
        "unpaired_status": "no_reliable_comparison",
        "agreement_is_verification": False,
    }


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _verified_path(asset: dict) -> Path:
    path = Path(asset["local_path"]).expanduser().resolve(strict=True)
    expected = asset.get("sha256")
    if not isinstance(expected, str) or _sha(path) != expected:
        raise SourceReadError("source hash mismatch")
    return path


def _visible_bounds(raw: list | tuple, page_rect: pymupdf.Rect) -> tuple[pymupdf.Rect, pymupdf.Rect | None]:
    """Keep measured geometry separate from its intersection with the PDF page."""
    try:
        values = [float(value) for value in raw]
        box = pymupdf.Rect(values)
    except (TypeError, ValueError):
        raise ValueError("invalid region bbox") from None
    if len(values) != 4 or not all(math.isfinite(value) for value in values) or box.is_empty or box.is_infinite:
        raise ValueError("invalid region bbox")
    visible = box & page_rect
    return box, None if visible.is_empty else visible


def _native(asset: dict) -> tuple[list[dict], list[dict], list[dict], dict]:
    reading = read_pdf_asset(asset, visible_only=True)
    with pymupdf.open(asset["local_path"]) as document:
        rotation_matrices = {i + 1: page.rotation_matrix for i, page in enumerate(document)}
        visible_pages = {i + 1: pymupdf.Rect(page.rect) for i, page in enumerate(document)}
    pages = []
    for page in reading["pages"]:
        pages.append(
            {
                "page": page["page"],
                "width_pt": page["width"],
                "height_pt": page["height"],
                "rotation": page["rotation"],
                "image_count": page["image_count"],
                "drawing_count": page.get("drawing_count", 0),
                "native_text_characters": page["text_characters"],
                "read_status": "native_text" if page["text_characters"] else "unread_visual",
                "requires_visual_read": page["needs_visual_read"],
            }
        )
    regions, alternatives = [], []
    for block in [*reading["blocks"], *reading["alternative_blocks"]]:
        is_alternative = block.get("eligible_for_extraction") is False
        source = "pymupdf_unverified" if is_alternative else "pymupdf"
        coordinates = block["anchor"]["coordinate_region"]
        # get_text uses unrotated PDF coordinates; rendered-page coordinates
        # are the shared IR contract used by MinerU and get_pixmap(clip=...).
        bbox = list(pymupdf.Rect(coordinates["bbox"]) * rotation_matrices[block["page"]])
        raw_box, visible = _visible_bounds(bbox, visible_pages[block["page"]])
        visible_bbox = list(visible) if visible is not None else None
        region_id = "region:" + _fingerprint([asset["sha256"], source, block["page"], bbox, block["text"]])[:24]
        region = {
            "id": region_id,
            "asset_ref": asset["asset_id"],
            "source": source,
            "kind": "native_text",
            "page": block["page"],
            "reader_flags": block["reader_flags"],
            "text": block["text"],
            "anchor": {
                "page": block["page"],
                "bbox": bbox,
                "raw_bbox": list(raw_box),
                "visible_bbox": visible_bbox,
                "clipped_to_page": visible_bbox != bbox,
                "visibility": "outside_page" if visible is None else ("full" if visible_bbox == bbox else "partial"),
                "unit": "pt",
                "origin": "top_left",
                "coordinate_space": "rendered_page",
                "rotation": coordinates["rotation"],
                "precision": "block_bbox",
            },
            "cells": [],
            "caption": "",
            "footnote": "",
            "image_refs": [],
        }
        if is_alternative:
            region.update(
                {
                    "eligible_for_extraction": False,
                    "verification_state": "unverified",
                    "visibility_reason": block["visibility_reason"],
                }
            )
            alternatives.append(region)
        else:
            regions.append(region)
    return (
        pages,
        regions,
        alternatives,
        {
            "reader_flags": reading["reader_flags"],
            "reader_version": reading["reader_version"],
            "visibility_policy": reading["visibility_policy"],
            "visual_correctness_verified": False,
        },
    )


def _runtime_identity(command: list[str] | None, *, tier: str = "basic") -> dict:
    if not command:
        return {"status": "not_ready", "reason": "mineru_command_not_configured"}
    executable = Path(command[0])
    try:
        binary_hash = _sha(executable)
    except OSError:
        return {"status": "not_ready", "reason": "mineru_binary_unreadable"}
    manifest_path = os.environ.get(
        "MATERIALS_MINERU_STANDARD_MODEL_MANIFEST" if tier == "standard" else "MATERIALS_MINERU_MODEL_MANIFEST"
    )
    if not manifest_path:
        return {"status": "not_ready", "reason": "mineru_model_manifest_required"}
    try:
        manifest_hash = _sha(Path(manifest_path))
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "not_ready", "reason": "mineru_model_manifest_unreadable"}
    try:
        home = Path(os.environ["MATERIALS_MINERU_HOME"]).expanduser().resolve(strict=True)
        models = manifest.get("models", [manifest])
        if not isinstance(models, list) or not models:
            raise ValueError("model repositories required")
        for model in models:
            repo = Path(model["repo"])
            if repo.is_absolute() or len(repo.parts) != 1 or repo.name in {".", ".."}:
                raise ValueError("invalid model repo")
            root = home / "models" / repo
            entries = model["files"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("model files required")
            for entry in entries:
                relative = Path(entry["path"])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise ValueError("unsafe model path")
                model_path = root / relative
                if model_path.stat().st_size != entry["bytes"] or _sha(model_path) != entry["sha256"]:
                    raise ValueError("model file changed")
    except (OSError, KeyError, TypeError, ValueError):
        return {"status": "not_ready", "reason": "mineru_model_files_unverified"}
    installed = probe_mineru_runtime(command)
    if installed["status"] != "available":
        return installed
    return {
        "status": "available",
        "command": executable.name,
        "binary_sha256": binary_hash,
        "command_sha256": _fingerprint(command),
        "model_manifest_sha256": manifest_hash,
        "installed_runtime": installed,
        "engines": {
            key: os.environ.get(key)
            for key in (
                "MINERU_MODEL_SMALL_BACKEND",
                "MINERU_MODEL_VLM_ENGINE",
                "MINERU_MODEL_VLM_MAX_CONCURRENCY",
                "MINERU_INTRA_OP_NUM_THREADS",
                "MINERU_INTER_OP_NUM_THREADS",
            )
        },
    }


def read_document_ir(
    asset: dict,
    work_dir: str | Path,
    *,
    mineru_command: list[str] | None = None,
    tier: str = "standard",
    cancelled: Callable[[], bool] | None = None,
    reread_state_dir: str | Path | None = None,
    reread_task_binding: str | None = None,
    reread_findings: list[dict] | None = None,
    legacy_diagnostics: bool = False,
) -> dict:
    """Read MinerU content and retain native geometry for source location.

    `asset` is a registered `asset_id`/`local_path`/`sha256` mapping. The source is
    read only. Work products live under `work_dir`, outside user runtime config.
    Standard and first-pass auto OCR are the default. Offline callers can opt
    into historical comparison/automatic rereads with `legacy_diagnostics`.
    Bound source-review findings can request a reread without that opt-in.
    """
    if tier not in {"basic", "standard"}:
        raise ValueError("unsupported document parser tier")
    source = _verified_path(asset)
    pages, regions, alternatives, native_reader = _native(asset)
    work = Path(work_dir).expanduser().resolve()
    if work == source or source in work.parents:
        raise ValueError("work_dir cannot be the source asset")
    command = resolve_mineru_command(mineru_command)
    runtime = _runtime_identity(command, tier=tier)
    parameters = {**PARAMETERS, "tier": tier}
    params_hash = _fingerprint(parameters)
    parser_checkpoint = {
        "ir_schema_version": SCHEMA_VERSION,
        "source_sha256": asset["sha256"],
        "runtime_sha256": _fingerprint(runtime),
        "parameters_sha256": params_hash,
    }
    policy = document_reading_policy(legacy_diagnostics=legacy_diagnostics)
    checkpoint = {**parser_checkpoint, "reading_policy_sha256": _fingerprint(policy)}
    result = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "asset_ref": asset["asset_id"],
        "source_sha256": asset["sha256"],
        "pages": pages,
        "regions": regions,
        "alternative_readings": alternatives,
        "native_reader": native_reader,
        "runtime": runtime,
        "reading_policy": policy,
        "checkpoint": checkpoint,
        "read_status": "partial",
        "gaps": [],
        "image_assets": [],
        "reading_disagreements": [],
        "reading_comparison": {
            "paired_region_count": 0,
            "status": "parser_not_available" if legacy_diagnostics else "disabled_by_policy",
            "agreement_is_verification": False,
        },
    }
    if cancelled and cancelled():
        result["gaps"].append({"kind": "reading_cancelled"})
        return result
    if runtime["status"] != "available":
        result["gaps"].append({"kind": "parser_not_ready", "reason": runtime["reason"]})
        for page in pages:
            if page["requires_visual_read"]:
                result["gaps"].append({"kind": "visual_read_not_done", "page": page["page"]})
        return result
    work.mkdir(parents=True, exist_ok=True)
    cached_path = work / "document-ir-v1.checkpoint.json"
    middle_path = work / "middle_json.json"
    parser_output = None
    if cached_path.is_file() and middle_path.is_file():
        try:
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict) or not isinstance(cached.get("checkpoint"), dict):
                raise ValueError("invalid parser checkpoint")
            cached_parser = {
                key: value for key, value in cached["checkpoint"].items() if key != "reading_policy_sha256"
            }
            if cached_parser == parser_checkpoint and cached.get("middle_sha256") == _sha(middle_path):
                parser_output = {
                    "status": "ok",
                    "middle": json.loads(middle_path.read_text(encoding="utf-8")),
                    "output_sha256": cached["middle_sha256"],
                    "reused": True,
                }
        except (OSError, ValueError, KeyError):
            parser_output = None
    if parser_output is None:
        parser_output = run_mineru(
            source, work, command, cancelled=cancelled, tier=tier, ocr_mode=parameters["ocr_mode"]
        )
    if parser_output["status"] != "ok":
        result["gaps"].append({"kind": "parser_not_complete", **parser_output})
        return result
    if _sha(source) != asset["sha256"]:
        raise SourceReadError("source hash changed during parse")
    try:
        adapted = adapt_middle_json(
            parser_output["middle"],
            asset_ref=asset["asset_id"],
            source_sha256=asset["sha256"],
            page_count=len(pages),
            page_dimensions={p["page"]: (p["width_pt"], p["height_pt"]) for p in pages},
        )
        if tier == "standard" and adapted["parser_mode"].get("tier") != "standard":
            raise ValueError("requested Standard tier not confirmed by parser")
    except ValueError as exc:
        result["gaps"].append({"kind": "parser_protocol_invalid", "reason": str(exc)})
        return result
    result["regions"].extend(adapted["regions"])
    if legacy_diagnostics:
        comparison = compare_readings(result["regions"])
        result["reading_disagreements"] = comparison.pop("disagreements")
        result["reading_comparison"] = comparison
    result["parser"] = {
        "status": adapted["status"],
        "producer": adapted["producer"],
        "mode": adapted["parser_mode"],
        "middle_sha256": parser_output["output_sha256"],
        "checkpoint_reused": bool(parser_output.get("reused")),
    }
    result["gaps"].extend(adapted["gaps"])
    for page in pages:
        parser_status = adapted["page_status"][page["page"]]
        if (
            (page["image_count"] or page["drawing_count"])
            and not page["native_text_characters"]
            and not any(region.get("page") == page["page"] for region in adapted["regions"])
        ):
            parser_status = "partial"
            result["gaps"].append({"kind": "parser_page_content_missing", "page": page["page"]})
        page["parser_status"] = parser_status
        page["read_status"] = "parsed" if parser_status == "parsed" else page["read_status"]
        if parser_status != "parsed" and page["requires_visual_read"]:
            result["gaps"].append({"kind": "visual_read_not_done", "page": page["page"]})
    # Preserve the first reading separately; subsequent OCR never overwrites it.
    from kb_ingest.core import atomic_bytes, atomic_json

    if middle_path.is_file() and _sha(middle_path) == parser_output["output_sha256"]:
        original_reading = work / "readings" / (parser_output["output_sha256"] + ".json")
        if not original_reading.is_file():
            atomic_bytes(original_reading, middle_path.read_bytes())
    if reread_state_dir is not None and (legacy_diagnostics or reread_findings):
        if not reread_task_binding:
            raise ValueError("reread task binding required")
        from kb_ingest.document_reread import apply_rereads

        apply_rereads(
            result,
            asset=asset,
            source=source,
            cache_dir=work / "reread-cache",
            state_dir=reread_state_dir,
            task_binding=reread_task_binding,
            command=command,
            runtime_for_tier=lambda requested_tier: (
                runtime if requested_tier == tier else _runtime_identity(command, tier=requested_tier)
            ),
            runner=run_mineru,
            adapter=adapt_middle_json,
            compare=compare_readings,
            cancelled=cancelled,
            findings=reread_findings,
            legacy_diagnostics=legacy_diagnostics,
        )
    if not result["gaps"] and all(p.get("parser_status") == "parsed" for p in pages):
        result["read_status"] = "complete"
    retain_visual_assets(asset, result, work, cancelled=cancelled)
    if _sha(source) != asset["sha256"]:
        raise SourceReadError("source hash changed during reread")
    atomic_json(cached_path, {"checkpoint": checkpoint, "middle_sha256": parser_output["output_sha256"]})
    if cancelled and cancelled():
        result["read_status"] = "partial"
        result["gaps"].append({"kind": "reading_cancelled"})
    return result


def retain_visual_assets(asset: dict, result: dict, work: Path, *, cancelled=None) -> None:
    """Retain original crops independently of a parser's optional image export.

    These assets preserve evidence, not an assertion that the OCR is correct.
    Missing geometry stays an explicit gap rather than an invented location.
    """
    result["image_assets"] = []
    for region in result["regions"]:
        if region["kind"] not in {"image", "chart", "table", "equation"}:
            continue
        region["retained_image_assets"] = []
        if cancelled and cancelled():
            result["gaps"].append({"kind": "visual_asset_cancelled", "region_id": region["id"]})
            result["read_status"] = "partial"
            break
        try:
            anchor, uri = render_region(asset, region)
            content = base64.b64decode(uri.split(",", 1)[1])
            sha = hashlib.sha256(content).hexdigest()
            relative = Path("images") / f"{sha}.png"
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or _sha(target) != sha:
                temporary = target.with_suffix(f".{uuid4().hex}.tmp")
                temporary.write_bytes(content)
                temporary.replace(target)
            image_id = "image-" + _fingerprint([asset["sha256"], region["id"], sha])[:24]
            result["image_assets"].append(
                {
                    "asset_id": image_id,
                    "region_id": region["id"],
                    "path": relative.as_posix(),
                    "sha256": sha,
                    "source_sha256": asset["sha256"],
                    "anchor": anchor,
                    "origin": "original_pdf_render",
                    "media_type": "image/png",
                }
            )
            region["retained_image_assets"].append(image_id)
        except (OSError, ValueError, RuntimeError) as exc:
            result["gaps"].append(
                {
                    "kind": "visual_asset_unavailable",
                    "region_id": region["id"],
                    "page": region.get("page"),
                    "reason": type(exc).__name__,
                }
            )
            result["read_status"] = "partial"


JPEG_QUALITY = 85


def render_region(
    asset: dict,
    region: dict,
    *,
    max_dimension: int = 1800,
    zoom: float = 2.0,
    image_format: str = "png",
    grayscale: bool = False,
) -> tuple[dict, str]:
    """Render an evidenced PDF crop; parser asset paths are never opened.

    `image_format='jpeg'` (quality 85) keeps model uploads several times smaller than PNG for scans and photos;
    `grayscale` drops colour that a scanned text page does not need.
    """
    if image_format not in ("png", "jpeg"):
        raise ValueError("render format must be png or jpeg")
    if type(max_dimension) is not int or not 1 <= max_dimension <= 4096:
        raise ValueError("render dimension must be within 1..4096 pixels")
    if type(zoom) not in (int, float) or not 0 < zoom <= 4:
        raise ValueError("render zoom must be within (0,4]")
    path = _verified_path(asset)
    page_number = region.get("page")
    anchor = region.get("anchor") or {}
    raw = anchor.get("bbox")
    if anchor.get("unit") != "pt" or not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("region bbox in PDF points required")
    if not isinstance(page_number, int) or isinstance(page_number, bool):
        raise ValueError("known source page required")
    with pymupdf.open(path) as document:
        if not 1 <= page_number <= len(document):
            raise ValueError("source page outside PDF")
        page = document[page_number - 1]
        raw_box, box = _visible_bounds(raw, page.rect)
        if box is None:
            raise ValueError("region bbox has no visible intersection with source page")
        scale = min(zoom, max_dimension / max(box.width, box.height))
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=box,
            alpha=False,
            colorspace=pymupdf.csGRAY if grayscale else pymupdf.csRGB,
        )
        data = pixmap.tobytes("jpeg", jpg_quality=JPEG_QUALITY) if image_format == "jpeg" else pixmap.tobytes("png")
        uri = f"data:image/{image_format};base64," + base64.b64encode(data).decode("ascii")
        return {
            "kind": "pdf",
            "asset_ref": asset["asset_id"],
            "page": page_number,
            "bbox": list(box),
            "raw_bbox": list(raw_box),
            "visible_bbox": list(box),
            "clipped_to_page": box != raw_box,
            "unit": "pt",
            "rotation": page.rotation,
            "source_sha256": asset["sha256"],
            "precision": "original_crop",
        }, uri
