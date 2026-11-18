"""Claim and settle the logical request within the caller's locked snapshot."""

from dataclasses import dataclass
from typing import TypedDict

from .errors import ModelCallError
from .identity import Json, result_hash
from .logical_request import (
    LogicalOwnership,
    LogicalRequestBinding,
    RequestKind,
    check_logical_request,
    request_metadata,
)


class ClaimOptions(TypedDict, total=False):
    legacy_hashes: tuple[str, ...]
    recover_pending: bool
    stage_input_id: str | None
    logical_binding: LogicalRequestBinding | None
    ownership: LogicalOwnership | None
    request_kind: RequestKind
    parent_request_hash: str | None


@dataclass
class Claim:
    model: str
    service_identity: str
    legacy_hashes: tuple[str, ...] = ()
    recover_pending: bool = False
    stage_input_id: str | None = None
    logical_binding: LogicalRequestBinding | None = None
    ownership: LogicalOwnership | None = None
    request_kind: RequestKind = RequestKind.GENERATION
    parent_request_hash: str | None = None


def _check_prior(prior: Json, claim: Claim) -> Json | None:
    if prior.get("model") != claim.model or (
        not prior.get("legacy") and prior.get("service_identity") != claim.service_identity
    ):
        message = "request_identity_mismatch"
        raise ModelCallError(message)
    if prior["state"] == "completed":
        result = prior.get("result")
        if not isinstance(result, dict) or result_hash(result) != prior.get("result_sha256"):
            message = "saved_result_binding_invalid"
            raise ModelCallError(message)
        return result
    if prior["state"] in {"sent", "unknown"}:
        raise ModelCallError(
            "model_outcome_unknown",
            outcome_unknown=True,
            unknown_retries_exhausted=prior.get("unknown_retries_exhausted", False),
        )
    if prior["state"] == "pending" and not claim.recover_pending:
        raise ModelCallError("model_request_in_progress", outcome_unknown=True)
    if prior.get("dispatched"):
        raise ModelCallError(prior.get("error_code") or "model_request_failed")
    return None


def _release_unsent(state: Json, prior: Json) -> None:
    # No sent marker exists and the caller owns the exclusive lease.
    # Release only these proven-unsent nonmonetary token slots (the call and its retries).
    for owned in [prior["call_id"], *(prior.get("attempt_ids") or [])]:
        token_row = state["pending"].pop(owned, None)
        finished = state["settled"].pop(owned, None)
        if token_row is not None or finished is not None:
            state["calls"] -= 1
        if finished is not None:
            state["output_tokens"] -= finished["output"]
            state["total_tokens"] -= finished["total"]


def claim_request(state: Json, request_hash: str, claim: Claim) -> tuple[str, Json | None]:
    check_logical_request(
        state,
        request_hash,
        claim.logical_binding,
        ownership=claim.ownership,
        request_kind=claim.request_kind,
        parent_request_hash=claim.parent_request_hash,
    )
    requests = state.setdefault("requests", {})
    if any(value in requests and requests[value].get("legacy") for value in claim.legacy_hashes):
        raise ModelCallError("legacy_model_outcome_unknown", outcome_unknown=True)
    known_ids = {value["call_id"] for value in requests.values()}
    known_ids |= {attempt for value in requests.values() for attempt in value.get("attempt_ids") or []}
    if any(key not in known_ids for key in state["pending"]):
        raise ModelCallError("legacy_model_outcome_unknown", outcome_unknown=True)
    prior = requests.get(request_hash)
    if prior:
        result = _check_prior(prior, claim)
        if result is not None:
            return prior["call_id"], result
        _release_unsent(state, prior)
    if claim.stage_input_id:
        # Changing model or prompt permits a new result only after the earlier dispatch is accounted for.
        for execution in requests.values():
            if execution.get("stage_input_id") == claim.stage_input_id and execution["state"] in {"sent", "unknown"}:
                raise ModelCallError(
                    "model_outcome_unknown",
                    outcome_unknown=True,
                    unknown_retries_exhausted=execution.get("unknown_retries_exhausted", False),
                )
    call_id = prior["call_id"] if prior else "model-" + request_hash
    requests[request_hash] = {
        "call_id": call_id,
        "request_hash": request_hash,
        "model": claim.model,
        "service_identity": claim.service_identity,
        "state": "pending",
        "dispatched": False,
        "result_sha256": None,
    }
    if claim.stage_input_id:
        requests[request_hash]["stage_input_id"] = claim.stage_input_id
    requests[request_hash].update(
        request_metadata(claim.logical_binding, claim.request_kind, claim.parent_request_hash)
    )
    return call_id, None
