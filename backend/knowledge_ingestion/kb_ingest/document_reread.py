"""Source-independent, bounded page rereading. A candidate is never a verdict."""

from __future__ import annotations

import fcntl
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .core import atomic_json, fingerprint

VERSION = "bounded-page-reread-v2"
_PROTOCOL = {"unknown_page", "duplicate_page", "unexpected_page", "unrequested_page", "parser_protocol_invalid"}
_STRUCTURE = {
    "invalid_table_span",
    "table_cell_overlap",
    "table_cell_without_row",
    "table_markup_unclosed",
    "nested_table_unsupported",
}
_CONFIRMED = {"confirmed_numeric_misread", "confirmed_table_structure_error", "critical_formula_quantity"}


def plan_rereads(document: dict, *, findings: list[dict] | None = None, legacy_diagnostics: bool = False) -> dict:
    """Only bound source-review findings select work by default.

    Offline callers can explicitly enable historical automatic diagnostics.
    Existing gaps and risks remain available for review in either policy.
    """
    result = {"version": VERSION, "requests": [], "review_required": [], "blocked_reason": None}
    if any(g.get("kind") in _PROTOCOL for g in document.get("gaps", [])):
        result["blocked_reason"] = "invalid_parser_protocol"
        return result
    regions = {r["id"]: r for r in document.get("regions", [])}
    pages = {p["page"]: p for p in document["pages"]}
    requests = {}

    def current_mode(page):
        return document.get("reread_page_modes", {}).get(str(page), document.get("parser", {}).get("mode", {}))

    def add(page, tier, reason):
        if page not in pages:
            return
        mode = current_mode(page)
        # An OCR retry can change reading mode, never downgrade a Standard page.
        if mode.get("tier") == "standard":
            tier = "standard"
        # Never rerun an already used successful OCR setting to seek agreement.
        if mode.get("ocr_mode") == "ocr" and (mode.get("tier") == "standard" or tier == "basic"):
            return
        row = requests.setdefault(page, {"pages": [page], "tier": tier, "ocr_mode": "ocr", "reasons": []})
        if tier == "standard":
            row["tier"] = tier
        if reason not in row["reasons"]:
            row["reasons"].append(reason)

    for gap in document.get("gaps", []):
        if legacy_diagnostics and gap.get("kind") in {"parser_page_missing", "parser_page_content_missing"}:
            page = gap.get("page")
            content = pages.get(page, {})
            if any(content.get(k, 0) for k in ("native_text_characters", "image_count", "drawing_count")):
                add(page, "basic", gap["kind"])
    for dispute in document.get("reading_disagreements", []):
        result["review_required"].append(deepcopy(dispute))
        for kind in ("numeric_reading_mismatch", "unit_reading_mismatch"):
            if legacy_diagnostics and kind in dispute.get("reasons", []):
                add(dispute.get("page"), "basic", "aligned_" + kind)
    for region in regions.values():
        diagnostics = region.get("diagnostics", [])
        for diagnostic in diagnostics:
            kind = diagnostic.get("kind") if isinstance(diagnostic, dict) else diagnostic
            if legacy_diagnostics and kind in _STRUCTURE:
                add(region.get("page"), "standard", kind)
        if diagnostics or region.get("reading_risks"):
            result["review_required"].append(
                {
                    "page": region.get("page"),
                    "region_id": region["id"],
                    "kind": "parser_reading_risk",
                    "diagnostics": deepcopy(diagnostics),
                    "risks": deepcopy(region.get("reading_risks", [])),
                    "resolution": "requires_original_image_check",
                }
            )
    for finding in findings or []:
        region = regions.get(finding.get("region_id"))
        if (
            not region
            or finding.get("origin") != "source_review"
            or finding.get("source_sha256") != document["source_sha256"]
            or finding.get("reading_sha256")
            != region.get("reading_sha256", document.get("parser", {}).get("middle_sha256"))
        ):
            result["review_required"].append({"kind": "stale_or_unbound_reread_finding"})
            continue
        if finding.get("kind") in _CONFIRMED:
            # A formula is expensive only when a reviewer identifies the actual field.
            if finding["kind"] == "critical_formula_quantity":
                from .contract import FIELDS

                field = FIELDS.get(finding.get("field_id"), {})
                if (
                    not set(field.get("value_kinds", []))
                    & {"number", "numeric_notation", "interval", "ratio", "statistics"}
                    or not set(field.get("owner_kinds", []))
                    & {
                        "formulation",
                        "component",
                        "process_step",
                        "reported_test",
                        "test_run",
                        "observation",
                        "sample",
                        "series",
                    }
                    or finding.get("image_reviewed") is True
                ):
                    continue
            add(region["page"], "standard", finding["kind"])
            result["review_required"].append(deepcopy(finding))
    result["requests"] = [requests[p] for p in sorted(requests)]
    return result


def install_candidate_reading(document, adapted, *, pages, reading_sha256, parameters):
    """Use whole new page versions as review input, archive old versions intact.

    Choosing a working representation never grants source verification. Different
    table structures are deliberately not joined by equal row/column numbers.
    """
    old = [r for r in document["regions"] if r.get("source") == "mineru" and r.get("page") in pages]
    updated = deepcopy(adapted["regions"])
    document.setdefault("alternative_readings", []).extend(deepcopy(old))
    document["regions"] = [r for r in document["regions"] if r not in old]
    # Agreement after rereading cannot clear the original native-text dispute.
    for region in document["regions"]:
        if region.get("page") in pages:
            region["requires_image_verification"] = True
    for region in updated:
        region.update(reading_sha256=reading_sha256, verification_state="unverified", requires_image_verification=True)
    document["regions"].extend(updated)
    for page in pages:
        before = [r for r in old if r.get("page") == page]
        after = [r for r in updated if r.get("page") == page]
        if before != after:
            document.setdefault("reading_disagreements", []).append(
                {
                    "kind": "reread_candidate_version",
                    "page": page,
                    "affected_region_ids": [r["id"] for r in document["regions"] if r.get("page") == page]
                    + [r["id"] for r in before],
                    "previous_reading_ids": [r["id"] for r in before],
                    "candidate_reading_ids": [r["id"] for r in after],
                    "pairing": "whole_page_alternatives_no_cell_alignment",
                    "resolution": "requires_original_image_check",
                }
            )
        document.setdefault("reread_page_modes", {})[str(page)] = {**parameters, **adapted.get("parser_mode", {})}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def apply_rereads(
    document,
    *,
    asset,
    source,
    cache_dir,
    state_dir,
    task_binding,
    command,
    runtime_for_tier,
    runner,
    adapter,
    compare,
    cancelled=None,
    findings=None,
    legacy_diagnostics: bool = False,
):
    """Local output cache plus per-task journal; no API/model invocation here."""
    from .document_reread_ledger import RereadLedger

    cache_dir, state_dir = Path(cache_dir), Path(state_dir)
    ledger = RereadLedger(state_dir / "ledger.json", task_binding=task_binding)
    summary = {
        "version": VERSION,
        "attempts": [],
        "review_required": [],
        "stopped": [],
        "selection": "unverified_whole_page_candidate",
    }
    document["reread"] = summary
    dimensions = {p["page"]: (p["width_pt"], p["height_pt"]) for p in document["pages"]}
    handled = set()
    for _ in range(2):
        decision = plan_rereads(document, findings=findings, legacy_diagnostics=legacy_diagnostics)
        summary["review_required"] = decision["review_required"]
        if decision["blocked_reason"]:
            summary["stopped"].append({"reason": decision["blocked_reason"]})
            break
        requests = [r for r in decision["requests"] if fingerprint(r) not in handled]
        if not requests:
            break
        for request in requests:
            handled.add(fingerprint(request))
            if cancelled and cancelled():
                summary["stopped"].append({"reason": "cancelled"})
                return document
            runtime = runtime_for_tier(request["tier"])
            if runtime.get("status") != "available":
                summary["stopped"].append(
                    {"pages": request["pages"], "reason": runtime.get("reason", "runtime_not_ready")}
                )
                continue
            parameters = {k: request[k] for k in ("pages", "tier", "ocr_mode")}
            identity = {
                "source_sha256": asset["sha256"],
                "runtime": runtime,
                "parameters": parameters,
                "format": "middle_json",
            }
            key = fingerprint(identity)
            target = cache_dir / key
            target.mkdir(parents=True, exist_ok=True)
            with (target / "request.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    summary["stopped"].append({"pages": request["pages"], "reason": "same_parse_in_progress"})
                    continue
                cached_output = None
                reused = False
                attempt_id = None
                receipt_path = target / "cache.json"
                try:
                    cached = json.loads(receipt_path.read_text())
                    raw_path = target / "middle_json.json"
                    if cached.get("identity") == identity and _sha(raw_path) == cached["output_sha256"]:
                        cached_output = {
                            "status": "ok",
                            "middle": json.loads(raw_path.read_text()),
                            "output_sha256": cached["output_sha256"],
                            "queue_seconds": 0,
                            "execution_seconds": 0,
                        }
                except (OSError, ValueError, KeyError):
                    pass
                # An atomic completion receipt can settle a crash after CLI completion.
                snapshot = ledger.snapshot()
                for old in snapshot["attempts"]:
                    completion = state_dir / "attempts" / old["attempt_id"] / "completion.json"
                    if old.get("request_key") == key and completion.is_file():
                        try:
                            ledger.recover(old["attempt_id"], json.loads(completion.read_text()))
                        except (ValueError, KeyError):
                            pass  # unknown/invalid receipts never refund a reservation
                reservation = ledger.reserve(key, asset["sha256"], request["pages"])
                if reservation["status"] not in {"reserved", "reused"}:
                    summary["stopped"].append(
                        {"pages": request["pages"], "reason": reservation.get("reason", "completed_output_unavailable")}
                    )
                    continue
                attempt_id = reservation["attempt_id"]
                attempt_dir = state_dir / "attempts" / attempt_id
                if reservation["status"] == "reused":
                    try:
                        safe = reservation["result"]
                        raw_path = attempt_dir / "middle_json.json"
                        if (
                            _sha(raw_path) != safe["output_sha256"]
                            or json.loads((attempt_dir / "request.json").read_text())["identity"] != identity
                        ):
                            raise ValueError("completed output changed")
                        output = {**safe, "middle": json.loads(raw_path.read_text())}
                    except (ValueError, KeyError, OSError):
                        summary["stopped"].append({"pages": request["pages"], "reason": "completed_output_unavailable"})
                        continue
                    reused = True
                else:
                    attempt_dir.mkdir(parents=True, exist_ok=True)
                    atomic_json(attempt_dir / "request.json", {"identity": identity, "reasons": request["reasons"]})
                    if cached_output is not None:
                        # A shared raw cache still needs a task-owned artifact identity.
                        from .core import atomic_bytes

                        atomic_bytes(attempt_dir / "middle_json.json", (target / "middle_json.json").read_bytes())
                        output = cached_output
                        reused = True
                    else:
                        output = runner(
                            source,
                            attempt_dir,
                            command,
                            cancelled=cancelled,
                            **parameters,
                            include_metrics=True,
                            execution_timeout_seconds=reservation["execution_timeout_seconds"],
                            queue_timeout_seconds=reservation["queue_timeout_seconds"],
                        )
                    if _sha(source) != asset["sha256"]:
                        output = {**output, "status": "failed", "error": "source_hash_changed_during_parse"}
                    safe = {k: v for k, v in output.items() if k not in {"middle", "diagnostic"}}
                    completion = {
                        "attempt_id": attempt_id,
                        "request_key": key,
                        "source_sha256": asset["sha256"],
                        "pages": request["pages"],
                        "result": safe,
                        "queue_seconds": output["queue_seconds"],
                        "execution_seconds": output["execution_seconds"],
                    }
                    atomic_json(attempt_dir / "completion.json", completion)
                    ledger.recover(attempt_id, completion)
                summary["attempts"].append(
                    {"request": request, "attempt_id": attempt_id, **safe, "cache_reused": reused}
                )
                if output["status"] != "ok":
                    summary["stopped"].append(
                        {"pages": request["pages"], "reason": output.get("error", "parser_failed")}
                    )
                    continue
                payload = output["middle"]
                # Write from immutable runner bytes, never reserialize and claim its hash.
                raw = (attempt_dir / "middle_json.json").read_bytes()
                if hashlib.sha256(raw).hexdigest() != output["output_sha256"]:
                    summary["stopped"].append({"pages": request["pages"], "reason": "output_integrity_failure"})
                    continue
                from .core import atomic_bytes

                atomic_bytes(target / "middle_json.json", raw)
                atomic_json(receipt_path, {"identity": identity, "output_sha256": output["output_sha256"]})
                try:
                    adapted = adapter(
                        payload,
                        asset_ref=asset["asset_id"],
                        source_sha256=asset["sha256"],
                        page_count=len(document["pages"]),
                        page_dimensions=dimensions,
                        requested_pages=request["pages"],
                    )
                    if any(g.get("kind") in _PROTOCOL for g in adapted["gaps"]):
                        raise ValueError("invalid page protocol")
                    actual = adapted.get("parser_mode", {})
                    if actual.get("tier") != request["tier"] or actual.get("ocr_mode") != "ocr":
                        raise ValueError("requested tier/mode not confirmed by parser")
                except ValueError as exc:
                    summary["stopped"].append(
                        {"pages": request["pages"], "reason": "reread_protocol_invalid", "detail": str(exc)}
                    )
                    continue
                usable = [
                    p
                    for p in request["pages"]
                    if adapted["page_status"].get(p) == "parsed" and any(r.get("page") == p for r in adapted["regions"])
                ]
                if not usable:
                    summary["stopped"].append({"pages": request["pages"], "reason": "reread_page_content_missing"})
                    continue
                adapted["regions"] = [r for r in adapted["regions"] if r.get("page") in usable]
                install_candidate_reading(
                    document,
                    adapted,
                    pages=usable,
                    reading_sha256=_sha(target / "middle_json.json"),
                    parameters=parameters,
                )
                for page in document["pages"]:
                    if page["page"] in usable:
                        page.update(read_status="parsed", parser_status="parsed")
                document["gaps"] = [
                    g
                    for g in document["gaps"]
                    if not (
                        g.get("page") in usable
                        and g.get("kind")
                        in {"parser_page_missing", "parser_page_content_missing", "visual_read_not_done"}
                    )
                ]
                if legacy_diagnostics:
                    comparison = compare(document["regions"])
                    document["reading_disagreements"] = [
                        d for d in document["reading_disagreements"] if d.get("kind") != "reading_disagreement"
                    ] + comparison.pop("disagreements")
                    document["reading_comparison"] = comparison
    summary["review_required"] = plan_rereads(document, legacy_diagnostics=legacy_diagnostics)["review_required"]
    summary["budget"] = ledger.snapshot().get("totals", {})
    document["gaps"].extend({"kind": "reread_unresolved", **deepcopy(item)} for item in summary["stopped"])
    return document
