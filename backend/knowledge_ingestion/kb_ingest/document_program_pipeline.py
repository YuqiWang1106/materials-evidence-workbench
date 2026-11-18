"""Single extraction per prepared group; no semantic-review or repair calls."""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from hashlib import sha256
from pathlib import Path

from . import pipeline_versions
from .contract import add_coverage, add_issue, new_package, validate_package
from .core import atomic_json, fingerprint
from .ingest.documents.document_ir import document_reading_policy, read_document_ir
from .ingest.documents.document_standard_inputs import prepare_standard_inputs
from .llm import ModelBudget, ModelCallError, ModelClient
from .llm.model_limits import document_budget_options
from .stage_cache import model_cache_identity, stage_context

VERSION = "program-only-v1"
REVIEW_POLICY = "program_only_v1"
PROGRAM_STAGE_VERSION = "program-extraction-v1"
PROGRAM_PROMPT_VERSION = "program-prompts-v1"


def _packet_identity(packet):
    return {k: v for k, v in packet.items() if k not in ("files", "images")} | {
        "files": [fingerprint(f) for f in packet.get("files", [])],
        "images": [sha256(i.encode()).hexdigest() for i in packet.get("images", [])],
    }


def _claim_group(path: Path, receipt: dict, budget: ModelBudget) -> None:
    # Serialize the brief claim across model/prompt revisions. A crash after dispatch
    # leaves the receipt in place, so changing cache keys cannot silently send it again.
    group_key = fingerprint(receipt["group_id"])
    with budget.request_lease("program-group-" + group_key):
        for previous in path.parent.glob(f"group-{group_key}-*.json"):
            if previous == path:
                raise ValueError("group already dispatched by another worker")
            previous_receipt = json.loads(previous.read_text())
            if previous_receipt["status"] == "dispatching" or previous_receipt.get("outcome_unknown"):
                raise ValueError("earlier group dispatch is unresolved; cannot dispatch a new revision")
        with path.open("x") as stream:
            json.dump(receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())


def _study_unit_mappings(raw, *, source_id, extraction_batch, group, offered):
    """Server metadata only; never rewrite a model candidate or infer identity."""
    studies = raw.get("studies", []) if isinstance(raw, dict) else []
    if not studies:
        return []
    from .study_units import unit_identity

    candidate_id = "raw-" + fingerprint(group)[:24]
    mappings, refs = [], set()
    for study in studies:
        ref = study.get("ref")
        if not isinstance(ref, str) or not ref or ref in refs:
            raise ValueError("study refs must be nonempty and unique within a candidate")
        refs.add(ref)
        continuation = study.get("continuation_of")
        if continuation is not None and (not isinstance(continuation, str) or continuation not in offered):
            raise ValueError("study continuation_of must name an offered prior completed unit")
        mappings.append(
            {
                "candidate_id": candidate_id,
                "study_ref": ref,
                "unit_id": continuation or unit_identity(source_id, extraction_batch, candidate_id, ref),
                "continuation_of": continuation,
            }
        )
    return mappings


def _update_study_roster(roster, raw, mappings):
    studies = {study["ref"]: study for study in raw.get("studies", [])}
    for mapping in mappings:
        study = studies[mapping["study_ref"]]
        # Bound only context text; exact scientific content is retained in raw.
        roster[mapping["unit_id"]] = {
            "unit_id": mapping["unit_id"],
            "label": str(study.get("label") or "")[:160],
            "research_question": str(study.get("research_question") or "")[:320],
            "source_anchors": list(study.get("source_anchors") or study.get("evidence") or [])[:8],
        }


def run_program_packets(prepared, *, asset, source_id, source_type, run_id, client, work, state_dir, cancelled=None):
    from .document_program_contract import CONTRACT_VERSION, PROGRAM_INSTRUCTIONS, PROGRAM_SCHEMA

    started = time.monotonic()
    work, state_dir = Path(work), Path(state_dir)
    work.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    if (
        not client.model
        or client.reasoning_effort != "medium"
        or client.max_output_tokens is not None
        or client.max_attempts != 1
        or not client.budget.active_time
        or client.budget.max_wall_seconds > 600
        or not client.budget.budget_state_path
    ):
        raise ValueError("program-only mode requires bounded settings and durable request state")
    groups = [p["group_id"] for p in prepared["packets"]]
    if len(groups) != len(set(groups)):
        raise ValueError("duplicate prepared group identity")
    binding = {
        "version": VERSION,
        "review_policy": REVIEW_POLICY,
        "reading_policy": document_reading_policy(),
        "source_id": source_id,
        "source_type": source_type,
        "asset_sha256": asset["sha256"],
        "task_identity": run_id.rsplit(":attempt", 1)[0],
        "packets": [_packet_identity(p) for p in prepared["packets"]],
        "coverage_manifest": fingerprint(
            {
                key: prepared["manifest"].get(key)
                for key in ("page_coverage", "unprocessed", "context_not_included", "reading_gaps")
            }
        ),
        "registry": fingerprint(prepared["registry"]),
        "output_limit_policy": "provider_default",
        "request_timeout_seconds": min(client.timeout, 600),
    }
    binding_path = state_dir / "binding.json"
    if binding_path.exists() and json.loads(binding_path.read_text()) != binding:
        raise ValueError("program-only task binding changed; create a new task")
    atomic_json(binding_path, binding)
    stage = stage_context(PROGRAM_STAGE_VERSION, PROGRAM_PROMPT_VERSION, PROGRAM_INSTRUCTIONS)
    binding = {
        **binding,
        **stage,
        "pipeline_version": pipeline_versions.PROGRAM_PIPELINE_VERSION,
        "model": model_cache_identity(client),
        "contract": CONTRACT_VERSION,
        "schema": fingerprint(PROGRAM_SCHEMA),
    }
    binding_hash = fingerprint(binding)
    atomic_json(work / "binding.json", binding)
    package = new_package(source_id, source_type, [asset], run_id)
    atomic_json(work / "source-registry.json", prepared["registry"])
    for coverage in package["coverage"]:
        if coverage.get("scope") == "registered_assets":
            coverage.update(handling="checked", reason="Registered source identity and prepared page inventory checked")
    package["header"].update(
        document_pipeline_version="2",
        document_artifact_layout="attempt-v1",
        document_extraction_mode=VERSION,
        review_policy=REVIEW_POLICY,
        candidate_review_protocol="human_reviewed_sol_candidate_v1",
        source_review_method="human_review_after_extraction",
        document_reading_policy=document_reading_policy(),
        document_input_strategy="mineru_standard_files_plus_source_pdf",
        document_output_format=CONTRACT_VERSION,
        actual_model=client.model,
    )
    manifest = prepared["manifest"]
    atomic_json(work / "source-registry.json", prepared["registry"])
    for page in manifest.get("page_coverage", []):
        add_coverage(
            package,
            {"asset_ref": asset["asset_id"], "page": page["page"]},
            "indexed",
            page.get("read_status") or "unknown",
        )
    group_results, traces = [], []
    study_roster = {}
    unresolved = list(manifest.get("unprocessed", [])) + list(manifest.get("context_not_included", []))
    for packet in prepared["packets"]:
        group = packet["group_id"]
        # Later packets include the roster derived from earlier model responses.
        group_binding = fingerprint([binding_hash, list(study_roster.values())])
        result_path = state_dir / f"group-{fingerprint(group)}-{group_binding}.json"
        receipt = json.loads(result_path.read_text()) if result_path.exists() else None
        if receipt is not None and receipt.get("binding") != group_binding:
            raise ValueError("group checkpoint binding differs")
        if receipt is None:
            snapshot = client.budget.snapshot()
            reason = (
                "cancelled"
                if cancelled and cancelled()
                else "interrupted model attempt remains unresolved"
                if snapshot["pending_reservations"]
                else "model task budget exhausted"
                if snapshot["remaining_seconds"] <= 0
                else "model stage deadline exhausted"
                if time.monotonic() - started >= 600
                else None
            )
            if reason:
                receipt = {
                    "status": "not_processed",
                    "reason": reason,
                    "seconds": 0,
                    "binding": group_binding,
                    "group_id": group,
                }
                # No HTTP dispatch occurred; a later task recovery may process it.
            else:
                receipt = {
                    "status": "dispatching",
                    "binding": group_binding,
                    "group_id": group,
                    "started_epoch": time.time(),
                }
                _claim_group(result_path, receipt, client.budget)
                begin = time.monotonic()
                original_timeout = client.timeout
                try:
                    client.timeout = min(original_timeout, 600, snapshot["remaining_seconds"], 600 - (begin - started))
                    request_text = packet["text"]
                    if study_roster:
                        request_text += "\nprior_study_units=" + json.dumps(
                            list(study_roster.values()), ensure_ascii=False
                        )
                    raw = client.call(
                        PROGRAM_INSTRUCTIONS,
                        request_text,
                        PROGRAM_SCHEMA,
                        phase="program-extract-" + group,
                        files=packet.get("files", []),
                        images=packet.get("images", []),
                        bypass_cache=True,
                        context={
                            **stage,
                            "mode": VERSION,
                            "binding": group_binding,
                            "group_id": group,
                            "stage_scope": binding["task_identity"],
                        },
                    )
                    receipt.update(raw=raw, raw_sha256=fingerprint(raw))
                    receipt["study_units"] = _study_unit_mappings(
                        raw,
                        source_id=source_id,
                        extraction_batch=binding["task_identity"],
                        group=group,
                        offered=study_roster,
                    )
                    receipt["status"] = "completed"
                except (ModelCallError, ValueError) as exc:
                    receipt.update(status="failed", reason=str(exc), error_type=type(exc).__name__)
                    if isinstance(exc, ModelCallError) and exc.outcome_unknown:
                        receipt["outcome_unknown"] = True
                finally:
                    client.timeout = original_timeout
                    receipt["seconds"] = time.monotonic() - begin
                    atomic_json(result_path, receipt)
        if receipt["status"] == "dispatching":
            receipt = {
                **receipt,
                "status": "not_processed",
                "reason": "interrupted dispatch has unknown result; no automatic redispatch",
            }
        raw = receipt.get("raw")
        if receipt["status"] == "completed":
            if fingerprint(raw) != receipt.get("raw_sha256"):
                raise ValueError("group checkpoint output integrity failure")
            mappings = _study_unit_mappings(
                raw, source_id=source_id, extraction_batch=binding["task_identity"], group=group, offered=study_roster
            )
            if receipt.get("study_units", []) != mappings:
                raise ValueError("group checkpoint study-unit identity differs")
            _update_study_roster(study_roster, raw, mappings)
            # Keep the exact model output as the candidate. Six-category
            # materialization is deferred until a researcher confirms content.
        summary = {k: v for k, v in receipt.items() if k != "raw"}
        group_results.append(summary)
        if raw is not None:
            atomic_json(work / ("raw-" + fingerprint(group)[:24] + ".json"), raw)
        if summary["status"] != "completed":
            unresolved.append(
                {
                    "group_id": group,
                    "pages": packet.get("pages", []),
                    "reason": summary.get("reason", "group incomplete"),
                }
            )
        add_coverage(
            package,
            {"group_id": group, "pages": packet.get("pages", [])},
            "extracted" if summary["status"] == "completed" else "not_checked",
            "Sol candidate saved for human review; no six-category materialization yet",
        )
        atomic_json(work / "group-results.json", group_results)
        atomic_json(work / "conversion-trace.json", traces)
    for problem in unresolved:
        add_issue(package, json.dumps(problem, ensure_ascii=False), severity="warning")
    gaps = manifest.get("reading_gaps", [])
    from .document_program_quality import assess_program_package

    quality = assess_program_package(package, read_gaps=gaps, unresolved=unresolved)
    validate_package(package)
    atomic_json(work / "quality-report.json", quality)
    atomic_json(work / "candidate-package.json", package)
    atomic_json(
        work / "performance.json",
        {
            "model": client.model,
            "mode": VERSION,
            "review_policy": REVIEW_POLICY,
            "elapsed_seconds": time.monotonic() - started,
            "model_seconds_recorded_total": sum(r.get("seconds", 0) for r in group_results),
            "stage_times": [{k: r.get(k) for k in ("group_id", "status", "seconds")} for r in group_results],
            "model_budget": client.budget.snapshot(),
            "automatic_review_calls": 0,
            "automatic_correction_calls": 0,
            "groups": len(groups),
            "status": quality["outcome"],
            "assertions": len(package["assertions"]),
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "scientific_acceptance": False,
        },
    )
    return package


def extract_program_document(assets, source_id, options, processing_run_id, *, project_budget_context=None):
    if len(assets) != 1 or not assets[0]["original_name"].lower().endswith(".pdf"):
        raise ValueError("Program-only extraction accepts exactly one PDF original")
    config = options.get("model_config") or {}
    if not config.get("model") or not config.get("api_key"):
        raise ValueError("Program-only extraction requires configured model credentials")
    asset = assets[0]
    shared = Path(options["work_dir"])
    work = shared / "artifacts" / fingerprint(processing_run_id)
    work.mkdir(parents=True, exist_ok=True)
    begin = time.monotonic()
    reader_dir = Path(options.get("reader_cache_dir", shared / "reading")) / asset["sha256"]
    document = read_document_ir(
        asset,
        reader_dir,
        mineru_command=options.get("mineru_command"),
        cancelled=options.get("cancel_check"),
        reread_state_dir=shared / "reading-state",
        reread_task_binding=fingerprint([source_id, processing_run_id.rsplit(":attempt", 1)[0]]),
        reread_findings=options.get("document_reading_findings"),
    )
    if document.get("asset_ref") != asset["asset_id"] or document.get("source_sha256") != asset["sha256"]:
        raise ValueError("document reader source identity differs from registered PDF")
    atomic_json(work / "document-ir.json", {"version": "1", "documents": [document]})
    middle_path = reader_dir / "middle_json.json"
    prepared = prepare_standard_inputs(
        Path(asset["local_path"]),
        document,
        middle_path if middle_path.is_file() else None,
        work / "attachments",
        reader_dir=reader_dir,
        reread_state_dir=shared / "reading-state",
        max_pages=30,
    )
    atomic_json(work / "input-manifest.json", prepared["manifest"])
    atomic_json(work / "input-summary.json", [_packet_identity(p) for p in prepared["packets"]])
    preparation_seconds = time.monotonic() - begin
    budget = document_budget_options(
        {"max_wall_seconds": 600, "active_time": True, "budget_state_path": shared / "model-budget.json"},
        max_output_tokens=None,
        model=config["model"],
    )
    client = ModelClient(
        project_budget_context=project_budget_context,
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
        model=config["model"],
        protocol=config.get("protocol", "responses"),
        budget=ModelBudget(**(budget | options.get("_model_workspace", {}))),
        ledger_path=shared / "program-model-attempts.jsonl",
        max_output_tokens=None,
        max_attempts=1,
        timeout=600,
        reasoning_effort="medium",
        cancel_check=options.get("cancel_check"),
        cache_dir=None,
        count_input_tokens=False,
        cache_namespace=options.get("_full_reparse_run"),
        incomplete_artifact_dir=work / "incomplete",
    )
    result = run_program_packets(
        prepared,
        asset=asset,
        source_id=source_id,
        source_type=options.get("source_type", "paper"),
        run_id=processing_run_id,
        client=client,
        work=work,
        state_dir=shared / "program-state",
        cancelled=options.get("cancel_check"),
    )
    metrics = json.loads((work / "performance.json").read_text())
    metrics.update(reading_preparation_seconds=preparation_seconds, total_seconds=time.monotonic() - begin)
    atomic_json(work / "performance.json", metrics)
    return result
