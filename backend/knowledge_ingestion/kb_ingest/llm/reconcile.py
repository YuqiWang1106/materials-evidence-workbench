"""Recover request ownership and conservative usage from durable attempts."""

import math

from .errors import ModelCallError, ModelRequestBindingError
from .identity import Json, result_hash
from .logical_request import LogicalRequestBinding, RequestKind


def _recover_binding(owner: Json, row: Json, request_hash: str) -> None:
    if "logical_binding" in row:
        binding = LogicalRequestBinding.read(row["logical_binding"])
        if "logical_binding" in owner and LogicalRequestBinding.read(owner["logical_binding"]) != binding:
            raise ModelRequestBindingError()
        try:
            kind = RequestKind(row["request_kind"])
        except (KeyError, ValueError):
            raise ModelRequestBindingError() from None
        parent = row.get("parent_request_hash")
        if kind == RequestKind.COUNT and (
            not isinstance(parent, str)
            or request_hash != result_hash({"kind": "input_token_count", "request_hash": parent})
        ):
            raise ModelRequestBindingError()
        for key in ("request_kind", "parent_request_hash"):
            if key in owner and owner[key] != row.get(key):
                raise ModelRequestBindingError()
        owner["logical_binding"] = binding.encoded()
        owner["request_kind"] = kind.value
        if parent is not None:
            owner["parent_request_hash"] = parent


def _owner(state: Json, attempt_id: str, row: Json) -> None:
    request_hash = row.get("request_hash")
    if request_hash and row.get("status") not in {"cache_hit", "project_budget_not_dispatched"}:
        owner = state.setdefault("requests", {}).setdefault(
            request_hash,
            {
                "call_id": attempt_id,
                "request_hash": request_hash,
                "model": row.get("requested_model", row.get("model")),
                "state": "unknown",
                "legacy": True,
                "result_sha256": None,
            },
        )
        _recover_binding(owner, row, request_hash)
        if row.get("stage_input_id"):
            owner["stage_input_id"] = row["stage_input_id"]
        if row.get("unknown_retries_exhausted"):
            owner["unknown_retries_exhausted"] = True
        if owner["call_id"] != attempt_id and attempt_id not in owner.setdefault("attempt_ids", []):
            owner["attempt_ids"].append(attempt_id)


def recover(state: Json, attempt_id: str, row: Json) -> None:
    _owner(state, attempt_id, row)
    if attempt_id in state["pending"] or attempt_id in state["settled"]:
        return
    state["calls"] += 1
    started = row.get("started_at_epoch")
    if isinstance(started, (int, float)) and type(started) in (int, float) and math.isfinite(started):
        state["started_at_epoch"] = min(state["started_at_epoch"], started)
    is_count = row.get("request_kind") == "input_token_count"
    output, total = row.get("reserved_output_tokens"), row.get("reserved_total_tokens")
    output = 0 if is_count else output if type(output) is int and output > 0 else state["limits"]["max_output_tokens"]
    total = 0 if is_count else total if type(total) is int and total >= output else state["limits"]["max_total_tokens"]
    if row.get("status") == "started":
        state["pending"][attempt_id] = {
            "output": output,
            "total": total,
            "started_at_epoch": started or state["started_at_epoch"],
            "seconds": row.get("reserved_seconds", state["limits"]["max_wall_seconds"]),
            "recovered_from_ledger": True,
        }
        return
    usage = row["usage"] if isinstance(row.get("usage"), dict) else {}
    reported_output, reported_total = usage.get("output_tokens"), usage.get("total_tokens")
    output = reported_output if type(reported_output) is int and reported_output >= 0 else output
    total = reported_total if type(reported_total) is int and reported_total >= output else total
    state["output_tokens"] += output
    state["total_tokens"] += total
    seconds = row.get("wall_seconds", row.get("reserved_seconds", state["limits"]["max_wall_seconds"]))
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        message = "attempt duration invalid; refusing to reset time usage"
        raise ModelCallError(message)
    if state["active_time"]:
        state["active_seconds"] += seconds
    state["settled"][attempt_id] = {
        "output": output,
        "total": total,
        "seconds": seconds,
        "usage_status": "count_usage_unavailable"
        if is_count
        else "reported"
        if usage
        else "legacy_unknown_conservative_ceiling",
        "recovered_from_ledger": True,
    }
