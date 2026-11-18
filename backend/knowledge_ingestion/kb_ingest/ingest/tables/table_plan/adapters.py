"""Compile source-backed legacy profiles to v2 instructions, without executing v1.

Semantic literals that have no exact source representation are not silently
invented; such profiles require an explicit v2 source binding before confirmation.
"""

import re
from copy import deepcopy

from openpyxl.utils.cell import column_index_from_string, coordinate_to_tuple, get_column_letter

from kb_ingest.contract import FIELDS, PROFILES
from kb_ingest.core import fingerprint

from .compiler import compile_plan, field_expected_type


def _col(value):
    return column_index_from_string(value) if isinstance(value, str) else value


def _at(row, column):
    return {"row": row, "column": _col(column)}


def adapt_profiles(profiles, tables_by_asset):
    from ..extractors.table_orientation import oriented_table
    from ..table_recipe import validate_profile_list

    record_domains = {}
    plan = {"version": "2", "views": [], "scopes": [], "objects": {}, "fields": [], "relations": []}
    for pi, original in enumerate(profiles):
        profile = deepcopy(original)
        aid = profile["asset_id"]
        tables = tables_by_asset.get(aid, [])
        if "sheet" not in profile and len(tables) == 1:
            profile["sheet"] = tables[0].get("sheet")
        validate_profile_list([profile], tables, aid)
        t = next(t for t in tables if t.get("sheet") == profile["sheet"])
        oriented = oriented_table(t, profile)
        cells = {(c["row"], c["column"]): c for r in oriented["rows"] for c in r["cells"]}
        vid = f"view_{pi}"
        plan["views"].append(
            {"id": vid, "asset_id": aid, "sheet": t["sheet"], "transpose": profile.get("orientation") == "transpose"}
        )
        layout = profile["layout"]
        header = profile.get("header_row", 1)
        ranges = profile.get("data_ranges") or [
            [
                profile.get("data_start_row", 1 if layout == "key_value" else (header or 0) + 1),
                profile.get("data_end_row", oriented["nrows"]),
            ]
        ]

        def source_literal(value, field=None):
            # Source coordinate is inferred only from an exact unique source
            # header occurrence; a repeated label requires an explicit binding.
            preferred = []
            if field and "column" in field and header:
                preferred = [cells.get((header, _col(field["column"])))]
            candidates = preferred + [c for (r, _), c in cells.items() if r < ranges[0][0] or layout == "key_value"]
            found = []
            for c in candidates:
                if c is None:
                    continue
                raw = c["raw_value"]
                b = _at(c["row"], c["column"])
                if raw == value:
                    if b not in found:
                        found.append(b)
                elif isinstance(raw, str) and isinstance(value, str) and value and raw.count(value) == 1:
                    start = raw.index(value)
                    b["span"] = [start, start + len(value)]
                    if b not in found:
                        found.append(b)
            if len(found) != 1:
                raise ValueError("Legacy literal requires an unambiguous original source binding")
            return found[0]

        blocks = ranges if "data_ranges" in profile else None
        for ri, rows in enumerate([[ranges[0][0], ranges[-1][1]]] if blocks else ranges):
            sid = f"scope_{pi}_{ri}"
            prefix = sid + "_"

            def obj(alias, kind, key, **extras):
                name = prefix + alias
                plan["objects"][name] = {"kind": kind, "scope": sid, "key": key, **extras}
                return name

            def rel(left, right, predicate):
                plan["relations"].append(
                    {"scope": sid, "from": prefix + left, "to": prefix + right, "predicate": predicate}
                )

            def semantic_qualifier(name, value, spec):
                if (
                    isinstance(value, str)
                    and ":" in value
                    and value.split(":", 1)[0] in ("source_column", "source_key", "source_calibration")
                ):
                    code, literal = value.split(":", 1)
                    return {"source_code": code, "source": source_literal(literal, spec)}
                if name.endswith("_id") or any(p["qualifiers"].get(name) == value for p in PROFILES.values()):
                    return {"semantic": str(value)}
                try:
                    return source_literal(value, spec)
                except ValueError:
                    return {"semantic": str(value)}

            def header_bindings(column, spec=None, axis="value"):
                from ..extractors.table_headers import resolve_headers

                resolved = resolve_headers(
                    {"asset_id": aid}, oriented, profile, column, spec or {}, row=rows[0], axis=axis
                )
                if resolved["unit"]["ambiguous"]:
                    raise ValueError("Conflicting original header unit bindings require review")
                unit = None
                if resolved["unit"]["unit"] is not None:
                    value = resolved["unit"]["unit"]
                    for b in resolved["unit"]["bindings"]:
                        r, c = b["row"], b["column"]
                        if profile.get("orientation") == "transpose":
                            r, c = c, r
                        raw = cells[(r, c)]["raw_value"]
                        if raw == value:
                            unit = _at(r, c)
                            break
                        if isinstance(raw, str) and isinstance(value, str) and raw.count(value) == 1:
                            start = raw.index(value)
                            unit = {**_at(r, c), "span": [start, start + len(value)]}
                            break
                    if unit is None:
                        raise ValueError("Normalized header unit requires an explicit original source span")
                return unit, [_at(c["row"], c["column"]) for c in resolved["cells"]]

            def field(spec, alias, source):
                declared = (
                    "number"
                    if spec.get("parse") == "number"
                    else {
                        "scalar": "number",
                        "integer": "number",
                        "number": "number",
                        "text": "text",
                        "category": "category",
                    }.get(spec.get("value_kind"))
                )
                kinds = set(FIELDS[spec["field_id"]]["value_kinds"])
                # Legacy mixed fields accepted both categorical source text and
                # numbers. Migration must not introduce a numeric-only promise.
                # Explicit declarations and quantity-only catalogue fields keep
                # their original strict interpretation.
                if (
                    declared is None
                    and "value_kind" not in spec
                    and "text" in kinds
                    and kinds & {"number", "integer", "scalar", "interval", "ratio", "statistics", "numeric_notation"}
                ):
                    declared = "auto"
                f = {
                    "scope": sid,
                    "target": prefix + alias,
                    "field_id": spec["field_id"],
                    "source": source,
                    "expected": field_expected_type(spec["field_id"], declared),
                }
                qualifiers = {
                    k: semantic_qualifier(k, v, spec)
                    for k, v in spec.get("qualifiers", {}).items()
                    if k not in spec.get("qualifier_columns", {})
                    and k not in spec.get("qualifier_metadata_sources", {})
                }
                qualifiers.update({k: _at(source["row"], c) for k, c in spec.get("qualifier_columns", {}).items()})
                qualifiers.update(
                    {
                        k: {"metadata": "sheet_name", "span": rule["span"]}
                        for k, rule in spec.get("qualifier_metadata_sources", {}).items()
                    }
                )
                if qualifiers:
                    f["qualifiers"] = qualifiers
                for name in ("unit", "basis"):
                    if name + "_column" in spec:
                        f[name] = _at(source["row"], spec[name + "_column"])
                    elif name + "_cell" in spec:
                        r, c = coordinate_to_tuple(spec[name + "_cell"])
                        f[name] = _at(r, c)
                    elif name == "unit" and spec.get("unit_row", profile.get("unit_row")):
                        f[name] = _at(spec.get("unit_row", profile.get("unit_row")), spec["column"])
                if "column" in spec:
                    resolved_unit, headers = header_bindings(spec["column"], spec)
                    if "unit" not in f and resolved_unit is not None:
                        f["unit"] = resolved_unit
                    if headers:
                        f["header_sources"] = headers
                plan["fields"].append(f)
                return f

            if layout in ("records", "objects"):
                plan["scopes"].append(
                    {
                        "id": sid,
                        "view": vid,
                        "kind": "row",
                        "rows": rows,
                        "columns": [1, oriented["ncols"]],
                        "missing_identity": "retain_unmapped",
                    }
                )
                if blocks:
                    plan["scopes"][-1]["blocks"] = [{"rows": b, "columns": [1, oriented["ncols"]]} for b in blocks]
                if layout == "objects":
                    for alias, d in profile["objects"].items():
                        extras = {"identity_scope": "block" if blocks else "scope"}
                        for ref in ("owner", "material"):
                            if ref in d:
                                extras[ref] = prefix + d[ref]
                        if "label_column" in d:
                            extras["label"] = _at("current", d["label_column"])
                        obj(alias, d["kind"], [_at("current", c) for c in d["key_columns"]], **extras)
                    for r in profile.get("relations", []):
                        rel(r["from"], r["to"], r["predicate"])
                else:
                    experiment = profile.get("experiment_columns", [])
                    sample = experiment + profile["sample_columns"]
                    test = sample + profile["test_columns"]
                    obj(
                        "sample",
                        "sample",
                        [_at("current", c) for c in sample],
                        identity_scope="block" if blocks else "scope",
                    )
                    obj(
                        "test",
                        "reported_test",
                        [_at("current", c) for c in test],
                        identity_scope="block" if blocks else "scope",
                    )
                    rel("sample", "test", "tested_by")
                    if experiment:
                        obj(
                            "experiment",
                            "content_unit",
                            [_at("current", c) for c in experiment],
                            identity_scope="block" if blocks else "scope",
                        )
                        rel("experiment", "sample", "contains_sample")
                for fi, spec in enumerate(profile["fields"]):
                    alias = spec["target"]
                    if layout == "records" and alias == "observation":
                        alias = "observation_" + str(fi)
                        obj(alias, "observation", [])
                        rel("test", alias, "produces_observation")
                    field(spec, alias, _at("current", spec["column"]))
            elif layout in ("channels", "curves"):
                specs = (
                    profile["series"]
                    if layout == "curves"
                    else [{"x_column": profile["x_column"], "y_column": c} for c in profile["y_columns"]]
                )
                # Each declared channel is its own bounded curve scope. No guessed
                # run equivalence is introduced from filenames or row counts.
                for ci, spec in enumerate(specs):
                    sid = f"scope_{pi}_{ri}_curve_{ci}"
                    prefix = sid + "_"
                    yc = _col(spec["y_column"])
                    plan["scopes"].append({"id": sid, "view": vid, "kind": "curve", "rows": rows, "columns": [yc, yc]})
                    label = _at(header, yc) if header else None
                    if spec.get("label_cell"):
                        r, c = coordinate_to_tuple(spec["label_cell"])
                        label = _at(r, c)
                    obj("series", "series", [label] if label else [])
                    f = field(
                        {"field_id": "observation.value", "column": yc, "value_kind": "number"},
                        "series",
                        _at("current", yc),
                    )
                    f["x_source"] = _at("current", spec["x_column"])
                    metric = _at(header, yc) if header else label
                    if metric:
                        f["qualifiers"] = {
                            "metric_label": metric,
                            "metric_id": {"source_code": "source_column", "source": metric},
                        }
                    xunit, xheaders = header_bindings(spec["x_column"], axis="x")
                    if xunit is not None:
                        f["x_unit"] = xunit
                    if xheaders:
                        f.setdefault("header_sources", []).extend(xheaders)
                    if spec.get("extra_columns"):
                        f["extra_sources"] = {
                            get_column_letter(_col(c)): _at("current", c) for c in spec["extra_columns"]
                        }
                    if spec.get("condition_from_label") and label:
                        rule = spec["condition_from_label"]
                        raw = cells[(label["row"], label["column"])]["raw_value"]
                        matched = re.search(rule["pattern"], str(raw))
                        if matched:
                            obj("condition", "reported_test", [label])
                            rel("condition", "series", "has_series")
                            plan["fields"].append(
                                {
                                    "scope": sid,
                                    "target": prefix + "condition",
                                    "field_id": "condition.parameter",
                                    "source": {**label, "span": list(matched.span("value"))},
                                    "unit": {**label, "span": list(matched.span("unit"))},
                                    "mode": "scalar",
                                    "qualifiers": {
                                        "parameter_id": {"semantic": rule["parameter_id"]},
                                        "parameter_label": {"semantic": rule["parameter_label"]},
                                    },
                                }
                            )
                    for calibration_row in profile.get("calibration_rows", []):
                        if cells.get((calibration_row, yc), {}).get("raw_value") in (None, ""):
                            continue
                        calibration_label = _at(calibration_row, 1)
                        plan["fields"].append(
                            {
                                "scope": sid,
                                "target": prefix + "series",
                                "field_id": "instrument.parameter",
                                "source": _at(calibration_row, yc),
                                "mode": "scalar",
                                "expected": "auto",
                                "qualifiers": {
                                    "parameter_label": calibration_label,
                                    "parameter_id": {"source_code": "source_calibration", "source": calibration_label},
                                },
                            }
                        )
            elif layout == "key_value":
                kc = _col(profile.get("key_column", 1))
                vc = _col(profile.get("value_column", 2))
                keyrows = {str(cells[(r, kc)]["raw_value"]): r for r in range(rows[0], rows[1] + 1) if (r, kc) in cells}
                keys = [
                    str(cells[(r, kc)]["raw_value"])
                    for r in range(rows[0], rows[1] + 1)
                    if (r, kc) in cells and cells[(r, kc)]["raw_value"] not in (None, "")
                ]
                if len(keys) != len(set(keys)):
                    raise ValueError(
                        (
                            "Repeated metadata keys require explicit bounded record scopes; duplicate "
                            "occurrences cannot use last-wins"
                        )
                    )
                record = profile.get("record_scope")
                domain = None
                labelrow = keyrows.get(profile.get("label_key"))
                labelvc = vc
                if record:
                    domain = fingerprint(
                        [
                            aid,
                            profile["sheet"],
                            record["row_range"],
                            record.get("key_column", kc),
                            record.get("value_column", vc),
                            record.get("label_key", profile.get("label_key")),
                        ]
                    )
                    labelkc = _col(record.get("key_column", kc))
                    labelvc = _col(record.get("value_column", vc))
                    labelkey = record.get("label_key", profile.get("label_key"))
                    labels = [
                        r
                        for r in range(record["row_range"][0], record["row_range"][1] + 1)
                        if cells.get((r, labelkc), {}).get("raw_value") == labelkey
                    ]
                    if len(labels) != 1:
                        raise ValueError("Enclosing record requires exactly one source identity label")
                    labelrow = labels[0]
                if domain in record_domains:
                    sid, prefix = record_domains[domain]
                else:
                    plan["scopes"].append(
                        {
                            "id": sid,
                            "view": vid,
                            "kind": "block",
                            "rows": record["row_range"] if record else rows,
                            "columns": [1, oriented["ncols"]],
                        }
                    )
                    obj("run", "instrument_run", [_at(labelrow, labelvc)] if labelrow else [])
                    if domain is not None:
                        record_domains[domain] = (sid, prefix)
                for fi, (key, spec) in enumerate(profile.get("fields", {}).items()):
                    if key not in keyrows:
                        raise ValueError("Key-value source key is absent")
                    target = spec.get("target", "run")
                    if target == "observation":
                        target = f"observation_{pi}_{fi}"
                        obj(target, "observation", [], label=_at(keyrows[key], kc))
                        rel(target, "run", "observes_subject")
                    field(spec, target, _at(keyrows[key], vc))
            else:
                raise ValueError("Unsupported legacy layout")
    compile_plan(plan, tables_by_asset)
    return plan
