"""Turn compiled table objects and facts into the same graph prose windows produce."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from functools import partial as partial_call
from pathlib import Path

from kb_ingest.core import atomic_json
from kb_ingest.core.conservation import require_conserved
from kb_ingest.core.contract_entity import normalize_name
from kb_ingest.core.interrupted import save_interrupted
from kb_ingest.llm import ModelCallError
from kb_ingest.llm.errors import ModelOutcomeUnknownError, ModelRequestBindingError
from kb_ingest.llm.logical_request import LogicalRequestFamily
from kb_ingest.observation import hooks as _observe

from .entity_tables import TABLE_REQUESTS, RequestGate, guided, map_table, mapped_tables, table_context

logger = logging.getLogger(__name__)

SUBJECT_KINDS = {"formulation", "patent_claim", "sample"}
ENTITY_KINDS = {"material": "material", "method_definition": "method", "equipment": "instrument"}
SECTION_BY_FIELD = (
    ("composition.", "原料"),
    ("process.", "制备过程"),
    ("observation.", "性能"),
    ("condition.", "测试方法"),
    ("method.", "测试方法"),
    ("claim.", "结论"),
)

LABEL_KEYS = (
    "metric_label",
    "metric_id",
    "parameter_label",
    "parameter_id",
    "component_label",
    "additive_system_label",
    "standard_role",
)


def _clean_name(label):
    """实施例1: -> 实施例1 and 实施 例1 -> 实施例1: a heading's colon and an OCR space are not part of the name."""
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff\d])", "", " ".join(str(label).split()))
    return text.rstrip(":：;；,，.。、 ") or str(label)


def _unit_label(fact):
    """质量份（份） -> 份: the unit a Chinese header states, as written there."""
    label = next(
        (str(item.get("value")) for item in fact.get("qualifiers") or [] if item.get("key") == "unit_label"), ""
    )
    inner = re.findall(r"[（(]([^（）()]+)[）)]", label)
    return (inner[-1] if inner else label).strip()


def _join(value, unit):
    value, unit = str(value or "").strip(), str(unit or "").strip().strip("()（）").strip()
    if not unit:
        return value
    return value + unit if unit in ("%", "％") or unit.startswith(("%", "％")) else value + " " + unit


def _label(fact, fallback):
    qualifiers = {item.get("key"): item.get("value") for item in fact.get("qualifiers") or []}
    for key in LABEL_KEYS:
        if qualifiers.get(key):
            return str(qualifiers[key])
    return fallback


def _quote(evidence, by_id):
    """The cited cells in table order (headers first, then the value), as one readable line."""
    cells = []
    for item in evidence:
        block = by_id.get(item.get("block_id"))
        cell = ((block or {}).get("anchor") or {}).get("table_cell")
        if cell and item.get("quote") and item not in cells:
            cells.append((cell["row"], cell["column"], item))
    cells.sort(key=lambda row: row[:2])
    parts = []
    for _, _, item in cells:
        if item["quote"] not in parts:
            parts.append(item["quote"])
    return " | ".join(parts[:8])


UNATTACHED = "no formulation or value to attach to"
UNPLACED = "its entity has no place in the source"


def accepted_graph(accepted, blocks, prefix):
    """Accepted table objects and facts as entities, attributes and related links (local ids carry `prefix`).

    Every fact the table program read becomes attribute lines or is listed under `skipped` with its text and reason.
    """
    by_id = {block["block_id"]: block for block in blocks}
    objects = {item["key"]: item for item in accepted.get("objects", [])}
    relations = accepted.get("relations", [])
    # A sample and its recipe are one formulation in the entity graph.
    parent = {key: key for key, item in objects.items() if item["kind"] in SUBJECT_KINDS}

    def root(key):
        while parent[key] != key:
            key = parent[key]
        return key

    for link in relations:
        if (
            link["predicate"] in ("has_formulation", "recipe_for")
            and link["from_key"] in parent
            and link["to_key"] in parent
        ):
            parent[root(link["to_key"])] = root(link["from_key"])
    entity_of, entities = {}, []

    def add(key, kind):
        item = objects[key]
        local_id = prefix + ":" + str(len(entities) + 1)
        mentions = [
            {"block_id": e["block_id"], "quote": e["quote"]}
            for e in item.get("identity_evidence") or []
            if e.get("block_id") in by_id and e.get("quote")
        ]
        entities.append(
            {
                "local_id": local_id,
                "type": kind,
                "name": _clean_name(item.get("label") or item.get("source_identifier") or key),
                "aliases": [],
                "mentions": mentions[:3],
            }
        )
        return local_id

    for key in parent:
        top = root(key)
        if top not in entity_of:
            entity_of[top] = add(top, "formulation")
        entity_of[key] = entity_of[top]
    for key, item in objects.items():
        if item["kind"] in ENTITY_KINDS:
            entity_of[key] = add(key, ENTITY_KINDS[item["kind"]])
    linked = {}
    for link in relations:
        linked.setdefault(link["from_key"], []).append((link["predicate"], link["to_key"]))
        linked.setdefault(link["to_key"], []).append(("~" + link["predicate"], link["from_key"]))

    def neighbours(key, predicates):
        return [other for predicate, other in linked.get(key, []) if predicate in predicates]

    def subjects(key):
        """Formulation entities a non-entity object (observation, test, process, claim) is about."""
        item = objects.get(key) or {}
        if key in parent:
            return [entity_of[key]]
        if item.get("kind") == "component":
            return [entity_of[item["owner_key"]]] if item.get("owner_key") in entity_of else []
        direct = neighbours(
            key,
            {
                "observes_subject",
                "tests_subject",
                "characterizes",
                "concerns",
                "interprets",
                "about_sample",
                "~process_applicability",
                "~uses_filler_treatment",
            },
        )
        if item.get("kind") == "observation" and not any(other in parent for other in direct):
            for test in neighbours(key, {"~produces_observation"}):
                direct += neighbours(test, {"tests_subject", "characterizes"})
        return sorted({entity_of[other] for other in direct if other in parent})

    def methods(key):
        tests = [key] + neighbours(key, {"~produces_observation"})
        return sorted(
            {entity_of[method] for test in tests for method in neighbours(test, {"uses_method"}) if method in entity_of}
        )

    names = {entity["local_id"]: entity["name"] for entity in entities}
    properties, attributes, related, skipped = {}, [], [], []
    facts = accepted.get("facts", [])
    named = attached = 0
    for fact in facts:
        key, field = fact["object_key"], fact["field_id"]
        item = objects.get(key) or {}
        value = _join(fact.get("raw_value"), fact.get("raw_unit") or _unit_label(fact))
        quote = _quote(fact.get("evidence") or [], by_id) or value
        cell = fact.get("value_cell") or next(
            (e["block_id"] for e in fact.get("evidence") or [] if e.get("block_id") in by_id), None
        )
        block_ids = list(dict.fromkeys(e["block_id"] for e in fact.get("evidence") or [] if e.get("block_id") in by_id))
        section = next((name for start, name in SECTION_BY_FIELD if field.startswith(start)), "说明")
        refs = []
        if key in entity_of and key not in parent:
            if normalize_name(fact.get("raw_value") or "") == normalize_name(item.get("label") or ""):
                named += 1
                continue  # The entity's own name, already its name.
            owners, section = [entity_of[key]], "说明"
            text = _label(fact, "") + " " + value if _label(fact, "") else value
        elif item.get("kind") == "component":
            owners = subjects(key)
            material = item.get("material_key") or next(iter(neighbours(key, {"refers_to_material"})), None)
            name = objects[material]["label"] if material in objects else item.get("label") or _label(fact, "")
            text = (name + " " + value).strip()
            refs = [entity_of[material]] if material in entity_of else []
        else:
            owners = subjects(key)
            label = _label(fact, item.get("label") or "")
            if normalize_name(label) in {normalize_name(names[owner]) for owner in owners}:
                label = ""  # An observation labelled by its sample says nothing about the metric.
            text = (label + " " + value).strip() if label and label not in value else value
            if field == "observation.value" and label:
                if label not in properties:
                    properties[label] = prefix + ":p" + str(len(properties) + 1)
                    metric_cell = next((e for e in fact.get("evidence") or [] if e.get("quote") == label), None)
                    entities.append(
                        {
                            "local_id": properties[label],
                            "type": "property",
                            "name": label,
                            "aliases": [],
                            "mentions": [metric_cell] if metric_cell else [],
                        }
                    )
                refs = [properties[label]]
            refs += methods(key)
        if not owners or cell is None or not value:
            skipped.append(
                {
                    "fact": fact["key"],
                    "field_id": field,
                    "reason": UNATTACHED,
                    "section": section,
                    "text": text,
                    "block_id": cell,
                    "quote": quote,
                }
            )
            continue
        attached += 1
        for owner in owners:
            attributes.append(
                {
                    "local_id": owner,
                    "section": section,
                    "text": text,
                    "block_id": cell,
                    "quote": quote,
                    "block_ids": block_ids,
                    "refs": [ref for ref in refs if ref != owner],
                    "numbers": "in_quote",
                    "from_table": True,
                }
            )
            for ref in refs:
                if ref != owner:
                    related.append(
                        {
                            "a": owner,
                            "b": ref,
                            "block_id": cell,
                            "quote": quote,
                            "block_ids": block_ids,
                            "derived_from": "attribute",
                        }
                    )
    # A property named only in a header with no exact quote is still placed at its value cell.
    for entity in entities:
        if not entity["mentions"]:
            source = next((row for row in attributes if entity["local_id"] in row["refs"]), None)
            if source:
                entity["mentions"] = [{"block_id": source["block_id"], "quote": source["quote"]}]
    require_conserved("table fact", entered=len(facts), kept=attached, set_aside=named + len(skipped))
    kept = {entity["local_id"] for entity in entities if entity["mentions"]}
    # An entity with no place in the source is not written, and neither are its lines: both are listed.
    skipped.extend(
        {"entity": entity["name"], "type": entity["type"], "reason": UNPLACED}
        for entity in entities
        if entity["local_id"] not in kept
    )
    unplaced = [row for row in attributes if row["local_id"] not in kept]
    skipped.extend(
        {key: row[key] for key in ("section", "text", "block_id", "quote")} | {"reason": UNPLACED} for row in unplaced
    )
    entities = [entity for entity in entities if entity["local_id"] in kept]
    attributes = [row for row in attributes if row["local_id"] in kept]
    for row in attributes:
        row["refs"] = [ref for ref in row["refs"] if ref in kept]
    related = [row for row in related if row["a"] in kept and row["b"] in kept]
    return {"entities": entities, "attributes": attributes, "related": related, "skipped": skipped}


def _table_outcome(outcome: dict) -> tuple[str, dict[str, object]]:
    """How one table ended, for the attempt's timeline: its status, then a code and counts. Never the reason text."""
    log = outcome.get("log")
    if log is None:
        return "not_processed", {"error_code": "request_allowance"}
    status = str(log["outcome"])
    codes = {"empty": "table_no_attributes", "partial": "table_partial", "failed": "mapping_invalid"}
    counts = {key: log[key] for key in ("requests", "attributes", "unread_cells") if key in log}
    return status, {"error_code": codes.get(status), **counts}


def _run_tables(tracked, tables, finished, work):
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(tables))) as pool:
            futures = [pool.submit(tracked, table) for table in tables]
            # Observe every already-dispatched result before propagating an earlier unrelated error.
            wait(futures)
            for future in futures:
                if not future.cancelled():
                    error = future.exception()
                    if isinstance(error, (ModelOutcomeUnknownError, ModelRequestBindingError)):
                        raise error
            return [future.result() for future in futures]
    except (ModelOutcomeUnknownError, ModelRequestBindingError) as error:
        # The executor has waited for already-scheduled independent tables, which may also have useful results.
        if work is not None:
            completed = [finished[key] for key in sorted(finished)]
            payload = {
                "status": "interrupted",
                "stage": "table_mapping",
                "reason": error.code,
                "graphs": [outcome["graph"] for outcome in completed if outcome.get("graph")],
                "tables": [outcome["log"] for outcome in completed if outcome.get("log")],
            }
            try:
                save_interrupted(Path(work), payload)
            except (OSError, TypeError, ValueError) as storage_error:
                logger.warning("Interrupted table preservation failed: %s", type(storage_error).__name__)
        raise


def table_phase(
    blocks, windows, assets, source_type, call, *, budget, work=None, only=None, progress=None, request_identity=None
):
    """Map every table at the same time while `budget` requests remain (the model client decides how many calls
    actually run together). Returns (graphs, covered table ids, unresolved), in table order. Only a table
    compiled in full leaves the prose windows. A table whose mapping fails, or whose plan leaves cells unread,
    is also read as prose, so nothing in it is lost; prefer_table then keeps one line where both use the same words.
    """
    gate = RequestGate(call, budget)
    save = (
        (lambda name, value: atomic_json(work / "table-mappings" / (name + ".json"), value))
        if work
        else (lambda *_: None)
    )
    all_tables = mapped_tables(blocks)
    plan = (
        request_identity.plan(LogicalRequestFamily.TABLE_MAPPING, [[table] for table in all_tables])
        if request_identity
        else ()
    )
    bindings = {table["block_id"]: binding for table, binding in zip(all_tables, plan, strict=bool(request_identity))}
    tables = [(number, table) for number, table in enumerate(all_tables, 1) if not only or table["block_id"] in only]

    def one(number, table):
        table_id = table["block_id"]
        left = gate.left()
        if left is not None and left < TABLE_REQUESTS:
            return {"unresolved": {"table_id": table_id, "reason": "request ceiling reached; table read as prose"}}
        selected = table_context(blocks, table_id, windows)
        counted = []
        try:
            compiled = map_table(
                table_id,
                selected,
                source_type=source_type,
                scope="表格 " + str(number),
                call=guided(
                    partial_call(call, logical_binding=bindings[table_id]) if request_identity else call, gate, counted
                ),
                save=save,
                work=work,
            )
        except (ModelCallError, ValueError) as exc:
            return {
                "unresolved": {
                    "table_id": table_id,
                    "reason": "table mapping unavailable; table read as prose",
                    "detail": type(exc).__name__ + ": " + str(exc)[:200],
                },
                "log": {"table_id": table_id, "outcome": "failed", "requests": len(counted)},
            }
        graph = accepted_graph(compiled["candidate"], selected, "t" + str(number))
        if not graph["attributes"]:
            return {
                "unresolved": {"table_id": table_id, "reason": "table mapping produced no values; table read as prose"},
                "log": {"table_id": table_id, "outcome": "empty", "requests": len(counted)},
            }
        unread = compiled["coverage"].get("unresolved_cell_ids") or []
        partial = bool(unread or compiled["diagnostics"])
        return {
            "graph": {"window_id": "table:" + table_id, **graph},
            "covered": None if partial else table_id,
            "unresolved": {
                "table_id": table_id,
                "reason": "some table cells not read by the mapping; table also read as prose",
                "unread_cells": len(unread),
                "diagnostics": len(compiled["diagnostics"]),
            }
            if partial
            else None,
            "log": {
                "table_id": table_id,
                "outcome": "partial" if partial else "mapped",
                "attributes": len(graph["attributes"]),
                "unread_cells": len(unread),
                "requests": len(counted),
            },
        }

    import threading

    progress_lock = threading.Lock()
    counts = {"completed": 0, "failed": 0}
    finished = {}
    if progress:
        progress(**counts, total=len(tables), unit="张表格")

    def tracked(item):
        step = _observe.start(work, "table", item[1]["block_id"])
        outcome = one(*item)
        with progress_lock:
            finished[item[0]] = outcome
        status, fields = _table_outcome(outcome)
        step.ended(status, **fields)
        with progress_lock:
            counts["completed"] += 1
            counts["failed"] += int(bool(outcome.get("unresolved")))
            if progress:
                progress(**counts, total=len(tables), unit="张表格")
        return outcome

    outcomes = _run_tables(tracked, tables, finished, work)
    graphs = [outcome["graph"] for outcome in outcomes if outcome.get("graph")]
    covered = {outcome["covered"] for outcome in outcomes if outcome.get("covered")}
    unresolved = [outcome["unresolved"] for outcome in outcomes if outcome.get("unresolved")]
    log = [outcome["log"] for outcome in outcomes if outcome.get("log")]
    if work is not None:
        atomic_json(work / "entity-tables.json", {"tables": log, "graphs": graphs})
    return graphs, covered, unresolved
