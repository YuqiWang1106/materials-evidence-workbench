"""Read located blocks, then save entities, attributes, and related links.
Each phase writes an artifact and can be rerun: read, windows, extract, check, merge, resolve, write, claims.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from pathlib import Path

from kb_ingest.contract import new_package
from kb_ingest.core import atomic_json, fingerprint
from kb_ingest.core.config import DEFAULT_DOCUMENT_MODEL
from kb_ingest.extraction.entity_tables import mapped_tables, prefer_table, without_tables
from kb_ingest.extraction.entity_tables_graph import table_phase
from kb_ingest.extraction.entity_windows import build_windows, reading_order
from kb_ingest.ingest.documents.document_closeout import CloseIssues, closeout
from kb_ingest.ingest.documents.document_pipeline import journal_workspace_id
from kb_ingest.ingest.documents.document_pipeline_blocks import ir_blocks
from kb_ingest.ingest.documents.handwriting_check import apply_to_documents as apply_handwriting_checks
from kb_ingest.ingest.documents.handwriting_check import handwriting_phase
from kb_ingest.ingest.documents.source_reading import read_documents
from kb_ingest.llm import ModelBudget, ModelCallError
from kb_ingest.llm.errors import ModelRequestBindingError
from kb_ingest.llm.logical_request import BudgetOpenEvidence, LogicalRequestFamily
from kb_ingest.llm.model_limits import RUN_REQUEST_CEILING, document_budget_options
from kb_ingest.observation import hooks as _observe
from kb_ingest.observation import model_calls as _calls
from kb_ingest.storage.entity_store import EntityStore
from kb_ingest.storage.entity_store_confirm import ConfirmationClaims, TaskPublication

from . import entity_meaning as _meaning
from . import entity_pipeline_blocks as _epb
from . import entity_pipeline_phases as _epp
from .entity_claims import plan_claim_groups
from .entity_contract import (
    RESOLVE_INSTRUCTIONS,
    RESOLVE_SCHEMA,
    validate_instance,
)
from .entity_extract import WINDOW_REQUESTS
from .entity_identity import identity_phase
from .entity_merge import link_named, linked_only, merge_windows
from .entity_model_calls import EntityModelCallContext, request_identity
from .entity_model_calls import client_call as _client_call
from .entity_pipeline_constants import CLOSING_REQUESTS
from .entity_pipeline_phases import check_summary, synonym_phase
from .entity_reading import select_primary_reading
from .entity_request_identity import EntityRequestIdentity
from .entity_resolve import resolve_graph
from .entity_visual import select_visual_blocks

VERSION = "entity-graph-v1"
ENTITY_STAGE_VERSION = "entity-stage-v1"
ENTITY_PROMPT_VERSION = "entity-prompts-v1"
MAX_OUTPUT_TOKENS = 32000


def window_budget(sent, ceiling=RUN_REQUEST_CEILING):
    """Windows that still fit under the request ceiling once `sent` requests (visual reads, tables) are made."""
    return max(0, (ceiling - sent - CLOSING_REQUESTS) // WINDOW_REQUESTS)


MAX_DOCUMENT_REQUESTS = 240
MIN_DOCUMENT_SECONDS, MAX_DOCUMENT_SECONDS = 3600, 4 * 3600


def plan_allowance(blocks, documents=None):
    """Requests and model time this document needs, from its windows, tables and visual reads.

    Each planned request may take a full REQUEST_TIMEOUT_SECONDS; a 25 % request margin and a 20 % time margin cover
    retries, table re-mapping and dense windows. The ceiling never drops below the fixed run ceiling.
    """
    visual, _ = select_visual_blocks(blocks, documents)
    scanned = sum(1 for block in visual if block["kind"] == "unread_page")
    windows, tables = len(build_windows(blocks)), len(mapped_tables(blocks))
    planned = len(visual) + tables + WINDOW_REQUESTS * (windows + scanned) + CLOSING_REQUESTS
    calls = min(MAX_DOCUMENT_REQUESTS, max(RUN_REQUEST_CEILING, math.ceil(planned * 1.25)))
    seconds = min(MAX_DOCUMENT_SECONDS, max(MIN_DOCUMENT_SECONDS, math.ceil(planned * REQUEST_TIMEOUT_SECONDS * 1.2)))
    return {
        "planned_requests": planned,
        "windows": windows,
        "tables": tables,
        "visual_reads": len(visual),
        "request_ceiling": calls,
        "time_allowance_seconds": seconds,
    }


def client_call(options, client, package):
    def cancelled():
        return bool(options.get("cancel_check") and options["cancel_check"]())

    context = EntityModelCallContext(
        identity=request_identity(options, client, package),
        source_revision_id=package["header"]["source_revision_id"],
        source_type=package["sources"][0]["source_type"],
        workspace_id=journal_workspace_id(options),
        work_dir=options["work_dir"],
        load_asset_ids=lambda: tuple(asset["asset_id"] for asset in package.get("assets", [])),
        reparse_run=options.get("_full_reparse_run"),
        observation_root=options.get("_observation_root"),
        cancel_check=cancelled,
    )
    return _client_call(context, client, stage_version=ENTITY_STAGE_VERSION, prompt_version=ENTITY_PROMPT_VERSION)


def resolve_phase(merged, entity_store, workspace_id, source_id, blocks, call, unresolved=None, embedder=None):
    def choose(items):
        try:
            result = call("resolve", RESOLVE_INSTRUCTIONS, {"items": items}, RESOLVE_SCHEMA)
            validate_instance(result, RESOLVE_SCHEMA)
        except (ModelCallError, ValueError) as exc:
            # Without an answer every ambiguous entity is uncertain: new, and related to each candidate.
            if unresolved is None:
                raise
            unresolved.append({"phase": "resolve", "reason": type(exc).__name__, "items": len(items)})
            return []
        return result["decisions"]

    similar = (
        _meaning.meaning_candidates(embedder, entity_store, workspace_id, merged, unresolved) if embedder else None
    )
    return resolve_graph(
        merged,
        workspace_id=workspace_id,
        source_id=source_id,
        blocks=blocks,
        lookup=lambda entity_type: entity_store.candidates(workspace_id, entity_type),
        choose=choose,
        similar=similar,
    )


def graph_summary(graph):
    return {
        "entities": dict(Counter(entity["type"] for entity in graph["entities"])),
        "attributes": len(graph["attributes"]),
        "related": len(graph["related"]),
    }


REQUEST_TIMEOUT_SECONDS = 360
# Stages that need the model; reported as not processed when no credentials are configured.
MODEL_STAGES = ("visual_reading", "table_mapping", "entity_extraction", "synonyms", "identity")
REQUEST_ATTEMPTS = 3


def _client(options, shared, work, processing_run_id, allowance=None):
    config = dict(options.get("model_config") or {})
    if not config.get("api_key"):
        return None
    config["model"] = config.get("model") or DEFAULT_DOCUMENT_MODEL
    config["reasoning_effort"] = "medium"
    # Dense windows can need 200-300 seconds per response; only configured transient failures retry.
    config["timeout"] = REQUEST_TIMEOUT_SECONDS
    config["max_attempts"] = REQUEST_ATTEMPTS
    config["retry_transient"] = True
    config["max_output_tokens"] = max(int(config.get("max_output_tokens") or 0), MAX_OUTPUT_TOKENS)
    ceiling = (allowance or {}).get("request_ceiling", RUN_REQUEST_CEILING)
    budget_options = document_budget_options(
        config.pop("budget", {}), max_output_tokens=config["max_output_tokens"], model=config["model"], ceiling=ceiling
    )
    budget_options.update(budget_state_path=shared / "model-budget.json", active_time=True)
    if allowance:
        budget_options.setdefault("max_wall_seconds", allowance["time_allowance_seconds"])
    config["require_full_timeout"] = True
    evidence = options.get("_model_budget_evidence")
    if not isinstance(evidence, BudgetOpenEvidence):
        raise ModelRequestBindingError()
    config.update(
        budget=ModelBudget(
            **(budget_options | options.get("_model_workspace", {})),
            open_evidence=evidence,
            creation_permit=options.get("_model_budget_permit"),
        ),
        ledger_path=work / "model-attempts.jsonl",
        cancel_check=options.get("cancel_check"),
    )
    return _calls.ObservedModelClient(**config)


def extract_entity_document(assets, source_id, options, processing_run_id):
    started = time.monotonic()
    shared = Path(options["work_dir"])
    work = shared / "artifacts" / fingerprint(processing_run_id)
    work.mkdir(parents=True, exist_ok=True)
    source_type = options.get("source_type", "paper")
    if source_type not in ("paper", "patent"):
        raise ValueError("PDF source type required")
    package = new_package(source_id, source_type, assets, processing_run_id)
    package["header"].update(document_artifact_layout="attempt-v1", document_extraction_mode=VERSION)

    stages = _observe.tracker(work)

    def report(stage, **metadata):
        stages.enter(stage, **metadata)
        atomic_json(
            work / "progress.json",
            {"stage": stage, "processing_run_id": processing_run_id, "updated_at": time.time(), **metadata},
        )

    options = {**options, "progress_callback": report, "_observation_root": work}
    report("reading", documents_total=len(assets))
    documents, gaps = read_documents(assets, source_id, options, processing_run_id, shared)
    stages.partial("reading_incomplete", when=gaps)
    reading_seconds = time.monotonic() - started
    atomic_json(work / "document-ir.json", {"version": "1", "documents": documents})
    atomic_json(work / "timing.json", {"reading_seconds": reading_seconds})
    full_blocks = ir_blocks(documents)
    blocks, selection = select_primary_reading(full_blocks)
    atomic_json(work / "entity-reading-selection.json", selection)
    extra = _epb.image_region_blocks(documents, assets, full_blocks)
    blocks = _epb.caption_blocks(reading_order(_epb.place_blocks(blocks, extra)))
    # The request ceiling and time allowance follow the document's size, not one fixed amount for every paper.
    allowance = plan_allowance(blocks, documents)
    atomic_json(work / "entity-allowance.json", allowance)
    ceiling = allowance["request_ceiling"]
    client = options.get("model_client") or _client(options, shared, work, processing_run_id, allowance)
    if client is None:
        stages.not_processed(MODEL_STAGES, "model_credentials_missing")
        report("finalizing")
        stages.partial("model_credentials_missing")
        return closeout(package, work, CloseIssues(gaps, [{"reason": "model credentials not configured"}]), started)
    store = options.get("entity_store")
    if store is None:
        raise ValueError("entity graph extraction requires the workspace store")
    _entity_store = store if isinstance(store, EntityStore) else EntityStore(store)
    _workspace_id = options.get("workspace_id") or "default"
    identity = request_identity(options, client, package)
    call = client_call(options, client, package)
    report("visual_reading")
    blocks, visual_readings, unresolved = _epp.visual_phase(
        blocks, assets, call, documents=documents, work=work, request_identity=identity
    )
    # The paid vision check of flagged handwritten cells runs only when asked; the two local readers are the default.
    handwriting_checks = []
    if (options.get("reader_options") or {}).get("handwriting_model_check") is True:
        with _epp.preserve_visual_on_interruption(work, blocks, visual_readings, unresolved):
            blocks, handwriting_checks, handwriting_gaps = handwriting_phase(
                blocks, assets, call, logical_bindings=_handwriting_bindings(identity, blocks), work=work
            )
        unresolved.extend(handwriting_gaps)
    stages.partial("visual_read_incomplete", when=unresolved)
    if handwriting_checks:
        atomic_json(work / "handwriting-checks.json", {"checks": handwriting_checks})
        apply_handwriting_checks(documents, blocks)
    atomic_json(work / "document-ir.json", {"version": "1", "documents": documents, "visual_readings": visual_readings})
    windows = build_windows(blocks)
    # Tables first, with the requests the prose windows leave over; mapped tables leave the prose windows.
    table_budget = ceiling - CLOSING_REQUESTS - WINDOW_REQUESTS * len(windows)
    report("table_mapping")
    table_graphs, covered, table_gaps = table_phase(
        blocks,
        windows,
        assets,
        source_type,
        call,
        budget=table_budget,
        work=work,
        request_identity=identity,
        progress=lambda **data: report("table_mapping", **data),
    )
    stages.partial("table_partial", when=table_gaps)
    unresolved.extend(table_gaps)
    if covered:
        windows = build_windows(without_tables(blocks, covered))
    window_bindings = (
        identity.plan(LogicalRequestFamily.WINDOW_EXTRACTION, [window["blocks"] for window in windows])
        if identity
        else None
    )
    allowed = window_budget(getattr(call, "sent", 0), ceiling)
    skipped = windows[allowed:]
    if skipped:
        unresolved.append(
            {
                "reason": "not_processed: the request allowance is used up; these windows can be resumed",
                "windows_skipped": len(skipped),
                "pages": sorted({page for window in skipped for page in window.get("pages") or []}),
            }
        )
    # Every window is listed, unprocessed ones too, so a supplement run can pick them up.
    atomic_json(
        work / "entity-windows.json",
        {
            "windows": [
                {
                    "window_id": window["window_id"],
                    "pages": window.get("pages"),
                    "block_ids": [block["block_id"] for block in window["blocks"]],
                    "characters": sum(len(block.get("text") or "") for block in window["blocks"]),
                    "processed": index < allowed,
                }
                for index, window in enumerate(windows)
            ]
        },
    )
    _observe.skipped(work, "window", [window["window_id"] for window in skipped], "request_allowance")
    windows = windows[:allowed]
    report("entity_extraction")
    checked, failures = _epp.extract_phase(
        windows,
        blocks,
        call,
        work,
        known=table_graphs,
        progress=lambda **data: report("entity_extraction", **data),
        ceiling=ceiling,
        logical_bindings=window_bindings[:allowed] if window_bindings is not None else None,
    )
    stages.partial("window_partial", when=failures or skipped)
    unresolved.extend(failures)
    report("synonyms")
    known = len(unresolved)
    merged, synonyms = synonym_phase(merge_windows(checked), call, unresolved)
    stages.partial("model_error", when=len(unresolved) > known)
    merged, unlinked = linked_only(link_named(prefer_table(merged)))
    report("identity")
    known = len(unresolved)
    merged, identities = identity_phase(merged, blocks, call, unresolved)
    stages.partial("model_error", when=len(unresolved) > known)
    report("finalizing")
    summary = {
        "windows": len(windows),
        "skipped_pages": sorted({page for window in skipped for page in window.get("pages") or []}),
        "failed_windows": [
            item["window"] for item in failures if item.get("window") and item["reason"] != "not_processed"
        ],
        "not_processed_windows": [window["window_id"] for window in skipped]
        + [item["window"] for item in failures if item.get("window") and item["reason"] == "not_processed"],
        "allowance": allowance,
        "tables": len(covered),
        "check": check_summary([], checked)["kept"],
        "merged": graph_summary(merged),
        "status": "awaiting_confirmation",
    }
    # The draft waits for a person to confirm or correct it; nothing is written to the knowledge base yet.
    atomic_json(
        work / "entity-graph.json",
        {**merged, "unlinked": unlinked, "synonyms": synonyms, "identities": identities, "summary": summary},
    )
    atomic_json(
        work / "entity-blocks.json",
        {
            block["block_id"]: {
                key: block.get(key)
                for key in (
                    "asset_ref",
                    "page",
                    "anchor",
                    "source",
                    "kind",
                    "text",
                    "table_region",
                    "table_context_for",
                    "segment_kind",
                    "ir_region",
                )
            }
            | ({"reading": block["reading"]} if block.get("reading") else {})
            for block in blocks
        },
    )
    package["header"]["entity_graph"] = summary
    dropped = sum(len(graph.get("dropped", [])) for graph in checked)
    if dropped:
        unresolved.append(
            {"phase": "check", "reason": "items without a matching source quote were not kept", "count": dropped}
        )
    return closeout(package, work, CloseIssues(gaps, unresolved), started, getattr(client, "model", None))


class _PublicationPlanTypeError(TypeError):
    pass


def confirm_entity_draft(work, *, entity_store, workspace_id, source_id, call, embedder=None, publish=None):
    """Prepare artifacts and model decisions, then commit the graph and publication receipt together.

    ``publish(summary)`` returns a TaskPublication applied in the same storage transaction.
    It must not take a Session, open another write transaction, or perform external work.
    Prepared files alone are not publication.
    """
    from .entity_review import effective_graph, load, normalize_names
    from .entity_review_evidence import validate_review_evidence

    draft, reviews, blocks = load(work)
    if draft is None:
        raise ValueError("no extracted draft to confirm")
    validate_review_evidence(draft, reviews, blocks)
    graph = normalize_names(effective_graph(draft, reviews))
    unresolved = []
    stages = _observe.tracker(work)
    stages.enter("resolve")
    resolved = resolve_phase(graph, entity_store, workspace_id, source_id, blocks, call, unresolved, embedder=embedder)
    embedding = any("embedding" in str(item.get("reason")) for item in unresolved)
    stages.partial("embedding_unavailable" if embedding else "model_error", when=unresolved)
    # Rehearse the exact fold/replacement rules to obtain the resulting claim IDs. Roll back
    # before model I/O: no draft rows become visible and the model ledger can write freely.
    projected_claims = entity_store.project_confirmation_claims(workspace_id, source_id, resolved)
    groups = {}
    stages.enter("claims")
    try:
        claims, groups = plan_claim_groups(projected_claims, source_id, call)
    except (ModelCallError, ValueError) as exc:
        claims = {"error": type(exc).__name__}
        unresolved.append({"phase": "claims", "reason": type(exc).__name__})
        stages.fail(exc)
    summary = {
        **(draft.get("summary") or {}),
        "status": "written",
        "reviewed": len(reviews),
        "confirmed": graph_summary(graph),
        "resolved": graph_summary(resolved),
        "decisions": dict(Counter(item["decision"] for item in resolved["decisions"])),
        "claims": claims,
        "unresolved": unresolved,
    }
    atomic_json(work / "entity-resolve.json", {"decisions": resolved["decisions"]})
    atomic_json(work / "entity-graph-resolved.json", {**resolved, "summary": summary})
    confirmation_claims = ConfirmationClaims(groups, projected_claims) if groups else None
    publication = publish(summary) if publish is not None else None
    if publication is not None and not isinstance(publication, TaskPublication):
        raise _PublicationPlanTypeError
    stages.enter("publishing")
    entity_store.confirm_source_graph(
        workspace_id,
        source_id,
        resolved,
        claims=confirmation_claims,
        publication=publication,
    )
    stages.finish()
    return summary


def task_call(options, package, processing_run_id):
    """The model call for work done after extraction (confirmation), with the same request state and journal."""
    shared = Path(options["work_dir"])
    work = shared / "artifacts" / fingerprint(processing_run_id)
    client = options.get("model_client") or _client(options, shared, work, processing_run_id)
    if client is None:
        raise ValueError("model credentials not configured")
    return client_call({**options, "_observation_root": work}, client, package)


def task_embedder(options, processing_run_id, cache_root):
    """Optional configured embeddings; names remain available without this capability."""
    from .entity_embedding import EntityEmbedder

    config = options.get("embedding_config") or options.get("model_config") or {}
    if options.get("model_client") is not None or not config.get("api_key"):
        return None
    legacy_required = bool(options.get("cost_ledger_path") or options.get("batch_id"))
    state_path = Path(options.get("work_dir", cache_root)) / "model-budget.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text())
            records = state.get("requests", {})
            old_activity = bool(state.get("calls") or state.get("pending") or state.get("settled"))
            legacy_required = (
                legacy_required or old_activity and (not records or any(row.get("legacy") for row in records.values()))
            )
        except (OSError, ValueError, AttributeError):
            legacy_required = True
    return EntityEmbedder(
        api_key=config["api_key"],
        base_url=config.get("base_url") or "https://api.openai.com/v1",
        model=config.get("model") if options.get("embedding_config") else "text-embedding-3-small",
        dimensions=config.get("dimensions") or 1536,
        legacy_cost_path=options.get("legacy_entity_cost_path") or options.get("cost_ledger_path"),
        legacy_state_required=legacy_required,
        legacy_run_id=options.get("cost_run_id", processing_run_id),
        cache_root=cache_root,
        model_workspace=options.get("_model_workspace", {}),
        observation_root=_timeline(options, processing_run_id),
    )


def _timeline(options, processing_run_id):
    """The attempt directory that holds the timeline; None when the caller has no task workspace."""
    shared = options.get("work_dir")
    return Path(shared) / "artifacts" / fingerprint(processing_run_id) if shared else None


def _handwriting_bindings(identity: EntityRequestIdentity | None, blocks: list[dict]) -> dict | None:
    if identity is None:
        return None
    table_ids = {
        block["table_region"]
        for block in blocks
        if block.get("kind") == "table_cell" and block.get("source") == "handwriting" and block.get("table_region")
    }
    tables = [block for block in blocks if block["block_id"] in table_ids]
    bindings = identity.plan(LogicalRequestFamily.HANDWRITING_CHECK, [[table] for table in tables])
    return {table["block_id"]: binding for table, binding in zip(tables, bindings, strict=True)}
