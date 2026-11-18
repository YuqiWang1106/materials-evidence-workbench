"""Best-effort, bounded diagnostic events for visual processing."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from kb_ingest.llm import allowance_exhausted
from kb_ingest.llm.errors import ModelOutcomeUnknownError, ModelRequestBindingError

from . import journal
from .hooks import error_code

type VisualContext = tuple[dict[str, str], dict[str, object] | None]
_context: ContextVar[VisualContext | None] = ContextVar("observation_visual_context", default=None)


@dataclass(frozen=True, slots=True)
class VisualObserver:
    """The bounded location metadata needed to write one attempt's events."""

    root: Path
    indexes: dict[str, int]
    reasons: dict[str, str]

    def __call__(self, step, status, blocks, **fields):
        locations = [location(block, self.indexes, self.reasons) for block in blocks]
        reason = "regular_visual_no_targets" if step == "selection" and not blocks else None
        if len(locations) == 1:
            reason = locations[0].get("reason")
        journal.record(
            self.root,
            kind="visual_step",
            stage="visual_reading",
            status=status,
            visual_step=step,
            visual_reason=reason,
            visual_targets=locations,
            **fields,
        )


def observer(root, assets, selection=None):
    """Return a recorder that emits only safe location and selection metadata."""
    if root is None:
        return None
    indexes = {a["asset_id"]: i for i, a in enumerate(assets, 1)} if len(assets) > 1 else {}
    reasons = {row["block_id"]: row["reason"] for row in (selection or {}).get("decisions", [])}
    return VisualObserver(root=Path(root), indexes=indexes, reasons=reasons)


def location(block, indexes, reasons):
    kind = "handwriting_table" if block.get("kind") == "table" else block.get("kind")
    return {
        "page": block.get("page"),
        "kind": kind,
        "bbox": ((block.get("anchor") or {}).get("coordinate_region") or {}).get("bbox"),
        "reason": "handwriting_flagged" if kind == "handwriting_table" else reasons.get(block.get("block_id")),
        "document_index": indexes.get(block.get("asset_ref")),
    }


def unavailable(record):
    if record is not None:
        with suppress(Exception):
            journal.mark_unavailable(record.root)


def create(root, assets, selection=None):
    """Never let a malformed observer prevent the processing pipeline from running."""
    with suppress(Exception):
        return observer(root, assets, selection)
    if root is not None:
        with suppress(Exception):
            journal.mark_unavailable(root)
    return None


def emit(record, step, status, blocks, **fields):
    """A broken diagnostic must never skip, repeat, or change processing."""
    if record is not None:
        with suppress(Exception):
            record(step, status, blocks, **fields)
            return
        unavailable(record)


@contextmanager
def scope(record, table=None):
    """Attach reasons or a handwritten-table location to the existing model event."""
    token = None
    if record is not None:
        token = _context.set((record.reasons, location(table, record.indexes, record.reasons) if table else None))
    try:
        yield
    finally:
        if token is not None:
            _context.reset(token)


def current():
    return _context.get()


def failure(error, phase):
    """Only report what is known; never infer a provider outcome from an error."""
    with suppress(Exception):
        if phase == "render":
            return "not_processed", "visual_batch_not_sent"
        if phase == "coverage":
            return "failed", "visual_coverage_invalid"
        if isinstance(error, ModelRequestBindingError):
            return "unknown", "model_request_binding_invalid"
        if isinstance(error, ModelOutcomeUnknownError):
            return "unknown", "model_outcome_unknown"
        if allowance_exhausted(error):
            return "not_processed", "request_allowance"
        return "unknown", error_code(error)
    return "unknown", "unknown_error"


@contextmanager
def guarded_scope(record, factory, *args):
    """Do not let diagnostics suppress, skip, or repeat the model call."""
    manager, entered = None, False
    with suppress(Exception):
        manager = factory(*args)
        manager.__enter__()
        entered = True
    if not entered:
        if manager is not None:
            with suppress(Exception):
                manager.__exit__(*sys.exc_info())
        manager = None
        unavailable(record)
    try:
        yield
    finally:
        if manager is not None:
            exited = False
            with suppress(Exception):
                manager.__exit__(*sys.exc_info())
                exited = True
            if not exited:
                unavailable(record)


def selection(record, blocks, selected, decisions):
    emit(record, "selection", "completed" if selected else "empty", selected, item_count=len(selected))
    skipped = {row.get("block_id") for row in decisions.get("decisions", []) if not row.get("selected")}
    for block in blocks:
        if block.get("block_id") in skipped:
            emit(record, "reuse", "completed", [block], item_id="visual-reuse-" + block["block_id"])


def render_failed(record, blocks, item_id, started):
    emit(
        record,
        "render",
        "failed",
        blocks,
        item_id=item_id,
        elapsed_seconds=time.monotonic() - started,
        error_code="visual_render_failed",
    )


def render_failed_if(record, phase, blocks, item_id, started):
    if phase == "render" and blocks:
        render_failed(record, blocks, item_id, started)


def results_failed(record, blocks, error, phase, prefix="visual-result-"):
    status, code = failure(error, phase)
    for block in blocks:
        emit(record, "result", status, [block], item_id=prefix + block["block_id"], error_code=code)


def region_result(record, block, item):
    empty = not item["transcript"].strip()
    status = "empty" if empty else "partial" if item["uncertainties"] else "completed"
    code = "visual_empty_transcript" if empty else None
    emit(
        record,
        "result",
        status,
        [block],
        item_id="visual-result-" + block["block_id"],
        review_count=len(item["uncertainties"]),
        error_code=code,
    )


def handwriting_result(record, table, flagged):
    unreadable = sum((cell.get("reading") or {}).get("state") == "unreadable" for cell in flagged)
    review = sum(bool((cell.get("reading") or {}).get("needs_review")) for cell in flagged)
    emit(
        record,
        "result",
        "partial" if unreadable else "completed",
        [table],
        item_id="visual-handwriting-result-" + table["block_id"],
        item_count=len(flagged),
        failed=unreadable,
        review_count=review,
    )
