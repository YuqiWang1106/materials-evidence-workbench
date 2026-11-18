"""Independent, exhaustive review of a program-expanded document table mapping.

This module consumes real reviewer responses and caller attestations of images
actually sent. It never calls a model, invents per-fact model checks, or confers
scientific approval. Final document relationship/publication gates still apply.
"""

from __future__ import annotations

import re
from copy import deepcopy

from . import contract, core
from .document_source_values import project_source_cell, source_cell_provenance_matches
from .ingest.documents.document_quality_verify import (
    _literal,
    _numeric_fact,
    _same_inherited_value,
    _unit_literal,
    evidence_token_supported,
    requires_image_review,
    review_targets,
)

VERSION = "document-table-review-1"
RULE_DIMENSIONS = ("identity", "field_meaning", "relationships", "conditions")
CELL_DIMENSIONS = ("value", "header", "identity", "unit")
VERDICTS = ("supported", "unsupported", "uncertain")


def _object(properties):
    return {
        "type": "object",
        "properties": deepcopy(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def _string(values=None):
    return {"type": "string", **({"enum": list(values)} if values is not None else {})}


def _evidence_schema():
    return {"type": "array", "minItems": 1, "items": _object({"block_id": _string(), "quote": _string()})}


def _schema(key, ids, dimensions, binding_sha256, extra):
    ids = list(dict.fromkeys(ids))
    props = {
        key: _string(ids),
        "verdict": _string(VERDICTS),
        "reason": _string(),
        "checks": _object({d: _string(VERDICTS) for d in dimensions}),
        **extra,
    }
    return _object(
        {
            "binding_sha256": _string([binding_sha256]) if binding_sha256 else _string(),
            "checks": {"type": "array", "minItems": len(ids), "maxItems": len(ids), "items": _object(props)},
        }
    )


def rule_review_schema(rule_ids, binding_sha256=None):
    """One independent semantic check per rule; IDs may be one actual batch."""
    return _schema("rule_id", rule_ids, RULE_DIMENSIONS, binding_sha256, {"evidence": _evidence_schema()})


def cell_review_schema(cell_ids, crop_ids, binding_sha256=None):
    """Every required cell, including headers/identity/units, must be read from pixels."""
    return _schema(
        "cell_id", cell_ids, CELL_DIMENSIONS, binding_sha256, {"crop_id": _string(crop_ids), "observed_text": _string()}
    )


def _normal(value):
    # Only layout whitespace differs. Numeral spelling, signs, prefixes and
    # exponents cannot be rounded, converted, or silently normalized.
    return re.sub(r"\s+", " ", str(value)).strip()


def _evidence_ids(item):
    return {
        e.get("block_id")
        for name in ("evidence", "identity_evidence")
        for e in item.get(name, [])
        if isinstance(e, dict)
    }


def _all_required_ids(compiled):
    proof = compiled.get("proof_requirements", {})
    ids = set()
    for row in proof.get("targets", {}).values():
        for key in ("source_cell_ids", "source_block_ids", "context_refs"):
            ids.update(row.get(key, []))
    for row in proof.get("rules", []):
        ids.update(row.get("source_block_ids", []))
        ids.update(_evidence_ids(row))
    for row in proof.get("existing_objects", {}).values():
        ids.update(_evidence_ids(row))
    for row in review_targets(compiled.get("candidate", {"objects": [], "facts": [], "relations": []})).values():
        ids.update(_evidence_ids(row))
    return ids


def build_review_binding(compiled, blocks):
    """Recomputed from CURRENT compiler output and IR, never from supplied hashes.

    Required image-context IDs use the same actual-image attestation/crop API as
    cells. Their literal support is checked by rule review; no long prose copying
    into the per-cell review is required.
    """
    ids = _all_required_ids(compiled)
    ordered = sorted(blocks, key=lambda b: b["block_id"])
    cells = [b for b in ordered if b.get("kind") == "table_cell"]
    required_cells = [b["block_id"] for b in cells if b["block_id"] in ids]
    image_context = [
        b["block_id"]
        for b in ordered
        if b["block_id"] in ids and b.get("kind") != "table_cell" and requires_image_review(b)
    ]
    result = {
        "version": VERSION,
        "mapping_sha256": compiled.get("mapping_sha256"),
        "compilation_sha256": core.fingerprint({k: v for k, v in compiled.items() if k != "compilation_sha256"}),
        "candidate_sha256": core.fingerprint(compiled.get("candidate")),
        "source_cells_sha256": core.fingerprint(cells),
        "source_sha256": core.fingerprint(ordered),
        "field_contract_sha256": core.fingerprint({"fields": contract.FIELDS, "relations": contract.RELATIONS}),
        "required_cell_ids": required_cells,
        "required_image_context_ids": image_context,
    }
    result["binding_sha256"] = core.fingerprint(result)
    return result


def _supports(evidence, registry):
    return bool(evidence) and all(
        isinstance(e, dict)
        and e.get("block_id") in registry
        and _normal(e.get("quote", ""))
        and _normal(e["quote"]) in _normal(registry[e["block_id"]].get("text", ""))
        for e in evidence
    )


def _index(rows, key):
    index = {}
    duplicates = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get(key)
        if name in index:
            duplicates.add(name)
        index[name] = row
    return index, duplicates


def _dimensions(check, names):
    return check.get("verdict") == "supported" and all(
        check.get("checks", {}).get(name) == "supported" for name in names
    )


def _crop_covers(crop, block):
    if (
        crop.get("asset_ref") != block.get("asset_ref")
        or crop.get("page") != block.get("page")
        or not re.fullmatch("[a-fA-F0-9]{64}", str(crop.get("image_sha256", "")))
        or block.get("block_id") not in crop.get("cell_ids", [])
    ):
        return False
    region = block.get("anchor", {}).get("coordinate_region", {})
    seen = crop.get("coordinate_region", {})
    if any(region.get(k) != seen.get(k) for k in ("width", "height", "unit", "origin")):
        return False
    if region.get("origin") != "top_left":
        return False
    try:
        x0, y0, x1, y1 = region["bbox"]
        a, b, c, d = seen["bbox"]
        return a <= x0 <= x1 <= c and b <= y0 <= y1 <= d and a < c and b < d
    except (KeyError, ValueError, TypeError):
        return False


def _intrinsic_dependencies(candidate, targets, existing):
    deps = {k: set() for k in targets}
    objects = {**existing, **{o["key"]: o for o in candidate["objects"]}}

    def obj_ref(key):
        return "o:" + key if "o:" + str(key) in targets else None

    for o in candidate["objects"]:
        for name in ("owner_key", "material_key"):
            if o.get(name):
                deps["o:" + o["key"]].add(obj_ref(o[name]) or "existing:" + o[name])
    for f in candidate["facts"]:
        deps["f:" + f["key"]].add(obj_ref(f["object_key"]) or "existing:" + f["object_key"])
        deps["f:" + f["key"]].update("f:" + key for key in f.get("input_fact_keys", []))
    for i, r in enumerate(candidate["relations"]):
        deps["r:" + str(i)].update(obj_ref(r[name]) or "existing:" + r[name] for name in ("from_key", "to_key"))
    # Attribution edges are dependencies of the result, not of an independent
    # shared sample/method. Removing one result must not erase the whole table.
    for f in candidate["facts"]:
        oid = f["object_key"]
        key = "f:" + f["key"]
        if objects.get(oid, {}).get("kind") != "observation":
            continue
        relevant = [
            (i, r)
            for i, r in enumerate(candidate["relations"])
            if (r["from_key"] == oid and r["predicate"] == "observes_subject")
            or (r["to_key"] == oid and r["predicate"] == "produces_observation")
        ]
        tests = {r["from_key"] for _, r in relevant if r["predicate"] == "produces_observation"}
        relevant += [
            (i, r)
            for i, r in enumerate(candidate["relations"])
            if r["from_key"] in tests and r["predicate"] in ("tests_subject", "uses_method")
        ]
        deps[key].update("r:" + str(i) for i, _ in relevant)
    return deps


def _unrooted(rows, key, dependency_keys, roots=()):
    """Ownership and derivation cannot use a cycle as independent support."""
    remaining = {
        row[key]: {
            value
            for name in dependency_keys
            for value in (row.get(name, []) if isinstance(row.get(name), list) else [row.get(name)])
            if value
        }
        for row in rows
    }
    resolved = set(roots)
    while True:
        new = {name for name, refs in remaining.items() if refs <= resolved}
        if not new:
            return set(remaining)
        resolved.update(new)
        for name in new:
            remaining.pop(name)


def _quote_selectors(value):
    if isinstance(value, dict):
        if value.get("kind") == "quote":
            yield value
        else:
            for child in value.values():
                yield from _quote_selectors(child)
    elif isinstance(value, list):
        for child in value:
            yield from _quote_selectors(child)


def _quote_source_span(selector, registry, table_id):
    block = registry.get(selector.get("block_id"))
    quote = selector.get("quote")
    if block is None or block.get("kind") in ("table", "visual_page"):
        return None
    if block.get("kind") == "table_cell" and block.get("table_region") != table_id:
        return None
    source = block.get("text")
    if not isinstance(source, str) or not isinstance(quote, str) or not quote:
        return None
    start = source.find(quote)
    # Include overlapping occurrences; do not normalize source characters or
    # choose the first of multiple possible locations.
    if start < 0 or source.find(quote, start + 1) >= 0:
        return None
    return block, [start, start + len(quote)]


def _typed_selector_supported(
    selector, value, kind, requirement, compiled, registry, *, value_cell=None, value_kind=None, proposed_unit=None
):
    """Check the selected occurrence, never a same-spelled token elsewhere.

    Review can receive older compiler artifacts, so literal evidence and a
    positive semantic verdict do not replace rechecking source coordinates.
    The finite selector is resolved from the current IR, without executing code.
    """
    from .source_token_boundaries import typed_span_supported

    if not isinstance(selector, dict):
        return False
    location = selector.get("kind")
    block = None
    if location in ("text", "cell", "quote"):
        block = registry.get(selector.get("cell_id" if location == "cell" else "block_id"))
        if block is None:
            return False
        if location == "cell" and block.get("kind") != "table_cell":
            return False
        if location == "text" and block.get("kind") in ("table", "table_cell", "visual_page"):
            return False
        if location == "quote":
            if block.get("kind") in ("table", "visual_page"):
                return False
            if block.get("kind") == "table_cell" and block.get("table_region") != compiled.get("coverage", {}).get(
                "table_id"
            ):
                return False
    elif location == "axis":
        coverage = compiled.get("coverage", {})
        axis = coverage.get("selection", {}).get("axis")
        row, column = selector.get("row"), selector.get("column")
        current = requirement.get("record_coordinate")
        if row == "current":
            if axis != "row":
                return False
            row = current
        if column == "current":
            if axis != "column":
                return False
            column = current
        if type(row) is not int or type(column) is not int:
            return False
        matches = []
        for b in registry.values():
            if b.get("kind") != "table_cell" or b.get("table_region") != coverage.get("table_id"):
                continue
            pos = b.get("anchor", {}).get("table_cell", {})
            r, c = pos.get("row"), pos.get("column")
            rs, cs = pos.get("row_span", 1), pos.get("col_span", 1)
            if any(type(v) is not int for v in (r, c, rs, cs)):
                continue
            if r <= row < r + rs and c <= column < c + cs and (selector.get("merged") or (row, column) == (r, c)):
                matches.append(b)
        if len(matches) != 1:
            return False
        block = matches[0]
    else:
        return False
    source = block.get("text")
    if not isinstance(source, str) or block["block_id"] not in requirement.get("source_block_ids", []):
        return False
    if location == "quote":
        resolved = _quote_source_span(selector, registry, compiled.get("coverage", {}).get("table_id"))
        if resolved is None:
            return False
        _, span = resolved
    else:
        span = selector.get("span")
    if span is None:
        span = [0, len(source)]
    if not isinstance(span, list) or len(span) != 2:
        return False
    start, end = span
    if any(type(v) is not int for v in span) or not 0 <= start < end <= len(source):
        return False
    selected = source[start:end]
    if kind == "number" and block.get("kind") == "table_cell":
        if selected != source or value_cell != block["block_id"] or requirement.get("value_cell") != block["block_id"]:
            return False
        try:
            projection = project_source_cell(source, value_kind, value, proposed_unit)
        except ValueError:
            return False
        start, end = projection["value_span"]
        selected = source[start:end]
    if isinstance(value, str):
        if _normal(value) != _normal(selected):
            return False
    elif not _literal(value, selected):
        return False
    return typed_span_supported(source, start, end, kind=kind)


def verify_table_mapping_review(compiled, blocks, rule_review, cell_review, *, image_reviewed_cell_ids, crop_bindings):
    """Return independent accepted records and a tamper-evident mapping proof.

    Positive model labels alone are insufficient: source literals, field types,
    program-read values, real image bindings and downstream dependencies all
    have to hold. Any absent/duplicate review rejects its consumers.
    """
    candidate = deepcopy(compiled["candidate"])
    targets = review_targets(candidate)
    requirements = compiled.get("proof_requirements", {})
    reqs = requirements.get("targets", {})
    binding = build_review_binding(compiled, blocks)
    registry = {b["block_id"]: b for b in blocks}
    rules, duplicate_rules = _index(requirements.get("rules", []), "rule_id")
    rule_checks, duplicate_rule_checks = _index(rule_review.get("checks", []), "rule_id")
    cells, duplicate_cells = _index(cell_review.get("checks", []), "cell_id")
    crops, duplicate_crops = _index(crop_bindings, "crop_id")
    images = set(image_reviewed_cell_ids)
    required_cells = set(binding["required_cell_ids"])
    required_context = set(binding["required_image_context_ids"])
    global_errors = []
    reasons = {}
    rule_errors = {}
    cell_errors = {}
    context_errors = {}
    if len(registry) != len(blocks):
        global_errors.append("duplicate source identity")
    if any(len(candidate[s]) != len({x["key"] for x in candidate[s]}) for s in ("objects", "facts")):
        global_errors.append("duplicate candidate identity")
    if set(reqs) != set(targets):
        global_errors.append("compiler target coverage is incomplete or foreign")
    if not re.fullmatch("[a-fA-F0-9]{64}", str(compiled.get("mapping_sha256", ""))):
        global_errors.append("mapping hash is absent")
    if compiled.get("compilation_sha256") != binding["compilation_sha256"]:
        global_errors.append("compilation hash is absent or stale")
    if any(r.get("binding_sha256") != binding["binding_sha256"] for r in (rule_review, cell_review)):
        global_errors.append("review binding is stale or belongs to another mapping/source")
    if set(rule_checks) - set(rules) or set(cells) - required_cells:
        global_errors.append("review contains foreign rule or cell identities")

    def covered(block_id, crop_id=None):
        if block_id not in images or block_id not in registry:
            return False
        choices = [crop_id] if crop_id else list(crops)
        return any(
            cid not in duplicate_crops and cid in crops and _crop_covers(crops[cid], registry[block_id])
            for cid in choices
        )

    for rid, rule in rules.items():
        check = rule_checks.get(rid, {})
        allowed_sources = set(rule.get("source_block_ids", [])) | set(rule.get("selector_source_block_ids", []))
        if rid in duplicate_rules or rid in duplicate_rule_checks:
            rule_errors[rid] = "duplicate rule or review"
        elif not _dimensions(check, RULE_DIMENSIONS):
            rule_errors[rid] = "independent rule review incomplete or unsupported"
        elif not _supports(rule.get("evidence"), registry) or not _supports(check.get("evidence"), registry):
            rule_errors[rid] = "rule evidence is not literal source content"
        elif (
            not _evidence_ids(rule) <= set(rule.get("source_block_ids", []))
            or not _evidence_ids(check) <= allowed_sources
        ):
            rule_errors[rid] = "rule review references an undeclared source"
        elif not _evidence_ids(rule) <= _evidence_ids(check):
            rule_errors[rid] = "rule semantic source coverage is incomplete"
        elif any(
            s.get("block_id") not in allowed_sources
            or _quote_source_span(s, registry, compiled.get("coverage", {}).get("table_id")) is None
            for s in _quote_selectors(rule.get("definition", {}))
        ):
            rule_errors[rid] = "quote selector lacks one exact location in its declared original source"
    for cid in required_cells:
        check = cells.get(cid, {})
        if cid in duplicate_cells:
            cell_errors[cid] = "duplicate cell review"
        elif not _dimensions(check, CELL_DIMENSIONS):
            cell_errors[cid] = "original cell review incomplete or unsupported"
        elif "observed_text" not in check or _normal(check["observed_text"]) != _normal(registry[cid].get("text", "")):
            cell_errors[cid] = "observed original cell differs from source transcription"
        elif not check.get("crop_id") or not covered(cid, check["crop_id"]):
            cell_errors[cid] = "cell lacks matching actual original image crop"
    for bid in required_context:
        if not covered(bid):
            context_errors[bid] = "context lacks matching actual original image crop"
    for rid, rule in rules.items():
        source_ids = set(rule.get("source_block_ids", []))
        if source_ids & (set(cell_errors) | set(context_errors)):
            rule_errors[rid] = "rule source reading lacks complete original image verification"
    existing = requirements.get("existing_objects", {})
    valid_existing = set()
    for key, obj in existing.items():
        refs = _evidence_ids(obj)
        identity_rules = {
            rid
            for rid, r in rules.items()
            if r.get("kind") == "object" and r.get("definition", {}).get("existing_key") == key
        }
        if (
            _supports(obj.get("evidence"), registry)
            and refs
            and identity_rules
            and not identity_rules & set(rule_errors)
            and (not obj.get("identity_evidence") or _supports(obj["identity_evidence"], registry))
            and not refs & (set(cell_errors) | set(context_errors))
        ):
            valid_existing.add(key)
    all_objects = {**existing, **{o["key"]: o for o in candidate["objects"]}}
    unrooted_objects = _unrooted(candidate["objects"], "key", ("owner_key", "material_key"), valid_existing)
    unrooted_facts = _unrooted(candidate["facts"], "key", ("input_fact_keys",))
    fact_index = {f["key"]: f for f in candidate["facts"]}
    dependencies = _intrinsic_dependencies(candidate, targets, existing)
    for key, item in targets.items():
        errors = list(global_errors)
        p = reqs.get(key, {})
        rids = set(p.get("rule_ids", []))
        sources = set(p.get("source_block_ids", []))
        sourcecells = set(p.get("source_cell_ids", []))
        references = _evidence_ids(item)
        if not rids or rids - set(rules):
            errors.append("target has missing rule requirements")
        if rids & set(rule_errors):
            errors.append("target rule review failed")
        if not _supports(item.get("evidence"), registry):
            errors.append("target evidence is not literal source content")
        if item.get("identity_evidence") and not _supports(item["identity_evidence"], registry):
            errors.append("target identity evidence is not literal source content")
        if not references <= sources or sources - set(registry):
            errors.append("target source coverage is incomplete or foreign")
        if sourcecells != {b for b in sources if registry.get(b, {}).get("kind") == "table_cell"}:
            errors.append("target required cell coverage is incomplete or foreign")
        if sourcecells & set(cell_errors):
            errors.append("target original cell review failed")
        contexts = set(p.get("context_refs", []))
        if not contexts <= sources:
            errors.append("target context source coverage is incomplete")
        image_context = sources & required_context
        reviewed_context = set().union(*(_evidence_ids(rule_checks.get(rid, {})) for rid in rids)) if rids else set()
        if image_context & set(context_errors) or not image_context <= reviewed_context:
            errors.append("target original image context review is incomplete")
        for group in ("dependencies", "identity_refs", "condition_refs"):
            dependencies[key].update(p.get(group, []))
        dependencies[key].update("existing:" + name for name in p.get("existing_object_refs", []))
        if key.startswith("o:") and item["key"] in existing:
            errors.append("existing identity must be referenced, not redeclared")
        if key.startswith("o:") and item["key"] in unrooted_objects:
            errors.append("object ownership is cyclic or has no verified root")
        if key.startswith("f:"):
            if item["key"] in unrooted_facts:
                errors.append("fact derivation is cyclic or has no verified root")
            field = contract.FIELDS.get(item.get("field_id"), {})
            if all_objects.get(item.get("object_key"), {}).get("kind") not in field.get("owner_kinds", []) or item.get(
                "value_kind"
            ) not in field.get("value_kinds", []):
                errors.append("field contract does not permit field, owner or value kind")
            cid = item.get("value_cell")
            cell = registry.get(cid, {})
            inherited = item.get("formation") == "explicit_inheritance"
            table_value = (
                _numeric_fact(item)
                and not inherited
                and any(
                    registry.get(e.get("block_id"), {}).get("kind") in ("table", "table_cell")
                    and _literal(item.get("raw_value"), e.get("quote", ""))
                    for e in item.get("evidence", [])
                )
                and not any(
                    registry.get(e.get("block_id"), {}).get("kind") not in ("table", "table_cell")
                    and _literal(item.get("raw_value"), e.get("quote", ""))
                    for e in item.get("evidence", [])
                )
            )
            if cid or item.get("program_value_source") or table_value:
                if (
                    cell.get("kind") != "table_cell"
                    or cid not in sourcecells
                    or p.get("value_cell") != cid
                    or not source_cell_provenance_matches(item, cell)
                ):
                    errors.append("table value lacks exact program-read cell provenance")
            else:
                quotes = " ".join(e.get("quote", "") for e in item.get("evidence", []))
                if item.get("raw_value") is not None and not _literal(item["raw_value"], quotes):
                    errors.append("noncell raw value is not literal in source")
            if inherited and (
                not item.get("input_fact_keys")
                or not any(
                    fk in fact_index and _same_inherited_value(item, fact_index[fk])
                    for fk in item.get("input_fact_keys", [])
                )
            ):
                errors.append("explicit inheritance does not preserve the supporting fact")
            if (
                not inherited
                and _numeric_fact(item)
                and not evidence_token_supported(
                    item.get("raw_value"), item.get("evidence", []), registry, kind="number"
                )
            ):
                errors.append("numeric value is not a complete token at the original source quote")
            for rid in rids:
                rule = rules.get(rid, {})
                if rule.get("kind") != "fact":
                    continue
                definition = rule.get("definition", {})
                for name, value, kind, required in (
                    ("value", item.get("raw_value"), "number", not inherited and _numeric_fact(item)),
                    ("unit", item.get("raw_unit"), "unit", bool(item.get("raw_unit"))),
                ):
                    if (
                        required
                        and name in definition
                        and not _typed_selector_supported(
                            definition[name],
                            value,
                            kind,
                            p,
                            compiled,
                            registry,
                            value_cell=item.get("value_cell"),
                            value_kind=item.get("value_kind"),
                            proposed_unit=item.get("raw_unit"),
                        )
                    ):
                        errors.append(f"{name} selector is not the complete literal at its original source position")
            quotes = " ".join(e.get("quote", "") for e in item.get("evidence", []))
            if item.get("raw_unit") and (
                not _unit_literal(item["raw_unit"], quotes)
                or not evidence_token_supported(item["raw_unit"], item.get("evidence", []), registry, kind="unit")
            ):
                errors.append("unit is not a complete literal token at the original source quote")
            if item.get("raw_basis") and _normal(item["raw_basis"]) not in _normal(quotes):
                errors.append("basis is not literal in source")
        elif key.startswith("r:"):
            spec = contract.RELATIONS.get(item.get("predicate"), {})
            if all_objects.get(item.get("from_key"), {}).get("kind") not in spec.get(
                "allowed_from_kinds", []
            ) or all_objects.get(item.get("to_key"), {}).get("kind") not in spec.get("allowed_to_kinds", []):
                errors.append("relation endpoint kinds are outside the common contract")
        if errors:
            reasons[key] = list(dict.fromkeys(errors))
    # Resolve missing/failed dependencies; verified cyclic association graphs are
    # allowed, but no absent or failed external identity can bootstrap itself.
    passed = set(targets) - set(reasons)
    while True:
        failed = {
            k
            for k in passed
            if any(
                (d[9:] not in valid_existing if d.startswith("existing:") else d not in passed) for d in dependencies[k]
            )
        }
        if not failed:
            break
        for key in failed:
            reasons[key] = ["unverified identity, condition or upstream target dependency"]
        passed -= failed
    accepted = {
        "objects": [o for o in candidate["objects"] if "o:" + o["key"] in passed],
        "facts": [f for f in candidate["facts"] if "f:" + f["key"] in passed],
        "relations": [r for i, r in enumerate(candidate["relations"]) if "r:" + str(i) in passed],
        "issues": candidate.get("issues", []),
    }
    proof = {
        **binding,
        "review_kind": "program_mapping_review",
        "accepted_candidate_sha256": core.fingerprint(accepted),
        "verified_target_ids": sorted(passed),
        "rule_review_sha256": core.fingerprint(rule_review),
        "accepted_target_ids": {
            "objects": ["o:" + o["key"] for o in accepted["objects"]],
            "facts": ["f:" + f["key"] for f in accepted["facts"]],
            "relations": ["r:" + str(i) for i in range(len(candidate["relations"])) if "r:" + str(i) in passed],
        },
        "verified_cell_ids": sorted(required_cells - set(cell_errors)) if not global_errors else [],
        "verified_image_context_ids": sorted(required_context - set(context_errors)) if not global_errors else [],
        "verified_rule_ids": sorted(set(rules) - set(rule_errors)) if not global_errors else [],
        "verified_existing_object_refs": sorted(valid_existing) if not global_errors else [],
        "existing_objects_sha256": core.fingerprint(existing),
        "cell_review_sha256": core.fingerprint(cell_review),
        "crop_bindings_sha256": core.fingerprint(crop_bindings),
        "actual_image_ids_sha256": core.fingerprint(sorted(images)),
        "human_expert_review": False,
        "scientific_validation": False,
        "targets": {
            k: {
                **deepcopy(reqs[k]),
                "source_cell_ids": sorted(reqs[k].get("source_cell_ids", [])),
                "target_sha256": core.fingerprint(targets[k]),
                "dependencies": sorted(dependencies[k]),
                "rule_checks_sha256": core.fingerprint([rule_checks[rid] for rid in sorted(reqs[k]["rule_ids"])]),
                "cell_checks_sha256": core.fingerprint([cells[cid] for cid in sorted(reqs[k]["source_cell_ids"])]),
            }
            for k in sorted(passed)
        },
    }
    proof["proof_sha256"] = core.fingerprint(proof)
    if global_errors:
        # A stale binding never counts as current verification, even if its
        # old reviewer rows happen to look individually positive.
        for key in required_cells:
            cell_errors.setdefault(key, "; ".join(global_errors))
        for key in rules:
            rule_errors.setdefault(key, "; ".join(global_errors))
        for key in required_context:
            context_errors.setdefault(key, "; ".join(global_errors))
    return {
        "accepted": accepted,
        "program_mapping_review": proof,
        "rejected": [{"target": k, "reason": "; ".join(v)} for k, v in sorted(reasons.items())],
        "coverage": {
            "required_cells": len(required_cells),
            "verified_cells": len(required_cells - set(cell_errors)),
            "required_image_contexts": len(required_context),
            "verified_image_contexts": len(required_context - set(context_errors)),
            "required_rules": len(rules),
            "verified_rules": len(set(rules) - set(rule_errors)),
            "required_targets": len(targets),
            "verified_targets": len(passed),
            "outcome": "complete"
            if (
                passed
                and len(passed) == len(targets)
                and not global_errors
                and not rule_errors
                and not cell_errors
                and not context_errors
                and not compiled.get("diagnostics")
                and not compiled.get("coverage", {}).get("unresolved_cell_ids")
            )
            else "partial",
            "cell_gaps": [{"cell_id": k, "reason": v} for k, v in sorted(cell_errors.items())],
            "rule_gaps": [{"rule_id": k, "reason": v} for k, v in sorted(rule_errors.items())],
            "context_gaps": [{"block_id": k, "reason": v} for k, v in sorted(context_errors.items())],
            "compiler_coverage": deepcopy(compiled.get("coverage")),
            "compiler_diagnostics": deepcopy(compiled.get("diagnostics")),
        },
    }
