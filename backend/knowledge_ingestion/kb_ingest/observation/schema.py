"""The closed vocabulary of a document-processing event.

An event carries codes, counts and durations only. Anything outside these allowlists is dropped before it is
written or shown, so source text, prompts, error messages, request hashes and keys never reach the journal.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from typing import TypeGuard

type Event = dict[str, object]

STAGES = frozenset(
    {
        "reading",
        "visual_reading",
        "table_mapping",
        "entity_extraction",
        "validating",
        "synonyms",
        "identity",
        "finalizing",
        "publishing",
        "resolve",
        "claims",
        "task_preflight",
        "candidate_validation",
        "candidate_save",
        "review_validation",
    }
)
KINDS = frozenset(
    {
        "stage",
        "table",
        "table_round",
        "window",
        "model_call",
        "model_attempt",
        "observation",
        "embedding_batch",
        "embedding_summary",
        "reader_step",
        "visual_step",
    }
)
STATUSES = frozenset(
    {
        "started",
        "completed",
        "failed",
        "partial",
        "empty",
        "mapped",
        "not_processed",
        "invalid",
        "unknown",
        "interrupted",
        "cache_hit",
    }
)
ERRORS = frozenset(
    {
        "mapping_invalid",
        "quote_not_found",
        "quote_not_unique",
        "invalid_source_grid",
        "table_no_attributes",
        "table_partial",
        "request_allowance",
        "reading_incomplete",
        "visual_read_incomplete",
        "window_partial",
        "embedding_unavailable",
        "model_credentials_missing",
        "embedding_timeout",
        "embedding_transport_error",
        "embedding_invalid_response",
        "embedding_cache_unavailable",
        "model_error",
        "response_invalid",
        "http_400",
        "http_401",
        "http_403",
        "http_404",
        "http_408",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "transport_not_sent",
        "transport_unknown",
        "transport_write_failed",
        "time_budget_exhausted",
        "model_outcome_unknown",
        "model_request_binding_invalid",
        "cancelled",
        "unknown_error",
        "pipeline_configuration_changed",
        "original_integrity_failure",
        "review_evidence_invalid",
        "task_processing_error",
        "publication_error",
        "local_io_error",
        "ocr_reader_not_configured",
        "ocr_second_not_configured",
        "ocr_queue_timeout",
        "ocr_reader_timeout",
        "ocr_reader_start_failed",
        "ocr_reader_output_invalid",
        "ocr_reader_incomplete",
        "ocr_reader_failed",
        "ocr_second_timeout",
        "ocr_second_incomplete",
        "ocr_second_failed",
        "ocr_second_capped",
        "ocr_render_failed",
        "ocr_comparison_failed",
        "ocr_cache_write_failed",
        "mineru_queue_timeout",
        "visual_render_failed",
        "visual_coverage_invalid",
        "visual_empty_page",
        "visual_empty_transcript",
        "visual_batch_not_sent",
    }
)
# Table-mapping compiler diagnostics that may be counted on an event.
DIAGNOSTICS = frozenset(
    {
        "unsafe_source_token_boundary",
        "unmapped_nonempty_cells",
        "duplicate_identity",
        "unresolved_object_dependency",
        "unresolved_result_context",
        "ambiguous_value",
        "unmapped_value",
        "unsupported_unit",
        "duplicate_fact",
        "cell_value_cut",
    }
)
# Statuses of one transport attempt in the model request ledger.
MODEL_STATUSES = frozenset(
    {
        "started",
        "completed",
        "failed",
        "unknown",
        "transport_unknown",
        "transport_write_failed",
        "transport_connect_failed",
        "transport_not_sent",
        "time_budget_exhausted",
        "invalid_json",
        "invalid_json_object",
        "returned_model_mismatch",
        "incomplete",
        "refusal_or_empty",
        "cancelled_after_dispatch",
        "project_budget_not_dispatched",
        "model_not_dispatched",
        "cache_hit",
    }
)
READERS = frozenset(
    {
        "pymupdf",
        "mineru",
        "mineru_queue",
        "ocr_render",
        "ocr_prepare",
        "ocr_first_queue",
        "ocr_second_queue",
        "ocr_first",
        "ocr_second",
        "ocr_compare",
        "ocr_cache",
        "ocr_limit",
    }
)
SOURCES = frozenset(
    {
        "journal_cache",
        "client_cache",
        "request_cache",
        "reader_cache",
        "not_dispatched",
        "transport_attempt",
        "unknown",
    }
)
REQUEST_STATES = frozenset({"pending", "sent", "unknown", "failed", "completed", "not_dispatched"})
PROTOCOLS = frozenset({"responses", "chat_completions", "embeddings"})
PARENT_KINDS = frozenset({"window", "table"})
VISUAL_KINDS = frozenset({"image", "chart", "figure", "equation", "formula", "unread_page", "handwriting_table"})
VISUAL_STEPS = frozenset({"selection", "reuse", "render", "result"})
VISUAL_REASONS = frozenset(
    {
        "usable_mineru_formula",
        "unread_page",
        "figure_requires_visual_reading",
        "formula_source_unresolved",
        "formula_has_reading_risk",
        "formula_text_not_reusable",
        "formula_body_incomplete_or_unsupported",
        "handwriting_flagged",
        "regular_visual_no_targets",
    }
)
MAX_COUNT = 100_000
MAX_VISUAL_TARGETS = 100

_ID_FIELDS = ("item_id", "mapping_key", "logical_id", "parent_id", "attempt_id")
_FLAG_FIELDS = ("will_retry", "possible_duplicate_charge")
_COUNT_FIELDS = (
    "round",
    "attempt",
    "completed",
    "total",
    "failed",
    "unread_cells",
    "attributes",
    "requests",
    "batch_size",
    "cached",
    "page_count",
    "item_count",
    "review_count",
    "disagreement_count",
)
_SECONDS_FIELDS = ("at", "elapsed_seconds")
_CHOICE_FIELDS: dict[str, frozenset[str]] = {
    "parent_kind": PARENT_KINDS,
    "reader": READERS,
    "ocr_mode": frozenset({"txt", "ocr"}),
    "source": SOURCES,
    "request_state": REQUEST_STATES,
    "protocol": PROTOCOLS,
    "error_code": ERRORS,
    "visual_step": VISUAL_STEPS,
    "visual_reason": VISUAL_REASONS,
}
EVENT_FIELDS = frozenset(
    {
        "kind",
        "stage",
        "status",
        "transport_status",
        "requested_model",
        "returned_model",
        "http_status",
        "diagnostic_codes",
        "visual_targets",
        "visual_targets_truncated",
        *_ID_FIELDS,
        *_FLAG_FIELDS,
        *_COUNT_FIELDS,
        *_SECONDS_FIELDS,
        *_CHOICE_FIELDS,
    }
)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,119}")
_HTTP_STATUS = re.compile(r"http_[1-5][0-9][0-9]")
_KEY_PREFIXES = ("sk-", "bearer", "gsk_", "hf_", "aiza", "xox", "ghp_", "github_pat_", "glpat-", "pplx-")


def safe_id(value: object) -> str | None:
    """An identifier safe to show: a short run of letters, digits and `_.:-`."""
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def safe_model(value: object, forbidden: Iterable[object] = ()) -> str | None:
    """A model name safe to show; a URL, a key-like string or anything holding a configured secret is not."""
    if not isinstance(value, str) or not _MODEL.fullmatch(value) or "://" in value:
        return None
    if value.lower().startswith(_KEY_PREFIXES):
        return None
    if any(isinstance(secret, str) and secret and secret in value for secret in forbidden):
        return None
    return value


def is_count(value: object) -> TypeGuard[int]:
    return type(value) is int and 0 <= value <= MAX_COUNT


def is_seconds(value: object) -> TypeGuard[float]:
    """A finite, non-negative number of seconds (a duration or an epoch instant)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def is_transport_status(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and (value in MODEL_STATUSES or _HTTP_STATUS.fullmatch(value) is not None)


def _is_http_status(value: object) -> bool:
    return type(value) is int and 100 <= value <= 599


def _is_flag(value: object) -> bool:
    return type(value) is bool


def _one_of(allowed: frozenset[str]) -> Callable[[object], bool]:
    return lambda value: isinstance(value, str) and value in allowed


def _drop_invalid(event: Event, keys: Iterable[str], valid: Callable[[object], bool]) -> None:
    for key in keys:
        if key in event and not valid(event[key]):
            del event[key]


def safe_event(data: Mapping[str, object]) -> Event | None:
    """The allowlisted copy of an event, or None when its kind, stage or status is not a known code.

    A field with an unknown name is left out; a known field with a value outside its allowlist is dropped.
    """
    event: Event = {key: value for key, value in data.items() if key in EVENT_FIELDS}
    if not (_one_of(KINDS)(event.get("kind")) and _one_of(STAGES)(event.get("stage"))):
        return None
    if not _one_of(STATUSES)(event.get("status")):
        return None
    _drop_invalid(event, _ID_FIELDS, lambda value: safe_id(value) is not None)
    for key, allowed in _CHOICE_FIELDS.items():
        _drop_invalid(event, (key,), _one_of(allowed))
    if event.get("kind") == "visual_step":
        if event.get("stage") != "visual_reading" or "visual_step" not in event:
            return None
    else:
        event.pop("visual_step", None)
        event.pop("visual_reason", None)
    _drop_invalid(event, ("transport_status",), is_transport_status)
    _drop_invalid(event, _FLAG_FIELDS, _is_flag)
    _drop_invalid(event, _COUNT_FIELDS, is_count)
    _drop_invalid(event, ("http_status",), _is_http_status)
    _drop_invalid(event, _SECONDS_FIELDS, is_seconds)
    _clean_models(event)
    _clean_visual_targets(event)
    if "diagnostic_codes" in event:
        event["diagnostic_codes"] = _diagnostic_counts(event["diagnostic_codes"])
    return event


def _clean_models(event: Event) -> None:
    """Keep the requested model name; a returned name is repeated only when it is that same name."""
    requested = safe_model(event.get("requested_model"))
    if requested is None:
        event.pop("requested_model", None)
    if requested is None or event.get("returned_model") != requested:
        event.pop("returned_model", None)


def _diagnostic_counts(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {key: count for key, count in raw.items() if key in DIAGNOSTICS and is_count(count)}


def _clean_visual_targets(event: Event) -> None:
    """Pages and boxes sent to the vision model, at most MAX_VISUAL_TARGETS of them."""
    raw = event.pop("visual_targets", None)
    truncated = event.pop("visual_targets_truncated", None) is True
    if event.get("stage") != "visual_reading" or not isinstance(raw, list):
        return
    targets = [target for item in raw if (target := _visual_target(item)) is not None]
    if not targets:
        return
    event["visual_targets"] = targets[:MAX_VISUAL_TARGETS]
    if truncated or len(targets) > MAX_VISUAL_TARGETS:
        event["visual_targets_truncated"] = True


def _visual_target(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    page, kind = item.get("page"), item.get("kind")
    if type(page) is not int or not 1 <= page <= MAX_COUNT or not _one_of(VISUAL_KINDS)(kind):
        return None
    target: dict[str, object] = {"page": page, "kind": kind}
    reason = item.get("reason")
    if isinstance(reason, str) and reason in VISUAL_REASONS:
        target["reason"] = reason
    index = item.get("document_index")
    if type(index) is int and 1 <= index <= MAX_COUNT:
        target["document_index"] = index
    box = item.get("bbox")
    if _is_box(box):
        target["bbox"] = box
    return target


def _is_box(box: object) -> bool:
    if not isinstance(box, list) or len(box) != 4:
        return False
    if not all(is_seconds(value) and value <= MAX_COUNT for value in box):
        return False
    return bool(box[0] < box[2] and box[1] < box[3])
