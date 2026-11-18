"""How the reading of one original ended, as timeline codes derived from the document IR it produced.

The readers are observed from outside: one step for the parser (PyMuPDF and MinerU together) and one for the
local OCR of scanned pages. Their queue waits and inner steps are not separate events.
"""

from __future__ import annotations

from pathlib import Path

from kb_ingest.observation import hooks

# The first of these gaps found decides how the parser step ended.
_PARSER_GAPS = (
    ("reading_cancelled", "interrupted", "cancelled"),
    ("parser_not_ready", "not_processed", "reading_incomplete"),
    ("parser_not_complete", "failed", "reading_incomplete"),
    ("parser_protocol_invalid", "failed", "reading_incomplete"),
)
_OCR_STATUS = {
    "complete": ("completed", None),
    "partial": ("partial", "ocr_reader_incomplete"),
    "failed": ("failed", "ocr_reader_failed"),
    "cancelled": ("interrupted", "cancelled"),
    "not_configured": ("not_processed", "ocr_reader_not_configured"),
}


def start(root: Path | None, reader: str, number: int) -> hooks.Step:
    """Record that a reader started on the `number`-th original."""
    return hooks.start(root, "reader_step", f"{reader}-{number}", reader=reader)


def parsed(step: hooks.Step, ir: dict) -> None:
    """End the parser step from the IR it returned."""
    kinds = {gap.get("kind") for gap in ir.get("gaps", [])}
    for kind, status, code in _PARSER_GAPS:
        if kind in kinds:
            step.ended(status, error_code=code)
            return
    parser = ir.get("parser") or {}
    fields = {"page_count": len(ir.get("pages", [])), "ocr_mode": (parser.get("mode") or {}).get("ocr_mode")}
    if ir.get("read_status") != "complete":
        step.ended("partial", error_code="reading_incomplete", **fields)
    elif parser.get("checkpoint_reused"):
        step.ended("cache_hit", source="reader_cache", **fields)
    else:
        step.ended("completed", **fields)


def local_ocr(step: hooks.Step, ir: dict) -> None:
    """End the local OCR step from the summary it left on the IR."""
    summary = ir.get("local_ocr") or {}
    status, code = _OCR_STATUS.get(str(summary.get("status")), ("unknown", None))
    review = summary.get("review") or {}
    step.ended(
        status,
        error_code=code,
        page_count=len(summary.get("pages") or []),
        review_count=review.get("needs_review"),
        disagreement_count=review.get("readers_disagree"),
    )


def photographed(step: hooks.Step, ir: dict) -> None:
    """End the reading of a photo or handwritten original, which has no parser step."""
    gaps = ir.get("gaps") or []
    step.ended("partial" if gaps else "completed", error_code="ocr_reader_incomplete" if gaps else None)
