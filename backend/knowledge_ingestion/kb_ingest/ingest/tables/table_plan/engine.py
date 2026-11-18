"""Single interpreter for preview, extraction and replay of source-bound proofs."""

from collections import defaultdict
from copy import deepcopy

from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

from kb_ingest.contract import (
    FIELDS,
    _profile,
    add_assertion,
    add_coverage,
    add_evidence,
    add_issue,
    add_object,
    add_relation,
    new_package,
)
from kb_ingest.core.serialization import canonical, fingerprint

from .compiler import COMPILER_VERSION, QUALITY_VERSION, compile_plan, field_expected_type, records


def _blank(value):
    return value is None or value == ""


class IdentityResolutionError(ValueError):
    """Expected source identity failure, eligible for explicit record isolation."""


class Interpreter:
    def __init__(self, assets, source_id, plan, run_id, tables_by_asset):
        self.compiled = compile_plan(plan, tables_by_asset)
        self.plan = self.compiled["plan"]
        self.package = new_package(source_id, "experimental", assets, run_id)
        self.package["header"].update(
            reading_plan_version="2",
            reading_plan=deepcopy(plan),
            plan_hash=self.compiled["plan_hash"],
            compiler_version=COMPILER_VERSION,
            table_quality_version=QUALITY_VERSION,
            typed_value_version="2",
            extraction_mode="bounded_table_plan",
            model_calls=0,
        )
        self.views = {v["id"]: v for v in plan["views"]}
        self.scopes = {s["id"]: s for s in plan["scopes"]}
        self.contexts = {c["id"]: c for c in plan.get("contexts", [])}
        self.tables = {}
        self.cells = {}
        self.evidence = {}
        self.used = defaultdict(set)
        self.instances = defaultdict(list)
        self.objects = {}
        self.pending_used = None
        source_evidence = {}
        source_cells = {}
        for vid, v in self.views.items():
            t = next(t for t in tables_by_asset[v["asset_id"]] if t.get("sheet") == v["sheet"])
            self.tables[vid] = t
            source_key = (v["asset_id"], v["sheet"])
            if source_key in source_evidence:
                self.cells[vid] = source_cells[source_key]
                self.evidence[vid] = source_evidence[source_key]
                continue
            self.cells[vid] = {(c["row"], c["column"]): c for row in t["rows"] for c in row["cells"]}
            source_cells[source_key] = self.cells[vid]
            ev = add_evidence(
                self.package,
                v["asset_id"],
                {
                    "format": t["format"],
                    "sheet": t.get("sheet"),
                    "row_range": [1, t["nrows"]],
                    "columns": list(range(1, t["ncols"] + 1)),
                },
                deepcopy(t),
                "table_transcription",
            )
            self.evidence[vid] = ev
            source_evidence[source_key] = ev
        self.unselected = []
        for asset in assets:
            for index, t in enumerate(tables_by_asset.get(asset["asset_id"], [])):
                source_key = (asset["asset_id"], t.get("sheet"))
                if source_key in source_evidence:
                    continue
                vid = ("unselected", asset["asset_id"], index)
                self.views[vid] = {"asset_id": asset["asset_id"], "sheet": t.get("sheet")}
                self.tables[vid] = t
                ev = add_evidence(
                    self.package,
                    asset["asset_id"],
                    {
                        "format": t["format"],
                        "sheet": t.get("sheet"),
                        "row_range": [1, t["nrows"]],
                        "columns": list(range(1, t["ncols"] + 1)),
                    },
                    deepcopy(t),
                    "table_transcription",
                )
                self.evidence[vid] = ev
                source_evidence[source_key] = ev
                self.unselected.append(source_key)
        self.package["header"]["source_table_inventory"] = [
            {"asset_ref": key[0], "sheet": key[1]} for key in source_evidence
        ]

    def resolve(self, b, rec, role):
        if "metadata" in b:
            if role not in ("condition", "identity"):
                raise ValueError("Metadata cannot fill a measurement or unit")
            view = self.views[self.scopes[rec["scope"]]["view"]]
            raw = view["sheet"][slice(*b["span"])]
            return (
                raw,
                {
                    "asset_ref": view["asset_id"],
                    "sheet": view["sheet"],
                    "metadata": "sheet_name",
                    "span": b["span"],
                    "raw": raw,
                },
                None,
            )
        if "semantic" in b:
            if role != "condition":
                raise ValueError("Declared semantic mapping is not source identity or measurement")
            return (
                b["semantic"],
                {"kind": "declared_semantic_mapping", "value": b["semantic"], "not_source_fact": True},
                None,
            )
        if "source_code" in b:
            if role != "condition":
                raise ValueError("Vocabulary code is condition metadata only")
            raw, proof, cell = self.resolve(b["source"], rec, role)
            return (
                b["source_code"] + ":" + str(raw),
                {"kind": "source_vocabulary_code", "operation": b["source_code"], "binding": proof},
                cell,
            )
        scope = self.scopes[rec["scope"]]
        vid = scope["view"]
        view = self.views[vid]
        original_rule = b
        if "context" in b:
            ctx = self.contexts.get(b["context"])
            if (
                ctx is None
                or ctx["scope"] != rec["scope"]
                or not ctx["rows"][0] <= rec["row"] <= ctx["rows"][1]
                or not ctx["columns"][0] <= rec["column"] <= ctx["columns"][1]
            ):
                raise ValueError("Context binding outside declared record scope bounds")
            if role not in ("identity", "unit", "condition") or ctx["role"] != role:
                raise ValueError("Context role cannot fill measurement or incompatible metadata")
            b = ctx["source"]
        r = rec["row"] if b["row"] == "current" else b["row"]
        c = rec["column"] if b["column"] == "current" else b["column"]
        if view.get("transpose"):
            r, c = c, r
        cell = self.cells[vid].get((r, c))
        if cell is None:
            raise ValueError("Source binding outside original cell bounds")
        requested = [r, c]
        if b.get("merged") and cell.get("merged_origin"):
            origin = coordinate_to_tuple(cell["merged_origin"])
            matches = [
                region
                for region in self.tables[vid].get("merged_cells", [])
                if (lambda z: z[0] <= c <= z[2] and z[1] <= r <= z[3])(range_boundaries(region))
            ]
            if len(matches) != 1 or origin != tuple(reversed(range_boundaries(matches[0])[:2])):
                raise ValueError("Merged origin lacks original bounded merge evidence")
            r, c = origin
            cell = self.cells[vid].get((r, c))
            if cell is None:
                raise ValueError("Merged origin missing")
        raw = cell["raw_value"]
        if role == "identity" and ("formula" in cell or cell.get("value_type") == "e"):
            raise IdentityResolutionError("Identity cannot use formula or error cell")
        if "span" in b:
            if not isinstance(raw, str) or b["span"][1] > len(raw):
                raise ValueError("Source text span outside bounds")
            raw = raw[slice(*b["span"])]
        # An omitted identity can inherit an explicitly declared finite block.
        # A contradictory nonblank identity in that same column must never be hidden.
        if "context" in original_rule and role == "identity":
            cr, cc = rec["row"], (rec["column"] if b["column"] == "current" else b["column"])
            if view.get("transpose"):
                cr, cc = cc, cr
            candidate = self.cells[vid].get((cr, cc), {}).get("raw_value")
            if not _blank(candidate) and canonical(candidate) != canonical(cell["raw_value"]):
                raise IdentityResolutionError("Conflicting source identity inside context scope")
        proof = {
            "asset_ref": view["asset_id"],
            "sheet": view["sheet"],
            "row": r,
            "column": c,
            "raw": deepcopy(raw),
            "selector": deepcopy(original_rule),
        }
        if requested != [r, c]:
            proof["merged_target"] = requested
        used = self.used if self.pending_used is None else self.pending_used
        used[(view["asset_id"], view["sheet"])].add((r, c))
        used[(view["asset_id"], view["sheet"])].add(tuple(requested))
        selected = {**cell, "raw_value": raw}
        return raw, proof, selected

    def build_record(self, rec):
        sid = rec["scope"]
        scope = self.scopes[sid]
        vid = scope["view"]
        built = {}
        proofs = {}
        for alias in self.compiled["object_order"]:
            definition = self.plan["objects"][alias]
            if definition["scope"] != sid:
                continue
            resolved = [self.resolve(b, rec, "identity") for b in definition["key"]]
            if any(_blank(v[0]) for v in resolved):
                raise IdentityResolutionError(
                    "Source object identity is blank; use an explicit bounded identity context"
                )
            key = [v[0] for v in resolved]
            identity_scope = definition.get("identity_scope", "record")
            domain = (
                [sid]
                if identity_scope == "scope"
                else [sid, rec["block"]]
                if identity_scope == "block"
                else [sid, rec["block"], rec["row"], rec["column"]]
            )
            extra = {k + "_ref": built[definition[k]] for k in ("owner", "material") if k in definition}
            oid = (
                f"{self.package['header']['source_id']}:object:"
                f"{fingerprint([alias, definition['kind'], domain, key, extra])[:20]}"
            )
            label = (
                " / ".join(str(v) for v in key) if key else f"local {alias} {rec['block']}:{rec['row']}:{rec['column']}"
            )
            bindings = [v[1] for v in resolved]
            if "label" in definition:
                label, label_binding, _ = self.resolve(definition["label"], rec, "identity")
                label = str(label)
                bindings.append(label_binding)
            occurrence = {"record_scope": deepcopy(rec), "bindings": bindings}
            if oid not in self.objects:
                add_object(
                    self.package,
                    definition["kind"],
                    label,
                    object_id=oid,
                    evidence_refs=[self.evidence[vid]],
                    source_context={
                        "plan_alias": alias,
                        "identity_scope": identity_scope,
                        "identity_keys": deepcopy(key),
                        "identity_status": "source_declared" if key else "local_only",
                        "occurrences": [occurrence],
                    },
                    **extra,
                )
                self.objects[oid] = self.package["objects"][-1]
            else:
                if self.objects[oid]["label"] != label:
                    raise ValueError("Conflicting labels for one scoped source identity")
                self.objects[oid]["source_context"]["occurrences"].append(occurrence)
            built[alias] = oid
            proofs[alias] = bindings
        for relation in self.plan.get("relations", []):
            if relation["scope"] != sid:
                continue
            left, right = relation["from"], relation["to"]
            add_relation(
                self.package,
                built[left],
                relation["predicate"],
                built[right],
                [self.evidence[vid]],
                proof={
                    "kind": "common_record_scope",
                    "record_scope": deepcopy(rec),
                    "left_bindings": proofs[left],
                    "right_bindings": proofs[right],
                },
            )
        for field in self.plan["fields"]:
            if field["scope"] == sid:
                self.build_field(field, rec, built[field["target"]])
        self.instances[sid].append((deepcopy(rec), built))

    def build_field(self, field, rec, subject):
        from kb_ingest.typed_values import normalize_unit, parse_typed_value

        p = self.package
        vid = self.scopes[rec["scope"]]["view"]
        ev = [self.evidence[vid]]
        qualifiers = {}
        bindings = {}
        for key, b in field.get("qualifiers", {}).items():
            value, proof, _ = self.resolve(b, rec, "condition")
            qualifiers[key] = value
            bindings["qualifier:" + key] = proof
        for index, b in enumerate(field.get("header_sources", [])):
            _, proof, _ = self.resolve(b, rec, "condition")
            bindings["header:" + str(index)] = proof
        unit = basis = None
        for name in ("unit", "basis"):
            if name in field:
                value, proof, _ = self.resolve(field[name], rec, "unit")
                bindings[name] = proof
                if name == "unit":
                    unit = value
                else:
                    basis = value
        declared = field.get("expected")
        if "expected_by" in field:
            policy = field["expected_by"]
            key = str(qualifiers.get(policy["qualifier"]))
            if key not in policy["types"]:
                raise ValueError("Result type is not confirmed for source metric")
            declared = policy["types"][key]
        expected = field_expected_type(field["field_id"], declared)
        number_format = field.get("number_format", "source")
        series = field.get("mode", "series" if self.scopes[rec["scope"]]["kind"] == "curve" else "scalar") == "series"
        selected = []
        for row in range(rec["rows"][0], rec["rows"][1] + 1) if series else [rec["row"]]:
            point = {**rec, "row": row}
            value, proof, cell = self.resolve(field["source"], point, "measurement")
            if field.get("measurement_mode") == "simple":
                from ..measurement_values import simple_measurement

                typed, inline, problem = simple_measurement(cell, expected, unit, number_format=number_format)
                if problem:
                    raise ValueError("Simple measurement source needs review: " + problem)
                if inline and unit in (None, ""):
                    unit = inline["raw"]
                    bindings["unit"] = {
                        **proof,
                        "raw": inline["raw"],
                        "span": inline["span"],
                        "derived_from": "inline_unit",
                    }
            else:
                typed = parse_typed_value(cell, expected=expected, number_format=number_format)
            selected.append((value, proof, typed))
        bindings["value"] = [s[1] for s in selected] if series else selected[0][1]
        context = {
            "record_scope": deepcopy(rec),
            "bindings": bindings,
            "expected": expected,
            "number_format": number_format,
        }
        values = [s[0] for s in selected]
        typed = [s[2] for s in selected]
        raw = values if series else values[0]
        if not series and (typed[0]["kind"] == "missing" or raw is None):
            profile = _profile(p, field["field_id"], self.objects[subject]["kind"], qualifiers)
            aid = f"{p['header']['source_id']}:assessment:{fingerprint([subject, field, rec])[:20]}"
            p["assessments"].append(
                {
                    "assessment_id": aid,
                    "subject_ref": subject,
                    "field_id": field["field_id"],
                    "profile_id": profile,
                    "qualifiers": qualifiers,
                    "status": "source_missing",
                    "typed_value": typed[0],
                    "target_ref": subject,
                    "raw_value": raw,
                    **({"raw_unit": unit, "raw_basis": basis} if field.get("measurement_mode") == "simple" else {}),
                    "handling": {
                        "blank": "source_blank",
                        "not_tested": "source_not_tested",
                        "not_detected": "source_not_detected",
                        "error": "excel_error",
                    }.get(typed[0].get("status"), "source_" + typed[0].get("status", "missing")),
                    "reason": "Original source cell retained; "
                    + typed[0].get("status", "missing")
                    + " is not a measurement",
                    "source_context": context,
                    "evidence_refs": ev,
                    "source_revision_id": p["header"]["source_revision_id"],
                }
            )
            return
        # Interpretation controls representation; the original cell value stays exact.
        kind = (
            "list"
            if series
            else expected
            if expected in ("text", "category")
            else "scalar"
            if type(raw) in (int, float)
            else "text"
        )
        if kind not in FIELDS[field["field_id"]]["value_kinds"]:
            if not series and isinstance(raw, str) and "numeric_notation" in FIELDS[field["field_id"]]["value_kinds"]:
                kind = "numeric_notation"
            elif not series and type(raw) in (int, float) and "text" in FIELDS[field["field_id"]]["value_kinds"]:
                kind = "text"
            else:
                raise ValueError("Original value representation is not supported by catalogue field")
        aid = add_assertion(
            p,
            subject,
            field["field_id"],
            raw,
            ev,
            raw_unit=unit,
            raw_basis=basis,
            qualifiers=qualifiers,
            value_kind=kind,
            source_context=context,
            assertion_id=f"{p['header']['source_id']}:assertion:{fingerprint([subject, field, rec, bindings])[:20]}",
        )
        assertion = next(a for a in reversed(p["assertions"]) if a["assertion_id"] == aid)
        assertion["value"].update(typed_value_version="2", unit_normalization=normalize_unit(unit))
        if series:
            assertion["value"]["typed_values"] = typed
            assertion["series_data"] = {"raw_values": values, "typed_values": typed, "point_count": len(values)}
            if "x_source" in field:
                xs = []
                xb = []
                xt = []
                for row in range(rec["rows"][0], rec["rows"][1] + 1):
                    x, b, c = self.resolve(field["x_source"], {**rec, "row": row}, "measurement")
                    xs.append(x)
                    xb.append(b)
                    xt.append(parse_typed_value(c, expected="number", number_format=number_format))
                bindings["x_value"] = xb
                assertion["series_data"].update(x_raw_values=xs, x_typed_values=xt)
            if "x_unit" in field:
                unit, proof, _ = self.resolve(field["x_unit"], rec, "unit")
                bindings["x_unit"] = proof
                assertion["series_data"]["x_unit"] = unit
            extras = {}
            for name, binding in field.get("extra_sources", {}).items():
                items = [
                    self.resolve(binding, {**rec, "row": row}, "measurement")
                    for row in range(rec["rows"][0], rec["rows"][1] + 1)
                ]
                extras[name] = {
                    "raw_values": [i[0] for i in items],
                    "typed_values": [
                        parse_typed_value(i[2], expected="number", number_format=number_format) for i in items
                    ],
                }
                bindings["extra:" + name] = [i[1] for i in items]
            assertion["series_data"]["extra_columns"] = extras
        else:
            assertion["value"]["typed_value"] = typed[0]
        if any(t.get("status") not in ("valid", "source_missing", "missing") for t in typed):
            add_issue(
                p,
                "Source value requires typed interpretation review",
                target_refs=[aid],
                evidence_refs=ev,
                code="typed_value_requires_review",
            )

    def join(self, rule, index):
        sides = {}
        for side in ("left", "right"):
            entries = defaultdict(list)
            for rec, built in self.instances[rule[side + "_scope"]]:
                resolved = [self.resolve(b, rec, "identity") for b in rule[side + "_on"]]
                if any(_blank(v[0]) for v in resolved):
                    add_issue(
                        self.package,
                        "Blank exact join key",
                        target_refs=[built[rule[side]]],
                        severity="error",
                        code="join_missing_key",
                    )
                    continue
                entries[canonical([v[0] for v in resolved])].append((rec, built[rule[side]], [v[1] for v in resolved]))
            sides[side] = entries
        for key, lefts in sides["left"].items():
            rights = sides["right"].get(key, [])
            if len(rights) != 1 or rule["cardinality"] == "one_to_one" and len(lefts) != 1:
                add_issue(
                    self.package,
                    "Exact source join has unmatched or multiple records",
                    target_refs=list(dict.fromkeys([x[1] for x in lefts + rights])),
                    severity="error",
                    code="join_unmatched" if not rights else "join_cardinality",
                )
                continue
            right = rights[0]
            for left in lefts:
                vids = [self.scopes[r["scope"]]["view"] for r in (left[0], right[0])]
                add_relation(
                    self.package,
                    left[1],
                    rule["predicate"],
                    right[1],
                    list(dict.fromkeys(self.evidence[v] for v in vids)),
                    proof={
                        "kind": "exact_source_join",
                        "join_index": index,
                        "cardinality": rule["cardinality"],
                        "left_record": left[0],
                        "right_record": right[0],
                        "left_bindings": left[2],
                        "right_bindings": right[2],
                    },
                )
        for key, rights in sides["right"].items():
            if key not in sides["left"]:
                add_issue(
                    self.package,
                    "Exact source join right key has no left record",
                    target_refs=list(dict.fromkeys(x[1] for x in rights)),
                    severity="error",
                    code="join_unmatched",
                )

    def execute(self):
        for scope in self.plan["scopes"]:
            for rec in records(scope):
                self.pending_used = defaultdict(set)
                if scope.get("missing_identity") == "retain_unmapped":
                    try:
                        for definition in self.plan["objects"].values():
                            if definition["scope"] != scope["id"]:
                                continue
                            for binding in definition["key"]:
                                raw, _, _ = self.resolve(binding, rec, "identity")
                                if _blank(raw):
                                    raise IdentityResolutionError("Missing source identity")
                    except IdentityResolutionError:
                        add_issue(
                            self.package,
                            "Record has unresolved source identity; retained without guessed objects",
                            evidence_refs=[self.evidence[scope["view"]]],
                            severity="error",
                            code="missing_record_identity",
                            record_scope=deepcopy(rec),
                            issue_id=f"{self.package['header']['source_id']}:issue:"
                            f"{fingerprint(['missing_record_identity', rec])[:20]}",
                        )
                        add_coverage(
                            self.package,
                            {"record_scope": deepcopy(rec)},
                            "not_checked",
                            "Identity cannot be resolved; source cells remain unbound",
                        )
                        self.pending_used = None
                        continue
                self.build_record(rec)
                for key, points in self.pending_used.items():
                    self.used[key].update(points)
                self.pending_used = None
        for index, rule in enumerate(self.plan.get("joins", [])):
            self.join(rule, index)
        p = self.package
        # Every retained cell has a recorded handling outcome. Unmapped content is
        # visible and prevents an apparently complete extraction.
        visited = set()
        unhandled = 0
        for vid, table in self.tables.items():
            v = self.views[vid]
            key = (v["asset_id"], v["sheet"])
            if key in visited:
                continue
            visited.add(key)
            states = defaultdict(list)
            for row in table["rows"]:
                for cell in row["cells"]:
                    coord = (cell["row"], cell["column"])
                    state = (
                        "extracted"
                        if coord in self.used[key]
                        else "source_blank"
                        if _blank(cell["raw_value"])
                        else "not_mapped"
                    )
                    states[state].append([*coord])
                    if state == "not_mapped":
                        unhandled += 1
            for state, coords in states.items():
                add_coverage(
                    p,
                    {"asset_ref": key[0], "sheet": key[1], "cell_state": state},
                    "extracted"
                    if state == "extracted"
                    else "source_uncertain"
                    if state == "source_blank"
                    else "not_checked",
                    "Every original cell classified by the shared plan",
                    cells=coords,
                )
        for asset, sheet in self.unselected:
            add_coverage(
                p,
                {"asset_ref": asset, "sheet": sheet, "scope": "unselected_table"},
                "not_checked",
                "Original worksheet is inventoried but not selected for interpretation",
            )
        add_coverage(p, "registered_assets", "checked", "Source bundle registered; table-level gaps remain explicit")
        for asset in p["assets"]:
            if not any(key[0] == asset["asset_id"] for key in visited):
                add_coverage(
                    p,
                    {"asset_ref": asset["asset_id"]},
                    "not_checked",
                    "Supporting asset is separate and not interpreted by this plan",
                )
        from ..table_quality import apply_scientific_guards

        apply_scientific_guards(p)
        p["header"]["table_plan_summary"] = {
            "records": self.compiled["record_count"],
            "unmapped_cells": unhandled,
            "completeness": "partial"
            if unhandled or self.unselected or any(i["severity"] == "error" for i in p["issues"])
            else "selected_scope_accounted",
            "objects": len(p["objects"]) - 1,
            "assertions": len(p["assertions"]),
            "assessments": len(p["assessments"]),
            "relations": len(p["relations"]),
        }
        return p


def extract_plan(assets, source_id, plan, processing_run_id, *, tables_by_asset=None):
    if tables_by_asset is None:
        from ..readers.tables import read_tables

        tables_by_asset = {
            a["asset_id"]: read_tables(a["local_path"])
            for a in assets
            if a.get("original_name", "").lower().endswith((".csv", ".xls", ".xlsx"))
        }
    registered = {a["asset_id"] for a in assets}
    if any(v.get("asset_id") not in registered for v in plan.get("views", [])):
        raise ValueError("Reading plan view is outside registered source bundle")
    return Interpreter(assets, source_id, plan, processing_run_id, tables_by_asset).execute()


def preview_plan(assets, source_id, plan, processing_run_id="preview", *, tables_by_asset=None):
    p = extract_plan(assets, source_id, plan, processing_run_id, tables_by_asset=tables_by_asset)
    return {
        "plan_hash": p["header"]["plan_hash"],
        "compiler_version": COMPILER_VERSION,
        "quality_version": QUALITY_VERSION,
        "package": p,
        "summary": p["header"]["table_plan_summary"],
    }
