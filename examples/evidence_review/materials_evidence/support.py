"""Bounded, source-literal support choices for direct document review.

An option is a possible citation, never a programmatic assertion that the
source supports a particular scientific fact. The reviewer must select it for
the exact target and the normal source checks still decide publication.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy

from .core import fingerprint

_PARENT_EDGES = {
    "contains_sample",
    "has_formulation",
    "contains_component",
    "tested_by",
    "produces_observation",
    "process_applicability",
}
_ROLE = {
    "sample": "sample",
    "component": "component",
    "formulation": "formulation",
    "reported_test": "test",
    "observation": "observation",
    "content_unit": "study",
    "method_definition": "method",
    "material": "material",
}
MAX_SUPPORT_OPTIONS = 64
MAX_SUPPORT_BYTES = 16_000


def _fact_core(fact):
    return {key: value for key, value in fact.items() if key != "evidence"}


def _structural_context(options):
    return [
        option
        for option in options
        if option["role"] not in ("potential_row_context", "potential_column_context", "table_metadata")
    ]


def _reference_receipt(evidence):
    """Keep duplicate counts while omitting potentially long source quotations."""
    return sorted(fingerprint(ref) for ref in evidence)


def _added_references_are_literal(evidence, prior_receipt, registry):
    from .ingest.documents.document_quality_verify import _text

    remaining = Counter(prior_receipt)
    for ref in evidence:
        signature = fingerprint(ref)
        if remaining[signature]:
            remaining[signature] -= 1
            continue
        if not isinstance(ref, dict) or not isinstance(ref.get("quote"), str) or not ref["quote"].strip():
            return False
        source = registry.get(ref.get("block_id"), {}).get("text")
        if not isinstance(source, str) or _text(ref["quote"]) not in _text(source):
            return False
    return True


def build_support_context(candidate, blocks, *, return_overflow=False):
    """Return independently selectable citations keyed by exact fact target.

    Only explicit candidate edges establish object ownership. A source cell's
    table_region offers same-table reading context, without assigning a header
    or any scientific role to that cell.
    """
    objects = {o["key"]: o for o in candidate["objects"]}
    facts = candidate["facts"]
    relations = candidate["relations"]
    by_child = {}
    for index, relation in enumerate(relations):
        if relation["predicate"] in _PARENT_EDGES:
            by_child.setdefault(relation["to_key"], []).append((index, relation))
    registry = {b["block_id"]: b for b in blocks}

    def sample_owner(object_key):
        seen = set()
        while object_key in objects and object_key not in seen:
            seen.add(object_key)
            if objects[object_key]["kind"] == "sample":
                return object_key
            parents = by_child.get(object_key, [])
            if len(parents) != 1:
                break
            object_key = parents[0][1]["from_key"]
        return None

    value_cell_samples = {}
    for row in facts:
        if row.get("value_cell"):
            value_cell_samples.setdefault(row["value_cell"], set()).add(sample_owner(row["object_key"]))
    # A known measurement cell is never a geometric header/row-label option,
    # even when it belongs to the same sample. Exact object/edge evidence is
    # collected independently above the geometry scan.
    table_candidates = {}
    for block in blocks:
        table_id = block.get("table_region")
        if (
            table_id
            and block.get("kind") in ("table_cell", "table_caption", "table_footnote")
            and block.get("block_id") not in value_cell_samples
        ):
            table_candidates.setdefault(table_id, []).append(block)
    sample_identity_cells = {}
    for obj in objects.values():
        if obj["kind"] == "sample":
            for ref in obj.get("evidence", []):
                if registry.get(ref.get("block_id"), {}).get("kind") == "table_cell":
                    sample_identity_cells.setdefault(ref["block_id"], set()).add(obj["key"])
    for row in facts:
        if row["field_id"].endswith(".identifier") and objects.get(row["object_key"], {}).get("kind") == "sample":
            for ref in row.get("evidence", []):
                if registry.get(ref.get("block_id"), {}).get("kind") == "table_cell":
                    sample_identity_cells.setdefault(ref["block_id"], set()).add(row["object_key"])
    result = {}
    overflows = {}
    for fact in facts:
        target = "f:" + fact["key"]
        entries = []
        seen = set()
        entry_bytes = 0
        overflow = False

        def add(role, source_target, evidence):
            nonlocal entry_bytes, overflow
            for ref in evidence or []:
                if overflow:
                    return
                block = registry.get(ref.get("block_id"))
                if not block or not isinstance(block.get("text"), str) or not block["text"].strip():
                    continue
                if not isinstance(ref.get("quote"), str) or not ref["quote"].strip():
                    continue
                if ref["quote"] not in block["text"]:
                    continue
                identity = (role, source_target, ref["block_id"], ref["quote"])
                if identity in seen:
                    continue
                entry = {
                    "id": fingerprint([target, *identity])[:20],
                    "role": role,
                    "source_target": source_target,
                    "evidence": [deepcopy(ref)],
                }
                size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
                if len(entries) + 1 > MAX_SUPPORT_OPTIONS or entry_bytes + size > MAX_SUPPORT_BYTES:
                    overflow = True
                    return
                seen.add(identity)
                entries.append(entry)
                entry_bytes += size

        current = fact["object_key"]
        chain = []
        while current in objects and current not in chain:
            if overflow:
                break
            chain.append(current)
            obj = objects[current]
            add(_ROLE.get(obj["kind"], "owner"), "o:" + current, obj.get("evidence"))
            parents = by_child.get(current, [])
            if len(parents) != 1:
                break
            index, relation = parents[0]
            if relation["from_key"] not in objects:
                break
            add("ownership_edge", "r:" + str(index), relation.get("evidence"))
            current = relation["from_key"]

        tests = [key for key in chain if objects[key]["kind"] == "reported_test"]
        if len(tests) == 1:
            test = tests[0]
            for other in facts:
                if overflow:
                    break
                if other["object_key"] == test and other["field_id"] == "condition.parameter":
                    add("condition", "f:" + other["key"], other.get("evidence"))
            method_links = [
                (i, r)
                for i, r in enumerate(relations)
                if r["predicate"] == "uses_method"
                and r["from_key"] == test
                and r["to_key"] in objects
                and objects[r["to_key"]]["kind"] == "method_definition"
            ]
            if len(method_links) == 1:
                index, relation = method_links[0]
                add("method_link", "r:" + str(index), relation.get("evidence"))
                method = relation["to_key"]
                add("method", "o:" + method, objects[method].get("evidence"))
                for other in facts:
                    if overflow:
                        break
                    if other["object_key"] == method and other["field_id"].startswith("method."):
                        add("metric_or_method_field", "f:" + other["key"], other.get("evidence"))

        cell = registry.get(fact.get("value_cell"))
        table_id = cell.get("table_region") if cell and cell.get("kind") == "table_cell" else None
        if table_id:
            position = cell.get("anchor", {}).get("table_cell", {})
            value_row, value_column = position.get("row"), position.get("column")
            current_sample = sample_owner(fact["object_key"])
            for block in table_candidates.get(table_id, []):
                if overflow:
                    break
                if block["block_id"] == fact["value_cell"] or not str(block.get("text", "")).strip():
                    continue
                role = "table_metadata"
                if block["kind"] == "table_cell":
                    identity_owners = sample_identity_cells.get(block["block_id"], set())
                    if identity_owners and identity_owners != {current_sample}:
                        continue
                    other = block.get("anchor", {}).get("table_cell", {})
                    row, column = other.get("row"), other.get("column")
                    if not all(type(x) is int for x in (value_row, value_column, row, column)):
                        continue
                    row_span, column_span = other.get("row_span", 1), other.get("col_span", 1)
                    if type(row_span) is not int or type(column_span) is not int or min(row_span, column_span) < 1:
                        continue
                    if row <= value_row < row + row_span:
                        role = "potential_row_context"
                    elif row < value_row and column <= value_column < column + column_span:
                        role = "potential_column_context"
                    else:
                        continue
                add(role, "block:" + block["block_id"], [{"block_id": block["block_id"], "quote": block["text"]}])
        if overflow:
            result[target] = []
            overflows[target] = (
                f"support context exceeds {MAX_SUPPORT_OPTIONS} options or {MAX_SUPPORT_BYTES} bytes for this fact"
            )
        else:
            result[target] = entries
    if return_overflow:
        return result, overflows
    if overflows:
        raise ValueError("; ".join(f"{target}: {reason}" for target, reason in overflows.items()))
    return result


def bind_review_support(candidate, contexts, selections, blocks):
    """Append only an explicit valid selection to a review copy.

    Returns (bound candidate, per-fact audit, invalid target reasons). The
    original numerical value must already have its own source citation.
    """
    bound = deepcopy(candidate)
    registry = {b["block_id"]: b for b in blocks}
    invalid = {}
    trace = []
    by_key = {"f:" + f["key"]: f for f in bound["facts"]}
    for target, selected in selections.items():
        if target not in by_key or not isinstance(selected, list) or any(not isinstance(s, str) for s in selected):
            invalid[target] = "invalid support target or selector"
            continue
        if len(selected) != len(set(selected)):
            invalid[target] = "duplicate support selection"
            continue
        allowed = {x["id"]: x for x in contexts.get(target, [])}
        if any(s not in allowed for s in selected):
            invalid[target] = "support selection is outside this target allowlist"
            continue
        fact = by_key[target]
        before = deepcopy(fact.get("evidence", []))
        value = fact.get("raw_value")
        cell_id = fact.get("value_cell")
        if selected and (
            value is None
            or not any(
                (ref.get("block_id") == cell_id if cell_id else str(value) in str(ref.get("quote", "")))
                and ref.get("block_id") in registry
                and isinstance(ref.get("quote"), str)
                and ref["quote"] in registry[ref["block_id"]].get("text", "")
                for ref in before
            )
        ):
            invalid[target] = "original value has no own literal source citation"
            continue
        for support_id in selected:
            option = allowed[support_id]
            for ref in option["evidence"]:
                block = registry.get(ref["block_id"])
                if not block or not isinstance(ref["quote"], str) or ref["quote"] not in block.get("text", ""):
                    invalid[target] = "selected support is not literal source content"
                    break
                if ref not in fact["evidence"]:
                    fact["evidence"].append(deepcopy(ref))
            if target in invalid:
                fact["evidence"] = before
                break
        if target in invalid:
            continue
        trace.append(
            {
                "target": target,
                "selected_ids": selected,
                "selected_roles": [allowed[s]["role"] for s in selected],
                "fact_core_sha256": fingerprint(_fact_core(fact)),
                "structural_context_sha256": fingerprint(_structural_context(contexts.get(target, []))),
                "before_evidence_sha256": fingerprint(before),
                "after_evidence_sha256": fingerprint(fact["evidence"]),
                "before_reference_receipt": _reference_receipt(before),
                "bound_reference_receipt": _reference_receipt(fact["evidence"]),
            }
        )
    return bound, trace, invalid


def replay_review_support(candidate, prior_trace, blocks):
    """Reapply only unchanged, previously selected review citations.

    The caller uses this only to compare a correction against prior accepted
    content. A changed fact or changed structural support fails closed; a new
    review must independently decide any new support.
    """
    contexts, overflows = build_support_context(candidate, blocks, return_overflow=True)
    registry = {block["block_id"]: block for block in blocks}
    facts = {"f:" + f["key"]: f for f in candidate["facts"]}
    selections = {}
    problems = {}
    for row in prior_trace:
        target = row.get("target")
        selected = row.get("selected_ids")
        if not selected:
            continue
        fact = facts.get(target)
        if target in selections or fact is None:
            problems[target] = "prior supported fact is missing or repeated"
            continue
        if target in overflows:
            problems[target] = overflows[target]
        elif fingerprint(_fact_core(fact)) != row.get("fact_core_sha256"):
            problems[target] = "prior supported fact core changed"
        elif not isinstance(row.get("before_reference_receipt"), list) or Counter(
            row["before_reference_receipt"]
        ) - Counter(_reference_receipt(fact.get("evidence", []))):
            problems[target] = "original source citation removed or changed"
        elif not _added_references_are_literal(fact.get("evidence", []), row["before_reference_receipt"], registry):
            problems[target] = "correction contains a nonliteral source citation"
        elif fingerprint(_structural_context(contexts.get(target, []))) != row.get("structural_context_sha256"):
            problems[target] = "owner, method or condition support changed"
        else:
            selections[target] = selected
    if problems:
        return None, problems
    bound, new_trace, invalid = bind_review_support(candidate, contexts, selections, blocks)
    if invalid:
        return None, invalid
    old = {row["target"]: row for row in prior_trace if row.get("selected_ids")}
    for row in new_trace:
        prior = old[row["target"]].get("bound_reference_receipt")
        if not isinstance(prior, list) or Counter(prior) - Counter(row["bound_reference_receipt"]):
            problems[row["target"]] = "previously reviewed evidence binding removed or changed"
    return (None, problems) if problems else (bound, {})
