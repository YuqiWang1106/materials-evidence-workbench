"""Strict, isolated Responses JSON adapter; never infer lost message boundaries.

``normalize_response_payload`` retains the original status/model/usage on rejection.
The shared ModelClient can therefore account for a paid completed response before
rejecting its empty normalized output. Preserve ``response_diagnostic`` from the
original provider payload separately; the normalized object is not provider evidence.
"""

import json
from copy import deepcopy


class ResponseShapeError(ValueError):
    """A completed response has no unambiguous structured candidate."""


def _reject(reason):
    raise ResponseShapeError(reason)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject("invalid_json_object")
        result[key] = value
    return result


def _strict_object(text):
    if not isinstance(text, str) or not text.strip():
        _reject("invalid_json_object")
    try:
        result = json.loads(
            text, object_pairs_hook=_unique_pairs, parse_constant=lambda _: _reject("invalid_json_object")
        )
        if not isinstance(result, dict):
            _reject("invalid_json_object")
        # Disallow infinities from otherwise valid numeric exponents, too.
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError, RecursionError):
        _reject("invalid_json_object")
    return result


def _canonical(value):
    # Object key order/whitespace are immaterial; true and 1 remain distinct.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _parse_message(parts):
    try:
        return _strict_object("".join(parts)), 0, int(len(parts) > 1)
    except ResponseShapeError:
        # Only independently complete objects can be deduplicated. Never split
        # concatenated JSON or repair malformed text using extraction heuristics.
        if len(parts) < 2:
            raise
        values = [_strict_object(part) for part in parts]
        if len({_canonical(value) for value in values}) != 1:
            _reject("conflicting_json_objects")
        return values[0], len(values) - 1, 0


def parse_response_payload(payload):
    """Return (candidate object, parsing audit) or raise ResponseShapeError.

    Eligible candidates are completed assistant messages with absent phase or
    ``final_answer``. Explicit ``commentary`` is retained in diagnostics only.
    No fragments are joined across messages; all eligible answers must agree.
    """
    if not isinstance(payload, dict):
        _reject("invalid_response_shape")
    if payload.get("status") != "completed":
        _reject("response_not_completed")
    if payload.get("error") is not None:
        _reject("provider_error")
    items = payload.get("output")
    if not isinstance(items, list):
        _reject("invalid_output_shape")
    candidates = []
    audit = {
        "accepted": True,
        "reason": "unambiguous_json_object",
        "eligible_message_count": 0,
        "equivalent_duplicate_count": 0,
        "fragmented_message_count": 0,
        "ignored_commentary_count": 0,
    }
    for item in items:
        if not isinstance(item, dict):
            _reject("invalid_output_shape")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message":
            _reject("unsupported_output_item")
        if item.get("role") != "assistant":
            _reject("unexpected_message_role")
        if item.get("status") != "completed":
            _reject("message_not_completed")
        content = item.get("content")
        if not isinstance(content, list):
            _reject("invalid_message_content")
        parts = []
        for part in content:
            if not isinstance(part, dict):
                _reject("invalid_message_content")
            if part.get("type") == "refusal":
                _reject("refusal")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                _reject("unsupported_message_content")
            parts.append(part["text"])
        phase = item.get("phase")
        if phase == "commentary":
            audit["ignored_commentary_count"] += 1
            continue
        if phase not in (None, "final_answer"):
            _reject("unknown_message_phase")
        if not parts:
            _reject("empty_candidate_message")
        value, duplicates, fragmented = _parse_message(parts)
        candidates.append(value)
        audit["eligible_message_count"] += 1
        audit["equivalent_duplicate_count"] += duplicates
        audit["fragmented_message_count"] += fragmented
    if not candidates:
        _reject("no_candidate_message")
    if len({_canonical(value) for value in candidates}) != 1:
        _reject("conflicting_json_objects")
    audit["equivalent_duplicate_count"] += len(candidates) - 1
    return candidates[0], audit


def normalize_response_payload(payload):
    """Adapt output only, preserving paid-response metadata even on failure."""
    normalized = deepcopy(payload) if isinstance(payload, dict) else {}
    try:
        value, audit = parse_response_payload(payload)
    except ResponseShapeError as exc:
        normalized["output"] = []
        return normalized, {"accepted": False, "reason": str(exc)}
    normalized["output"] = [
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": _canonical(value)}],
        }
    ]
    return normalized, audit
