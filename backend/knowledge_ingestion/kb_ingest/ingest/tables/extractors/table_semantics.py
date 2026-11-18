"""Shared deterministic cell semantics for every supported table layout.

This module never evaluates formulas, assigns a unit not present in the source,
or turns missing/ambiguous/error markers into measurements.
"""

import math
import re
from copy import deepcopy
from decimal import Decimal

from kb_ingest.contract import (
    FIELDS,
    _append,
    _profile,
    add_assertion,
    add_coverage,
    add_evidence,
    add_issue,
    add_object,
)
from kb_ingest.core import fingerprint

from .table_orientation import original_cell

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
ERROR_VALUES = {"#DIV/0!", "#N/A", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!", "#SPILL!", "#CALC!"}
NOT_TESTED = {"not tested", "not measured", "not determined", "not reported", "未测", "未测试", "未报告"}
AMBIGUOUS = {"nd", "n.d.", "n/a", "na", "n.a.", "—", "–", "-", "--", "…", "...", "?"}


def _decimal_number(text):
    """Keep decimal meaning exact; JSON numeric views must round-trip losslessly."""
    number = Decimal(text)
    parsed = None
    # Bound exponent expansion as well as JSON consumers' finite numeric range.
    if number.is_finite() and number.adjusted() <= 308:
        if number == number.to_integral_value():
            parsed = int(number)
        else:
            candidate = float(number)
            if math.isfinite(candidate) and Decimal(str(candidate)) == number:
                parsed = candidate
    return {"parsed": parsed, "parsed_decimal": str(number)}


def classify_cell(cell, *, interpret_missing=True):
    raw = cell["raw_value"]
    if "formula" in cell or cell.get("value_type") in ("f", "formula", "array_formula"):
        return {"status": "formula_requires_review", "parsed": None}
    if cell.get("value_type") in ("e", "error", "excel_error") or (isinstance(raw, str) and raw in ERROR_VALUES):
        return {"status": "excel_error", "parsed": None}
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"status": "source_blank", "parsed": None}
    if isinstance(raw, str):
        normalized = raw.strip().casefold()
        # CSV has no formula cell type. In measurement/quantity contexts an
        # explicit expression cannot establish an observed value. Identity and
        # name callers opt out, preserving equation-like names as literal text.
        if interpret_missing and raw.lstrip().startswith("="):
            return {"status": "formula_requires_review", "parsed": None}
        if interpret_missing and normalized in NOT_TESTED:
            return {"status": "source_not_tested", "parsed": None}
        if interpret_missing and normalized in AMBIGUOUS:
            return {"status": "ambiguous_placeholder", "parsed": None}
        if re.fullmatch(NUMBER, raw.strip()):
            return {"status": "value", **_decimal_number(raw.strip())}
        interval = re.fullmatch(rf"\s*({NUMBER})\s*(?:to|–|~|\.\.)\s*({NUMBER})\s*", raw, re.I)
        if interval:
            lo, hi = map(Decimal, interval.groups())
            if lo.is_finite() and hi.is_finite() and lo <= hi:
                ends = [_decimal_number(v) for v in interval.groups()]
                result = {"status": "value", "parsed": None, "decimal_interval": {"lower": str(lo), "upper": str(hi)}}
                if all(v["parsed"] is not None for v in ends):
                    result["interval"] = {"lower": ends[0]["parsed"], "upper": ends[1]["parsed"]}
                return result
    if type(raw) is int or (type(raw) is float and math.isfinite(raw)):
        return {"status": "value", "parsed": raw}
    return {"status": "value", "parsed": None}


def classify_numeric_cell(cell):
    """A curve point requires a scalar numeric view, unlike a categorical field."""
    result = classify_cell(cell)
    if result["status"] == "value" and result["parsed"] is None:
        result["status"] = "numeric_precision_requires_review" if "parsed_decimal" in result else "unparsed_numeric"
    return result


def binding(asset, table, cell, role, **extra):
    cell = original_cell(cell)
    return {
        "role": role,
        "asset_ref": asset["asset_id"],
        "sheet": table["sheet"],
        "row": cell["row"],
        "column": cell["column"],
        **extra,
    }


def identity_context(asset, table, cells):
    return {
        "identity_values": [c["raw_value"] for c in cells],
        "cell_bindings": [binding(asset, table, c, "identity", value_index=i) for i, c in enumerate(cells)],
    }


def merge_object(package, kind, key, label, evidence, context, **extra):
    """Stable explicit identity, with all original key evidence kept on reuse."""
    oid = f"{package['header']['source_id']}:object:{fingerprint([kind, key])[:20]}"
    old = next((o for o in package["objects"] if o["object_id"] == oid), None)
    if old is None:
        return add_object(
            package,
            kind,
            label,
            object_id=oid,
            scope_key=fingerprint(key),
            evidence_refs=list(evidence),
            source_context=deepcopy(context),
            **extra,
        )
    for name in ("owner_ref", "material_ref"):
        if name in extra and old.get(name) != extra[name]:
            raise ValueError("An explicit object key points to incompatible owners/materials")
    if old.get("source_context", {}).get("identity_values") != context.get("identity_values"):
        raise ValueError("An explicit object key has incompatible source identity values")
    old["evidence_refs"] = list(dict.fromkeys(old["evidence_refs"] + list(evidence)))
    bindings = old.setdefault("source_context", {}).setdefault("cell_bindings", [])
    for item in context.get("cell_bindings", []):
        if item not in bindings:
            bindings.append(deepcopy(item))
    return oid


def cell_evidence(package, asset, table, cell):
    from .experimental import _anchor

    return add_evidence(
        package,
        asset["asset_id"],
        _anchor(table, [cell["row"], cell["row"]], [cell["column"]]),
        {"cells": [original_cell(cell)]},
        "table_cells",
    )


def _recognized_header_unit(text):
    """Recognize common unit expressions, never arbitrary parenthetical prose.

    Unlisted units remain unknown unless a profile binds a dedicated source
    unit row/column. This is syntax recognition only, with no conversion.
    """
    text = text.strip()
    if text in {
        "1",
        "%",
        "wt%",
        "wt.%",
        "vol%",
        "vol.%",
        "at%",
        "at.%",
        "mol%",
        "mass%",
        "phr",
        "ppm",
        "ppb",
        "count",
        "counts",
        "a.u.",
        "arb. units",
        "dimensionless",
    }:
        return True
    bases = {
        "m",
        "g",
        "s",
        "A",
        "K",
        "mol",
        "cd",
        "Hz",
        "N",
        "Pa",
        "J",
        "W",
        "C",
        "V",
        "F",
        "Ω",
        "Ohm",
        "S",
        "Wb",
        "T",
        "H",
        "lm",
        "lx",
        "Bq",
        "Gy",
        "Sv",
        "kat",
        "L",
        "l",
        "eV",
    }
    other = {
        "min",
        "mins",
        "minute",
        "minutes",
        "sec",
        "secs",
        "second",
        "seconds",
        "h",
        "hr",
        "hour",
        "hours",
        "d",
        "day",
        "days",
        "bar",
        "atm",
        "cal",
        "erg",
        "dyn",
        "°C",
        "°F",
        "%",
    }
    normalized = text.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-"))
    factors = re.split(r"\s*[*/·⋅]\s*|\s+", normalized)
    for factor in factors:
        match = re.fullmatch(r"([A-Za-zµμΩ°%]+)(?:\^?[+-]?\d+)?", factor)
        if not match:
            return False
        atom = match.group(1)
        if atom in bases | other:
            continue
        prefixed = re.fullmatch(r"(?:da|[YZEPTGMkhdcmµμunpfazy])(.+)", atom)
        if not prefixed or prefixed.group(1) not in bases:
            return False
    return bool(factors)


_DELIMITED_UNIT_TOKENS = {
    "Hz",
    "kHz",
    "MHz",
    "GHz",
    "Ohm",
    "kOhm",
    "MOhm",
    "Ω",
    "kΩ",
    "MΩ",
    "Pa",
    "kPa",
    "MPa",
    "GPa",
    "eV",
    "keV",
    "MeV",
    "ppm",
    "ppb",
    "wt%",
    "wt.%",
    "vol%",
    "vol.%",
    "at%",
    "at.%",
    "mol%",
    "°C",
    "°F",
}


def _trailing_underscore_unit(text):
    """Return only an unambiguous, complete source token after a separator."""
    if not isinstance(text, str):
        return None
    stem = text.rstrip()
    parenthetical = re.search(r"[（(]([^()（）]+)[）)]\s*$", stem)
    if parenthetical:
        # A trailing prose note may qualify the header; do not infer past it.
        if not _recognized_header_unit(parenthetical.group(1)):
            return None
        stem = stem[: parenthetical.start()].rstrip()
    match = re.search(r"_([^_\s()（）]+)$", stem)
    if not match or match.start() == 0 or stem[match.start() - 1] == "_":
        return None
    token = match.group(1)
    return token if token in _DELIMITED_UNIT_TOKENS and _recognized_header_unit(token) else None


def resolve_unit(asset, table, column, header_cell=None, unit_cells=(), role="unit"):
    """Compare all declared source locations; no precedence hides disagreement."""
    candidates, bindings, invalid = [], [], False
    for cell in unit_cells:
        status = classify_cell(cell)["status"]
        if status == "source_blank":
            continue
        bindings.append(binding(asset, table, cell, role))
        if status != "value" or not isinstance(cell["raw_value"], str) or cell["raw_value"].startswith("="):
            invalid = True
        else:
            candidates.append(cell["raw_value"])
    if header_cell and isinstance(header_cell["raw_value"], str):
        match = re.search(r"[（(]([^()（）]+)[）)]\s*$", header_cell["raw_value"])
        if match and _recognized_header_unit(match.group(1)):
            if classify_cell(header_cell)["status"] != "value":
                invalid = True
            candidates.append(match.group(1))
            bindings.append(binding(asset, table, header_cell, role, extraction="trailing_parentheses"))
        token = _trailing_underscore_unit(header_cell["raw_value"])
        if token:
            if classify_cell(header_cell)["status"] != "value":
                invalid = True
            candidates.append(token)
            bindings.append(binding(asset, table, header_cell, role, extraction="trailing_underscore_unit"))
    invalid = invalid or len({fingerprint(v) for v in candidates}) > 1
    return {
        "unit": None if invalid or not candidates else candidates[0],
        "ambiguous": invalid,
        "candidates": candidates,
        "bindings": bindings,
    }


def resolve_qualifiers(table, row, spec, used):
    from .experimental import _cell, _column

    qualifiers = deepcopy(spec.get("qualifiers", {}))
    cells = []
    for name, col in spec.get("qualifier_columns", {}).items():
        cell = _cell(table, row, _column(col))
        used.add((row, cell["column"]))
        if classify_cell(cell, interpret_missing=False)["status"] != "value":
            raise ValueError("Dynamic field qualifiers require unambiguous source cells")
        value = cell["raw_value"]
        if name in qualifiers and qualifiers[name] != value:
            raise ValueError("Static and source dynamic qualifiers disagree")
        qualifiers[name] = value
        cells.append(cell)
    return qualifiers, cells


def sheet_qualifier_bindings(package, asset, table, spec, qualifiers):
    """Bind declared static qualifiers to exact original worksheet-title spans."""
    bindings, refs = {}, []
    transcription = next(
        (
            e["evidence_id"]
            for e in package["evidence"]
            if e.get("content_kind") == "table_transcription"
            and e.get("asset_ref") == asset["asset_id"]
            and e.get("anchor", {}).get("sheet") == table.get("sheet")
        ),
        None,
    )
    for name, source in spec.get("qualifier_metadata_sources", {}).items():
        span = source.get("span") if isinstance(source, dict) else None
        title = table.get("sheet")
        if (
            not isinstance(source, dict)
            or source.get("kind") != "sheet_name"
            or transcription is None
            or table.get("format") != "xlsx"
            or not isinstance(title, str)
            or not isinstance(span, list)
            or len(span) != 2
            or any(type(value) is not int for value in span)
            or not 0 <= span[0] < span[1] <= len(title)
            or not isinstance(qualifiers.get(name), str)
            or title[span[0] : span[1]] != qualifiers[name]
            or name in spec.get("qualifier_columns", {})
        ):
            raise ValueError("Declared sheet-name qualifier does not match original source metadata")
        ref = add_evidence(
            package,
            asset["asset_id"],
            {"format": "xlsx", "sheet": title, "metadata_key": "sheet_name", "span": deepcopy(span)},
            {"raw_title": title, "selected_value": qualifiers[name]},
            "table_sheet_metadata",
        )
        bindings[name] = {
            "kind": "sheet_name",
            "asset_ref": asset["asset_id"],
            "sheet": title,
            "span": deepcopy(span),
            "evidence_ref": ref,
            "transcription_ref": transcription,
        }
        refs.extend([transcription, ref])
    return bindings, refs


def assess_cell(package, target, field_id, owner_kind, qualifiers, cell, evidence, context, status=None):
    status = status or classify_cell(cell)["status"]
    _append(
        package,
        "assessments",
        {
            "assessment_id": f"{package['header']['source_id']}:absence:"
            f"{fingerprint([target, field_id, qualifiers, context])[:20]}",
            "target_ref": target,
            "field_id": field_id,
            "profile_id": _profile(package, field_id, owner_kind, qualifiers),
            "qualifiers": qualifiers,
            "handling": status,
            "evidence_refs": evidence,
            "source_context": context,
            "raw_value": cell["raw_value"],
            "reason": (
                "Original source cell retained; missing, ambiguous, error or formula content is not a measurement"
            ),
        },
    )


def scalar_assertion(
    package,
    asset,
    table,
    profile,
    row,
    spec,
    target,
    *,
    evidence=(),
    used=None,
    statement_kind=None,
    assessment_owner_kind=None,
    header_cell=None,
):
    """Create one source-grounded field or assessment with common quality rules."""
    from .experimental import _cell, _column

    used = used if used is not None else set()
    col = _column(spec["column"])
    cell = _cell(table, row, col)
    if cell.get("_absent_original"):
        original = original_cell(cell)
        add_issue(
            package,
            "Mapped value field is physically absent from a ragged source row; no cell was invented",
            [target],
            list(evidence),
            code="absent_original_field",
        )
        add_coverage(
            package,
            {
                "asset_ref": asset["asset_id"],
                "sheet": table["sheet"],
                "row": original["row"],
                "column": original["column"],
            },
            "not_checked",
            "Mapped source field is absent; no measurement or original-cell claim created",
        )
        return None
    used.add((row, col))
    qualifiers, qualifier_cells = resolve_qualifiers(table, row, spec, used)
    metadata_bindings, metadata_refs = sheet_qualifier_bindings(package, asset, table, spec, qualifiers)
    from .table_headers import apply_metric, resolve_headers

    headers = resolve_headers(asset, table, profile, col, spec, row=row, header_cell=header_cell)
    if spec["field_id"] == "observation.value":
        apply_metric(qualifiers, headers)
    unit = headers["unit"]
    basis = _cell(table, row, _column(spec["basis_column"])) if spec.get("basis_column") is not None else None
    ev_cells = [
        c
        for c in [cell] + qualifier_cells + headers["cells"] + ([basis] if basis else [])
        if not c.get("_absent_original")
    ]
    used.update((c["row"], c["column"]) for c in ev_cells)
    evs = list(
        dict.fromkeys(list(evidence) + metadata_refs + [cell_evidence(package, asset, table, c) for c in ev_cells])
    )
    context = {
        "cell_bindings": [binding(asset, table, cell, "value")] + (unit["bindings"] if not unit["ambiguous"] else [])
    }
    context["header_context"] = [headers["context"]]
    if unit["ambiguous"]:
        context["unit_candidate_bindings"] = unit["bindings"]
    if basis and classify_cell(basis)["status"] == "value":
        context["cell_bindings"].append(binding(asset, table, basis, "basis"))
    if qualifier_cells:
        context["qualifier_bindings"] = {
            key: binding(asset, table, _cell(table, row, _column(column)), "qualifier")
            for key, column in spec.get("qualifier_columns", {}).items()
        }
    if metadata_bindings:
        context["qualifier_metadata_bindings"] = metadata_bindings
    obj = next(o for o in package["objects"] if o["object_id"] == target)
    classification = classify_cell(
        cell,
        interpret_missing=spec["field_id"].startswith(
            ("observation.", "composition.", "condition.", "process.parameter")
        ),
    )
    if FIELDS[spec["field_id"]]["value_kinds"] == ["text"]:
        # The catalogue, not a numeric-looking lexeme, determines whether a
        # field is a quantity. Preserve identifiers such as '001' and native
        # numeric name cells verbatim, without numeric or interval views.
        classification = {"status": classification["status"], "parsed": None}
    context["value_status"] = classification["status"]
    if classification["status"] != "value":
        assess_cell(
            package, target, spec["field_id"], assessment_owner_kind or obj["kind"], qualifiers, cell, evs, context
        )
        return None
    if "unit" in spec or "basis" in spec:
        raise ValueError("Units and composition basis must be bound to original source cells")
    basis_invalid = basis is not None and classify_cell(basis)["status"] not in ("value", "source_blank")
    raw_basis = basis["raw_value"] if basis and classify_cell(basis)["status"] == "value" else None
    aid = add_assertion(
        package,
        target,
        spec["field_id"],
        cell["raw_value"],
        evs,
        qualifiers=qualifiers,
        raw_unit=unit["unit"],
        raw_basis=raw_basis,
        value_kind=spec.get(
            "value_kind",
            "interval"
            if "decimal_interval" in classification
            else "scalar"
            if classification["parsed"] is not None or "parsed_decimal" in classification
            else "text",
        ),
        statement_kind=statement_kind or "source_recorded_metadata",
        source_context=context,
    )
    assertion = next(a for a in package["assertions"] if a["assertion_id"] == aid)
    assertion["value"]["parsed"] = classification["parsed"]
    if "parsed_decimal" in classification:
        assertion["value"]["parsed_decimal"] = classification["parsed_decimal"]
    if "interval" in classification:
        assertion["value"]["parsed_interval"] = classification["interval"]
    if "decimal_interval" in classification:
        assertion["value"]["parsed_decimal_interval"] = classification["decimal_interval"]
    if unit["ambiguous"] or basis_invalid:
        code = "ambiguous_row_unit" if unit["ambiguous"] else "ambiguous_source_basis"
        issue = add_issue(
            package,
            "Source unit/basis is ambiguous or formula-derived; no interpretation chosen",
            [target],
            evs,
            severity="error",
            code=code,
        )
        assertion["use_status"] = "blocked"
        assertion["issue_refs"].append(issue)
        assertion["source_context"]["unit_candidates"] = unit["candidates"]
    if headers["context"]["issues"]:
        issue = add_issue(
            package,
            "Header interpretation is conflicting or source-invalid; retained for review",
            [target],
            evs,
            severity="error",
            code="ambiguous_header_context",
        )
        assertion["use_status"] = "blocked"
        assertion["issue_refs"].append(issue)
    return aid


def record_unmapped(package, asset, table, used):
    remaining = [
        original_cell(c)["address"]
        for r in table["rows"]
        for c in r["cells"]
        if (c["row"], c["column"]) not in used and classify_cell(c)["status"] != "source_blank"
    ]
    scope = {"asset_ref": asset["asset_id"], "sheet": table["sheet"], "scope": "unmapped_cells"}
    previous = next((c for c in package["coverage"] if c["scope"] == scope), None)
    if previous:
        # Unmapped in the complete package means unused by every successful
        # profile, not unused by whichever profile happened to execute last.
        previously_unmapped = set(previous.get("cell_addresses", []))
        remaining = [address for address in remaining if address in previously_unmapped]
    add_coverage(
        package,
        scope,
        "not_checked" if remaining else "checked",
        "Source cells outside explicit semantic mappings remain uninterpreted"
        if remaining
        else "All nonblank source cells covered by declared mappings",
        cell_addresses=remaining,
    )


def record_scope_domain(scope):
    """The typed physical identity; mapped field blocks do not change it."""
    return {
        key: scope.get(key) for key in ("asset_ref", "sheet", "row_range", "key_column", "value_column", "label_key")
    }


def record_scope_identity(scope, cell_at):
    """Validate one enclosing record and its independently mapped field blocks.

    An enclosing record has exactly one source identity label. Its device or
    method subsections may repeat unrelated keys; each mapped field block must
    still have unambiguous keys. This is independent of caller run_key strings.
    """

    def extent(block):
        pair = block.get("row_range")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(type(row) is not int for row in pair)
            or pair[0] < 1
            or pair[1] < pair[0]
        ):
            raise ValueError("Record scope requires a positive ordered row_range")
        if pair[1] - pair[0] > 1_000_000:
            raise ValueError("Record scope exceeds supported extent")
        for key in ("key_column", "value_column"):
            if type(block.get(key)) is not int or block[key] < 1:
                raise ValueError("Record scope requires original key/value columns")
        return pair

    lo, hi = extent(scope)
    if scope.get("label_key") is None:
        raise ValueError("Record scope requires a source identity label_key")
    labels = [
        cell_at(row, scope["value_column"])
        for row in range(lo, hi + 1)
        if cell_at(row, scope["key_column"])["raw_value"] == scope["label_key"]
    ]
    if (
        len(labels) != 1
        or classify_cell(labels[0], interpret_missing=False)["status"] != "value"
        or classify_cell(cell_at(labels[0]["row"], scope["key_column"]), interpret_missing=False)["status"] != "value"
    ):
        raise ValueError("Enclosing record must contain exactly one nonformula source identity label")
    blocks = scope.get("field_blocks", [scope])
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Record scope must include declared field blocks")
    for block in blocks:
        start, end = extent(block)
        if any(block[key] != scope[key] for key in ("key_column", "value_column")):
            raise ValueError(
                (
                    "Cross-column record ownership is unsupported; field blocks require the same key/value "
                    "columns as their identity"
                )
            )
        if start < lo or end > hi:
            raise ValueError("Field block lies outside the declared enclosing record")
        seen = set()
        for row in range(start, end + 1):
            key, value = cell_at(row, block["key_column"]), cell_at(row, block["value_column"])
            if classify_cell(key)["status"] == classify_cell(value)["status"] == "source_blank":
                continue
            marker = fingerprint(key["raw_value"])
            if classify_cell(key, interpret_missing=False)["status"] != "value" or marker in seen:
                raise ValueError("Mapped field block has missing/formula/repeated metadata keys")
            seen.add(marker)
    return labels[0]
