"""Finite source-bound PDF table templates; no model execution or approval.

Coordinates are the reader's zero-based logical table grid, NOT invented PDF cell rectangles. This compiler only expands
explicit source selections. Independent rule/cell review and the ordinary document quality gates remain mandatory.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from kb_ingest import contract, core
from kb_ingest.document_source_values import project_source_cell
from kb_ingest.source_token_boundaries import typed_span_supported
from kb_ingest.typed_values import parse_typed_value

from .document_quality import resolve_cell_values
from .document_table_cell_cut import CELL_VALUE_CUT, CELL_VALUE_CUT_REASON, cut_fact_indexes
from .document_table_mapping_schema import TABLE_MAPPING_SCHEMA

VERSION = "document-table-mapping-1"
MAX_RECORDS = 4096
MAX_EXPANSION = 100_000
MAX_GRID_CELLS = 100_000
_CONTEXT_FIELDS = {"method.name", "method.standard", "method.instrument", "condition.parameter", "material.identity"}


class MappingError(ValueError):
    """Unsafe/unrepresentable mapping. No partially accepted configuration."""


class _MissingBinding(ValueError):
    pass


def _schema_fit(value, schema):
    """Pick useful error detail from the matching finite schema alternative."""
    if "anyOf" in schema:
        return max(_schema_fit(value, s) for s in schema["anyOf"])
    kind = schema.get("type")
    correct = (
        kind == "object"
        and type(value) is dict
        or kind == "array"
        and type(value) is list
        or kind == "string"
        and type(value) is str
        or kind == "null"
        and value is None
        or kind == "integer"
        and type(value) is int
        or kind == "boolean"
        and type(value) is bool
    )
    score = 10 if correct else -10
    if kind == "object" and type(value) is dict:
        for key, prop in schema["properties"].items():
            if key in value and "enum" in prop:
                score += 100 if value[key] in prop["enum"] else -100
    return score


def _item_path(path, index, value):
    result = f"{path}[{index}]"
    return result + f"(id={value['id']!r})" if isinstance(value, dict) and isinstance(value.get("id"), str) else result


def _validate(value, schema, path="mapping"):
    if "anyOf" in schema:
        errors = []
        for option in sorted(schema["anyOf"], key=lambda s: _schema_fit(value, s), reverse=True):
            try:
                _validate(value, option, path)
                return
            except MappingError as exc:
                errors.append(exc)
        raise errors[0]
    kind = schema["type"]
    matches = {
        "null": value is None,
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "boolean": type(value) is bool,
    }
    if not matches[kind]:
        raise MappingError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise MappingError(f"{path}: unknown option")
    if kind == "object":
        missing = set(schema["required"]) - set(value)
        extra = set(value) - set(schema["properties"])
        if missing:
            raise MappingError(f"{path}.{sorted(missing)[0]}: required key missing")
        if extra:
            raise MappingError(f"{path}.{sorted(extra)[0]}: unsupported key")
        for k, v in value.items():
            _validate(v, schema["properties"][k], f"{path}.{k}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100_000):
            raise MappingError(f"{path}: collection bounds exceeded")
        for i, v in enumerate(value):
            _validate(v, schema["items"], _item_path(path, i, v))
    elif kind == "integer":
        if not schema.get("minimum", 0) <= value <= schema.get("maximum", 100_000):
            raise MappingError(f"{path}: coordinate out of bounds")
    elif kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4096):
        raise MappingError(f"{path}: string bounds exceeded")


def _selector_locations(value, path="mapping"):
    if isinstance(value, dict):
        if value.get("kind") in ("axis", "cell", "text", "quote"):
            yield value, path
        else:
            for k, v in value.items():
                yield from _selector_locations(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _selector_locations(v, _item_path(path, i, v))


def _selectors(value):
    for selector, _ in _selector_locations(value):
        yield selector


def _dynamic(value):
    return any(s.get("kind") == "axis" and "current" in (s.get("row"), s.get("column")) for s in _selectors(value))


def _evidence(bindings):
    evidence = []
    for b in bindings:
        e = {"block_id": b.block["block_id"], "quote": b.value}
        if e not in evidence:
            evidence.append(e)
    return evidence


@dataclass(frozen=True)
class _Binding:
    block: dict
    value: str


def _complete_typed_binding(binding, selector, kind):
    # Validate the selected occurrence in the unchanged source, not the sliced
    # quote (nor another identical but complete occurrence elsewhere).
    source = binding.block["text"]
    start, end = (
        _quote_span(source, selector["quote"])
        if selector["kind"] == "quote"
        else selector["span"]
        if selector["span"] is not None
        else (0, len(source))
    )
    return typed_span_supported(source, start, end, kind=kind)


def _quote_span(source, quote):
    start = source.find(quote)
    if start < 0:
        raise MappingError("quote is not an exact literal in the original block")
    # A second match can overlap the first (e.g. aa in aaa).
    if source.find(quote, start + 1) >= 0:
        raise MappingError("quote occurs more than once in the original block")
    return start, start + len(quote)


class _Table:
    def __init__(self, blocks, table_id):
        self.registry = {b["block_id"]: b for b in blocks}
        if len(self.registry) != len(blocks):
            raise MappingError("duplicate source block IDs")
        if self.registry.get(table_id, {}).get("kind") != "table":
            raise MappingError("unknown table")
        self.table_id = table_id
        self.cells = {}
        self.grid = {}
        self.max_row = -1
        self.max_column = -1
        for b in blocks:
            if b.get("kind") != "table_cell" or b.get("table_region") != table_id:
                continue
            a = b.get("anchor", {})
            p = a.get("page_numbers")
            if a.get("kind") != "pdf" or not p or any(type(x) is not int or x < 1 for x in p):
                raise MappingError("source cell lacks real PDF page anchor")
            cell = a.get("table_cell", {})
            row, column = cell.get("row"), cell.get("column")
            rs, cs = cell.get("row_span", 1), cell.get("col_span", 1)
            if any(type(x) is not int for x in (row, column, rs, cs)) or min(row, column) < 0 or min(rs, cs) < 1:
                raise MappingError("invalid source grid coordinates")
            if row + rs > 100_001 or column + cs > 100_001 or len(self.grid) + rs * cs > MAX_GRID_CELLS:
                raise MappingError("source table exceeds bounded grid")
            if type(b.get("text")) is not str:
                raise MappingError("source cell text must be a transcription")
            for r in range(row, row + rs):
                for c in range(column, column + cs):
                    if (r, c) in self.grid:
                        raise MappingError("overlapping source cell spans")
                    self.grid[r, c] = b
            self.cells[b["block_id"]] = b
            self.max_row = max(self.max_row, row + rs - 1)
            self.max_column = max(self.max_column, column + cs - 1)
        if not self.cells:
            raise MappingError("table has no registered logical cells")

    def resolve(self, s, current, axis):
        if s["kind"] == "text":
            b = self.registry.get(s["block_id"])
            if not b or b.get("kind") in ("table", "table_cell", "visual_page"):
                raise MappingError("text selector needs a registered textual context block")
        elif s["kind"] == "quote":
            b = self.registry.get(s["block_id"])
            if not b or b.get("kind") in ("table", "visual_page"):
                raise MappingError("quote selector needs a registered text block or same-table cell")
            if b.get("kind") == "table_cell" and b.get("table_region") != self.table_id:
                raise MappingError("cross-table quote selector is unsupported")
        elif s["kind"] == "cell":
            b = self.registry.get(s["cell_id"])
            if not b or b.get("kind") != "table_cell":
                raise MappingError("unknown source cell")
            if b.get("table_region") != self.table_id:
                raise MappingError("cross-table selector is unsupported")
        else:
            row, column = s["row"], s["column"]
            if row == "current":
                if axis != "row" or current is None:
                    raise MappingError("row current is incompatible with axis/cardinality")
                row = current
            if column == "current":
                if axis != "column" or current is None:
                    raise MappingError("column current is incompatible with axis/cardinality")
                column = current
            if row > self.max_row or column > self.max_column:
                raise MappingError("selector beyond source grid")
            b = self.grid.get((row, column))
            if b is None:
                raise _MissingBinding(f"no registered cell at row {row}, column {column}")
            pos = b["anchor"]["table_cell"]
            if (row, column) != (pos["row"], pos["column"]) and not s["merged"]:
                raise _MissingBinding("merged cell continuation needs explicit merged selector")
        raw = b.get("text")
        if type(raw) is not str:
            raise MappingError("source context has no literal text")
        span = _quote_span(raw, s["quote"]) if s["kind"] == "quote" else s["span"]
        if span is not None:
            start, end = span
            if start >= end or end > len(raw):
                raise MappingError("text span outside original source")
            raw = raw[start:end]
        if not raw.strip():
            raise _MissingBinding(f"empty selected content in {b['block_id']}")
        return _Binding(b, raw)


def table_mapping_context(blocks, table_id):
    """Finite model-facing grid. All positions/anchors are reader supplied."""
    table = _Table(blocks, table_id)
    return {
        "version": VERSION,
        "table_id": table_id,
        "coordinate_base": 0,
        "row_count": table.max_row + 1,
        "column_count": table.max_column + 1,
        "cells": [
            {
                "block_id": b["block_id"],
                "text": b["text"],
                "anchor": deepcopy(b["anchor"]),
                "source": b.get("source"),
                "asset_ref": b.get("asset_ref"),
                **{k: b["anchor"]["table_cell"].get(k, 1) for k in ("row", "column", "row_span", "col_span")},
            }
            for b in sorted(
                table.cells.values(),
                key=lambda b: (b["anchor"]["table_cell"]["row"], b["anchor"]["table_cell"]["column"]),
            )
        ],
        "coordinate_meaning": "logical table grid; PDF anchor is original table region, not a cell bbox",
    }


def compile_table_mapping(mapping, blocks, existing_objects=None):
    """Return deterministic candidates plus obligations, never verified facts.

    Invalid configurations raise MappingError. Source missingness/identity ambiguity
    yields bounded diagnostics and withholds dependent records; independent records
    can survive. No same-name merge or cross-table/inherited value inference occurs.
    """
    _validate(mapping, TABLE_MAPPING_SCHEMA)
    mapping = deepcopy(mapping)
    try:
        table = _Table(blocks, mapping["table_id"])
    except MappingError as exc:
        raise MappingError(f"mapping.table_id: {exc}") from None
    selection = mapping["selection"]
    axis = selection["axis"]
    for key, bound in (("rows", table.max_row), ("columns", table.max_column)):
        a, b = selection[key]
        if a > b or b > bound:
            raise MappingError(f"mapping.selection.{key}: selection outside source grid")
    start, end = selection["rows" if axis == "row" else "columns"]
    records = list(range(start, end + 1))
    if (
        len(records) > MAX_RECORDS
        or len(records) * sum(len(mapping[k]) for k in ("objects", "facts", "relations")) > MAX_EXPANSION
    ):
        raise MappingError("mapping.selection: bounded template expansion exceeded")
    templates = {o["id"]: o for o in mapping["objects"]}
    for section in ("objects", "facts", "relations"):
        seen = set()
        for index, item in enumerate(mapping[section]):
            if item["id"] in seen:
                raise MappingError(f"{_item_path('mapping.' + section, index, item)}.id: duplicate template ID")
            seen.add(item["id"])
    existing = {o["key"]: deepcopy(o) for o in existing_objects or []}
    if len(existing) != len(existing_objects or []):
        raise MappingError("existing_objects: duplicate existing object key")
    used_existing = {}
    for index, o in enumerate(mapping["objects"]):
        path = _item_path("mapping.objects", index, o)
        if o["cardinality"] == "existing":
            prior = existing.get(o["existing_key"])
            if (
                not prior
                or prior.get("kind") != o["kind"]
                or o["identity"]
                or o["label"]
                or o["evidence"]
                or o["owner"]
                or o["material"]
            ):
                raise MappingError(
                    f"{path}.existing_key: existing reference must use supplied immutable identity of the declared kind"
                )
            for supports in (prior.get("evidence"), prior.get("identity_evidence") or prior.get("evidence")):
                if not supports or any(
                    e.get("block_id") not in table.registry
                    or not e.get("quote")
                    or e["quote"] not in table.registry[e["block_id"]].get("text", "")
                    for e in supports
                ):
                    raise MappingError(f"{path}.existing_key: existing identity lacks current literal evidence")
            used_existing[prior["key"]] = prior
            continue
        if o["existing_key"] is not None or not o["identity"] or not o["label"] or not o["evidence"]:
            raise MappingError(f"{path}: new object needs source identity, label and rule evidence")
        if o["cardinality"] == "shared" and _dynamic(o):
            raise MappingError(f"{path}.cardinality: shared object cannot depend on current record")
        if o["kind"] == "component":
            owner = templates.get(o["owner"], {})
            material = templates.get(o["material"], {})
            if owner.get("kind") not in ("formulation", "patent_claim"):
                raise MappingError(f"{path}.owner: component requires an explicit formulation or patent claim template")
            if material.get("kind") != "material":
                raise MappingError(f"{path}.material: component requires an explicit material template")
        elif o["owner"] is not None or o["material"] is not None:
            raise MappingError(f"{path}.owner: only component objects carry owner/material links")
        for field in ("owner", "material"):
            ref = o[field]
            if ref and ref not in templates:
                raise MappingError(f"{path}.{field}: unknown object dependency")
            if ref and o["cardinality"] == "shared" and templates[ref]["cardinality"] == "per_record":
                raise MappingError(f"{path}.{field}: shared object cannot depend on per-record object")
    for index, f in enumerate(mapping["facts"]):
        path = _item_path("mapping.facts", index, f)
        owner = templates.get(f["target"])
        rule = contract.FIELDS[f["field_id"]]
        if not owner:
            raise MappingError(f"{path}.target: unknown object template")
        if owner["kind"] not in rule["owner_kinds"]:
            raise MappingError(f"{path}.field_id: field does not allow target kind {owner['kind']!r}")
        if f["value_kind"] not in rule["value_kinds"]:
            raise MappingError(f"{path}.value_kind: value kind violates field catalogue")
        if len({q["key"] for q in f["qualifiers"]}) != len(f["qualifiers"]):
            raise MappingError(f"{path}.qualifiers: duplicate qualifier key")
        if (
            owner["kind"] == "patent_claim" or templates.get(owner.get("owner"), {}).get("kind") == "patent_claim"
        ) and f["statement_kind"] == "source_reported_measurement":
            raise MappingError(f"{path}.statement_kind: patent scope cannot be a reported measurement")
    for index, r in enumerate(mapping["relations"]):
        path = _item_path("mapping.relations", index, r)
        rule = contract.RELATIONS[r["predicate"]]
        for endpoint in ("from", "to"):
            if templates.get(r[endpoint], {}).get("kind") not in rule["allowed_" + endpoint + "_kinds"]:
                raise MappingError(f"{path}.{endpoint}: relation endpoint violates predicate catalogue")
    # Reject impossible selectors even if another missing binding would hide them.
    for s, path in _selector_locations(mapping):
        for record in (start, end):
            try:
                table.resolve(s, record, axis)
            except _MissingBinding:
                pass
            except MappingError as exc:
                raise MappingError(f"{path}: {exc}") from None
    cut_facts = cut_fact_indexes(
        [f["value"] for f in mapping["facts"]], lambda s: table.resolve(s, None, axis), _MissingBinding
    )
    candidate = {"objects": [], "facts": [], "relations": [], "issues": []}
    targets = {}
    rules = {}
    diagnostics = []
    instances = {}
    bindings_for = {}
    identities = {}
    mapping_hash = core.fingerprint(mapping)

    def diagnostic(code, rule, record, reason, bindings=()):
        d = {
            "code": code,
            "rule_id": rule,
            "record_coordinate": record,
            "reason": str(reason),
            "source_block_ids": sorted({b.block["block_id"] for b in bindings}),
        }
        if d not in diagnostics:
            diagnostics.append(d)
        issue = {
            "description": f"{code}: {reason}",
            "object_keys": [],
            "severity": "warning",
            "evidence": _evidence(bindings),
        }
        if issue not in candidate["issues"]:
            candidate["issues"].append(issue)

    def resolve_many(selectors, record):
        return [table.resolve(s, record, axis) for s in selectors]

    def make_rule(kind, template):
        rid = kind + ":" + template["id"]
        rules[rid] = {
            "rule_id": rid,
            "kind": kind,
            "definition": deepcopy(template),
            "source_block_ids": [],
            "selector_source_block_ids": [],
            "evidence": [],
        }
        return rid

    def support_rule(rid, support):
        rule = rules[rid]
        rule["source_block_ids"] = sorted(set(rule["source_block_ids"]) | {b.block["block_id"] for b in support})
        rule["selector_source_block_ids"] = sorted(
            set(rule["selector_source_block_ids"]) | set(rule["source_block_ids"])
        )
        for e in _evidence(support):
            if e not in rule["evidence"]:
                rule["evidence"].append(e)

    def proof(key, rid, bindings, record, dependencies=(), existing_refs=(), value_cell=None):
        ids = sorted({b.block["block_id"] for b in bindings})
        rules[rid]["selector_source_block_ids"] = sorted(set(rules[rid]["selector_source_block_ids"]) | set(ids))
        targets[key] = {
            "rule_ids": [rid],
            "source_block_ids": ids,
            "source_cell_ids": [i for i in ids if table.registry[i].get("kind") == "table_cell"],
            "context_refs": [i for i in ids if table.registry[i].get("kind") != "table_cell"],
            "dependencies": sorted(set(dependencies)),
            "identity_refs": sorted(set(dependencies)),
            "condition_refs": [],
            "existing_object_refs": sorted(set(existing_refs)),
            "record_coordinate": record,
            "value_cell": value_cell,
        }

    def obj_key(alias, record):
        t = templates[alias]
        coord = record if t["cardinality"] == "per_record" else None
        return instances.get((alias, coord))

    def obj_dependencies(keys):
        return (["o:" + k for k in keys if k and k not in used_existing], [k for k in keys if k in used_existing])

    # Assemble identity candidates before facts, then invalidate repeated labels
    # within this explicit source scope. Repeated names are never auto-merged.
    pending = []
    for o in templates.values():
        rid = make_rule("object", o)
        if o["cardinality"] == "existing":
            instances[o["id"], None] = o["existing_key"]
            b = [
                _Binding(table.registry[e["block_id"]], e["quote"])
                for e in used_existing[o["existing_key"]]["evidence"]
            ]
            support_rule(rid, b)
            continue
        for record in records if o["cardinality"] == "per_record" else [None]:
            try:
                identity = resolve_many(o["identity"], record)
                label = table.resolve(o["label"], record, axis)
                support = resolve_many(o["evidence"], record)
            except _MissingBinding as exc:
                diagnostic("missing_binding", rid, record, exc)
                continue
            signature = core.fingerprint([o["kind"], [x.value for x in identity]])
            identities.setdefault(signature, []).append((o["id"], record))
            key = "tm:" + core.fingerprint([mapping_hash, o["id"], record])[:24]
            instances[o["id"], record] = key
            pending.append((o, record, key, identity, label, support, rid))
    duplicates = {pair for group in identities.values() if len(group) > 1 for pair in group}
    for alias, record in sorted(duplicates, key=str):
        instances.pop((alias, record), None)
        diagnostic(
            "duplicate_identity",
            "object:" + alias,
            record,
            "repeated source identity needs explicit scope/repeat mapping",
        )
    for o, record, key, identity, label, support, rid in pending:
        if (o["id"], record) in duplicates:
            continue
        refs = [obj_key(ref, record) for ref in (o["owner"], o["material"]) if ref]
        if any(r is None for r in refs):
            instances.pop((o["id"], record), None)
            diagnostic("unresolved_object_dependency", rid, record, "owner/material identity is unresolved")
            continue
        bound = identity + [label] + support
        bindings_for[key] = bound
        candidate["objects"].append(
            {
                "key": key,
                "kind": o["kind"],
                "label": label.value,
                "scope": mapping["scope"],
                "owner_key": obj_key(o["owner"], record) if o["owner"] else None,
                "material_key": obj_key(o["material"], record) if o["material"] else None,
                "source_identifier": identity[0].value if len(identity) == 1 else None,
                "identity_evidence": _evidence(identity),
                "evidence": _evidence(bound),
            }
        )
        dependencies, external = obj_dependencies(refs)
        proof("o:" + key, rid, bound, record, dependencies, external)
        support_rule(rid, support)
    # Remove descendants independently of template declaration order.
    while True:
        available = {o["key"] for o in candidate["objects"]} | set(used_existing)
        removed = [
            o
            for o in candidate["objects"]
            if any(o.get(k) and o[k] not in available for k in ("owner_key", "material_key"))
        ]
        if not removed:
            break
        for o in removed:
            candidate["objects"].remove(o)
            targets.pop("o:" + o["key"], None)
            for instance, k in list(instances.items()):
                if k == o["key"]:
                    instances.pop(instance)
            diagnostic("unresolved_object_dependency", "object", None, "unsupported dependent identity")
    fact_signatures = {}
    attempted_values = []
    required_context = []
    for index, f in enumerate(mapping["facts"]):
        rid = make_rule("fact", f)
        dynamic = templates[f["target"]]["cardinality"] == "per_record" or _dynamic(f)
        for record in records if dynamic else [None]:
            try:
                intended = table.resolve(f["value"], record, axis)
                if intended.block["kind"] == "table_cell":
                    attempted_values.append((rid, record, intended.block["block_id"]))
            except _MissingBinding:
                pass
            if index in cut_facts:
                diagnostic(CELL_VALUE_CUT, rid, record, CELL_VALUE_CUT_REASON)
                continue
            owner = obj_key(f["target"], record)
            if owner is None:
                diagnostic("unresolved_object_dependency", rid, record, "fact subject identity is unresolved")
                continue
            if f["field_id"] in _CONTEXT_FIELDS:
                required_context.append((rid, record, owner))
            try:
                value = table.resolve(f["value"], record, axis)
                unit = table.resolve(f["unit"], record, axis) if f["unit"] else None
                basis = table.resolve(f["basis"], record, axis) if f["basis"] else None
                qvalues = [table.resolve(q["source"], record, axis) for q in f["qualifiers"]]
                support = resolve_many(f["evidence"], record)
            except _MissingBinding as exc:
                # Blank value versus missing context is preserved distinctly.
                code = "missing_binding"
                try:
                    table.resolve(f["value"], record, axis)
                except _MissingBinding as vex:
                    if "empty selected content" in str(vex):
                        code = "missing_or_unreadable_value"
                diagnostic(code, rid, record, exc)
                continue
            expected = "text" if f["value_kind"] in ("text", "category") else "number"
            raw_value = value.value
            raw_unit = unit.value if unit else None
            projection = None
            if expected == "number" and value.block["kind"] == "table_cell":
                source_status = parse_typed_value(value.value, expected="number")["status"]
                if source_status in (
                    "blank",
                    "not_tested",
                    "not_detected",
                    "not_applicable",
                    "not_reported",
                    "nan",
                    "not_available",
                    "non_finite",
                    "error",
                ):
                    diagnostic("missing_or_unreadable_value", rid, record, source_status, [value])
                    continue
                try:
                    projection = project_source_cell(value.block["text"], f["value_kind"], proposed_unit=raw_unit)
                except ValueError as exc:
                    diagnostic("unresolved_value_type", rid, record, exc, [value] + ([unit] if unit else []))
                    continue
                raw_value = projection["raw_value"]
                raw_unit = projection["raw_unit"]
            parsed = parse_typed_value(raw_value, expected=expected)
            if parsed["status"] in (
                "blank",
                "not_tested",
                "not_detected",
                "not_applicable",
                "not_reported",
                "nan",
                "not_available",
                "non_finite",
                "error",
            ):
                diagnostic("missing_or_unreadable_value", rid, record, parsed["status"], [value])
                continue
            if parsed["status"] != "valid":
                diagnostic("unresolved_value_type", rid, record, parsed["status"], [value])
                continue
            unsafe = []
            numeric_literal = (
                expected == "number" or parse_typed_value(raw_value, expected="number")["status"] == "valid"
            )
            if numeric_literal and not (
                typed_span_supported(value.block["text"], *projection["value_span"], kind="number")
                if projection
                else _complete_typed_binding(value, f["value"], "number")
            ):
                unsafe.append(("value", value))
            if unit and not _complete_typed_binding(unit, f["unit"], "unit"):
                unsafe.append(("unit", unit))
            if unsafe:
                diagnostic(
                    "unsafe_source_token_boundary",
                    rid,
                    record,
                    "selector cuts or cannot establish complete original " + ", ".join(k for k, _ in unsafe),
                    [_Binding(b.block, b.block["text"]) for _, b in unsafe],
                )
                continue
            qualifiers = [{"key": q["key"], "value": v.value} for q, v in zip(f["qualifiers"], qvalues)]
            bound = [value] + ([unit] if unit else []) + ([basis] if basis else []) + qvalues + support
            # Direct fact evidence includes its actual subject identity and all
            # source context. This is not a claim that every piece is sufficient.
            if owner in bindings_for:
                bound += bindings_for[owner]
            elif owner in used_existing:
                bound += [_Binding(table.registry[e["block_id"]], e["quote"]) for e in used_existing[owner]["evidence"]]
            signature = core.fingerprint(
                [
                    owner,
                    f["field_id"],
                    value.block["block_id"],
                    value.value,
                    unit.value if unit else None,
                    basis.value if basis else None,
                    qualifiers,
                    f["statement_kind"],
                ]
            )
            if signature in fact_signatures:
                diagnostic(
                    "duplicate_fact",
                    rid,
                    record,
                    "same subject, field, source cell and qualifiers already mapped",
                    [value],
                )
                continue
            key = "tmf:" + core.fingerprint([mapping_hash, f["id"], record])[:24]
            fact_signatures[signature] = key
            selected = value.block["block_id"] if value.block["kind"] == "table_cell" else None
            candidate["facts"].append(
                {
                    "key": key,
                    "object_key": owner,
                    "field_id": f["field_id"],
                    "raw_value": raw_value,
                    "raw_unit": raw_unit,
                    "raw_basis": basis.value if basis else None,
                    "value_kind": f["value_kind"],
                    "qualifiers": qualifiers,
                    "statement_kind": f["statement_kind"],
                    "formation": "direct",
                    "input_fact_keys": [],
                    "evidence": _evidence(bound),
                    "value_cell": selected,
                }
            )
            dependencies, external = obj_dependencies([owner])
            proof("f:" + key, rid, bound, record, dependencies, external, selected)
            support_rule(rid, support)
    # Conflicting reports remain traceable issues, not two definite answers.
    groups = {}
    for f in candidate["facts"]:
        signature = core.fingerprint(
            [f["object_key"], f["field_id"], f["qualifiers"], f["raw_unit"], f["raw_basis"], f["statement_kind"]]
        )
        groups.setdefault(signature, []).append(f)
    for group in groups.values():
        if len({core.fingerprint(f["raw_value"]) for f in group}) < 2:
            continue
        support = []
        for f in group:
            support.extend(_Binding(table.registry[e["block_id"]], e["quote"]) for e in f["evidence"])
            candidate["facts"].remove(f)
            targets.pop("f:" + f["key"], None)
        diagnostic(
            "source_conflict",
            "facts",
            None,
            "same subject, field and conditions have differing original values",
            support,
        )
    relation_signatures = set()
    for r in mapping["relations"]:
        rid = make_rule("relation", r)
        dynamic = any(templates[a]["cardinality"] == "per_record" for a in (r["from"], r["to"])) or _dynamic(r)
        for record in records if dynamic else [None]:
            left, right = obj_key(r["from"], record), obj_key(r["to"], record)
            if left is None or right is None:
                diagnostic("unresolved_object_dependency", rid, record, "relation endpoint identity is unresolved")
                continue
            try:
                support = resolve_many(r["evidence"], record)
            except _MissingBinding as exc:
                diagnostic("missing_binding", rid, record, exc)
                continue
            signature = (left, r["predicate"], right)
            if signature in relation_signatures:
                diagnostic("duplicate_relation", rid, record, "identical relation already mapped")
                continue
            relation_signatures.add(signature)
            bound = list(support)
            for k in (left, right):
                bound += bindings_for.get(k, [])
                if k in used_existing:
                    bound += [_Binding(table.registry[e["block_id"]], e["quote"]) for e in used_existing[k]["evidence"]]
            index = len(candidate["relations"])
            candidate["relations"].append(
                {
                    "from_key": left,
                    "predicate": r["predicate"],
                    "to_key": right,
                    "applicability": "applicable",
                    "basis_kind": "structural_expansion",
                    "evidence": _evidence(bound),
                }
            )
            dependencies, external = obj_dependencies([left, right])
            proof("r:" + str(index), rid, bound, record, dependencies, external)
            support_rule(rid, support)
    # Scientific result identity cannot survive absent attribution edges.
    object_index = {**used_existing, **{o["key"]: o for o in candidate["objects"]}}
    for f in list(candidate["facts"]):
        oid = f["object_key"]
        if object_index[oid]["kind"] != "observation" or f["field_id"] != "observation.value":
            continue
        subjects = {
            r["to_key"] for r in candidate["relations"] if r["from_key"] == oid and r["predicate"] == "observes_subject"
        }
        tests = {
            r["from_key"]
            for r in candidate["relations"]
            if r["to_key"] == oid and r["predicate"] == "produces_observation"
        }
        test_subjects = {
            r["to_key"] for r in candidate["relations"] if r["from_key"] in tests and r["predicate"] == "tests_subject"
        }
        test_subjects |= {
            r["from_key"] for r in candidate["relations"] if r["to_key"] in tests and r["predicate"] == "tested_by"
        }
        methods = {
            r["to_key"] for r in candidate["relations"] if r["from_key"] in tests and r["predicate"] == "uses_method"
        }
        valid = (
            len(tests) == 1
            and len(test_subjects) == 1
            and (not subjects or subjects == test_subjects)
            and (f["statement_kind"] != "source_reported_measurement" or bool(methods))
        )
        if not valid:
            p = targets.pop("f:" + f["key"])
            candidate["facts"].remove(f)
            support = [_Binding(table.registry[e["block_id"]], e["quote"]) for e in f["evidence"]]
            diagnostic(
                "unresolved_result_context",
                p["rule_ids"][0],
                p["record_coordinate"],
                "result requires a unique test and subject; reported measurements also require an explicit method",
                support,
            )
    # Facts inherit only explicitly declared supporting relations/conditions.
    # This proof graph never grants approval and never infers undeclared edges.
    context_fields = _CONTEXT_FIELDS
    completed_context = {(p["rule_ids"][0], p["record_coordinate"]) for k, p in targets.items() if k.startswith("f:")}
    missing_context = [
        (rid, record, owner) for rid, record, owner in required_context if (rid, record) not in completed_context
    ]
    edges = []
    from_support = {
        "observes_subject",
        "tests_subject",
        "uses_method",
        "has_formulation",
        "refers_to_material",
        "contains_component",
        "method_applicability",
        "process_applicability",
    }
    to_support = {"produces_observation", "tested_by", "recipe_for"}
    for i, r in enumerate(candidate["relations"]):
        if r["predicate"] in from_support:
            edges.append((r["from_key"], r["to_key"], "r:" + str(i)))
        if r["predicate"] in to_support:
            edges.append((r["to_key"], r["from_key"], "r:" + str(i)))
    for f in list(candidate["facts"]):
        p = targets["f:" + f["key"]]
        reachable = {f["object_key"]}
        edge_refs = set()
        while True:
            old = set(reachable)
            for consumer, support, rkey in edges:
                if consumer in reachable:
                    reachable.add(support)
                    edge_refs.add(rkey)
            if old == reachable:
                break
        unresolved_conditions = [rid for rid, record, owner in missing_context if owner in reachable]
        if f["field_id"] not in context_fields and unresolved_conditions:
            candidate["facts"].remove(f)
            targets.pop("f:" + f["key"])
            support = [_Binding(table.registry[e["block_id"]], e["quote"]) for e in f["evidence"]]
            diagnostic(
                "unresolved_required_condition",
                p["rule_ids"][0],
                p["record_coordinate"],
                "declared source context did not compile: " + ", ".join(sorted(set(unresolved_conditions))),
                support,
            )
            continue
        conditions = {
            "f:" + x["key"]
            for x in candidate["facts"]
            if x["key"] != f["key"] and x["object_key"] in reachable and x["field_id"] in context_fields
        }
        # Same-object context facts can be reciprocal. Their own proof already
        # includes source meaning, so attach context conditions only to results.
        if f["field_id"] in context_fields:
            conditions = set()
        p["condition_refs"] = sorted(conditions)
        deps, external = obj_dependencies(reachable)
        p["identity_refs"] = sorted(set(deps) | edge_refs)
        p["dependencies"] = sorted(set(p["dependencies"]) | set(deps) | edge_refs | conditions)
        p["existing_object_refs"] = sorted(set(p["existing_object_refs"]) | set(external))
    # Reusing an accepted prior object does not bypass the new mapping rule
    # that declares its applicability to this table. There is no new object
    # target for that alias, so carry its rule directly to every consumer.
    existing_rule_ids = {}
    for o in templates.values():
        if o["cardinality"] == "existing":
            existing_rule_ids.setdefault(o["existing_key"], set()).add("object:" + o["id"])
    for p in targets.values():
        required = set(p["rule_ids"])
        for key in p["existing_object_refs"]:
            required.update(existing_rule_ids.get(key, ()))
        p["rule_ids"] = sorted(required)
    candidate = resolve_cell_values(candidate, blocks)
    selected = {
        b["block_id"]
        for (row, column), b in table.grid.items()
        if selection["rows"][0] <= row <= selection["rows"][1]
        and selection["columns"][0] <= column <= selection["columns"][1]
    }
    # A single finite reading plan cannot stop halfway through an apparent
    # table body. Header cells above/left of the body may be cited as context,
    # but nonempty cells continuing below or to the right must be included in
    # the reading rectangle. If the source actually contains another section,
    # this plan is unsupported until that boundary is represented explicitly.
    continuation = sorted(
        {
            b["block_id"]
            for (row, column), b in table.grid.items()
            if b["block_id"] not in selected
            and b["text"].strip()
            and (
                row > selection["rows"][1]
                and column >= selection["columns"][0]
                or column > selection["columns"][1]
                and row >= selection["rows"][0]
            )
        }
    )
    if continuation:
        shown = ", ".join(continuation[:8])
        raise MappingError(
            f"mapping.selection: unaccounted nonempty table cells "
            f"({len(continuation)}): {shown}; widen the reading region or keep this table unresolved"
        )
    used = {i for p in targets.values() for i in p["source_cell_ids"]}
    nonempty = {i for i in selected if table.cells[i]["text"].strip()}
    successful = {
        (rid, p["record_coordinate"]) for k, p in targets.items() if k.startswith("f:") for rid in p["rule_ids"]
    }
    unresolved_bindings = [
        {"rule_id": rid, "record_coordinate": coord, "source_cell_id": cid}
        for rid, coord, cid in attempted_values
        if (rid, coord) not in successful
    ]
    unresolved = sorted((nonempty - used) | {b["source_cell_id"] for b in unresolved_bindings})
    if unresolved:
        diagnostic(
            "unmapped_nonempty_cells",
            "coverage",
            None,
            "selected nonempty cells lack a compiled target",
            [_Binding(table.cells[i], table.cells[i]["text"]) for i in unresolved],
        )
    coverage = {
        "table_id": mapping["table_id"],
        "selection": deepcopy(selection),
        "selected_cell_ids": sorted(selected),
        "used_cell_ids": sorted(used),
        "unresolved_cell_ids": unresolved,
        "unresolved_fact_bindings": unresolved_bindings,
        "blank_cell_ids": sorted(selected - nonempty),
        "outside_selection_nonempty_cell_ids": sorted(
            i for i, b in table.cells.items() if i not in selected and b["text"].strip()
        ),
        "record_count": len(records),
        "status": "partial" if diagnostics else "compiled_awaiting_review",
    }
    result = {
        "version": VERSION,
        "candidate": candidate,
        "proof_requirements": {"targets": targets, "rules": list(rules.values()), "existing_objects": used_existing},
        "coverage": coverage,
        "diagnostics": diagnostics,
        "mapping_sha256": mapping_hash,
        "source_cells_sha256": core.fingerprint([table.cells[k] for k in sorted(table.cells)]),
    }
    result["compilation_sha256"] = core.fingerprint(result)
    return result
