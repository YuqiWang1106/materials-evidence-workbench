"""Source-bound candidate verification and evidence-token support checks."""

from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from kb_ingest.contract import FIELDS
from kb_ingest.core import fingerprint
from kb_ingest.document_source_values import source_cell_provenance_matches


def requires_image_review(block):
    """OCR and competing native readings need the same original-pixel gate."""
    return block.get("source") in ("mineru", "visual", "ocr") or block.get("requires_image_verification") is True


def _text(value):
    return re.sub(r"\s+", "", str(value)).replace("−", "-").replace("–", "-")


def _literal(value, quotes):
    numeric_text = isinstance(value, str) and re.fullmatch(
        r"[+\-−]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?", value.strip()
    )
    if (isinstance(value, (int, float)) and not isinstance(value, bool)) or numeric_text:
        # CJK prose normally has no spaces around measurements. Latin/scientific
        # identifiers and the interior of another number remain excluded.
        values = re.findall(r"(?<![A-Za-z0-9_.])[+\-−]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?", quotes)
        if numeric_text:
            return any(v.replace("−", "-") == value.strip().replace("−", "-") for v in values)
        try:
            return any(Decimal(v.replace("−", "-")) == Decimal(str(value).replace("−", "-")) for v in values)
        except InvalidOperation:
            return False
    literal = _text(value)
    if re.search(r"\d", literal) and re.fullmatch(r"[\d.,+\-eE×x*/±<>=≤≥%()\[\]:^]+", literal):
        return re.search(r"(?<![\d.])" + re.escape(literal) + r"(?![\d.])", _text(quotes)) is not None
    return bool(literal) and literal in _text(quotes)


def _unit_literal(unit, quotes):
    # Whitespace may be layout-only, but a prefix/exponent changes the unit.
    symbol = _text(unit)
    pattern = r"\s*".join(re.escape(char) for char in symbol)
    # Compare the same typography on both sides, without changing saved raw
    # units or erasing boundaries between a unit and following prose.
    source = str(quotes).replace("−", "-").replace("–", "-")
    return (
        bool(symbol)
        and re.search(r"(?<![A-Za-zµμ°])" + pattern + r"(?![A-Za-zµμ\d⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺^]|\s*[-+]\s*\d)", source) is not None
    )


def _numeric_fact(item):
    field = FIELDS.get(item.get("field_id"))
    if field:
        numeric_kinds = {
            "integer",
            "number",
            "scalar",
            "numeric_notation",
            "interval",
            "ratio",
            "statistics",
            "constraint",
        }
        if not numeric_kinds.intersection(field.get("value_kinds", [])):
            return False
        # Source-only metadata may retain dates/identifiers as their original
        # text, even where the catalogue also allows scalar years or intervals.
        # A numeric field on a scientific object does not gain this exception
        # by selecting value_kind=text; its full numeric boundary still applies.
        if item.get("value_kind") == "text" and field.get("owner_kinds") == ["source"]:
            return False
    value = item.get("raw_value")
    return (
        (isinstance(value, (int, float)) and not isinstance(value, bool))
        or item.get("value_kind") in ("scalar", "numeric_notation", "interval")
        or (isinstance(value, str) and bool(re.fullmatch(r"[\d\s.,+\-−–eE×x*/±<>=≤≥%()\[\]:^]+", value)))
    )


def evidence_token_supported(value, evidence, registry, *, kind, allow_prose_context=False, table_header_label=None):
    """Check the value at its quoted occurrence in the FULL source block.

    A copied substring such as Pa from MPa or 12 from 312 cannot establish its
    own boundaries. Legacy numeric JSON values may match equivalent complete
    source numerals; strings always preserve their source spelling/precision.
    """
    from kb_ingest.source_token_boundaries import reviewed_table_header_unit_supported, typed_evidence_supported

    for e in evidence:
        block = registry.get(e.get("block_id"), {})
        source = block.get("text")
        quote = e.get("quote")
        if not isinstance(source, str) or not isinstance(quote, str):
            continue
        literals = [value] if isinstance(value, str) else []
        if kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            literals = [
                token
                for token in re.findall(r"(?<![A-Za-z0-9_.])[+\-−]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?", quote)
                if _literal(value, token)
            ]
        if any(
            typed_evidence_supported(source, quote, literal, kind=kind, allow_prose_context=allow_prose_context)
            for literal in literals
        ):
            return True
        if (
            kind == "unit"
            and allow_prose_context is True
            and table_header_label is not None
            and block.get("kind") == "table_cell"
            and block.get("table_region")
            and isinstance(block.get("anchor", {}).get("table_cell"), dict)
            and reviewed_table_header_unit_supported(source, quote, value, table_header_label)
        ):
            return True
    return False


def _same_inherited_value(left, right):
    # No unit conversion, changed proportion basis, or interval normalization is
    # hidden inside inheritance. Scalar numeric representations may be equivalent.
    if any(left.get(k) != right.get(k) for k in ("field_id", "raw_unit", "raw_basis", "qualifiers")):
        return False
    lv, rv = left.get("raw_value"), right.get("raw_value")
    if isinstance(lv, bool) or isinstance(rv, bool):
        return type(lv) is type(rv) and lv == rv
    if isinstance(lv, str) or isinstance(rv, str):
        return isinstance(lv, str) and isinstance(rv, str) and _text(lv) == _text(rv)
    try:
        return Decimal(str(lv).replace("−", "-")) == Decimal(str(rv).replace("−", "-"))
    except InvalidOperation:
        return _text(lv) == _text(rv)


def review_targets(candidate):
    return {
        **{"o:" + x["key"]: x for x in candidate["objects"]},
        **{"f:" + x["key"]: x for x in candidate["facts"]},
        **{"r:" + str(i): x for i, x in enumerate(candidate["relations"])},
    }


def verify_candidate(
    candidate,
    blocks,
    review,
    *,
    visual_reviewed=None,
    existing_objects=None,
    target_block_ids=None,
    semantically_reviewed_units=None,
    include_direct_rejections=False,
):
    """Independent source review AND deterministic checks; absent review fails closed.

    Direct review may explicitly certify unit completeness for exact ``f:key``
    targets. This disambiguates prose or a fully labelled table-header boundary,
    never a known truncated numeric/unit expression.
    """
    for section in ("objects", "facts"):
        keys = [x["key"] for x in candidate[section]]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate candidate identity")
    if len(blocks) != len({b["block_id"] for b in blocks}):
        raise ValueError("duplicate source region identity")
    registry = {b["block_id"]: b for b in blocks}
    visual_reviewed = visual_reviewed or set()
    semantically_reviewed_units = set(semantically_reviewed_units or ())
    targets_allowed = None if target_block_ids is None else set(target_block_ids)
    if targets_allowed is not None and not targets_allowed <= set(registry):
        raise ValueError("unknown extraction target source region")
    existing = {o["key"]: o for o in existing_objects or []}
    objects = {**existing, **{o["key"]: o for o in candidate["objects"]}}
    identity_conflicts = {
        o["key"]
        for o in candidate["objects"]
        if o["key"] in existing
        and any(
            o.get(k) != existing[o["key"]].get(k)
            for k in ("kind", "label", "scope", "source_identifier", "owner_key", "material_key")
        )
    }
    checks = {}
    for x in review.get("checks", []):
        if x.get("target") in checks:
            checks[x["target"]] = {"verdict": "uncertain", "reason": "duplicate review target"}
        else:
            checks[x.get("target")] = x
    rejected = []
    passed = set()
    fact_index = {f["key"]: f for f in candidate["facts"]}

    def supports(evidence):
        return bool(evidence) and all(
            e.get("block_id") in registry
            and e.get("quote")
            and _text(e["quote"]) in _text(registry[e["block_id"]]["text"])
            for e in evidence
        )

    for key, item in review_targets(candidate).items():
        check = checks.get(key, {})
        reason = None
        if key.startswith("o:") and item["key"] in existing:
            reason = (
                "existing object identity cannot be changed"
                if item["key"] in identity_conflicts
                else "existing identity must be referenced, not redeclared"
            )
        elif check.get("verdict") != "supported":
            reason = check.get("reason", "independent semantic review absent")
        elif not supports(item.get("evidence")) or not supports(check.get("evidence")):
            reason = "review or candidate evidence is not literal source content"
        elif not {e["block_id"] for e in item["evidence"]} & {e["block_id"] for e in check["evidence"]}:
            reason = "review does not support the cited source region"
        elif targets_allowed is not None and not {e["block_id"] for e in item["evidence"]} & targets_allowed:
            reason = "context-only source cannot create a new extraction target"
        elif any(
            requires_image_review(registry[e["block_id"]]) and e["block_id"] not in visual_reviewed
            for e in item["evidence"]
        ):
            reason = "source reading requires original image review"
        elif key.startswith("f:"):
            field = FIELDS.get(item.get("field_id"), {})
            field_valid = objects.get(item.get("object_key"), {}).get("kind") in field.get(
                "owner_kinds", []
            ) and item.get("value_kind") in field.get("value_kinds", [])
            quotes = " ".join(e["quote"] for e in item["evidence"])
            metric_labels = [
                q.get("value")
                for q in item.get("qualifiers", [])
                if isinstance(q, dict) and q.get("key") == "metric_label"
            ]
            metric_label = metric_labels[0] if len(metric_labels) == 1 else None
            inherited = item.get("formation") == "explicit_inheritance"
            marker = item.get("program_value_source")
            selected = registry.get(item.get("value_cell"))
            program_value_ok = source_cell_provenance_matches(item, selected)
            numeric_table_value = (
                _numeric_fact(item)
                and not inherited
                and any(
                    registry[e["block_id"]]["kind"] in ("table", "table_cell")
                    and _literal(item.get("raw_value"), e["quote"])
                    for e in item["evidence"]
                )
                and not any(
                    registry[e["block_id"]]["kind"] not in ("table", "table_cell")
                    and _literal(item.get("raw_value"), e["quote"])
                    for e in item["evidence"]
                )
            )
            if not field_valid:
                reason = "field contract does not permit the field, owner kind or value kind"
            elif (marker or item.get("value_cell") or numeric_table_value) and not program_value_ok:
                reason = "table value requires a valid program-read cell selector and provenance"
            elif inherited and (
                not item.get("input_fact_keys")
                or not any(
                    source in fact_index and _same_inherited_value(item, fact_index[source])
                    for source in item.get("input_fact_keys", [])
                )
            ):
                reason = "inherited value, field, unit and basis must match an explicit input fact"
            elif (
                not inherited
                and (item.get("formation", "direct") in ("direct", "explicit_override") or _numeric_fact(item))
                and not _literal(item.get("raw_value"), quotes)
            ):
                reason = "raw value is not literal in supporting evidence"
            elif (
                not inherited
                and _numeric_fact(item)
                and not evidence_token_supported(item.get("raw_value"), item["evidence"], registry, kind="number")
            ):
                reason = "numeric value is not a complete token at the original source quote"
            elif item.get("raw_unit") and (
                not _unit_literal(item["raw_unit"], quotes)
                or not evidence_token_supported(
                    item["raw_unit"],
                    item["evidence"],
                    registry,
                    kind="unit",
                    allow_prose_context=key in semantically_reviewed_units,
                    table_header_label=metric_label,
                )
            ):
                reason = "unit is not a complete literal token at the original source quote"
            elif item.get("raw_basis") and _text(item["raw_basis"]) not in _text(quotes):
                reason = "proportion basis is not literal in supporting evidence"
        if reason:
            rejected.append({"target": key, "reason": reason})
        else:
            passed.add(key)
    direct_rejected = deepcopy(rejected) if include_direct_rejections else None
    # Dependents cannot survive an unverified subject or inheritance chain.
    known_objects = {o["key"] for o in candidate["objects"] if "o:" + o["key"] in passed}
    known_objects |= set(existing) - identity_conflicts
    while True:
        removed = set()
        for obj in candidate["objects"]:
            if obj["key"] not in known_objects:
                continue
            if any(obj.get(k) and obj[k] not in known_objects for k in ("owner_key", "material_key")):
                removed.add(obj["key"])
        if not removed:
            break
        known_objects -= removed
    valid_facts = {
        f["key"] for f in candidate["facts"] if "f:" + f["key"] in passed and f["object_key"] in known_objects
    }
    resolved = {f["key"] for f in candidate["facts"] if f["key"] in valid_facts and not f.get("input_fact_keys")}
    while True:
        new = {
            f["key"]
            for f in candidate["facts"]
            if f["key"] in valid_facts and f.get("input_fact_keys") and set(f["input_fact_keys"]) <= resolved
        } - resolved
        if not new:
            break
        resolved |= new
    result = {
        "objects": [o for o in candidate["objects"] if o["key"] in known_objects and o["key"] not in existing],
        "facts": [f for f in candidate["facts"] if f["key"] in resolved],
        "relations": [
            r
            for i, r in enumerate(candidate["relations"])
            if "r:" + str(i) in passed and r["from_key"] in known_objects and r["to_key"] in known_objects
        ],
        "issues": candidate.get("issues", []),
    }
    retained = {
        *("o:" + o["key"] for o in result["objects"]),
        *("f:" + f["key"] for f in result["facts"]),
        *("r:" + str(i) for i, r in enumerate(candidate["relations"]) if r in result["relations"]),
    }
    for key in review_targets(candidate):
        if key not in retained and not any(r["target"] == key for r in rejected):
            rejected.append({"target": key, "reason": "unverified subject, relation or inheritance dependency"})
    return result, {
        "candidate_sha256": fingerprint(candidate),
        "review_sha256": fingerprint(review),
        "rejected": rejected,
        "omissions": review.get("omissions", []),
        "review_kind": "model_source_review",
        "human_expert_review": False,
        **({"direct_rejected": direct_rejected} if include_direct_rejections else {}),
    }
