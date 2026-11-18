"""Shared scientific-record guards, independent of table layout or material names.

Parsing proves cell content, not scientific meaning. These guards verify declared
direct bindings and prohibit incompatible facts from being joined as one event.
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from kb_ingest.contract import ContractError, add_issue, strict_equal
from kb_ingest.core import fingerprint
from kb_ingest.scientific_subjects import ACTIVITY_KINDS, enforce_exclusive_ownership, subject_kinds

from .temporal_identity import temporal_identity as _temporal_identity

QUALITY_VERSION = "tabular-quality-1"
SAMPLE_TEST_FORWARD = frozenset({"tested_by"})
SAMPLE_TEST_REVERSE = frozenset({"tests_subject", "characterizes"})
NON_VALUES = {
    "source_blank",
    "source_not_tested",
    "ambiguous_placeholder",
    "excel_error",
    "formula_requires_review",
    "unreadable",
    "extraction_failed",
}
NON_VALUES |= {"numeric_precision_requires_review", "unparsed_numeric", "ambiguous_units"}


def inactive(item):
    return any(
        item.get(key) in ("blocked", "withdrawn", "rejected", "superseded")
        for key in ("use_status", "lifecycle_status")
    )


def _tables(package):
    cells, tables = {}, {}
    for ev in package["evidence"]:
        if ev.get("content_kind") != "table_transcription":
            continue
        t, anchor = ev["content"], ev["anchor"]
        if not isinstance(t, dict) or not isinstance(t.get("rows"), list):
            raise ContractError("Malformed table transcription")
        key = (ev["asset_ref"], t.get("sheet"))
        if anchor.get("sheet") != t.get("sheet"):
            raise ContractError("Table anchor disagrees with source sheet")
        if t["rows"] and anchor.get("row_range") != [1, t["nrows"]]:
            raise ContractError("Table anchor disagrees with source extent")
        if key in tables and not strict_equal(tables[key], t):
            raise ContractError("Conflicting transcriptions for one original sheet")
        tables[key] = t
        previous = 0
        for row in t["rows"]:
            number = row.get("row")
            if type(number) is not int or number <= previous or number > t["nrows"]:
                raise ContractError("Invalid original table row identity")
            previous = number
            columns = set()
            for cell in row["cells"]:
                col = cell.get("column")
                if cell.get("row") != number or type(col) is not int or col < 1 or col > t["ncols"] or col in columns:
                    raise ContractError("Invalid original cell identity")
                columns.add(col)
                cells[key + (number, col)] = cell
    # Every displayed evidence excerpt must agree with the retained table, not
    # only the assertion's direct binding. Otherwise a correct value could be
    # accompanied by a misleading snippet or physical-line link.
    evidence_index = {e["evidence_id"]: e for e in package["evidence"]}
    table_rows = {key: {r["row"]: r for r in t["rows"]} for key, t in tables.items()}
    for ev in package["evidence"]:
        if ev.get("content_kind") == "table_sheet_metadata":
            anchor, content = ev.get("anchor"), ev.get("content")
            if not isinstance(anchor, dict) or not isinstance(content, dict):
                raise ContractError("Malformed worksheet metadata evidence")
            key = (ev.get("asset_ref"), anchor.get("sheet"))
            table = tables.get(key)
            span = anchor.get("span")
            title = table.get("sheet") if table else None
            if (
                table is None
                or table.get("format") != "xlsx"
                or anchor.get("format") != "xlsx"
                or anchor.get("metadata_key") != "sheet_name"
                or set(anchor) != {"format", "sheet", "metadata_key", "span"}
                or not isinstance(title, str)
                or not isinstance(span, list)
                or len(span) != 2
                or any(type(n) is not int for n in span)
                or not 0 <= span[0] < span[1] <= len(title)
                or set(content) != {"raw_title", "selected_value"}
                or content["raw_title"] != title
                or content["selected_value"] != title[span[0] : span[1]]
            ):
                raise ContractError("Worksheet metadata evidence differs from retained original title")
            continue
        if ev.get("content_kind") not in ("table_transcription", "table_cells", "table_row"):
            continue
        anchor = ev["anchor"]
        key = (ev["asset_ref"], anchor.get("sheet"))
        if key not in tables:
            raise ContractError("Cell evidence has no retained source table")
        table = tables[key]
        if anchor.get("format") != table.get("format"):
            raise ContractError("Evidence format disagrees with source table")
        if table["rows"] and "physical_lines" in anchor:
            by_row = table_rows[key]
            try:
                lo, hi = anchor["row_range"]
                expected_lines = [by_row[lo]["physical_lines"][0], by_row[hi]["physical_lines"][1]]
            except (KeyError, TypeError, ValueError) as exc:
                raise ContractError("CSV evidence line range cannot be resolved") from exc
            if anchor["physical_lines"] != expected_lines:
                raise ContractError("CSV evidence physical lines disagree with original rows")
        if ev["content_kind"] == "table_transcription":
            continue

        def inspect(content):
            if isinstance(content, dict):
                if {"row", "column", "raw_value"} <= content.keys():
                    original = cells.get(key + (content["row"], content["column"]))
                    if original is None or not strict_equal(original, content):
                        raise ContractError("Copied cell evidence differs from table transcription")
                    _supported(
                        package,
                        {"evidence_refs": [ev["evidence_id"]]},
                        {"asset_ref": key[0], "sheet": key[1], "column": content["column"]},
                        [content["row"]],
                        evidence_index,
                    )
                else:
                    for value in content.values():
                        inspect(value)
            elif isinstance(content, list):
                for value in content:
                    inspect(value)

        inspect(ev["content"])
        if isinstance(ev["content"], dict) and "columns" in ev["content"]:
            from openpyxl.utils import column_index_from_string

            lo, hi = anchor["row_range"]
            for label, values in ev["content"]["columns"].items():
                col = column_index_from_string(label)
                original = [cells.get(key + (r, col), {}).get("raw_value") for r in range(lo, hi + 1)]
                if col not in anchor["columns"] or not strict_equal(original, values):
                    raise ContractError("Series excerpt differs from original source cells")
    return cells, tables


def _check_qualifier_metadata(item, tables, evidence):
    """Require a native title proof for each declared metadata qualifier."""
    context = item.get("source_context", {})
    bindings = context.get("qualifier_metadata_bindings", {})
    if not isinstance(bindings, dict):
        raise ContractError("Qualifier metadata bindings must be an object")
    referenced = {
        ref
        for ref in item.get("evidence_refs", [])
        if evidence.get(ref, {}).get("content_kind") == "table_sheet_metadata"
    }
    if referenced != {b.get("evidence_ref") for b in bindings.values() if isinstance(b, dict)}:
        raise ContractError("Worksheet metadata evidence and qualifier bindings disagree")
    for name, binding in bindings.items():
        if not isinstance(binding, dict) or set(binding) != {
            "kind",
            "asset_ref",
            "sheet",
            "span",
            "evidence_ref",
            "transcription_ref",
        }:
            raise ContractError("Malformed qualifier metadata binding")
        key = (binding["asset_ref"], binding["sheet"])
        table = tables.get(key)
        title = table.get("sheet") if table else None
        span = binding["span"]
        metadata = evidence.get(binding["evidence_ref"], {})
        transcription = evidence.get(binding["transcription_ref"], {})
        if (
            binding["kind"] != "sheet_name"
            or table is None
            or table.get("format") != "xlsx"
            or not isinstance(title, str)
            or not isinstance(span, list)
            or len(span) != 2
            or any(type(n) is not int for n in span)
            or not 0 <= span[0] < span[1] <= len(title)
            or item.get("qualifiers", {}).get(name) != title[span[0] : span[1]]
            or name in context.get("qualifier_bindings", {})
            or binding["evidence_ref"] not in item.get("evidence_refs", [])
            or binding["transcription_ref"] not in item.get("evidence_refs", [])
            or metadata.get("content_kind") != "table_sheet_metadata"
            or metadata.get("asset_ref") != key[0]
            or metadata.get("anchor", {}).get("sheet") != key[1]
            or metadata.get("anchor", {}).get("span") != span
            or transcription.get("content_kind") != "table_transcription"
            or transcription.get("asset_ref") != key[0]
            or transcription.get("anchor", {}).get("sheet") != key[1]
        ):
            raise ContractError("Qualifier worksheet metadata binding is not supported by original evidence")
        if any((cell.get("asset_ref"), cell.get("sheet")) != key for cell in context.get("cell_bindings", [])):
            raise ContractError("Qualifier worksheet metadata belongs to a different source table")


def _binding_rows(binding):
    if "rows" in binding:
        rows = binding["rows"]
    elif "row_range" in binding:
        lo, hi = binding["row_range"]
        if type(lo) is not int or type(hi) is not int or lo < 1 or hi < lo:
            raise ContractError("Invalid cell binding extent")
        if hi - lo > 1_000_000:
            raise ContractError("Cell binding exceeds supported extent")
        rows = list(range(lo, hi + 1))
    else:
        rows = [binding.get("row")]
    if not isinstance(rows, list) or not rows or any(type(r) is not int or r < 1 for r in rows):
        raise ContractError("Invalid cell binding rows")
    return rows


def _transform(raw, binding):
    extraction = binding.get("extraction")
    if extraction in (None, "raw"):
        return raw
    if extraction == "trailing_parentheses":
        match = re.search(r"[（(]([^()（）]+)[）)]\s*$", raw) if isinstance(raw, str) else None
        return match.group(1) if match else None
    if extraction == "trailing_underscore_unit":
        from .extractors.table_semantics import _trailing_underscore_unit

        token = _trailing_underscore_unit(raw)
        if token is None:
            raise ContractError("Declared source unit token is not present in header")
        return token
    if extraction == "regex_group":
        if not isinstance(raw, str):
            raise ContractError("Text extraction requires a text cell")
        match = re.search(binding["pattern"], raw)
        if not match:
            raise ContractError("Declared source extraction has no match")
        return match.group(binding["group"])
    raise ContractError("Unregistered cell extraction rule")


def _supported(package, item, binding, rows, evidence=None):
    if evidence is None:
        evidence = {e["evidence_id"]: e for e in package["evidence"]}
    for row in rows:
        supported = False
        for eid in item.get("evidence_refs", []):
            ev = evidence.get(eid)
            if not ev or ev.get("asset_ref") != binding["asset_ref"]:
                continue
            a = ev.get("anchor", {})
            if a.get("sheet") != binding.get("sheet"):
                continue
            extent = a.get("row_range", [a.get("row", 0), a.get("row", 0)])
            cols = a.get("columns", [a.get("column")])
            if len(extent) == 2 and extent[0] <= row <= extent[1] and binding["column"] in cols:
                supported = True
                break
        if not supported:
            raise ContractError("Cell binding is outside its supporting evidence")


def _check_header_context(item, cells, evidence):
    """New semantic labels carry the same immutable original-cell obligation."""
    from .extractors.table_headers import classify_header_cell
    from .extractors.table_semantics import classify_cell

    for context in item.get("source_context", {}).get("header_context", []):
        for entry in [*context.get("header_path", []), *context.get("scope_cells", []), *context.get("roles", [])]:
            try:
                source = entry["source"]
                cell = cells[(source["asset_ref"], source.get("sheet"), source["row"], source["column"])]
                if (
                    not strict_equal(entry["raw_value"], cell["raw_value"])
                    or entry["source_cell"] != cell["address"]
                    or entry["status"] != classify_header_cell(cell)["status"]
                ):
                    raise ContractError("Header context differs from original file transcription")
                _supported(None, item, source, [source["row"]], evidence=evidence)
                if "role" in entry and (
                    entry["role"] not in ("material", "stage", "metric", "unit", "note")
                    or context["column"] not in entry["columns"]
                ):
                    raise ContractError("Header role has invalid applicability")
            except ContractError:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise ContractError("Header context has invalid original-cell evidence") from error
        unit = context.get("unit", {})
        if "value" in item:
            expected_unit = (
                item.get("series_data", {}).get("x_unit")
                if context.get("axis") == "x"
                else item["value"].get("raw_unit")
            )
            if not strict_equal(unit.get("value"), expected_unit):
                raise ContractError(
                    "Header unit projection: Bound source cell does not equal the declared assertion unit"
                )
        unit_role = "x_unit" if context.get("axis") == "x" else "unit"
        sources = item.get("source_context", {})
        bindings = [
            b
            for b in sources.get("cell_bindings", []) + sources.get("unit_candidate_bindings", [])
            if b.get("role") == unit_role
        ]
        if not strict_equal(unit.get("sources", []), bindings):
            raise ContractError("Header unit sources differ from original unit bindings")
        candidates = []
        for bound in bindings:
            cell = cells[(bound["asset_ref"], bound.get("sheet"), bound["row"], bound["column"])]
            # A rejected formula header may still retain the syntactically
            # recognized unit token as an ambiguous candidate, never as a unit.
            if isinstance(cell["raw_value"], str) and (
                classify_cell(cell)["status"] == "value" or bound.get("extraction")
            ):
                value = _transform(cell["raw_value"], bound)
                if value not in candidates:
                    candidates.append(value)
        if not strict_equal(unit.get("candidates", []), candidates):
            raise ContractError("Header unit candidates differ from source unit bindings")
        if len(candidates) > 1 and not unit.get("ambiguous"):
            raise ContractError("Header unit conflict cannot be hidden")
        if context.get("issues") and "value" in item and not inactive(item):
            raise ContractError("Header interpretation issues must block affected assertions")


def _expected(item, binding):
    role = binding["role"]
    if role == "identity":
        expected = item.get("source_context", {})["identity_values"]
    elif role == "value":
        expected = item["value"]["raw_value"]
    elif role == "unit":
        expected = item["value"]["raw_unit"]
    elif role == "basis":
        expected = item["value"]["raw_basis"]
    elif role == "x_value":
        expected = item["series_data"]["raw_x_values"]
    elif role == "x_unit":
        expected = item["series_data"]["x_unit"]
    elif role == "qualifier":
        expected = item["qualifiers"][binding["qualifier_key"]]
    else:
        raise ContractError("Unknown cell binding role")
    if "value_index" in binding:
        index = binding["value_index"]
        if type(index) is not int or index < 0 or not isinstance(expected, list) or index >= len(expected):
            raise ContractError("Cell binding index outside declared value")
        expected = expected[index]
    return expected


def _bound_classifications(assertion, cells):
    """Derive admission from original cell metadata, never a caller's label."""
    from .extractors.table_semantics import classify_cell, classify_numeric_cell

    context = assertion.get("source_context", {})
    bindings = context.get("cell_bindings", [])
    series = "series_data" in assertion
    roles = defaultdict(list)
    interpret_missing = assertion["field_id"].startswith(
        ("observation.", "composition.", "condition.", "process.parameter")
    )
    for b in bindings:
        if b.get("role") not in ("value", "x_value"):
            continue
        derived = []
        for row in _binding_rows(b):
            try:
                original = cells[(b["asset_ref"], b.get("sheet"), row, b["column"])]
                original = {**original, "raw_value": _transform(original["raw_value"], b)}
            except (KeyError, TypeError, ValueError, re.error) as exc:
                raise ContractError("Source state binding cannot be resolved") from exc
            derived.append(
                classify_numeric_cell(original)
                if series
                else classify_cell(original, interpret_missing=interpret_missing)
            )
        if "value_index" in b:
            roles[b["role"]].append((b["value_index"], derived[0]))
        else:
            roles[b["role"]].extend(enumerate(derived))
    if not roles.get("value"):
        return None
    values = {role: [entry for _, entry in sorted(entries, key=lambda e: e[0])] for role, entries in roles.items()}
    states = [d["status"] for role in ("value", "x_value") for d in values.get(role, [])]
    # A curve may have explicit empty points, but needs a measured numeric pair.
    unsafe = [s for s in states if s not in (("value", "source_blank") if series else ("value",))]
    status = next((s for s in ("formula_requires_review", "excel_error") if s in unsafe), None)
    status = status or (unsafe[0] if unsafe else "value")
    if (
        series
        and status == "value"
        and not any(
            x["parsed"] is not None and y["parsed"] is not None
            for x, y in zip(values.get("x_value", []), values["value"])
        )
    ):
        status = "source_blank" if all(s == "source_blank" for s in states) else "unreadable"
    unit_candidates = defaultdict(list)
    for b in bindings + context.get("unit_candidate_bindings", []) + context.get("basis_candidate_bindings", []):
        if b.get("role") not in ("unit", "x_unit", "basis"):
            continue
        for row in _binding_rows(b):
            try:
                cell = cells[(b["asset_ref"], b.get("sheet"), row, b["column"])]
                derived = classify_cell(cell)
                raw = _transform(cell["raw_value"], b)
            except (KeyError, TypeError, ValueError, re.error) as exc:
                raise ContractError("Unit/basis state binding cannot be resolved") from exc
            if derived["status"] not in ("value", "source_blank"):
                status = derived["status"]
            if raw is not None and raw != "":
                unit_candidates[b["role"]].append(raw)
    if status == "value" and any(len({fingerprint(v) for v in group}) > 1 for group in unit_candidates.values()):
        status = "ambiguous_units"
    return {"status": status, "values": values}


def _check_source_states(package, cells, *, finalize=False):
    for a in package["assertions"]:
        derived = _bound_classifications(a, cells)
        if derived is None:
            continue
        context = a.setdefault("source_context", {}) if finalize else a.get("source_context", {})
        status = derived["status"]
        if finalize:
            context["value_status"] = status
            if status != "value":
                a["use_status"] = "blocked"
                if "series_data" not in a and derived["values"]["value"][0]["status"] != "value":
                    a["value"]["parsed"] = None
                    for key in ("parsed_interval", "parsed_decimal", "parsed_decimal_interval"):
                        a["value"].pop(key, None)
        elif status != "value" and (context.get("value_status") != status or not inactive(a)):
            raise ContractError("Assertion admission contradicts original cell status")
        if "series_data" in a:
            for role, key in [("value", "value_statuses"), ("x_value", "x_value_statuses")]:
                expected = [v["status"] for v in derived["values"].get(role, [])]
                if finalize:
                    a["series_data"][key] = expected
                elif key in a["series_data"] and not strict_equal(a["series_data"][key], expected):
                    raise ContractError("Series status view differs from bound original cells")
        elif status != "value" and derived["values"]["value"][0]["status"] != "value":
            if any(
                a["value"].get(k) is not None
                for k in ("parsed", "parsed_interval", "parsed_decimal", "parsed_decimal_interval")
            ):
                raise ContractError("Non-measurement source cell has a numeric view")


def _check_record_domains(package, cells, *, finalize=False):
    """A key/value run has one physical identity, including every occurrence."""
    from .extractors.table_semantics import record_scope_domain, record_scope_identity

    for obj in package["objects"]:
        if obj["kind"] != "instrument_run":
            continue
        context = obj.get("source_context", {})
        scopes = context.get("record_scopes", [])
        if not scopes and not (context.get("record_domain") and context.get("identity_values")):
            continue
        try:
            if not isinstance(scopes, list) or not scopes:
                raise ValueError("Record scopes must be an array")
            domains = [record_scope_domain(scope) for scope in scopes]
            domain = domains[0]
            if any(not strict_equal(d, domain) for d in domains[1:]):
                raise ValueError("One run contains more than one physical record domain")
            if "record_domain" in context and not strict_equal(context["record_domain"], domain):
                raise ValueError("Record scope differs from the declared run identity")
            for scope in scopes:
                label = record_scope_identity(
                    scope, lambda row, col: cells[(scope["asset_ref"], scope.get("sheet"), row, col)]
                )
                if not strict_equal(context.get("identity_values"), [label["raw_value"]]):
                    raise ValueError("Run identity differs from its record label")
                for binding in context.get("cell_bindings", []):
                    if binding.get("role") != "identity":
                        continue
                    if (
                        binding.get("asset_ref") != scope["asset_ref"]
                        or binding.get("sheet") != scope.get("sheet")
                        or binding.get("column") != scope["value_column"]
                        or _binding_rows(binding) != [label["row"]]
                    ):
                        raise ValueError("Run identity occurrence lies outside its physical record label")
        except (KeyError, TypeError, ValueError, ContractError) as exc:
            if finalize:
                obj["use_status"] = "blocked"
                add_issue(
                    package,
                    "Run has inconsistent physical record identity; no cross-record ownership permitted",
                    [obj["object_id"]],
                    obj.get("evidence_refs", []),
                    severity="error",
                    code="ambiguous_record_domain",
                )
            elif not inactive(obj):
                raise ContractError("Run identity does not belong to one verified physical record domain") from exc


def _local_occurrences(item, evidence):
    """Each row occurrence includes a complete typed identity or local cells."""
    context = item.get("source_context", {})
    identities = context.get("identity_values")
    groups = defaultdict(lambda: {"indices": set(), "columns": set()})
    if identities:
        for b in context.get("cell_bindings", []):
            if b.get("role") != "identity" or "value_index" not in b:
                continue
            for row in _binding_rows(b):
                group = groups[(b["asset_ref"], b.get("sheet"), row)]
                group["indices"].add(b["value_index"])
                group["columns"].add(b["column"])
        return {key: g["columns"] for key, g in groups.items() if g["indices"] == set(range(len(identities)))}, True
    for eid in item.get("evidence_refs", []):
        ev = evidence.get(eid, {})
        if ev.get("content_kind") not in ("table_cells", "table_row"):
            continue
        anchor = ev.get("anchor", {})
        rows = anchor.get("row_range", [])
        if len(rows) == 2 and rows[0] == rows[1]:
            groups[(ev["asset_ref"], anchor.get("sheet"), rows[0])]["columns"].update(anchor.get("columns", []))
    return {key: g["columns"] for key, g in groups.items()}, False


def _relation_covers(relation, key, columns, evidence):
    asset, sheet, row = key
    covered = set()
    for eid in relation.get("evidence_refs", []):
        ev = evidence.get(eid, {})
        anchor = ev.get("anchor", {})
        extent = anchor.get("row_range", [])
        if (
            ev.get("asset_ref") == asset
            and anchor.get("sheet") == sheet
            and len(extent) == 2
            and extent[0] <= row <= extent[1]
        ):
            covered.update(anchor.get("columns", []))
    return bool(columns) and columns <= covered


def _record_block_proof(run, other, relation, occurrences, cells, evidence):
    """Key/value layouts bind separate rows through an explicit source record."""
    from .extractors.table_semantics import record_scope_identity

    identities = run.get("source_context", {}).get("identity_values")
    if not identities or len(identities) != 1:
        return False
    for scope in run.get("source_context", {}).get("record_scopes", []):
        try:
            asset, sheet = scope["asset_ref"], scope.get("sheet")
            label_cell = record_scope_identity(scope, lambda row, col: cells[(asset, sheet, row, col)])
            if not strict_equal(label_cell["raw_value"], identities[0]):
                continue
            label = (asset, sheet, label_cell["row"])
            kc, vc = scope["key_column"], scope["value_column"]
            if vc not in occurrences[run["object_id"]][0].get(label, set()):
                continue
            if not _relation_covers(relation, label, {kc, vc}, evidence):
                continue
            for block in scope.get("field_blocks", [scope]):
                lo, hi = block["row_range"]
                for key, columns in occurrences[other["object_id"]][0].items():
                    if (
                        key[:2] == (asset, sheet)
                        and lo <= key[2] <= hi
                        and {block["key_column"], block["value_column"]} <= columns
                        and _relation_covers(relation, key, columns, evidence)
                    ):
                        return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def _unproven_relations(package, cells, evidence):
    objects = {o["object_id"]: o for o in package["objects"]}
    occurrences = {oid: _local_occurrences(obj, evidence) for oid, obj in objects.items()}
    implicit = [
        {
            "from_ref": obj["object_id"],
            "to_ref": obj[reference],
            "evidence_refs": obj.get("evidence_refs", []),
            "applicability": "as_reported",
            "use_status": obj.get("use_status"),
            "lifecycle_status": obj.get("lifecycle_status"),
            "reference_owner": obj,
            "reference_field": reference,
        }
        for obj in package["objects"]
        for reference in ("owner_ref", "material_ref")
        if obj.get(reference)
    ]
    for relation in package["relations"] + implicit:
        if inactive(relation) or relation.get("applicability") in (
            "requires_review",
            "candidate",
            "unresolved",
            "unknown",
            "not_applicable",
            "superseded",
            "rejected",
            "withdrawn",
        ):
            continue
        left, right = objects[relation["from_ref"]], objects[relation["to_ref"]]
        if left["kind"] == "source" or right["kind"] == "source":
            continue
        lrows, lid = occurrences[left["object_id"]]
        rrows, rid = occurrences[right["object_id"]]
        proven = False
        for row in lrows.keys() & rrows.keys():
            lc, rc = lrows[row], rrows[row]
            # A local header/cell excerpt must actually contain the endpoint's
            # identity, not merely occur elsewhere on the same physical row.
            if lid != rid and not (lc <= rc if lid else rc <= lc):
                continue
            if not lid and not rid and not lc & rc:
                continue
            if _relation_covers(relation, row, lc | rc, evidence):
                proven = True
                break
        if not proven:
            for run, other in ((left, right), (right, left)):
                if run["kind"] == "instrument_run" and _record_block_proof(
                    run, other, relation, occurrences, cells, evidence
                ):
                    proven = True
                    break
        if not proven:
            yield relation


def verify_table_bindings(package):
    """Validate internal table/anchor/value consistency without granting review."""
    if package.get("header", {}).get("table_quality_version") == "tabular-quality-2":
        from .table_plan.quality import verify_plan_package

        return verify_plan_package(package)
    cells, tables = _tables(package)
    evidence = {e["evidence_id"]: e for e in package["evidence"]}
    for item in package["assertions"] + package["assessments"]:
        _check_qualifier_metadata(item, tables, evidence)
        _check_header_context(item, cells, evidence)
    _check_source_states(package, cells)
    _check_record_domains(package, cells)
    if next(_unproven_relations(package, cells, evidence), None) is not None:
        raise ContractError("Table relation lacks common local source identity evidence")
    count, items = 0, 0
    for item in package["assertions"] + package["objects"]:
        bindings = item.get("source_context", {}).get("cell_bindings", [])
        if not isinstance(bindings, list):
            raise ContractError("Cell bindings must be an array")
        bindings = bindings + [
            {**binding, "role": "qualifier", "qualifier_key": key}
            for key, binding in item.get("source_context", {}).get("qualifier_bindings", {}).items()
        ]
        if item.get("source_context", {}).get("evidence_validation") == "verified" and not bindings:
            raise ContractError("Verified cell evidence is missing its bindings")
        if bindings:
            items += 1
            roles = {b.get("role") for b in bindings}
            if "value" in item:
                required = {"value"}
                if item["value"].get("raw_unit") is not None:
                    required.add("unit")
                if item["value"].get("raw_basis") is not None:
                    required.add("basis")
                if "series_data" in item:
                    required.add("x_value")
                    if item["series_data"].get("x_unit") is not None:
                        required.add("x_unit")
                if not required <= roles:
                    raise ContractError("Verified field is missing value/unit/basis coordinate bindings")
                from .extractors.table_semantics import classify_cell

                value = item["value"]
                if value.get("parsed") is not None:
                    derived = classify_cell({"raw_value": value["raw_value"]})["parsed"]
                    if not strict_equal(value["parsed"], derived):
                        raise ContractError("Numeric view differs from the preserved original value")
                if value.get("parsed_interval") is not None:
                    if value["parsed_interval"] != classify_cell({"raw_value": value["raw_value"]}).get("interval"):
                        raise ContractError("Interval view differs from the preserved original value")
                derived = classify_cell({"raw_value": value["raw_value"]})
                for key, derived_key in [
                    ("parsed_decimal", "parsed_decimal"),
                    ("parsed_decimal_interval", "decimal_interval"),
                ]:
                    if key in value and not strict_equal(value[key], derived.get(derived_key)):
                        raise ContractError("Exact decimal view differs from the preserved original value")
                if value.get("normalized_value") is not None:
                    raise ContractError("No table normalization rule is registered for derived values")
        for binding in bindings:
            try:
                rows = _binding_rows(binding)
                col = binding["column"]
                if type(col) is not int or col < 1:
                    raise ContractError("Invalid binding column")
                values = [
                    _transform(cells[(binding["asset_ref"], binding.get("sheet"), r, col)]["raw_value"], binding)
                    for r in rows
                ]
                actual = values if "rows" in binding or "row_range" in binding else values[0]
                expected = _expected(item, binding)
                _supported(package, item, binding, rows, evidence)
            except (KeyError, IndexError, TypeError, ValueError, re.error) as exc:
                raise ContractError("Cell binding cannot be resolved against retained source evidence") from exc
            if not strict_equal(actual, expected):
                raise ContractError("Bound source cell does not equal the declared original value")
            if "series_data" in item and binding["role"] in ("value", "x_value"):
                from .extractors.table_semantics import classify_cell

                numeric = [
                    classify_cell(cells[(binding["asset_ref"], binding.get("sheet"), r, col)])["parsed"] for r in rows
                ]
                numeric = numeric if "rows" in binding or "row_range" in binding else numeric[0]
                stored = item["series_data"]["values" if binding["role"] == "value" else "x_values"]
                if "value_index" in binding:
                    stored = stored[binding["value_index"]]
                if not strict_equal(numeric, stored):
                    raise ContractError("Series numeric view differs from bound original cells")
                decimal_key = "decimal_values" if binding["role"] == "value" else "x_decimal_values"
                if decimal_key in item["series_data"]:
                    derived_decimal = [
                        classify_cell(cells[(binding["asset_ref"], binding.get("sheet"), r, col)]).get("parsed_decimal")
                        for r in rows
                    ]
                    derived_decimal = (
                        derived_decimal if "rows" in binding or "row_range" in binding else derived_decimal[0]
                    )
                    stored_decimal = item["series_data"][decimal_key]
                    if "value_index" in binding:
                        stored_decimal = stored_decimal[binding["value_index"]]
                    if not strict_equal(derived_decimal, stored_decimal):
                        raise ContractError("Series exact decimal view differs from source cells")
            count += len(rows)
        for role in {b["role"] for b in bindings}:
            if role == "qualifier":
                continue
            expected = _expected(item, {"role": role})
            if isinstance(expected, list):
                role_bindings = [b for b in bindings if b["role"] == role]
                if all("value_index" in b for b in role_bindings):
                    if {b["value_index"] for b in role_bindings} != set(range(len(expected))):
                        raise ContractError("Array binding does not cover every declared original value")
    return {
        "checked_bindings": count,
        "bound_items": items,
        "scope": "direct_cell_consistency_not_scientific_semantic_review",
    }


def _semantic_qualifiers(assertion):
    # An explicit identity replaces only its own display label. Other labels
    # still distinguish metrics, parameters, stages, and other source scopes.
    q = assertion.get("qualifiers", {})
    identified = {
        k[:-3]
        for k, v in q.items()
        if k.endswith("_id")
        and ((isinstance(v, str) and v.strip()) or (type(v) in (int, float) and Decimal(str(v)).is_finite()))
    }
    return {k: v for k, v in q.items() if not (k.endswith("_label") and k[:-6] in identified)}


def _exact_decimal_identity(raw):
    """Canonical decimal components without context-dependent rounding."""
    number = Decimal(str(raw))
    if not number.is_finite():
        return str(number)
    if number.is_zero():
        return [0, [0], 0]
    sign, digits, exponent = number.as_tuple()
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return [sign, digits, exponent]


def _value_identity(assertion):
    value = assertion["value"]
    typed = value.get("typed_value")
    if value.get("typed_value_version") == "2" and isinstance(typed, dict):
        # Preserve typed semantics: uncertainty, bounds and cached calculations
        # are never collapsed into their central scalar. Raw spelling may vary.
        semantic = {k: v for k, v in typed.items() if k not in ("raw", "number_format")}
        numeric_keys = ("decimal", "lower", "upper", "center", "uncertainty")
        if typed.get("kind") == "duration" and typed.get("status") == "valid":
            numeric_keys += ("canonical",)
        for key in numeric_keys:
            if semantic.get(key) is not None:
                try:
                    semantic[key] = _exact_decimal_identity(semantic[key])
                except InvalidOperation:
                    pass
        if typed.get("kind") in ("datetime", "time") and typed.get("status") == "valid":
            semantic["canonical"] = _temporal_identity(typed["kind"], typed["canonical"])
        if typed.get("kind") in ("text", "category", "unparsed"):
            semantic["raw"] = typed.get("raw")
        unit = value.get("unit_normalization", {})
        unit_key = unit.get("canonical") if unit.get("status") == "recognized" else value.get("raw_unit")
        return fingerprint([semantic, unit_key, value.get("raw_basis")])
    raw = value.get("parsed")
    if raw is None:
        raw = value["raw_value"]
    if value.get("kind") in ("scalar", "number", "integer") and (type(raw) in (int, float) or isinstance(raw, str)):
        try:
            number = Decimal(str(raw))
            if number.is_finite():
                raw = {"decimal": str(number.normalize())}
        except InvalidOperation:
            pass
    return fingerprint([raw, value.get("raw_unit"), value.get("raw_basis")])


def blocked_objects(package):
    blocked = {o["object_id"] for o in package["objects"] if inactive(o)}
    # Descendants of an invalid experiment/test identity cannot act as free-
    # floating records after their connecting edges are filtered away.
    containment = {
        "contains_sample",
        "tested_by",
        "produces_observation",
        "has_series",
        "has_formulation",
        "contains_component",
        "defines_step",
    }
    changed = True
    while changed:
        changed = False
        for r in package["relations"]:
            if inactive(r) or r.get("applicability") in ("superseded", "rejected", "withdrawn"):
                continue
            if r["from_ref"] in blocked and r["predicate"] in containment and r["to_ref"] not in blocked:
                blocked.add(r["to_ref"])
                changed = True
            if (
                r["to_ref"] in blocked
                and r["predicate"] in ({"observes_subject"} | SAMPLE_TEST_REVERSE)
                and r["from_ref"] not in blocked
            ):
                blocked.add(r["from_ref"])
                changed = True
    return blocked


def apply_scientific_guards(package):
    """Shared scientific ownership/conflict gate for every table contract version."""
    objects = {o["object_id"]: o for o in package["objects"]}
    subjects = subject_kinds(package)
    if package["header"].get("table_quality_version") == "tabular-quality-2":
        enforce_exclusive_ownership(
            package,
            [
                (i, r)
                for i, r in enumerate(package["relations"])
                if not inactive(r) and r.get("applicability") not in ("superseded", "rejected", "withdrawn")
            ],
        )
    owners = defaultdict(set)
    sample_owners = defaultdict(set)
    for r in package["relations"]:
        if inactive(r) or r.get("applicability") in ("superseded", "rejected", "withdrawn"):
            continue
        if r["predicate"] == "produces_observation":
            owners[r["to_ref"]].add(r["from_ref"])
        elif r["predicate"] == "observes_subject":
            owners[r["from_ref"]].add(r["to_ref"])
        if r["predicate"] in SAMPLE_TEST_FORWARD and objects[r["from_ref"]]["kind"] in subjects:
            sample_owners[r["to_ref"]].add(r["from_ref"])
        elif r["predicate"] in SAMPLE_TEST_REVERSE and objects[r["to_ref"]]["kind"] in subjects:
            sample_owners[r["from_ref"]].add(r["to_ref"])
    for test, samples in sample_owners.items():
        if len(samples) > 1:
            physical = all(objects[s]["kind"] == "sample" for s in samples)
            description = (
                "One test points to multiple samples; aggregate and individual results require distinct identities"
                if physical
                else (
                    "One test points to multiple scientific subjects; aggregate and individual results "
                    "require distinct identities"
                )
            )
            add_issue(
                package,
                description,
                [test],
                severity="error",
                code="ambiguous_test_sample_owner" if physical else "ambiguous_test_subject_owner",
            )
            objects[test]["use_status"] = "blocked"
    slots = defaultdict(list)
    for a in package["assertions"]:
        status = a.get("source_context", {}).get("value_status")
        if status in NON_VALUES:
            a["use_status"] = "blocked"
            continue
        if inactive(a):
            continue
        obj = objects[a["subject_ref"]]
        # Lists/curves and author interpretations may legitimately contain
        # several statements. Scalar factual slots have one value per identity.
        if obj["kind"] in ("series", "claim", "patent_claim", "source") or (
            obj["kind"] == "content_unit" and obj["kind"] not in subjects
        ):
            continue
        owner = a["subject_ref"]
        if obj["kind"] == "observation":
            test_owners = {x for x in owners[owner] if objects[x]["kind"] in ACTIVITY_KINDS}
            if len(test_owners) > 1:
                issue = add_issue(
                    package,
                    "One observation has multiple incompatible activity owners",
                    [owner],
                    a["evidence_refs"],
                    severity="error",
                    code="ambiguous_observation_owner",
                )
                obj["use_status"] = "blocked"
                a["use_status"] = "blocked"
                if issue not in a["issue_refs"]:
                    a["issue_refs"].append(issue)
            elif test_owners:
                owner = next(iter(test_owners))
        value = a["value"]
        if value.get("typed_value_version") == "2":
            # A typed scalar may have structured original storage (duration,
            # formula/cache). Shape of the raw JSON is not its scientific grain.
            # The interpreter explicitly separates scalar and series meanings.
            if not isinstance(value.get("typed_value"), dict):
                continue
        elif isinstance(value["raw_value"], (list, dict)):
            continue
        key = (owner, a["field_id"], fingerprint(_semantic_qualifiers(a)))
        slots[key].append(a)
    for (owner, field, _), assertions in slots.items():
        if len({_value_identity(a) for a in assertions}) <= 1:
            continue
        kind = objects[owner]["kind"]
        code = (
            "conflicting_result_slot"
            if field.startswith("observation.")
            else "conflicting_test_condition"
            if kind == "reported_test"
            else "conflicting_sample_field"
            if kind == "sample"
            else "conflicting_object_field"
        )
        evidence = sorted({e for a in assertions for e in a["evidence_refs"]})
        issue = add_issue(
            package,
            "Incompatible values for one declared object and scientific field; no value selected",
            [owner],
            evidence,
            severity="error",
            code=code,
        )
        objects[owner]["use_status"] = "blocked"
        for a in assertions:
            a["use_status"] = "blocked"
            if issue not in a["issue_refs"]:
                a["issue_refs"].append(issue)
    blocked = blocked_objects(package)
    for o in package["objects"]:
        if o["object_id"] in blocked:
            o["use_status"] = "blocked"
    for a in package["assertions"]:
        if a["subject_ref"] in blocked:
            a["use_status"] = "blocked"
    for r in package["relations"]:
        if r["from_ref"] in blocked or r["to_ref"] in blocked:
            r["use_status"] = "blocked"
    return package


def finalize_table_package(package):
    """Run once after all mappings; idempotent when invoked again by checks."""
    if package.get("header", {}).get("table_quality_version") == "tabular-quality-2":
        verify_table_bindings(package)
        return package
    cells, _ = _tables(package)
    evidence_index = {e["evidence_id"]: e for e in package["evidence"]}
    _check_source_states(package, cells, finalize=True)
    _check_record_domains(package, cells, finalize=True)
    for relation in _unproven_relations(package, cells, evidence_index):
        if "reference_owner" in relation:
            relation["reference_owner"]["use_status"] = "blocked"
        else:
            relation["applicability"] = "requires_review"
        add_issue(
            package,
            (
                "Relationship has no shared local source identity or verified record block; retained "
                "without query ownership"
            ),
            [relation["from_ref"], relation["to_ref"]],
            relation.get("evidence_refs", []),
            severity="error",
            code="unproven_table_relation",
        )
    apply_scientific_guards(package)
    check = verify_table_bindings(package)
    for item in package["assertions"] + package["objects"]:
        ctx = item.setdefault("source_context", {})
        ctx["evidence_validation"] = "verified" if ctx.get("cell_bindings") else "not_checked"
    package["header"]["table_quality_version"] = QUALITY_VERSION
    package["header"]["table_evidence_check"] = check
    return package


def verify_original_tables(package, asset_paths):
    """Reopen originals using recorded reader settings before candidate storage.

    This validates source transcription, not the scientific meaning of a column.
    Unknown dialects produce no cell claims and remain explicitly unparsed.
    """
    from .readers.tables import read_tables

    _, transcriptions = _tables(package)
    parsed_assets = {}
    for (asset_ref, sheet), table in transcriptions.items():
        if asset_ref not in asset_paths:
            raise ContractError("Original required for cell evidence verification")
        options = table.get("parse_diagnostics", {}).get("effective_reader_options", {})
        if table.get("format") == "csv" and not options:
            options = {"encoding": table.get("encoding", "utf-8-sig"), "delimiter": table.get("delimiter", ",")}
        cache_key = (asset_ref, fingerprint(options))
        if cache_key not in parsed_assets:
            parsed_assets[cache_key] = {t.get("sheet"): t for t in read_tables(asset_paths[asset_ref], options=options)}
        current = parsed_assets[cache_key]
        if package.get("header", {}).get("table_quality_version") == "tabular-quality-2":
            retained_sheets = {s for a, s in transcriptions if a == asset_ref}
            if retained_sheets != set(current):
                raise ContractError("Original worksheet inventory is incomplete")
        if sheet not in current or not strict_equal(table["rows"], current[sheet]["rows"]):
            raise ContractError("Retained cell evidence differs from original file")
        if package.get("header", {}).get("table_quality_version") == "tabular-quality-2" and any(
            not strict_equal(table.get(key), current[sheet].get(key))
            for key in (
                "format",
                "sheet",
                "nrows",
                "ncols",
                "merged_cells",
                "hidden",
                "hidden_columns",
                "encoding",
                "delimiter",
                "raw_text",
            )
        ):
            raise ContractError("Retained table metadata differs from original file")
    return {"checked_tables": len(transcriptions)}
