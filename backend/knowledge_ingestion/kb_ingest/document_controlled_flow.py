"""Opt-in controlled extraction using the ordinary six-category publication gate.

This module does not publish, enable a default, or open reference answers. It
reuses direct-source review, correction protection and formal materialization.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from . import pipeline_versions
from .contract import add_coverage, add_object, new_package
from .core import atomic_json, fingerprint
from .document_compact import COMPACT_INSTRUCTIONS, COMPACT_SCHEMA, convert_compact
from .document_direct_issues import materialize_direct_issues
from .document_direct_missing import materialize_missing
from .ingest.documents.document_closeout import CloseIssues, closeout
from .ingest.documents.document_direct_pipeline import _materialize_review_disagreements
from .ingest.documents.document_direct_preservation import (
    _retain_source_issues,
    _retained_review_disagreements,
    correction_support_view,
    preserves_reviewed,
)
from .ingest.documents.document_direct_review import _review
from .ingest.documents.document_pipeline import StageJournal, journal_workspace_id
from .ingest.documents.extractors.documents_inventory import UnitObjectInventory
from .ingest.documents.extractors.documents_materialize import _materialize
from .llm import BudgetExceeded, ModelCallError
from .stage_cache import model_cache_identity, stage_context

VERSION = "compact-controlled-v1"
CONTROLLED_STAGE_VERSION = "controlled-extraction-v1"
CONTROLLED_PROMPT_VERSION = "controlled-prompts-v1"


def request_fingerprint(instructions, payload, schema, images, files):
    return fingerprint(
        {
            "instructions": instructions,
            "payload": payload,
            "schema": schema,
            "images": [sha256(x.encode()).hexdigest() for x in images],
            "files": [
                {k: (sha256(v.encode()).hexdigest() if k == "file_data" else v) for k, v in f.items()} for f in files
            ],
        }
    )


def fact_lifecycle(trace, report, materialized, package):
    rejected = {r["target"]: r["reason"] for r in (report or {}).get("rejected", [])}
    assertions = {a["assertion_id"]: a for a in package["assertions"]}
    result = []
    for entry in trace.get("facts", []):
        row = deepcopy(entry)
        key = entry.get("candidate_field_key")
        row.update(
            conversion="converted" if key else "rejected",
            source_review="not_reviewed",
            release="withheld",
            assertion_id=None,
        )
        if key and report is None:
            row["reason"] = "no completed source review for this revision"
        elif key:
            target = "f:" + key
            if target in rejected:
                row.update(source_review="rejected", reason=rejected[target])
            else:
                row["source_review"] = "supported"
                aid = materialized.get("targets", {}).get(target)
                row["assertion_id"] = aid
                record = assertions.get(aid)
                if record is None:
                    row["reason"] = "accepted candidate was not materialized"
                elif record.get("use_status") == "document_verified":
                    row.update(release="document_verified", reason=None)
                else:
                    row["reason"] = "formal package assessment withheld the record"
        result.append(row)
    return result


def run_controlled(
    prepared,
    *,
    asset,
    source_id,
    source_type,
    run_id,
    client,
    work,
    output_format="compact",
    legacy_contract=None,
    whole_source=False,
    stage_dir=None,
):
    """One end-to-end run; all calls, including one correction, share one client/run."""
    started = time.monotonic()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if source_type not in {"paper", "patent"} or output_format not in {"compact", "legacy"}:
        raise ValueError("invalid controlled extraction mode")
    if not client.budget.active_time or client.budget.max_wall_seconds > 600 or not client.budget.budget_state_path:
        raise ValueError("controlled runs require durable active model time at most 600 seconds")
    if output_format == "legacy" and legacy_contract is None:
        raise ValueError("legacy comparison requires its frozen contract adapter")
    schema, instructions, adapter = (
        (COMPACT_SCHEMA, COMPACT_INSTRUCTIONS, lambda x: (x, None)) if output_format == "compact" else legacy_contract
    )
    binding = {
        "version": VERSION,
        "input": prepared["manifest"]["input_sha256"],
        "pipeline_version": pipeline_versions.CONTROLLED_PIPELINE_VERSION,
        "source_id": source_id,
        "run_id": run_id,
        "source_type": source_type,
        "output_format": output_format,
        "whole_source": whole_source,
        "model": model_cache_identity(client),
        "schema": fingerprint(schema),
        "max_attempts": getattr(client, "max_attempts", None),
        "timeout": getattr(client, "timeout", None),
        **stage_context(CONTROLLED_STAGE_VERSION, CONTROLLED_PROMPT_VERSION, instructions),
    }
    if (work / "binding.json").exists() and json.loads((work / "binding.json").read_text()) != binding:
        raise ValueError("controlled run binding changed; create a separate evaluation, never overwrite history")
    atomic_json(work / "binding.json", binding)
    # Website attempts share completed stages. Each attempt keeps its own immutable audit binding.
    stage_binding = {**binding, "run_id": run_id.rsplit(":attempt", 1)[0]} if stage_dir else binding
    journal = StageJournal(
        stage_dir or work / "stages", {**stage_binding, "workspace_id": journal_workspace_id(prepared)}
    )
    registry = prepared["registry"]
    call_times = json.loads(times_path.read_text()) if (times_path := work / "stage-times.json").exists() else []
    assets = {asset["asset_id"]: asset}
    diagnostics, diagnostic_history, disagreements, histories = [], [], [], []
    completed = first_trace = None

    def call(phase, prompt, payload, response_schema, *, images=None, files=None):
        images, files = images or [], files or []
        stage = stage_context(CONTROLLED_STAGE_VERSION, CONTROLLED_PROMPT_VERSION, prompt)
        key = phase + "-" + request_fingerprint(prompt, payload, response_schema, images, files)
        previous = journal.get(key)
        if previous is not None:
            return previous
        if time.monotonic() - started >= 600:
            raise BudgetExceeded("document model-stage wall deadline exhausted")
        if client.budget.snapshot().get("pending_reservations"):
            raise BudgetExceeded("interrupted model attempt remains unresolved; no silent redispatch")
        begin = time.monotonic()
        original_timeout = getattr(client, "timeout", 240)
        client.timeout = min(original_timeout, 600 - (begin - started))
        call_status = "failed"
        try:
            response = client.call(
                prompt,
                json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload,
                response_schema,
                phase=phase,
                images=images,
                files=files,
                bypass_cache=True,
                context={
                    **stage,
                    "mode": VERSION,
                    "binding": fingerprint(stage_binding),
                    "stage_scope": fingerprint([source_id, asset["sha256"], stage_binding["run_id"]]),
                },
            )
            call_status = "completed"
        finally:
            client.timeout = original_timeout
            call_times.append({"phase": phase, "seconds": time.monotonic() - begin, "status": call_status})
            atomic_json(times_path, call_times)
        return journal.put(key, response)

    payload = json.loads(prepared["text"])
    try:
        for attempt in range(2):
            phase = "extract" if attempt == 0 else "correct"
            raw = call(phase, instructions, payload, schema, images=prepared["images"], files=prepared["files"])
            atomic_json(work / f"raw-{attempt}.json", raw)
            history = {"attempt": attempt, "trace": {"facts": []}}
            histories.append(history)
            try:
                compact, legacy_trace = adapter(raw)
                atomic_json(work / f"compact-{attempt}.json", compact)
                candidate, trace = convert_compact(compact, registry)
                if legacy_trace is not None:
                    trace["legacy_adapter"] = legacy_trace
                history["trace"] = deepcopy(trace)
                atomic_json(work / f"conversion-{attempt}.json", trace)
                atomic_json(work / f"candidate-{attempt}.json", candidate)
                if first_trace is None:
                    first_trace = trace
                if completed is not None:
                    candidate = _retain_source_issues(candidate, completed["candidate"])
                    preservation_view, support_problems = correction_support_view(
                        candidate, completed["accepted"], completed["report"], registry
                    )
                    history["support_preservation"] = support_problems
                    if preservation_view is None or not preserves_reviewed(
                        preservation_view, completed["accepted"], original=completed["candidate"]
                    ):
                        reason = (
                            "correction removed or changed previously supported records; correction not source-reviewed"
                        )
                        history["not_selected_reason"] = reason
                        diagnostics.append({"reason": reason})
                        diagnostic_history.append({"attempt": attempt, "problems": [{"reason": reason}]})
                        break
                accepted, report = _review(
                    candidate,
                    registry,
                    assets,
                    call,
                    phase + "-source-review",
                    target_block_ids=[b["block_id"] for b in registry],
                    source_scope_complete=whole_source,
                    source_type=source_type,
                    declared_scope=json.loads(prepared["text"])["scope"],
                )
                report["rejected"] += [
                    {"target": "i:" + str(i), "reason": issue["description"]}
                    for i, issue in enumerate(candidate.get("issues", []))
                    if issue.get("severity") == "error"
                ]
                atomic_json(work / f"review-{attempt}.json", report)
                atomic_json(work / f"accepted-{attempt}.json", accepted)
                history["report"] = deepcopy(report)
                if completed is not None and not preserves_reviewed(
                    accepted, completed["accepted"], original=completed["candidate"], candidate_context=candidate
                ):
                    disagreements = _retained_review_disagreements(
                        completed["accepted"], candidate, completed["report"], report, original=completed["candidate"]
                    )
                    reason = "correction review disagrees; retain prior records but revoke disputed facts"
                    history["not_selected_reason"] = reason
                    diagnostics.append({"reason": reason})
                    diagnostic_history.append({"attempt": attempt, "problems": [{"reason": reason}]})
                    break
                completed = {
                    "candidate": candidate,
                    "accepted": accepted,
                    "report": report,
                    "trace": trace,
                    "attempt": attempt,
                }
                problems = report["rejected"] + [{"reason": x} for x in report["omissions"]]
                # Only the selected current review constrains current unresolved
                # state. Earlier failures remain in the attempt/audit artifacts.
                diagnostics = deepcopy(problems)
            except ValueError as exc:
                trace = getattr(exc, "trace", history["trace"])
                atomic_json(work / f"conversion-{attempt}.json", trace)
                history.update(trace=deepcopy(trace), conversion_error=str(exc))
                if first_trace is None:
                    first_trace = trace
                problems = [{"reason": str(exc)}]
                diagnostics = (diagnostics if completed is not None else []) + problems
            diagnostic_history.append({"attempt": attempt, "problems": deepcopy(problems)})
            if not problems:
                break
            payload = {
                **json.loads(prepared["text"]),
                "previous_output": raw,
                "correction_scope": (
                    "Fix only listed errors/omissions, preserve all source-supported facts and conflicts."
                ),
                "diagnostics": problems,
            }
    except (ModelCallError, ValueError) as exc:
        failure = {"reason": str(exc), "type": type(exc).__name__}
        diagnostics.append(failure)
        diagnostic_history.append({"attempt": attempt, "problems": [deepcopy(failure)]})
    package = new_package(source_id, source_type, [asset], run_id)
    package["header"].update(
        document_artifact_layout="attempt-v1",
        document_extraction_mode="direct-six-v1",
        document_output_format=output_format,
        document_input_strategy=prepared["manifest"]["arm"],
        source_review_method="direct-source-v1",
        controlled_extraction_version=VERSION,
        actual_model=client.model,
    )
    for page in prepared["manifest"]["page_coverage"]:
        add_coverage(
            package,
            {"asset_ref": asset["asset_id"], "page": page["page"]},
            "indexed",
            page.get("read_status") or "unknown",
        )
    materialized = {"targets": {}}
    if completed:
        accepted, report = completed["accepted"], completed["report"]
        unit = add_object(package, "content_unit", "Declared source scope", unit_kind="study", source_scope=VERSION)
        materialized = _materialize(
            package, {**accepted, "issues": []}, registry, unit, part_key=VERSION, inventory=UnitObjectInventory(unit)
        )
        mapped = set(materialized["targets"].values())
        checked_evidence = {
            eid
            for section, key in [("objects", "object_id"), ("assertions", "assertion_id"), ("relations", "relation_id")]
            for row in package[section]
            if row[key] in mapped
            for eid in row.get("evidence_refs", [])
        }
        for evidence in package["evidence"]:
            if evidence["evidence_id"] in checked_evidence:
                evidence["support_review"] = "model_semantic_review"
        materialize_direct_issues(package, accepted, registry, materialized)
        missing = materialize_missing(package, accepted.get("missing_records", []), registry, materialized)
        _materialize_review_disagreements(package, disagreements, {**materialized["targets"], **missing["targets"]})
        add_coverage(
            package,
            {"scope": json.loads(prepared["text"])["scope"]},
            "partial" if report["rejected"] or report["omissions"] else "reviewed",
            "Independent model source review and code checks, not expert scientific validation",
        )
    else:
        report = {"rejected": [], "omissions": ["no completed source review"]}
        add_coverage(
            package, {"scope": json.loads(prepared["text"])["scope"]}, "not_checked", "No completed source review"
        )
    gaps = prepared["manifest"].get("reading_gaps", []) + [
        p for p in prepared["manifest"]["page_coverage"] if p.get("read_status") not in ("parsed", "complete")
    ]
    atomic_json(
        work / "diagnostic-history.json",
        {
            "attempts": diagnostic_history,
            "current_unresolved": diagnostics,
            "selected_attempt": completed["attempt"] if completed else None,
        },
    )
    result = closeout(package, work, CloseIssues(gaps, diagnostics), started, client.model)
    trace = completed["trace"] if completed else first_trace or {"facts": []}
    lifecycle = fact_lifecycle(trace, report, materialized, package)
    if not completed:
        for item in lifecycle:
            item.update(source_review="not_reviewed", release="withheld", reason="no completed source review")
    atomic_json(work / "fact-lifecycle.json", lifecycle)
    for history in histories:
        history["fact_lifecycle"] = fact_lifecycle(
            history["trace"],
            history.get("report"),
            materialized if completed and history["attempt"] == completed["attempt"] else {"targets": {}},
            package,
        )
        if not completed or history["attempt"] != completed["attempt"]:
            for row in history["fact_lifecycle"]:
                if row["conversion"] != "rejected":
                    row.update(
                        release="not_selected_revision",
                        reason=history.get(
                            "not_selected_reason",
                            "superseded or rejected revision; no implicit identity matching to final records",
                        ),
                    )
    atomic_json(
        work / "all-attempt-facts.json",
        {
            "attempts": histories,
            "selected_attempt": completed["attempt"] if completed else None,
            "selection_rule": (
                "Only the selected reviewed revision is materialized; original attempt facts are retained "
                "unchanged here."
            ),
            "final_fact_lifecycle": lifecycle,
        },
    )
    atomic_json(work / "materialization.json", materialized)
    atomic_json(work / "candidate-package.json", result)
    performance = json.loads((work / "performance.json").read_text())
    performance.update(
        model=client.model,
        output_format=output_format,
        input_strategy=prepared["manifest"]["arm"],
        stage_times=call_times,
        model_seconds_recorded_total=sum(t["seconds"] for t in call_times),
        timing_scope="elapsed_seconds is this invocation; stage_times and model_budget persist across resume",
        model_budget=client.budget.snapshot(),
        extraction_attempt=completed["attempt"] if completed else None,
        requires_offline_scientific_acceptance=True,
    )
    atomic_json(work / "performance.json", performance)
    return result
