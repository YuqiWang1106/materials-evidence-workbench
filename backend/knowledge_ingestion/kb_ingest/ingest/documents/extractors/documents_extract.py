"""Run evidence-bound paper and patent extraction."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kb_ingest import pipeline_versions
from kb_ingest.contract import (
    FIELDS,
    RELATIONS,
    add_coverage,
    add_evidence,
    add_issue,
    add_object,
    new_package,
    validate_package,
)
from kb_ingest.core import fingerprint
from kb_ingest.llm import ModelBudget, ModelCallError, ModelClient
from kb_ingest.stage_cache import stage_context

from ..readers.documents import SourceReadError, chunk_blocks, read_pdf_asset, render_page
from .documents import (
    DISCOVERY_INSTRUCTIONS,
    DISCOVERY_PROMPT_VERSION,
    DISCOVERY_SCHEMA,
    DISCOVERY_STAGE_VERSION,
    EXTRACTION_INSTRUCTIONS,
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SCHEMA,
    EXTRACTION_STAGE_VERSION,
)
from .documents_inventory import UnitObjectInventory, _prompt_blocks
from .documents_materialize import _check_observation_closure, _materialize


def extract_document(assets: list[dict], source_id: str, options: dict, processing_run_id: str) -> dict:
    """Discover scoped units, extract each bounded context, and retain all failures.

    Options accept an injected model_client or model_config with credentials read by
    the caller. No old-project import, hidden reference input, or implicit provider.
    """
    source_type = options.get("source_type", "paper")
    if source_type not in {"paper", "patent"}:
        raise ValueError("document source_type must be paper or patent")
    pkg = new_package(source_id, source_type, assets, processing_run_id)
    pkg["header"]["status"] = "partial"
    model_source_context = {
        "source_id": source_id,
        "source_revision_id": pkg["header"]["source_revision_id"],
        "asset_sha256": {a["asset_id"]: a["sha256"] for a in assets},
    }
    model_source_context["pipeline"] = pipeline_versions.PIPELINE_VERSION
    model_source_context["stage_scope"] = pkg["header"]["source_revision_id"]
    discovery_context = {
        **model_source_context,
        **stage_context(DISCOVERY_STAGE_VERSION, DISCOVERY_PROMPT_VERSION, DISCOVERY_INSTRUCTIONS),
    }
    extraction_context = {
        **model_source_context,
        **stage_context(EXTRACTION_STAGE_VERSION, EXTRACTION_PROMPT_VERSION, EXTRACTION_INSTRUCTIONS),
    }
    registry, page_metadata, asset_map = {}, [], {a["asset_id"]: a for a in assets}
    for asset in assets:
        if Path(asset["original_name"]).suffix.lower() != ".pdf":
            add_coverage(
                pkg,
                {"asset_ref": asset["asset_id"]},
                "unsupported",
                "Document input currently requires PDF; attachment retained",
            )
            continue
        try:
            index = read_pdf_asset(asset)
        except (OSError, SourceReadError) as exc:
            add_issue(pkg, f"Source read failed: {type(exc).__name__}", severity="error")
            add_coverage(pkg, {"asset_ref": asset["asset_id"]}, "unreadable", "Source bytes cannot be safely indexed")
            continue
        registry.update({b["block_id"]: b for b in index["blocks"]})
        page_metadata.extend(index["pages"])
        for block in index["blocks"]:
            add_evidence(pkg, asset["asset_id"], block["anchor"], block["text"], content_kind="native_pdf_text")
        add_coverage(
            pkg,
            {"asset_ref": asset["asset_id"], "pages": len(index["pages"])},
            "native_text_indexed",
            "Text and image locations read; scientific coverage not verified",
        )
    for page in page_metadata:
        if page["needs_visual_read"]:
            add_coverage(
                pkg,
                {"asset_ref": page["asset_ref"], "visual_page": page["page"]},
                "not_checked",
                "Visual content awaits a bounded image-reading call",
            )
    client = options.get("model_client")
    if client is None and options.get("model_config"):
        config = dict(options["model_config"])
        budget_options = config.pop("budget", {})
        client = ModelClient(**config, budget=ModelBudget(**(budget_options | options.get("_model_workspace", {}))))
    if client is None:
        add_issue(pkg, "No model configured; native evidence indexed but semantic extraction not run", severity="error")
        return pkg
    max_chars = int(options.get("context_chars", 18000))
    selected_pages = options.get("page_numbers")
    model_blocks = list(registry.values())
    if selected_pages is not None:
        selected_pages = set(selected_pages)
        if not selected_pages or any(not isinstance(p, int) or p < 1 for p in selected_pages):
            raise ValueError("page_numbers must contain positive one-based page numbers")
        for asset in assets:
            omitted = [
                p["page"]
                for p in page_metadata
                if p["asset_ref"] == asset["asset_id"] and p["page"] not in selected_pages
            ]
            if omitted:
                add_coverage(
                    pkg,
                    {"asset_ref": asset["asset_id"], "omitted_pages": omitted},
                    "not_checked",
                    "Explicit page subset; omitted pages are not source absence",
                )
        model_blocks = [b for b in model_blocks if b["page"] in selected_pages]
        page_metadata = [p for p in page_metadata if p["page"] in selected_pages]
    discoveries = []
    cancelled = threading.Event()
    for ordinal, blocks in enumerate(chunk_blocks(model_blocks, max_chars=max_chars)):
        registry.update({b["block_id"]: b for b in blocks})
        supplied_ids = {b["block_id"] for b in blocks}
        scope = {"phase": "discovery", "chunk": ordinal, "block_ids": [b["block_id"] for b in blocks]}
        if cancelled.is_set():
            add_coverage(pkg, scope, "not_checked", "cancelled before dispatch")
            continue
        try:
            result = client.call(
                DISCOVERY_INSTRUCTIONS,
                json.dumps(
                    {"source_type": source_type, "blocks": _prompt_blocks(blocks), "available_pages": page_metadata},
                    ensure_ascii=False,
                ),
                DISCOVERY_SCHEMA,
                phase="discovery",
                context={**discovery_context, "chunk": ordinal},
            )
            for unit in result.get("units", []):
                ids = unit.get("block_ids", []) + unit.get("context_block_ids", [])
                if not unit.get("scope") or not unit.get("label") or not ids or any(i not in supplied_ids for i in ids):
                    add_issue(pkg, "Discovery returned missing or unknown source blocks", severity="error")
                    continue
                discoveries.append(unit)
            for gap in result.get("gaps", []):
                add_issue(pkg, str(gap), severity="warning")
            add_coverage(
                pkg, scope, "candidate_discovery", "Model discovery completed; object recall not independently verified"
            )
        except ModelCallError as exc:
            if str(exc) in {"cancelled", "cancellation_check_failed"}:
                cancelled.set()
            add_issue(pkg, f"Discovery not completed: {exc}", severity="error")
            add_coverage(pkg, scope, "not_checked", "Model failure or budget limit, not source absence")
    # Only exact scope+label+kind matches are joined; no fuzzy same-name matching.
    units = {}
    for unit in discoveries:
        identity = fingerprint([unit.get("kind"), unit["scope"], unit["label"]])
        if identity not in units:
            units[identity] = dict(unit)
        else:
            for key in ("block_ids", "context_block_ids", "visual_pages", "object_labels"):
                units[identity][key] = list(dict.fromkeys(units[identity].get(key, []) + unit.get(key, [])))
    fields = [
        {
            "field_id": f["field_id"],
            "definition": f["definition"],
            "owner_kinds": f["owner_kinds"],
            "value_kinds": f["value_kinds"],
        }
        for f in FIELDS.values()
    ]
    relations = [
        {"predicate": r["predicate"], "from": r["allowed_from_kinds"], "to": r["allowed_to_kinds"]}
        for r in RELATIONS.values()
    ]

    package_lock = threading.RLock()
    visual_success = set()
    for identity, unit in units.items():
        unit_id = f"{source_id}:unit:{identity[:20]}"
        anchor_id = add_object(pkg, "content_unit", unit["label"], unit_ref=unit_id, source_scope=unit["scope"])
        pkg["units"].append(
            {
                "unit_id": unit_id,
                "anchor_object_ref": anchor_id,
                "kind": unit["kind"],
                "source_revision_id": pkg["header"]["source_revision_id"],
                "boundary_basis": "model_source_discovery",
                "is_independent_experiment_claim": False,
            }
        )

    def extract_unit(pair):
        identity, unit = pair
        unit_id = f"{source_id}:unit:{identity[:20]}"
        inventory = UnitObjectInventory(unit_id)
        selected_ids = list(dict.fromkeys(unit["block_ids"] + unit.get("context_block_ids", [])))
        selected = [registry[i] for i in selected_ids]
        parts = chunk_blocks(selected, max_chars=max_chars)
        selected_pages = {(b["asset_ref"], b["page"]) for b in selected}
        selected_assets = {b["asset_ref"] for b in selected}
        requested = set(unit.get("visual_pages", []))
        required_images = {
            (p["asset_ref"], p["page"])
            for p in page_metadata
            if (p["asset_ref"], p["page"]) in selected_pages
            and p["needs_visual_read"]
            or p["asset_ref"] in selected_assets
            and p["page"] in requested
        }
        image_limit = int(options.get("images_per_call", 3))
        if image_limit < 1:
            raise ValueError("images_per_call must be positive")
        jobs, assigned_images = [], set()
        for part_no, part in enumerate(parts):
            page_pairs = {(b["asset_ref"], b["page"]) for b in part}
            pages = (
                sorted(required_images & page_pairs - assigned_images) if options.get("include_images", True) else []
            )
            batches = [pages[i : i + image_limit] for i in range(0, len(pages), image_limit)] or [[]]
            for batch_no, batch in enumerate(batches):
                jobs.append(
                    (part_no, batch_no, part, batch, "native_and_visual" if batch_no == 0 else "visual_batch_only")
                )
            assigned_images.update(pages)
        remaining_images = sorted(required_images - assigned_images)
        if options.get("include_images", True):
            # An explicitly requested image page may contain no selected native block.
            for offset in range(0, len(remaining_images), image_limit):
                jobs.append(
                    (
                        len(parts),
                        offset // image_limit,
                        parts[-1] if parts else [],
                        remaining_images[offset : offset + image_limit],
                        "visual_batch_only",
                    )
                )
        elif remaining_images:
            with package_lock:
                add_issue(pkg, "Visual reading disabled for required source pages", [unit_id], severity="error")
        for part_no, batch_no, part, pages, focus in jobs:
            scope = {"unit_id": unit_id, "part": part_no, "visual_batch": batch_no}
            if cancelled.is_set():
                with package_lock:
                    add_coverage(pkg, scope, "not_checked", "cancelled before dispatch")
                    for aid, page in pages:
                        add_coverage(
                            pkg,
                            {**scope, "asset_ref": aid, "visual_page": page},
                            "not_checked",
                            "cancelled before dispatch",
                        )
                continue
            active, images, rendered_pages = list(part), [], []
            for aid, page in pages:
                try:
                    visual, uri = render_page(asset_map[aid], page)
                    active.append(visual)
                    images.append(uri)
                    rendered_pages.append((aid, page))
                except (SourceReadError, OSError) as exc:
                    with package_lock:
                        add_issue(pkg, "Image rendering failed: " + type(exc).__name__, [unit_id], severity="error")
                        add_coverage(
                            pkg,
                            {**scope, "asset_ref": aid, "visual_page": page},
                            "not_checked",
                            "Image renderer failed",
                        )
            inventory_prompt = inventory.as_prompt()
            try:
                if len(json.dumps(inventory_prompt, ensure_ascii=False)) > int(
                    options.get("inventory_char_limit", 48000)
                ):
                    raise ModelCallError(
                        "shared object inventory exceeds configured context bound; remaining scope preserved"
                    )
                candidate = client.call(
                    EXTRACTION_INSTRUCTIONS,
                    json.dumps(
                        {
                            "source_type": source_type,
                            "unit": unit,
                            "focus": focus,
                            "blocks": _prompt_blocks(active),
                            "object_inventory": inventory_prompt,
                            "fields": fields,
                            "relation_types": relations,
                        },
                        ensure_ascii=False,
                    ),
                    EXTRACTION_SCHEMA,
                    phase="extract",
                    images=images,
                    context={
                        **extraction_context,
                        "unit": identity,
                        "part": part_no,
                        "visual_batch": batch_no,
                        "visual_pages": rendered_pages,
                    },
                )
                with package_lock:
                    _materialize(pkg, candidate, active, unit_id, f"{part_no}:{batch_no}", inventory=inventory)
                    add_coverage(
                        pkg,
                        scope,
                        "candidate_extracted",
                        "Source references checked; scientific completeness and relations remain unreviewed",
                    )
                    for aid, page in rendered_pages:
                        visual_success.add((aid, page))
                        add_coverage(
                            pkg,
                            {"asset_ref": aid, "visual_page": page},
                            "candidate_visual_read",
                            "Page image supplied to a completed candidate call; transcription unreviewed",
                        )
                        add_coverage(
                            pkg,
                            {**scope, "asset_ref": aid, "visual_page": page},
                            "candidate_visual_read",
                            "Image batch completed with shared object inventory",
                        )
            except ModelCallError as exc:
                if str(exc) in {"cancelled", "cancellation_check_failed"}:
                    cancelled.set()
                with package_lock:
                    add_issue(pkg, "Unit not fully processed: " + str(exc), [unit_id], severity="error")
                    add_coverage(pkg, scope, "not_checked", str(exc))
                    for aid, page in rendered_pages:
                        add_coverage(pkg, {**scope, "asset_ref": aid, "visual_page": page}, "not_checked", str(exc))
                        if (aid, page) not in visual_success:
                            add_coverage(pkg, {"asset_ref": aid, "visual_page": page}, "not_checked", str(exc))

    workers = min(2, max(1, int(options.get("unit_workers", 2))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(extract_unit, units.items()))
    _check_observation_closure(pkg)
    add_coverage(
        pkg,
        "registered_assets",
        "candidate_extracted" if units else "not_checked",
        "No reference-answer input used; full mandatory target coverage not established",
    )
    pkg["header"]["status"] = "partial" if any(i["severity"] == "error" for i in pkg["issues"]) else "candidate"
    validate_package(pkg)
    return pkg
