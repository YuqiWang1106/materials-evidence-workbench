"""Execute, refine, apply, and locally recheck an explicit failed-window supplement run.

Graph workspace is resolved through graph_workspace before any run mutation.
"""

import json
import re
import time
from copy import deepcopy
from hashlib import sha256

from kb_ingest.core import StateConflict, atomic_json, fingerprint
from kb_ingest.core.workspace import graph_workspace
from kb_ingest.extraction.entity_check import BlockText, check_graph
from kb_ingest.extraction.entity_extract import extract_window, known_before, known_catalogue
from kb_ingest.extraction.entity_merge import link_named, linked_only, merge_windows
from kb_ingest.extraction.entity_pipeline import graph_summary, resolve_phase, task_call
from kb_ingest.extraction.entity_request_identity import EntityRequestIdentity
from kb_ingest.extraction.entity_review import task_root
from kb_ingest.ingest.resources import model_workspace
from kb_ingest.llm.errors import ModelOutcomeUnknownError, ModelRequestBindingError
from kb_ingest.llm.logical_request import BudgetCreationPermit, BudgetOpenEvidence, BudgetOpenMode, LogicalRequestFamily
from kb_ingest.storage import source_repository, task_repository
from kb_ingest.storage.entity_store import EntityStore
from kb_ingest.storage.entity_supplement_store import append_graph
from kb_ingest.storage.transactions import begin_write

from .entity_supplement import (
    VERSION,
    hydrate_known_entities,
    parent_binding,
    published_snapshot,
    read_run,
    read_runs,
    recovered_windows,
    restore_formulation_codes,
    run_path,
    save_run,
    split_failed_windows,
    view,
)


def refine(
    service,
    task_id,
    parent_run_id,
    *,
    request_id,
    expected_binding,
    unit_ids,
    operator,
    acknowledge_unknown,
    max_chars=1000,
):
    """A new authorized intent, never a replay or settlement of the old unknown request."""
    if not re.fullmatch(r"[a-f0-9]{32}", request_id) or not operator.strip():
        raise ValueError("invalid request identity")
    if acknowledge_unknown is not True:
        raise ValueError("explicit unknown-outcome acknowledgement required")
    if not 400 <= max_chars <= 1200:
        raise ValueError("refinement size must be 400..1200")
    if not unit_ids or len(set(unit_ids)) != len(unit_ids):
        raise ValueError("select unique failed units")
    task, _ = task_root(service, task_id)
    with service.store.lock("source:" + task["source_id"]):
        task, _, files, binding = published_snapshot(service, task_id)
        path = run_path(service, task_id, request_id)
        parent_path = run_path(service, task_id, parent_run_id)
        parent = read_run(parent_path, task=task)
        intent = {
            "binding": expected_binding,
            "window_ids": [],
            "operator": operator,
            "parent_run_id": parent_run_id,
            "unit_ids": unit_ids,
            "acknowledge_unknown": True,
            "max_chars": max_chars,
        }
        selected = [u for u in parent["units"] if u["unit_id"] in unit_ids]
        intent["window_ids"] = list(dict.fromkeys(u["parent_window"] for u in selected))
        if (path / "run.json").exists() or request_id in ((task.get("receipt") or {}).get("entity_supplements") or {}):
            run = read_run(path, task=task)
            if run["intent"] != intent:
                raise StateConflict("request id already used")
            return run
        if binding != expected_binding or parent["intent"]["binding"] != binding:
            raise StateConflict("source changed")
        if parent["state"] not in ("applied", "failed"):
            raise StateConflict("finish or apply parent first")
        if len(selected) != len(unit_ids) or any(u["state"] not in ("failed", "unknown") for u in selected):
            raise ValueError("only terminal failed units may be refined")
        prior = view(service, task_id)["runs"]
        if any(
            r["intent"].get("parent_run_id") == parent_run_id and set(unit_ids) & set(r["intent"]["unit_ids"])
            for r in prior
        ):
            raise StateConflict("units already refined; inspect the existing child")
        units = []
        for old in selected:
            folder = parent_path / old["unit_id"]
            if (folder / "raw.json").exists() or (folder / "candidate.json").exists():
                raise StateConflict("saved response exists; recover it instead")
            if any((folder / "stages").rglob("*.json")):
                raise StateConflict("saved stage response exists; recover it instead")
            ledger_file = folder / "model-budget.json"
            if ledger_file.exists():
                ledger = json.loads(ledger_file.read_text())
                if any(row.get("result") is not None for row in ledger.get("requests", {}).values()):
                    raise StateConflict("saved execution result exists; recover it instead")
            parts = split_failed_windows(
                [{"window_id": old["unit_id"], "block_ids": old["block_ids"]}],
                files["entity-blocks.json"],
                [old["unit_id"]],
                max_chars,
            )
            if len(parts) < 2 or any(p["characters"] >= old["characters"] for p in parts):
                raise StateConflict("cannot split smaller without breaking source blocks")
            units.extend(
                {**p, "parent_window": old["parent_window"], "parent_unit_id": old["unit_id"], "state": "pending"}
                for p in parts
            )
        if len(units) > 100:
            raise ValueError("too many refinement units")
        run = {
            "run_id": request_id,
            "intent": intent,
            "state": "queued",
            "units": units,
            "parent_binding": parent_binding(parent_path, parent, unit_ids),
            "created_at": time.time(),
            "scientific_validation": False,
            "version": VERSION,
        }
        path.mkdir(parents=True, exist_ok=True)
        atomic_json(path / "source-snapshot.json", files)
        save_run(path, run)
        return run


def execute(service, task_id, run_id, *, call_factory=None):
    task, workspace = task_root(service, task_id)[0], graph_workspace(service)
    path = run_path(service, task_id, run_id)
    with service.store.lock("source:" + task["source_id"]):
        run = read_run(path, task=service.store.get_task(task_id))
        if run["state"] not in ("queued", "running", "interrupted"):
            return run
        # The source lock excludes a live executor. Only explicit resume reaches here.
        for unit in run["units"]:
            if unit["state"] == "started":
                unit.update(state="unknown", error="interrupted_after_dispatch")
        _, _, files, binding = published_snapshot(service, task_id)
        if binding != run["intent"]["binding"]:
            raise StateConflict("source changed")
        if run["intent"].get("parent_run_id"):
            parent_path = run_path(service, task_id, run["intent"]["parent_run_id"])
            if (
                parent_binding(
                    parent_path, read_run(parent_path, task=service.store.get_task(task_id)), run["intent"]["unit_ids"]
                )
                != run["parent_binding"]
            ):
                raise StateConflict("parent execution changed; inspect before dispatch")
        run["state"] = "running"
        save_run(path, run)
        blocks = files["entity-blocks.json"]
        registry = [{**value, "block_id": key} for key, value in blocks.items()]
        source_order = list(dict.fromkeys(b for w in files["entity-windows.json"]["windows"] for b in w["block_ids"]))
        store = EntityStore(service.store)
        for unit in run["units"]:
            if unit["state"] != "pending":
                continue
            if getattr(service, "_stop", None) and service._stop.is_set():
                run["state"] = "interrupted"
                save_run(path, run)
                return run
            part = path / unit["unit_id"]
            part.mkdir(exist_ok=True)
            unit["state"] = "started"
            save_run(path, run)
            try:
                if call_factory:
                    call = call_factory(unit)
                else:
                    call = _supplement_call(service, task, run, path, unit, blocks, workspace, binding)
                chosen = [{**blocks[b], "block_id": b} for b in unit["block_ids"]]
                known = known_before(known_catalogue([files["entity-graph.json"]]), {"blocks": chosen}, source_order)
                if (part / "raw.json").exists():
                    raw = json.loads((part / "raw.json").read_text())
                else:
                    raw = extract_window({"blocks": chosen}, call, known=known, extra_calls=0)
                    atomic_json(part / "raw.json", raw)
                checked = check_graph(raw, BlockText(chosen), window_block_ids=unit["block_ids"])
                checked = hydrate_known_entities(checked, files["entity-graph.json"], raw)
                checked = restore_formulation_codes(checked, files["entity-graph.json"])
                # Keep distinct saved sample codes apart despite overlapping display aliases.
                old_codes = {
                    e.get("code") or e["name"]: e
                    for e in files["entity-graph.json"]["entities"]
                    if e["type"] == "formulation"
                }
                for entity in checked["entities"]:
                    if entity["type"] == "formulation" and entity.get("code") in old_codes:
                        entity.update(name=entity["code"], aliases=[])
                merged, unlinked = linked_only(link_named(merge_windows([checked])))
                for entity in merged["entities"]:
                    if entity["type"] == "formulation" and entity.get("code") in old_codes:
                        old = old_codes[entity["code"]]
                        entity.update(name=old["name"], aliases=list(old.get("aliases", [])))
                unresolved = []

                # Resolve in small batches to avoid another huge closing call.
                def bounded_call(name, instructions, payload, schema):
                    if name != "resolve":
                        return call(name, instructions, payload, schema)
                    decisions = []
                    for start in range(0, len(payload["items"]), 10):
                        decisions.extend(
                            call(name, instructions, {"items": payload["items"][start : start + 10]}, schema)[
                                "decisions"
                            ]
                        )
                    return {"decisions": decisions}

                resolved = resolve_phase(
                    merged, store, workspace, task["source_id"], registry, bounded_call, unresolved
                )
                if unresolved:
                    raise ValueError("entity_resolution_incomplete")
                if not resolved["attributes"]:
                    raise ValueError("no_supported_attributes")
                atomic_json(part / "candidate.json", resolved)
                unit.update(
                    state="ready",
                    candidate_sha256=fingerprint(resolved),
                    summary=graph_summary(resolved),
                    dropped=len(checked.get("dropped", [])),
                )
            except Exception as exc:
                # Preserve the ledger and any successful artifacts; never auto-resend.
                _set_unit_error(unit, exc)
            save_run(path, run)
        run["state"] = "ready" if any(u["state"] == "ready" for u in run["units"]) else "failed"
        run["recovered_windows"] = [
            w
            for w in run["intent"]["window_ids"]
            if all(u["state"] == "ready" for u in run["units"] if u["parent_window"] == w)
        ]
        if run["intent"].get("parent_run_id"):
            run["recovered_windows"] = []
        run["candidate_sha256"] = fingerprint([(u["unit_id"], u.get("candidate_sha256")) for u in run["units"]])
        save_run(path, run)
        return run


def _set_unit_error(unit, error):
    if isinstance(error, ModelOutcomeUnknownError):
        # Only independent pending units may proceed after an unknown request.
        unit.update(state="unknown", error=error.code)
    elif isinstance(error, ModelRequestBindingError):
        # A missing binding does not prove the provider received a request.
        unit.update(state="failed", error=error.code)
    else:
        unit.update(state="failed", error=type(error).__name__)


def apply(service, task_id, run_id, *, expected_candidate_sha256):
    task, workspace = task_root(service, task_id)[0], graph_workspace(service)
    path = run_path(service, task_id, run_id)
    with service.store.lock("source:" + task["source_id"]):
        run = read_run(path, task=service.store.get_task(task_id))
        if expected_candidate_sha256 != run.get("candidate_sha256"):
            raise StateConflict("supplement candidate changed")
        if run["state"] == "applied":
            return run
        if run["state"] not in ("ready", "applying"):
            raise StateConflict("supplement is not ready")
        _, original_root, _, binding = published_snapshot(service, task_id)
        if binding != run["intent"]["binding"]:
            raise StateConflict("source or review changed")
        if run["intent"].get("parent_run_id"):
            parent_path = run_path(service, task_id, run["intent"]["parent_run_id"])
            if (
                parent_binding(
                    parent_path, read_run(parent_path, task=service.store.get_task(task_id)), run["intent"]["unit_ids"]
                )
                != run["parent_binding"]
            ):
                raise StateConflict("parent execution changed; inspect before apply")
        candidates = []
        for unit in run["units"]:
            if unit["state"] != "ready":
                continue
            data = json.loads((path / unit["unit_id"] / "candidate.json").read_text())
            if fingerprint(data) != unit["candidate_sha256"]:
                raise StateConflict("candidate integrity mismatch")
            candidates.append((unit, data))
        run["publication"] = {
            "base_graph_sha256": sha256((original_root / "entity-graph-resolved.json").read_bytes()).hexdigest()
            if (original_root / "entity-graph-resolved.json").exists()
            else None,
            "base_blocks_sha256": sha256((original_root / "entity-blocks.json").read_bytes()).hexdigest(),
            "package_sha256": task["package_sha256"],
            "parts": [
                {"unit_id": unit["unit_id"], "candidate_sha256": unit["candidate_sha256"]} for unit, _ in candidates
            ],
        }
        run["state"] = "applying"
        save_run(path, run)
        run["state"] = "applied"
        run["updated_at"] = time.time()
        if run["intent"].get("parent_run_id"):
            others = [
                prior
                for _, prior in read_runs(service.store, service.store.get_task(task_id))
                if prior.get("run_id") != run_id
            ]
            windows = json.loads((original_root / "entity-windows.json").read_text())["windows"]
            recovered = recovered_windows([*others, run], windows)
            run["recovered_windows"] = [w for w in run["intent"]["window_ids"] if w in recovered]
        with begin_write(service.store.engine) as session:
            row = task_repository.get_task(session, service.store.workspace_id, task_id)
            source = source_repository.get_source(session, service.store.workspace_id, task["source_id"])
            if (
                row is None
                or row.state != "saved"
                or source is None
                or source.published_task_id != task_id
                or row.package_sha256 != task["package_sha256"]
            ):
                raise StateConflict("supplement publication no longer owns the saved source")
            for unit, data in candidates:
                unit["added"] = append_graph(service.store.engine, workspace, task["source_id"], data, session=session)
            receipt = deepcopy(row.receipt or {})
            receipt["entity_supplements"] = {**receipt.get("entity_supplements", {}), run_id: deepcopy(run)}
            row.receipt = receipt
        # No correctness decision depends on this mirror. Preserve the committed
        # timestamp so parent bindings cannot change when a mirror is repaired.
        try:
            atomic_json(path / "run.json", run)
        except OSError:
            pass
        return run


def recheck(service, task_id, run_id):
    """Explicitly retry local postprocessing of a completed extraction, never the extraction call."""
    task, _ = task_root(service, task_id)
    path = run_path(service, task_id, run_id)
    with service.store.lock("source:" + task["source_id"]):
        run = read_run(path, task=service.store.get_task(task_id))
        if run["state"] not in ("ready", "failed", "interrupted"):
            raise StateConflict("wait for the current supplement to finish")
        for unit in run["units"]:
            raw_path = path / unit["unit_id"] / "raw.json"
            ledger_path = path / unit["unit_id"] / "model-budget.json"
            safe = False
            if ledger_path.exists():
                ledger = json.loads(ledger_path.read_text())
                requests = ledger.get("requests") or {}
                safe = (
                    bool(requests)
                    and not ledger.get("pending")
                    and all(row.get("state") in ("completed", "failed") for row in requests.values())
                )
            if unit["state"] == "failed" and raw_path.exists() and safe:
                unit["state"] = "pending"
                unit.pop("error", None)
        if any(u["state"] == "pending" for u in run["units"]):
            run["state"] = "queued"
            save_run(path, run)
        return run


def _supplement_call(service, task, run, path, unit, blocks, workspace, binding):
    package = deepcopy(service.candidate(task["task_id"]))
    identity = EntityRequestIdentity.from_package(service.store.workspace_id, package)
    if package["header"]["source_id"] != task["source_id"] or {a["asset_id"] for a in package["assets"]} != set(
        task["asset_ids"]
    ):
        raise ModelRequestBindingError()
    if any(service.store.get_asset(a["asset_id"])["sha256"] != a["sha256"] for a in package["assets"]):
        raise ModelRequestBindingError()
    units = [[blocks[block_id] for block_id in item["block_ids"]] for item in run["units"]]
    keys = [fingerprint([item["unit_id"], item["parent_window"], item.get("parent_unit_id")]) for item in run["units"]]
    position = next(index for index, item in enumerate(run["units"]) if item["unit_id"] == unit["unit_id"])
    extraction = identity.plan(LogicalRequestFamily.WINDOW_EXTRACTION, units, unit_keys=keys)[position]
    resolution = identity.plan(LogicalRequestFamily.RESOLVE, units, unit_keys=keys)[position]
    part = path / unit["unit_id"]
    owner = part / "model-budget.json"
    mode = BudgetOpenMode.OPEN_EXISTING if owner.exists() else BudgetOpenMode.INIT_NEW
    evidence = BudgetOpenEvidence(mode, owner, service.store.workspace_id)
    options = dict(deepcopy(service.document_options), _model_workspace=model_workspace(service.store))
    options.update(
        work_dir=str(part),
        workspace_id=workspace,
        cancel_check=service._stop.is_set,
        _entity_request_identity=identity,
        _model_budget_evidence=evidence,
    )
    if mode == BudgetOpenMode.INIT_NEW:
        if (part / "raw.json").exists() or (part / "candidate.json").exists():
            raise ModelRequestBindingError()
        intent = deepcopy(run["intent"])

        def owns_started():
            stored = read_run(path, task=service.store.get_task(task["task_id"]))
            current = next((row for row in stored["units"] if row["unit_id"] == unit["unit_id"]), None)
            return stored["intent"] == intent and current == unit and current["state"] == "started"

        options["_model_budget_permit"] = BudgetCreationPermit(evidence, owns_started)
    # The existing cache-only revision remains separate from the original source binding.
    package["header"]["source_revision_id"] = fingerprint([binding, run["run_id"], unit["unit_id"]])
    original = task_call(options, package, f"{run['run_id']}:{unit['unit_id']}")

    def call(name, instructions, payload, schema):
        if name == "resolve":
            logical = resolution
        elif name in ("entities-attributes-related", "attributes-related", "attributes-gaps"):
            logical = extraction
        else:
            raise ModelRequestBindingError()
        return original(name, instructions, payload, schema, logical_binding=logical)

    return call
