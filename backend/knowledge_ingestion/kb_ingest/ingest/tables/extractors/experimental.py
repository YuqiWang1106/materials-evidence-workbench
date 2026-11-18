"""Explicit table mappings to candidate records, with no reference-answer input.

Supported layouts are key_value, channels, curves, records and objects. Mapping profiles choose
physical rows/columns; records/objects require explicit source identity keys.
Rows alone never imply independent experiments. Unknown layouts are preserved
as evidence and remain pending.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy

from kb_ingest.contract import (
    add_assertion,
    add_coverage,
    add_evidence,
    add_issue,
    add_object,
    add_relation,
    new_package,
)
from kb_ingest.core import fingerprint
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_from_string, range_boundaries

from ..readers.tables import read_tables
from .mapping_transaction import release_evidence, snapshot_mapping
from .profile_regions import MAX_REGIONS, expand_profile
from .table_orientation import oriented_table
from .table_semantics import (
    assess_cell,
    binding,
    classify_cell,
    classify_numeric_cell,
    identity_context,
    merge_object,
    scalar_assertion,
)

EXTRACTOR_VERSION = "experimental-tables-3"


def _detect_profile(table, asset):
    """Conservative, source-ID independent structural signatures, never expected counts."""
    if not table["rows"]:
        return None
    base = {"asset_id": asset["asset_id"], "sheet": table["sheet"], "selection": "automatic_structure_signature"}
    if table["ncols"] == 2:
        kv = {
            str(r["cells"][0]["raw_value"]): r["cells"][1]["raw_value"] for r in table["rows"] if len(r["cells"]) == 2
        }
        if {"TEST IDENT", "MATERIAL", "SCAN COUNT", "SCAN TIME"} <= kv.keys():
            return {
                **base,
                "template_id": "instrument-scalar-v1",
                "layout": "key_value",
                "run_key": f"scalar:{asset['asset_id']}:{table['sheet']}",
                "label_key": "TEST IDENT",
                "fields": {
                    "TEST IDENT": {
                        "field_id": "activity.identifier",
                        "qualifiers": {"identifier_type": "instrument_test_label"},
                    },
                    "LABORATORY": {"field_id": "activity.participant", "qualifiers": {"role": "laboratory"}},
                    "OPERATOR": {"field_id": "activity.participant", "qualifiers": {"role": "operator"}},
                    "SCAN COUNT": {"field_id": "activity.scan_count", "parse": "number"},
                    "END OF TEST SCAN": {"field_id": "activity.end_marker_scan_index", "parse": "number"},
                    "END OF TEST TIME": {"field_id": "activity.end_marker_time", "parse": "number"},
                    "SCAN TIME": {
                        "field_id": "condition.parameter",
                        "parse": "number",
                        "qualifiers": {"parameter_id": "sampling_interval", "parameter_label": "SCAN TIME"},
                    },
                    "HEAT FLUX": {
                        "field_id": "condition.parameter",
                        "parse": "number",
                        "qualifiers": {"parameter_id": "heat_flux", "parameter_label": "HEAT FLUX"},
                    },
                    "TIME TO IGN": {
                        "target": "observation",
                        "field_id": "observation.value",
                        "parse": "number",
                        "qualifiers": {"metric_id": "source_key:TIME TO IGN", "metric_label": "TIME TO IGN"},
                    },
                },
            }
    first = [c["raw_value"] for c in table["rows"][0]["cells"]]
    row_keys = {str(r["cells"][0]["raw_value"]): r["row"] for r in table["rows"][:12] if r["cells"]}
    if (
        len(first) >= 3
        and first[:2] == ["Names", "Time"]
        and {"Chan Gain", "Offset", "Gain", "Units", "Baseline"} <= row_keys.keys()
    ):
        start = row_keys["Baseline"] + 1
        if start <= table["nrows"] and all(
            _number(_cell(table, r, 2)) is not None for r in range(start, table["nrows"] + 1)
        ):
            return {
                **base,
                "template_id": "calibrated-instrument-channels-v1",
                "layout": "channels",
                "header_row": 1,
                "unit_row": row_keys["Units"],
                "data_start_row": start,
                "x_column": 2,
                "y_columns": list(range(3, table["ncols"] + 1)),
                "calibration_rows": [row_keys[k] for k in ["Chan Gain", "Offset", "Gain", "Baseline"]],
            }
    merges = [range_boundaries(s) for s in table["merged_cells"]]
    if table["nrows"] >= 3 and len(merges) >= 2 and all(r0 == r1 == 1 and c1 - c0 == 2 for c0, r0, c1, r1 in merges):
        specs = []
        for c0, _, c1, _ in sorted(merges):
            left, middle = _cell(table, 2, c0)["raw_value"], _cell(table, 2, c0 + 1)["raw_value"]
            if not (
                isinstance(left, str)
                and isinstance(middle, str)
                and re.fullmatch(r"Time\s*\((?:s|sec|seconds)\)", left, re.I)
                and re.fullmatch(r"Time\s*\((?:min|minutes)\)", middle, re.I)
            ):
                break
            specs.append(
                {
                    "x_column": c0,
                    "y_column": c1,
                    "extra_columns": [c0 + 1],
                    "label_cell": f"{get_column_letter(c0)}1",
                    "condition_from_label": {
                        "pattern": r"\bat\s*(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>MPa|kPa|Pa)\b",
                        "parameter_id": "source_header_condition",
                        "parameter_label": "source header condition",
                    },
                }
            )
        else:
            return {
                **base,
                "template_id": "repeated-seconds-minutes-response-v1",
                "layout": "curves",
                "header_row": 2,
                "data_start_row": 3,
                "series": specs,
            }
    numeric_columns = [
        c
        for c in range(1, table["ncols"] + 1)
        if sum(_number(_cell(table, r, c)) is not None for r in range(1, table["nrows"] + 1)) >= 3
    ]
    if len(numeric_columns) < 2:
        return None
    # A headerless numeric pair could be a curve or independent experimental
    # rows. Column values alone cannot establish that relationship.
    if all(_number(_cell(table, 1, c)) is not None for c in numeric_columns):
        return None
    header_row = next(
        (
            r
            for r in range(1, min(table["nrows"], 10))
            if any(
                isinstance(_cell(table, r, c)["raw_value"], str) and not _blank(_cell(table, r, c))
                for c in numeric_columns
            )
            and all(_number(_cell(table, r + 1, c)) is not None for c in numeric_columns)
        ),
        None,
    )
    if header_row is None:
        return None
    headers = {c: _cell(table, header_row, c)["raw_value"] for c in numeric_columns}
    if len(numeric_columns) % 2 == 0 and all(
        headers[numeric_columns[i]] in (None, "") and headers[numeric_columns[i + 1]] not in (None, "")
        for i in range(0, len(numeric_columns), 2)
    ):
        specs = [
            {"x_column": numeric_columns[i], "y_column": numeric_columns[i + 1]}
            for i in range(0, len(numeric_columns), 2)
        ]
        template = "alternating-blank-label-pairs-v1"
    elif headers[numeric_columns[0]] in (None, "") and all(headers[c] not in (None, "") for c in numeric_columns[1:]):
        specs = [{"x_column": numeric_columns[0], "y_column": c} for c in numeric_columns[1:]]
        template = "shared-first-axis-v1"
    else:
        axis = [
            c
            for c in numeric_columns
            if re.fullmatch(r"(?:temperature|temp|tem|time)(?:\s*\([^)]*\))?", str(headers[c]), re.I)
        ]
        if not axis or axis[0] != numeric_columns[0]:
            return None
        specs, current = [], None
        for c in numeric_columns:
            if c in axis:
                current = c
            else:
                specs.append({"x_column": current, "y_column": c})
        if not specs:
            return None
        template = "named-shared-axes-v1"
    return {
        **base,
        "template_id": template,
        "layout": "curves",
        "header_row": header_row,
        "data_start_row": header_row + 1,
        "series": specs,
    }


def _link_instrument_templates(tasks):
    scalars = [t for t in tasks if t[2].get("template_id") == "instrument-scalar-v1"]
    scans = [t for t in tasks if t[2].get("template_id") == "calibrated-instrument-channels-v1"]
    candidates = {}
    for scan in scans:
        _, table, profile, _, _ = scan
        times = [_number(_cell(table, r, 2)) for r in range(profile["data_start_row"], table["nrows"] + 1)]
        for scalar in scalars:
            meta = {str(r["cells"][0]["raw_value"]): r["cells"][1] for r in scalar[1]["rows"] if len(r["cells"]) == 2}
            count, step = _number(meta["SCAN COUNT"]), _number(meta["SCAN TIME"])
            if (
                count == len(times)
                and step is not None
                and step > 0
                and len(times) > 1
                and all(math.isclose(b - a, step, abs_tol=1e-8) for a, b in zip(times, times[1:]))
            ):
                candidates.setdefault(id(scan), []).append(scalar)
    for scan in scans:
        matches = candidates.get(id(scan), [])
        if (
            len(matches) == 1
            and sum(any(id(t) == id(matches[0]) for t in choices) for choices in candidates.values()) == 1
        ):
            scan[2]["run_key"] = matches[0][2]["run_key"]
            scan[2]["grouping_signature"] = "unique scalar scan-count and interval match in submitted source"
            scan[2]["candidate_scalar_scope"] = _run_scope(matches[0][0], matches[0][1], matches[0][2])
        else:
            scan[2]["grouping_unresolved"] = True


def _column(value):
    column = column_index_from_string(value) if isinstance(value, str) else value
    if type(column) is not int or column < 1:
        raise ValueError("Columns must be one-based integers or Excel column letters")
    return column


def _cell(table, row, column):
    column = _column(column)
    if type(row) is not int or row < 1 or row > table["nrows"] or column > table["ncols"]:
        raise ValueError("Configured cell is outside table")
    cells = table["rows"][row - 1]["cells"]
    if column > len(cells):
        return {
            "row": row,
            "column": column,
            "address": f"{get_column_letter(column)}{row}",
            "raw_value": None,
            "missing_kind": "absent_field",
            "value_type": "absent",
            "_absent_original": True,
        }
    return cells[column - 1]


def _address(table, address):
    col, row = coordinate_from_string(address)
    return _cell(table, row, col)


def _blank(cell):
    return classify_cell(cell)["status"] == "source_blank"


def _number(cell):
    return classify_cell(cell)["parsed"]


def _header(table, profile, column):
    row = profile.get("header_row")
    return _cell(table, row, column)["raw_value"] if row is not None else None


def _anchor(table, rows=None, columns=None):
    if table.get("_orientation") == "transpose":
        rows = rows or [1, table["nrows"]]
        columns = columns or list(range(1, table["ncols"] + 1))
        return _anchor(table["_original_table"], [min(columns), max(columns)], list(range(rows[0], rows[1] + 1)))
    anchor = {
        "format": table["format"],
        "sheet": table["sheet"],
        "row_range": rows or [1, table["nrows"]],
        "columns": columns or list(range(1, table["ncols"] + 1)),
        "index_base": 1,
    }
    if table["format"] == "csv" and table["rows"] and anchor["row_range"][0] > 0:
        lo, hi = anchor["row_range"]
        anchor["physical_lines"] = [
            table["rows"][lo - 1]["physical_lines"][0],
            table["rows"][hi - 1]["physical_lines"][1],
        ]
    return anchor


def _ensure_run(package, runs, key, label, evidence):
    if key not in runs:
        runs[key] = add_object(
            package,
            "instrument_run",
            label or str(key),
            scope_key=str(key),
            evidence_refs=evidence,
            identity_status="requires_review",
            grouping_basis="explicit_table_mapping",
        )
        add_relation(
            package,
            f"{package['header']['source_id']}:source",
            "reports",
            runs[key],
            evidence,
            basis_kind="explicit_table_mapping",
            applicability="candidate_grouping",
        )
    else:
        obj = next(o for o in package["objects"] if o["object_id"] == runs[key])
        obj["evidence_refs"] = list(dict.fromkeys(obj["evidence_refs"] + evidence))
        if label and obj["label"] != label:
            add_issue(
                package,
                "Configured run has conflicting source labels; no identity reconciliation performed",
                [runs[key]],
                evidence,
                code="run_label_conflict",
            )
    return runs[key]


def _key_value_domain(asset, table, profile):
    declaration = profile.get("record_scope", {})
    if not isinstance(declaration, dict):
        raise ValueError("record_scope must declare an enclosing source record")
    return {
        "asset_ref": asset["asset_id"],
        "sheet": table["sheet"],
        "row_range": declaration.get(
            "row_range", [profile.get("data_start_row", 1), profile.get("data_end_row", table["nrows"])]
        ),
        "key_column": _column(declaration.get("key_column", profile.get("key_column", 1))),
        "value_column": _column(declaration.get("value_column", profile.get("value_column", 2))),
        "label_key": declaration.get("label_key", profile.get("label_key")),
    }


def _run_scope(asset, table, profile):
    if profile.get("layout") == "key_value":
        # A run is a source record's physical domain. Caller names and repeated
        # labels cannot join another block, column group, sheet or original.
        return ("key_value_record", fingerprint(_key_value_domain(asset, table, profile)))
    return (asset["asset_id"], table["sheet"], profile["run_key"])


def _key_values(package, asset, table, profile, table_evidence, runs):
    if "run_key" not in profile:
        raise ValueError("key_value layout requires explicit run_key")
    key_column = _column(profile.get("key_column", 1))
    value_column = _column(profile.get("value_column", 2))
    start, end = profile.get("data_start_row", 1), profile.get("data_end_row", table["nrows"])
    _cell(table, start, max(key_column, value_column))
    _cell(table, end, max(key_column, value_column))
    if start > end:
        raise ValueError("data_start_row exceeds data_end_row")
    entries, seen = [], set()
    for row in range(start, end + 1):
        key, value = _cell(table, row, key_column), _cell(table, row, value_column)
        if _blank(key) and _blank(value):
            continue
        if classify_cell(key, interpret_missing=False)["status"] != "value":
            raise ValueError("Metadata keys require source text, not blank/error/formula content")
        marker = str(key["raw_value"])
        if marker in seen:
            raise ValueError(
                "Repeated metadata keys require explicitly bounded record blocks; one run cannot absorb several records"
            )
        seen.add(marker)
        entries.append((key, value))
    domain = _key_value_domain(asset, table, profile)
    scope = {
        **domain,
        "field_blocks": [{"row_range": [start, end], "key_column": key_column, "value_column": value_column}],
    }
    if "record_scope" in profile:
        from .table_semantics import record_scope_identity

        if "row_range" not in profile["record_scope"]:
            raise ValueError("record_scope requires an explicit row_range")
        labels = [record_scope_identity(scope, lambda row, col: _cell(table, row, col))]
    else:
        labels = [v for k, v in entries if k["raw_value"] == profile.get("label_key")]
    if labels and classify_cell(labels[0], interpret_missing=False)["status"] != "value":
        raise ValueError("Run label is missing, erroneous or formula-derived")
    label = str(labels[0]["raw_value"]) if labels else None
    run = _ensure_run(package, runs, _run_scope(asset, table, profile), label, [table_evidence])
    obj = next(o for o in package["objects"] if o["object_id"] == run)
    if labels:
        context = identity_context(asset, table, labels)
        if obj.get("source_context") and obj["source_context"].get("identity_values") != context["identity_values"]:
            raise ValueError("Configured run key refers to different source run labels")
        prior = obj.setdefault("source_context", context)
        for item in context["cell_bindings"]:
            if item not in prior["cell_bindings"]:
                prior["cell_bindings"].append(item)
    obj.setdefault("source_context", {})["record_domain"] = domain
    if labels:
        scopes = obj["source_context"].setdefault("record_scopes", [])
        if not scopes:
            scopes.append(scope)
        else:
            for block in scope["field_blocks"]:
                if block not in scopes[0]["field_blocks"]:
                    scopes[0]["field_blocks"].append(block)
    block_evidence = add_evidence(
        package,
        asset["asset_id"],
        _anchor(table, [start, end], [key_column, value_column]),
        {"cells": [c for pair in entries for c in pair]},
        "table_cells",
    )
    obj.setdefault("raw_metadata", []).extend(
        {"asset_ref": asset["asset_id"], "key": k, "value": v} for k, v in entries
    )
    identity_evidence = []
    if labels:
        identity_row = labels[0]["row"]
        identity_columns = [domain["key_column"], domain["value_column"]]
        identity_evidence = [
            add_evidence(
                package,
                asset["asset_id"],
                _anchor(table, [identity_row, identity_row], identity_columns),
                {"cells": [_cell(table, identity_row, c) for c in identity_columns]},
                "table_cells",
            )
        ]
    used = set()
    if labels:
        used.update((labels[0]["row"], column) for column in identity_columns)
    for key, value in entries:
        mapping = profile.get("fields", {}).get(str(key["raw_value"]))
        if not mapping:
            continue
        ev = add_evidence(
            package,
            asset["asset_id"],
            _anchor(table, [key["row"], key["row"]], [key_column, value_column]),
            {"key": key, "value": value},
            "table_cells",
        )
        target_kind = mapping.get("target", "run")
        if target_kind not in ("observation", "run"):
            raise ValueError("key_value field target must be run or observation")
        status = classify_cell(value, interpret_missing=target_kind == "observation")["status"]
        if mapping.get("parse") == "number" and status == "value" and _number(value) is None:
            add_issue(
                package,
                "Numeric metadata mapping could not parse the original value",
                [run],
                [ev],
                code="unparsed_metadata_number",
            )
            continue
        subject = run
        if target_kind == "observation" and status == "value":
            subject = add_object(
                package,
                "observation",
                str(key["raw_value"]),
                evidence_refs=[ev],
                scope_key=f"{asset['asset_id']}:{table['sheet']}:{key['address']}",
                sample_identity="not_established",
            )
            add_relation(
                package,
                subject,
                "observes_subject",
                run,
                [ev, block_evidence] + identity_evidence,
                basis_kind="structural_expansion",
            )
        scalar_assertion(
            package,
            asset,
            table,
            {**profile, "header_row": profile.get("header_row")},
            value["row"],
            {**mapping, "column": value_column},
            subject,
            evidence=[ev],
            used=used,
            header_cell=key,
            assessment_owner_kind="observation" if target_kind == "observation" else None,
            statement_kind="source_recorded_result" if target_kind == "observation" else "source_recorded_metadata",
        )
    from .table_semantics import record_unmapped

    record_unmapped(package, asset, table, used)


def _curve(package, asset, table, profile, spec, table_evidence, run_ref):
    xcol, ycol = _column(spec["x_column"]), _column(spec["y_column"])
    extra_columns = [_column(c) for c in spec.get("extra_columns", [])]
    columns = list(dict.fromkeys([xcol, ycol] + extra_columns))
    start, end = profile.get("data_start_row", 1), profile.get("data_end_row", table["nrows"])
    # Validate declared range before trimming blank tails. Interior blanks stay in place.
    _cell(table, start, max(columns))
    _cell(table, end, max(columns))
    if start > end:
        raise ValueError("data_start_row exceeds data_end_row")
    while end >= start and all(_blank(_cell(table, end, c)) for c in columns):
        end -= 1
    if end < start:
        add_issue(package, "Mapped series has no data cells", evidence_refs=[table_evidence], code="empty_series")
        return set()
    rows = list(range(start, end + 1))
    used = {(row, column) for row in rows for column in columns}
    cells = {c: [_cell(table, r, c) for r in rows] for c in columns}
    raw = {c: [v["raw_value"] for v in values] for c, values in cells.items()}
    xheader, yheader = _header(table, profile, xcol), _header(table, profile, ycol)
    label_cell = _address(table, spec["label_cell"]) if spec.get("label_cell") else None
    label = (
        str(label_cell["raw_value"])
        if label_cell and not _blank(label_cell)
        else str(yheader or f"{table['sheet'] or asset['original_name']}:{get_column_letter(ycol)}")
    )
    ev = add_evidence(
        package,
        asset["asset_id"],
        _anchor(table, [start, end], columns),
        {"columns": {get_column_letter(c): raw[c] for c in columns}},
        "table_cells",
    )
    header_rows = sorted(
        set(
            r
            for r in [profile.get("header_row"), profile.get("unit_row"), label_cell["row"] if label_cell else None]
            if r
        )
    )
    header_evidence = []
    for row in header_rows:
        header_columns = list(
            dict.fromkeys(columns + ([label_cell["column"]] if label_cell and label_cell["row"] == row else []))
        )
        used.update((row, column) for column in header_columns)
        header_evidence.append(
            add_evidence(
                package,
                asset["asset_id"],
                _anchor(table, [row, row], header_columns),
                {"cells": [_cell(table, row, c) for c in header_columns]},
                "table_cells",
            )
        )
    from .table_headers import resolve_headers
    from .table_semantics import cell_evidence

    xheaders = resolve_headers(asset, table, profile, xcol, axis="x")
    yheaders = resolve_headers(asset, table, profile, ycol, axis="y")
    xheader, yheader = xheaders["primary_label"], yheaders["primary_label"]
    if label_cell is None:
        label = str(yheader or f"{table['sheet'] or asset['original_name']}:{get_column_letter(ycol)}")
    for resolved in (xheaders, yheaders):
        for cell in resolved["cells"]:
            used.add((cell["row"], cell["column"]))
            header_evidence.append(cell_evidence(package, asset, table, cell))
    header_evidence = list(dict.fromkeys(header_evidence))
    evidence = [ev] + header_evidence
    metric_label = str(yheaders["metric"] if yheaders["metric"] is not None else yheader or get_column_letter(ycol))
    series = add_object(
        package,
        "series",
        label,
        evidence_refs=evidence,
        scope_key=f"{asset['asset_id']}:{table['sheet']}:{start}:{xcol}:{ycol}",
        series_origin="source_tabular_sequence",
        sample_identity="not_established",
    )
    if run_ref:
        add_relation(
            package,
            run_ref,
            "has_series",
            series,
            evidence,
            basis_kind="explicit_table_mapping",
            applicability="candidate_grouping",
        )
    else:
        add_relation(package, f"{package['header']['source_id']}:source", "contains_curve", series, evidence)
    if (
        label_cell
        and classify_cell(label_cell, interpret_missing=False)["status"] == "value"
        and spec.get("condition_from_label")
    ):
        rule = spec["condition_from_label"]
        condition = re.search(rule["pattern"], str(label_cell["raw_value"]))
        if condition and {"value", "unit"} <= condition.groupdict().keys():
            test = merge_object(
                package,
                "reported_test",
                ["source_header_group", asset["asset_id"], table["sheet"], label_cell["address"]],
                label,
                header_evidence,
                identity_context(asset, table, [label_cell]),
                identity_status="source_header_group; physical specimen and run identity not established",
            )
            assertion = add_assertion(
                package,
                test,
                "condition.parameter",
                condition.group("value"),
                header_evidence,
                raw_unit=condition.group("unit"),
                qualifiers={"parameter_id": rule["parameter_id"], "parameter_label": rule["parameter_label"]},
                value_kind="scalar",
                statement_kind="source_header_condition",
                source_context={
                    "value_status": "value",
                    "cell_bindings": [
                        binding(
                            asset,
                            table,
                            label_cell,
                            "value",
                            extraction="regex_group",
                            pattern=rule["pattern"],
                            group="value",
                        ),
                        binding(
                            asset,
                            table,
                            label_cell,
                            "unit",
                            extraction="regex_group",
                            pattern=rule["pattern"],
                            group="unit",
                        ),
                    ],
                },
            )
            next(a for a in package["assertions"] if a["assertion_id"] == assertion)["value"]["parsed"] = _number(
                {"raw_value": condition.group("value")}
            )
            add_relation(package, test, "has_series", series, evidence, basis_kind="source_header_group")
    classified = {column: [classify_numeric_cell(c) for c in cells[column]] for column in (xcol, ycol)}
    x_values = [c["parsed"] for c in classified[xcol]]
    values = [c["parsed"] for c in classified[ycol]]
    xunit, yunit = xheaders["unit"], yheaders["unit"]
    statuses = [c["status"] for c in classified[ycol]]
    xstatuses = [c["status"] for c in classified[xcol]]
    data = {
        "source_rows": rows,
        "raw_x_values": raw[xcol],
        "x_values": x_values,
        "values": values,
        "x_label": xheaders["metric"] if xheaders["metric"] is not None else xheader,
        "x_unit": xunit["unit"],
        "y_label": metric_label,
        "y_unit": yunit["unit"],
        "value_statuses": statuses,
        "x_value_statuses": xstatuses,
        "decimal_values": [c.get("parsed_decimal") for c in classified[ycol]],
        "x_decimal_values": [c.get("parsed_decimal") for c in classified[xcol]],
        "point_row_count": len(rows),
        "complete_numeric_point_count": sum(x is not None and y is not None for x, y in zip(x_values, values)),
        "missing_count": sum(_blank(c) for c in cells[ycol]),
        "x_missing_count": sum(_blank(c) for c in cells[xcol]),
        "parsing_rule": (
            "exact decimal parsing; numeric views only when lossless and finite; unresolved numeric points "
            "remain explicit; no interpolation, unit conversion or formula evaluation"
        ),
        "extra_columns": {
            get_column_letter(c): {"label": _header(table, profile, c), "raw_values": raw[c]} for c in extra_columns
        },
    }
    bindings = [
        {
            "role": role,
            "asset_ref": asset["asset_id"],
            "sheet": table["sheet"],
            "row_range": [start, end],
            "column": column,
        }
        for role, column in [("value", ycol), ("x_value", xcol)]
    ]
    bindings += (yunit["bindings"] if not yunit["ambiguous"] else []) + (
        xunit["bindings"] if not xunit["ambiguous"] else []
    )
    invalid_statuses = {s for s in statuses + xstatuses if s not in ("value", "source_blank")}
    aid = add_assertion(
        package,
        series,
        "observation.value",
        raw[ycol],
        evidence,
        raw_unit=yunit["unit"],
        qualifiers={"metric_id": "source_column:" + metric_label, "metric_label": metric_label},
        statement_kind="source_tabular_values",
        series_data=data,
        source_context={
            "asset_ref": asset["asset_id"],
            "sheet": table["sheet"],
            "x_column": xcol,
            "y_column": ycol,
            "header_context": [xheaders["context"], yheaders["context"]],
            "cell_bindings": bindings,
            "value_status": "value",
            "unit_candidate_bindings": (yunit["bindings"] if yunit["ambiguous"] else [])
            + (xunit["bindings"] if xunit["ambiguous"] else []),
        },
    )
    assertion = next(a for a in package["assertions"] if a["assertion_id"] == aid)
    if (
        xunit["ambiguous"]
        or yunit["ambiguous"]
        or xheaders["context"]["issues"]
        or yheaders["context"]["issues"]
        or invalid_statuses
        or not data["complete_numeric_point_count"]
    ):
        issue = add_issue(
            package,
            "Series has ambiguous units or unresolved/error/formula values; retained for review",
            [series],
            evidence,
            severity="error",
            code="ambiguous_series_values_or_units",
        )
        assertion["use_status"] = "blocked"
        assertion["issue_refs"].append(issue)
    for column in (xcol, ycol):
        for cell, classification in zip(cells[column], classified[column]):
            status = classification["status"]
            if status != "value":
                assess_cell(
                    package,
                    series,
                    "observation.value",
                    "series",
                    {
                        "metric_id": "source_column:" + str(yheader or get_column_letter(ycol)),
                        "metric_label": str(yheader or get_column_letter(ycol)),
                    },
                    cell,
                    evidence,
                    {"cell_bindings": [binding(asset, table, cell, "value")], "value_status": status},
                    status,
                )
    for row in profile.get("calibration_rows", []):
        calibration, label_source = _cell(table, row, ycol), _cell(table, row, 1)
        used.update({(row, ycol), (row, 1)})
        if not _blank(calibration):
            calibration_evidence = add_evidence(
                package,
                asset["asset_id"],
                _anchor(table, [row, row], [1, ycol]),
                {"cells": [label_source, calibration]},
                "table_cells",
            )
            scalar_assertion(
                package,
                asset,
                table,
                {"header_row": None},
                row,
                {
                    "column": ycol,
                    "field_id": "instrument.parameter",
                    "qualifiers": {
                        "parameter_id": "source_calibration:" + str(label_source["raw_value"]),
                        "parameter_label": str(label_source["raw_value"]),
                    },
                },
                series,
                evidence=[calibration_evidence],
                header_cell=label_source,
                statement_kind="source_recorded_calibration",
            )
    formula_missing = [c["address"] for col in columns for c in cells[col] if c.get("cache_status") == "unavailable"]
    if formula_missing:
        add_issue(
            package,
            "Formula cached value unavailable; expressions retained, no evaluation or numeric substitution",
            [series],
            evidence,
            code="formula_cache_unavailable",
            cell_addresses=formula_missing,
        )
    if not data["x_unit"] or yunit["unit"] is None:
        add_issue(
            package,
            "One or both series units are not established by mapped source headers",
            [series],
            evidence,
            code="series_unit_unresolved",
        )
    add_coverage(
        package,
        {"series_ref": series, "scope": "sample_identity_and_metric_semantics"},
        "not_checked",
        "Physical columns were mapped; scientific meaning and sample equivalence require source review",
    )
    return used


def extract_experimental(
    assets: list[dict], source_id: str, options: dict, processing_run_id: str, *, tables_by_asset: dict | None = None
) -> dict:
    """Return a portable candidate package. No network, model or reference-file IO."""
    options = options or {}
    if options.get("reading_plan") is not None:
        from ..table_plan import extract_plan

        if tables_by_asset is None:
            tables_by_asset = {
                a["asset_id"]: read_tables(
                    a["local_path"], options=options.get("reader_options", {}).get(a["asset_id"], {})
                )
                for a in assets
                if a.get("original_name", "").lower().endswith((".csv", ".xls", ".xlsx"))
            }
        return extract_plan(
            assets, source_id, options["reading_plan"], processing_run_id, tables_by_asset=tables_by_asset
        )
    package = new_package(source_id, "experimental", assets, processing_run_id)
    package["header"].update(
        extractor_version=EXTRACTOR_VERSION, extraction_mode="table_structure_mapping", model_calls=0
    )
    profiles, tasks, runs = options.get("table_profiles", []), [], {}
    for asset in assets:
        name = asset.get("original_name", "")
        if not name.lower().endswith((".csv", ".xls", ".xlsx")):
            add_coverage(
                package,
                {"asset_ref": asset["asset_id"]},
                "not_checked",
                "Supporting metadata/document retained; this extractor does not interpret it",
            )
            continue
        reader_options = options.get("reader_options", {}).get(asset["asset_id"], {})
        tables = (
            tables_by_asset.get(asset["asset_id"], [])
            if tables_by_asset is not None
            else read_tables(asset["local_path"], options=reader_options)
        )
        for table in tables:
            if table.get("extraction_safe") is False:
                ev = add_evidence(
                    package,
                    asset["asset_id"],
                    {"format": table["format"], "sheet": table["sheet"], "scope": "unresolved_text"},
                    {"raw_text": table.get("raw_text"), "parse_diagnostics": table.get("parse_diagnostics", {})},
                    "unresolved_table_text",
                )
                add_issue(
                    package,
                    "Source dialect/structure is unresolved; cell mappings were not executed",
                    evidence_refs=[ev],
                    severity="error",
                    code="unsafe_table_structure",
                )
                add_coverage(
                    package,
                    {"asset_ref": asset["asset_id"], "sheet": table["sheet"]},
                    "not_checked",
                    "Raw text retained; confirmed cell coordinates are unavailable",
                )
                continue
            ev = add_evidence(package, asset["asset_id"], _anchor(table), table, "table_transcription")
            scope = {"asset_ref": asset["asset_id"], "sheet": table["sheet"]}
            matches = [
                p
                for p in profiles
                if (p.get("asset_id") == asset["asset_id"] or p.get("original_name") == name)
                and (p.get("sheet") is None or p["sheet"] == table["sheet"])
            ]
            if not matches and options.get("auto_detect", True):
                detected = _detect_profile(table, asset)
                matches = [detected] if detected else []
            for issue in table["issues"]:
                add_issue(package, issue["description"], evidence_refs=[ev], code=issue["code"])
            if not matches:
                add_issue(
                    package,
                    "Table read and anchored; an explicit table mapping is required",
                    evidence_refs=[ev],
                    code="table_mapping_required",
                )
                add_coverage(
                    package, scope, "not_checked", "Verbatim cells retained; scientific mapping not configured"
                )
                continue
            for profile in matches:
                tasks.append((asset, table, profile, ev, scope))
    _link_instrument_templates(tasks)
    # Scalar labels first so asset submission order cannot change a grouped run label.
    tasks.sort(key=lambda task: task[2].get("layout") != "key_value")
    expanded_regions = 0
    for asset, table, profile, ev, scope in tasks:
        # A mapping is a transaction: failures cannot leak partial objects or
        # skip conflict finalization while retaining queryable assertions.
        committed_package, committed_runs = package, runs
        package, runs = snapshot_mapping(package, runs)
        mapping_scope = {**scope, "mapping_id": fingerprint(profile)[:20]}
        try:
            table = oriented_table(table, profile)
            segments = expand_profile(profile, table["nrows"])
            if "data_ranges" in profile and expanded_regions + len(segments) > MAX_REGIONS:
                raise ValueError(f"data_ranges exceeds {MAX_REGIONS} expanded regions in this extraction")
            for segment in segments:
                layout = segment.get("layout")
                if layout == "key_value":
                    _key_values(package, asset, table, segment, ev, runs)
                elif layout == "records":
                    from .row_records import extract_rows

                    extract_rows(package, asset, table, segment)
                elif layout == "objects":
                    from .object_records import extract_objects

                    extract_objects(package, asset, table, segment)
                elif layout in ("channels", "curves"):
                    run = (
                        _ensure_run(
                            package,
                            runs,
                            _run_scope(asset, table, segment),
                            f"{asset['original_name']}:{table['sheet'] or 'table'}",
                            [ev],
                        )
                        if "run_key" in segment
                        else None
                    )
                    if layout == "channels":
                        specs = [{"x_column": segment["x_column"], "y_column": c} for c in segment["y_columns"]]
                    else:
                        specs = segment["series"]
                    used = set()
                    for spec in specs:
                        used.update(_curve(package, asset, table, segment, spec, ev, run))
                    from .table_semantics import record_unmapped

                    record_unmapped(package, asset, table, used)
                    if segment.get("grouping_unresolved"):
                        add_issue(
                            package,
                            "Instrument scan lacks a unique matching scalar record; no run association asserted",
                            evidence_refs=[ev],
                            code="instrument_run_grouping_unresolved",
                        )
                    elif segment.get("grouping_signature"):
                        run_object = next(o for o in package["objects"] if o["object_id"] == run)
                        scalar_run = runs.get(segment["candidate_scalar_scope"])
                        add_issue(
                            package,
                            (
                                "Sampling count and interval match uniquely in this source bundle, but do "
                                "not independently establish the same physical run; association remains "
                                "candidate"
                            ),
                            target_refs=[ref for ref in (run, scalar_run) if ref],
                            evidence_refs=list(
                                dict.fromkeys(
                                    run_object["evidence_refs"]
                                    + (
                                        next(o for o in package["objects"] if o["object_id"] == scalar_run)[
                                            "evidence_refs"
                                        ]
                                        if scalar_run
                                        else []
                                    )
                                )
                            ),
                            code="instrument_run_identity_candidate",
                        )
                        add_coverage(
                            package,
                            {"run_ref": run, "scope": "physical_run_identity"},
                            "not_checked",
                            (
                                "No common source-declared run identifier found in both files; sampling "
                                "signature supports a candidate only"
                            ),
                        )
                else:
                    raise ValueError(f"Unsupported table layout: {layout}")
            if "data_ranges" in profile:
                expanded_regions += len(segments)
            add_coverage(
                package,
                mapping_scope,
                "extracted",
                "Configured tabular ranges extracted; original cells and unresolved semantics retained",
                mapping_profile=deepcopy(profile),
            )
        except (ValueError, KeyError, TypeError) as error:
            package, runs = committed_package, committed_runs
            add_issue(
                package,
                f"Table mapping failed: {error}",
                evidence_refs=[ev],
                severity="error",
                code="invalid_table_mapping",
            )
            add_coverage(
                package, mapping_scope, "not_checked", "Mapping failed; no partial semantic results were committed"
            )
    add_coverage(
        package,
        "registered_assets",
        "checked",
        "All registered assets classified; per-asset and semantic gaps remain explicit",
    )
    add_coverage(
        package,
        "scientific_interpretation",
        "not_checked",
        (
            "No sample equivalence, synthesis process, test qualification, causal interpretation or "
            "fitness-for-use inferred"
        ),
    )
    from ..table_quality import finalize_table_package

    return release_evidence(finalize_table_package(package))
