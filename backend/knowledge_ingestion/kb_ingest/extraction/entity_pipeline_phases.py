"""Visual read, window extraction, check and synonym phases of entity extraction."""

from __future__ import annotations

import logging
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from kb_ingest.core import atomic_json
from kb_ingest.core.interrupted import save_interrupted
from kb_ingest.ingest.documents.document_ir import render_region
from kb_ingest.ingest.documents.document_pipeline_visual import read_visual_regions
from kb_ingest.llm import ModelCallError, allowance_exhausted
from kb_ingest.llm.errors import ModelOutcomeUnknownError, ModelRequestBindingError
from kb_ingest.llm.logical_request import LogicalRequestFamily
from kb_ingest.llm.model_limits import RUN_REQUEST_CEILING
from kb_ingest.observation import hooks as _observe
from kb_ingest.observation import visual_steps as _visual

from .entity_check import BlockText, check_graph
from .entity_contract import (
    PAGE_READ_INSTRUCTIONS,
    PAGE_READ_SCHEMA,
    SYNONYM_INSTRUCTIONS,
    SYNONYM_SCHEMA,
    validate_instance,
)
from .entity_extract import (
    WINDOW_REQUESTS,
    extract_window,
    known_before,
    known_catalogue,
)
from .entity_merge import merge_synonyms, synonym_payload
from .entity_pipeline_blocks import SCAN_RENDER_PIXELS, apply_page_readings
from .entity_pipeline_constants import CLOSING_REQUESTS
from .entity_request_identity import visual_region_units
from .entity_visual import select_visual_blocks

logger = logging.getLogger(__name__)


def visual_phase(blocks, assets, call, *, documents=None, work=None, request_identity=None):
    """Scanned pages are transcribed segment by segment with positions; embedded images are read as regions.
    Returns (blocks with scanned pages replaced by their segments, readings, unresolved)."""
    asset_map = {asset["asset_id"]: asset for asset in assets}
    selected, selection = select_visual_blocks(blocks, documents)
    if work is not None:
        atomic_json(Path(work) / "entity-visual-selection.json", selection)
    observer = _visual.create(work, assets, selection)
    _visual.selection(observer, blocks, selected, selection)
    pages = [block for block in selected if block["kind"] == "unread_page"]
    page_plan = (
        request_identity.plan(LogicalRequestFamily.VISUAL_PAGE, [[block] for block in pages])
        if request_identity
        else ()
    )
    page_bindings = {
        block["block_id"]: binding for block, binding in zip(pages, page_plan, strict=bool(request_identity))
    }
    region_plan = (
        request_identity.plan(LogicalRequestFamily.VISUAL_REGIONS, visual_region_units(selected))
        if request_identity
        else None
    )
    readings: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    for block in selected:
        if block["kind"] != "unread_page":
            continue
        phase = "render"
        render_id = "visual-page-render-" + block["block_id"]
        _visual.emit(observer, "render", "started", [block], item_id=render_id)
        render_started = time.monotonic()
        try:
            # Scanned tables are small print: a sharper render than region previews keeps digits and characters legible.
            image = render_region(
                asset_map[block["asset_ref"]],
                {"page": block["page"], "anchor": block["anchor"]["coordinate_region"]},
                max_dimension=SCAN_RENDER_PIXELS,
                zoom=4,
                image_format="jpeg",
                grayscale=True,
            )[1]
            render_elapsed = time.monotonic() - render_started
            _visual.emit(observer, "render", "completed", [block], item_id=render_id, elapsed_seconds=render_elapsed)
            phase = "call"
            with (
                _visual.guarded_scope(observer, _observe.visual_asset, block["asset_ref"]),
                _visual.guarded_scope(observer, _visual.scope, observer),
            ):
                result = call(
                    "visual-page",
                    PAGE_READ_INSTRUCTIONS,
                    {"block_id": block["block_id"], "page": block["page"]},
                    PAGE_READ_SCHEMA,
                    images=[image],
                    **({"logical_binding": page_bindings[block["block_id"]]} if request_identity else {}),
                )
            phase = "coverage"
            validate_instance(result, PAGE_READ_SCHEMA)
            if not result["segments"]:
                raise ValueError("page reading returned no text")
            readings.append(
                {
                    **result,
                    "block_id": block["block_id"],
                    "asset_ref": block["asset_ref"],
                    "page": block["page"],
                    "transcript": "\n".join(segment["text"] for segment in result["segments"]),
                }
            )
            unresolved.extend(
                {"block_id": block["block_id"], "reason": item} for item in result.get("uncertainties", [])
            )
            count = len(result.get("uncertainties", []))
            _visual.emit(
                observer,
                "result",
                "partial" if count else "completed",
                [block],
                item_id="visual-page-result-" + block["block_id"],
                review_count=count,
            )
        except (ModelOutcomeUnknownError, ModelRequestBindingError) as exc:
            status, code = _visual.failure(exc, phase)
            _visual.emit(
                observer, "result", status, [block], item_id="visual-page-result-" + block["block_id"], error_code=code
            )
            _save_interrupted_visual(work, blocks, readings, [], exc.code, unresolved)
            raise
        except (ModelCallError, ValueError) as exc:
            if phase == "render":
                _visual.emit(
                    observer,
                    "render",
                    "failed",
                    [block],
                    item_id=render_id,
                    elapsed_seconds=time.monotonic() - render_started,
                    error_code="visual_render_failed",
                )
            status, code = _visual.failure(exc, phase)
            if phase == "coverage" and isinstance(result, dict) and not result.get("segments"):
                code = "visual_empty_page"
            _visual.emit(
                observer, "result", status, [block], item_id="visual-page-result-" + block["block_id"], error_code=code
            )
            unresolved.append({"block_id": block["block_id"], "reason": type(exc).__name__})
    blocks = apply_page_readings(blocks, readings)

    def preserve_mixed(mixed, gaps, error):
        _save_interrupted_visual(work, blocks, readings, mixed, error.code, [*unresolved, *gaps])

    mixed, gaps = read_visual_regions(
        selected, asset_map, call, logical_bindings=region_plan, on_interrupted=preserve_mixed, observer=observer
    )
    return blocks, readings + mixed, unresolved + gaps


def _save_interrupted_visual(work, blocks, page_readings, mixed_readings, reason, gaps=()):
    if work is None:
        return
    try:
        completed_ids = {row["block_id"] for row in mixed_readings}
        completed_ids.update(
            row["block_id"] + "s" + str(index + 1) for row in page_readings for index, _ in enumerate(row["segments"])
        )
        completed_blocks = [
            block for block in apply_page_readings(blocks, page_readings) if block["block_id"] in completed_ids
        ]
        save_interrupted(
            Path(work),
            {
                "status": "interrupted",
                "stage": "visual_reading",
                "reason": reason,
                "completed_readings": [*page_readings, *mixed_readings],
                "completed_blocks": completed_blocks,
                "unresolved": list(gaps),
            },
        )
    except (OSError, TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        # The original unknown remains authoritative; never log the source or path.
        logger.warning("Interrupted visual preservation failed: %s", type(error).__name__)


@contextmanager
def preserve_visual_on_interruption(work, blocks, readings, gaps):
    """Retain the completed visual stage if a dependent step is interrupted."""
    try:
        yield
    except (ModelOutcomeUnknownError, ModelRequestBindingError) as error:
        pages = [row for row in readings if row.get("segments")]
        mixed = [row for row in readings if not row.get("segments")]
        _save_interrupted_visual(work, blocks, pages, mixed, error.code, gaps)
        raise


def extract_phase(
    windows, blocks, call, work=None, known=(), progress=None, ceiling=RUN_REQUEST_CEILING, logical_bindings=None
):
    """Windows run in order; each later window sees the entity names found so far. Output is checked per window.

    `known` graphs (mapped tables) come first: windows see their formulations, and they merge with the rest.
    """
    text = BlockText(blocks)
    raw: list[dict[str, object]] = []
    checked, unresolved = list(known), []
    if progress:
        progress(completed=0, total=len(windows), failed=0, unit="个窗口")
    for position, window in enumerate(windows):
        step = _observe.start(work, "window", window["window_id"])
        # Extra attribute calls for dense windows only use requests that the remaining windows do not need.
        spare = ceiling - CLOSING_REQUESTS - getattr(call, "sent", 0) - WINDOW_REQUESTS * (len(windows) - position)
        try:
            result = extract_window(
                window,
                call,
                known_before(known_catalogue(checked), window, text.order),
                extra_calls=max(0, spare),
                logical_binding=logical_bindings[position] if logical_bindings is not None else None,
            )
        except (ModelOutcomeUnknownError, ModelRequestBindingError) as error:
            if work is not None:
                try:
                    _save_interrupted_windows(work, raw, checked, error.code, window["window_id"])
                except (OSError, TypeError, ValueError) as storage_error:
                    # Do not expose the document, path or error details in logs.
                    logger.warning("Interrupted window preservation failed: %s", type(storage_error).__name__)
            raise
        except (ModelCallError, ValueError) as exc:
            # Not sent because the document's allowance is used up: not a failure, the window can be resumed.
            reason = "not_processed" if allowance_exhausted(exc) else type(exc).__name__
            unresolved.append({"window": window["window_id"], "reason": reason, "detail": str(exc)[:200]})
            step.failed(exc, not_processed=reason == "not_processed")
            if progress:
                progress(completed=position + 1, total=len(windows), failed=len(unresolved), unit="个窗口")
            continue
        raw.append({"window_id": window["window_id"], **result})
        kept = check_graph(result, text, window_block_ids=[block["block_id"] for block in window["blocks"]])
        checked.append({"window_id": window["window_id"], **kept})
        step.ended(attributes=len(kept.get("attributes", [])))
        if progress:
            progress(completed=position + 1, total=len(windows), failed=len(unresolved), unit="个窗口")
    if work is not None:
        atomic_json(work / "entity-raw.json", {"windows": raw})
        atomic_json(
            work / "entity-check.json",
            {
                "windows": [{key: value for key, value in item.items()} for item in checked],
                "summary": check_summary(raw, checked),
            },
        )
    return checked, unresolved


def _save_interrupted_windows(work, raw, checked, reason, window_id):
    save_interrupted(
        Path(work),
        {
            "status": "interrupted",
            "reason": reason,
            "interrupted_window_id": window_id,
            "raw_windows": raw,
            "checked_windows": checked,
        },
    )


def check_summary(raw, checked):
    def count(graphs, key):
        return sum(len(graph.get(key, [])) for graph in graphs)

    dropped = Counter((item["kind"], item["reason"]) for graph in checked for item in graph.get("dropped", []))
    kept_numbers = Counter(item.get("numbers") for graph in checked for item in graph.get("attributes", []))
    lost = dropped.get(("attribute", "number is not in the source"), 0)
    with_numbers = kept_numbers["in_quote"] + kept_numbers["extended"] + lost
    return {
        "proposed": {key: count(raw, key) for key in ("entities", "attributes", "related")},
        "kept": {key: count(checked, key) for key in ("entities", "attributes", "related")},
        "dropped": [{"kind": kind, "reason": reason, "count": value} for (kind, reason), value in dropped.items()],
        # Regression measure for prompts: share of attributes with numbers whose quote already shows them.
        "numbers": {
            "attributes_with_numbers": with_numbers,
            "in_quote": kept_numbers["in_quote"],
            "located_by_nearby_blocks": kept_numbers["extended"],
            "dropped": lost,
            "quote_coverage": round(kept_numbers["in_quote"] / with_numbers, 3) if with_numbers else None,
        },
    }


def synonym_phase(graph, call, unresolved=None):
    """One call per paper groups names of the same concept that different windows wrote differently."""
    payload = synonym_payload(graph)
    if not payload:
        return graph, []
    try:
        result = call("synonyms", SYNONYM_INSTRUCTIONS, {"entities": payload}, SYNONYM_SCHEMA)
        validate_instance(result, SYNONYM_SCHEMA)
    except (ModelCallError, ValueError) as exc:
        if unresolved is None:
            raise
        unresolved.append({"phase": "synonyms", "reason": type(exc).__name__})
        return graph, []
    return merge_synonyms(graph, result["groups"])
