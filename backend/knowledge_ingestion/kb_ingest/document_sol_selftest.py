"""Explicit, isolated MinerU-text + original-PDF document task path.

This reuses the already bounded controlled extraction and publication gate.
No source-specific prompt, answer key or implicit model fallback is accepted.
"""

from __future__ import annotations

from pathlib import Path

from .core import atomic_json, fingerprint
from .document_controlled_flow import run_controlled
from .document_controlled_inputs import build_controlled_inputs
from .ingest.documents.document_ir import read_document_ir
from .ingest.documents.document_pipeline import journal_workspace_id
from .llm import ModelBudget, ModelClient
from .llm.model_limits import document_budget_options


def extract_sol_full_pdf(assets, source_id, options, processing_run_id):
    if len(assets) != 1 or not assets[0]["original_name"].lower().endswith(".pdf"):
        raise ValueError("Full-PDF self-test accepts exactly one PDF original")
    config = options.get("model_config") or {}
    if not config.get("model") or not config.get("api_key"):
        raise ValueError("Document model credentials must be explicitly configured")
    asset = assets[0]
    shared = Path(options["work_dir"])
    document = read_document_ir(
        asset,
        Path(options.get("reader_cache_dir", shared / "reading")) / asset["sha256"],
        mineru_command=options.get("mineru_command"),
        cancelled=options.get("cancel_check"),
        reread_state_dir=shared / "reading-state",
        reread_task_binding=fingerprint([source_id, processing_run_id.rsplit(":attempt", 1)[0]]),
        reread_findings=options.get("document_reading_findings"),
    )
    if document.get("runtime", {}).get("status") != "available":
        raise RuntimeError("MinerU is not ready; no model call was made")
    if document.get("parser", {}).get("status") not in {"complete", "partial"}:
        raise RuntimeError("MinerU parse did not complete; no model call was made")

    work = shared / "artifacts" / fingerprint(processing_run_id)
    work.mkdir(parents=True, exist_ok=True)
    atomic_json(work / "document-ir.json", {"version": "1", "documents": [document]})
    task = {
        "scope": (
            "All explicitly reported studies or patent examples, samples, compositions, preparation, tests,"
            " results, author explanations and source limitations; keep claims separate from measurements."
        ),
        "source_type": options.get("source_type", "paper"),
        "source_sha256": asset["sha256"],
    }
    prepared = build_controlled_inputs(
        Path(asset["local_path"]), {"documents": [document]}, task, "B", work / "visuals"
    )
    atomic_json(work / "input-manifest.json", prepared["manifest"])
    # Base64 PDF bytes stay in memory and the model request, never in audit files.
    atomic_json(
        work / "input-summary.json",
        {
            "text_sha256": fingerprint(prepared["text"]),
            "pdf_sha256": asset["sha256"],
            "input_strategy": "mineru_text_full_pdf",
            "source_pages": prepared["manifest"]["source_pages"],
        },
    )

    output_limit = 20_000
    budget = document_budget_options(
        {"max_wall_seconds": 600, "active_time": True, "budget_state_path": shared / "model-budget.json"},
        max_output_tokens=output_limit,
        model=config["model"],
    )
    client = ModelClient(
        api_key=config["api_key"],
        base_url=config.get("base_url", "https://api.openai.com/v1"),
        model=config["model"],
        protocol=config.get("protocol", "responses"),
        budget=ModelBudget(**(budget | options.get("_model_workspace", {}))),
        ledger_path=work / "model-attempts.jsonl",
        max_output_tokens=output_limit,
        max_attempts=1,
        timeout=240,
        reasoning_effort="medium",
        cache_dir=None,
        count_input_tokens=False,
        cache_namespace=options.get("_full_reparse_run"),
        incomplete_artifact_dir=work / "incomplete",
    )
    prepared["workspace_id"] = journal_workspace_id(options)
    return run_controlled(
        prepared,
        asset=asset,
        source_id=source_id,
        source_type=task["source_type"],
        run_id=processing_run_id,
        client=client,
        work=work,
        output_format="compact",
        whole_source=True,
        stage_dir=shared / "controlled-stages",
    )
