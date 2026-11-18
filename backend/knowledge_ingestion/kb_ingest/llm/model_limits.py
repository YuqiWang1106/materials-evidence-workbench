from typing import Any

"""Nonmonetary input, output and request bounds for model execution."""

RUN_REQUEST_CEILING = 48
MODEL_MAX_INPUT_TOKENS = 922_000
MODEL_MAX_OUTPUT_TOKENS = 128_000
MODEL_INPUT_LIMITS = {"gpt-6-luna": 922_000, "gpt-6-sol": 922_000, "gpt-5.4-mini": 272_000}


def model_output_reservation(model: str, max_output_tokens: int | None) -> int:
    value = MODEL_MAX_OUTPUT_TOKENS if max_output_tokens is None else max_output_tokens
    if type(value) is not int or not 1 <= value <= MODEL_MAX_OUTPUT_TOKENS:
        message = "invalid output token capacity"
        raise ValueError(message)
    return value


def document_budget_options(
    options: dict[str, Any] | None,
    *,
    max_output_tokens: int | None,
    model: str = "gpt-6-luna",
    ceiling: int = RUN_REQUEST_CEILING,
) -> dict[str, Any]:
    """`ceiling` is the document's request ceiling: the fixed run ceiling, or one sized to the document."""
    result = dict(options or {})
    calls = result.get("max_calls", ceiling)
    if type(calls) is not int or calls < 1:
        message = "invalid document call capacity"
        raise ValueError(message)
    calls = min(calls, ceiling)
    output = model_output_reservation(model, max_output_tokens)
    result["max_calls"] = calls
    result.setdefault("max_output_tokens", calls * output)
    result.setdefault("max_total_tokens", calls * (MODEL_INPUT_LIMITS.get(model, MODEL_MAX_INPUT_TOKENS) + output))
    return result
