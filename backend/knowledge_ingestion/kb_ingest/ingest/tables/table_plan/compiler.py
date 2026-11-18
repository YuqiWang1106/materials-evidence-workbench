"""Bounded JSON-only reading-plan compiler; all coordinates are one-based."""

from copy import deepcopy

from kb_ingest.contract import FIELDS, RELATIONS
from kb_ingest.core import fingerprint

COMPILER_VERSION = "table-plan-2.0"
QUALITY_VERSION = "tabular-quality-2"
MAX_RECORDS = 100_000
MAX_BINDINGS = 1_000_000
MAX_OUTPUTS = 200_000


def field_expected_type(field_id, declared=None):
    """Resolve one catalogue-constrained interpretation for every plan entrypoint."""
    kinds = set(FIELDS[field_id]["value_kinds"])
    supported = {"number", "text", "category", "auto", "date", "time", "datetime", "duration"}
    if declared is not None and declared not in supported:
        raise ValueError("Unsupported expected value type")
    if kinds == {"text"}:
        if declared not in (None, "auto", "text"):
            raise ValueError("Expected value type conflicts with catalogue text-only field")
        return "text"
    if declared is not None and declared != "auto":
        return declared
    if declared == "auto":
        return "auto"
    if kinds & {"number", "integer", "scalar", "interval", "ratio", "statistics", "numeric_notation"}:
        return "number"
    if "category" in kinds:
        return "category"
    return "text" if "text" in kinds else "auto"


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("Plan contains unsupported configuration keys or non-object")
    if not set(required) <= set(value):
        raise ValueError("Plan is missing required configuration keys")


def bounds(pair, upper):
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or any(type(v) is not int for v in pair)
        or not 1 <= pair[0] <= pair[1] <= upper
    ):
        raise ValueError("Plan range is outside source bounds")


def binding(rule, *, measurement=False):
    if isinstance(rule, dict) and "metadata" in rule:
        keys(rule, {"metadata", "span"}, {"metadata", "span"})
        if measurement or rule["metadata"] != "sheet_name":
            raise ValueError("Metadata is not measurement data")
        span = rule["span"]
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(v) is not int for v in span)
            or not 0 <= span[0] < span[1] <= 4096
        ):
            raise ValueError("Invalid metadata span")
        return
    if isinstance(rule, dict) and "semantic" in rule:
        keys(rule, {"semantic"}, {"semantic"})
        if measurement or not isinstance(rule["semantic"], str) or not 1 <= len(rule["semantic"]) <= 256:
            raise ValueError("Semantic mapping is metadata only, not a measurement")
        return
    if isinstance(rule, dict) and "source_code" in rule:
        keys(rule, {"source_code", "source"}, {"source_code", "source"})
        if measurement or rule["source_code"] not in ("source_column", "source_key", "source_calibration"):
            raise ValueError("Unsupported source vocabulary code")
        if "source_code" in rule["source"] or "semantic" in rule["source"]:
            raise ValueError("Nested vocabulary codes are unsupported")
        binding(rule["source"])
        return
    if isinstance(rule, dict) and "context" in rule:
        keys(rule, {"context"}, {"context"})
        if measurement:
            raise ValueError("Context cannot fill a measurement")
        return
    keys(rule, {"row", "column", "merged", "span"}, {"row", "column"})
    for name in ("row", "column"):
        v = rule[name]
        if v != "current" and (type(v) is not int or v < 1):
            raise ValueError("Source binding requires fixed or current coordinates")
    if "merged" in rule and type(rule["merged"]) is not bool:
        raise ValueError("merged must be boolean")
    if measurement and rule.get("merged"):
        raise ValueError("Merged identity cannot fill a measurement")
    if "span" in rule:
        span = rule["span"]
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(v) is not int for v in span)
            or not 0 <= span[0] < span[1] <= 4096
        ):
            raise ValueError("Invalid bounded source text span")


def records(scope):
    blocks = scope.get("blocks") or [{"rows": scope["rows"], "columns": scope["columns"]}]
    for block_index, block in enumerate(blocks):
        rs, cs = block["rows"], block["columns"]
        if scope["kind"] == "row":
            coords = ((r, cs[0]) for r in range(rs[0], rs[1] + 1))
        elif scope["kind"] in ("column", "curve"):
            coords = ((rs[0], c) for c in range(cs[0], cs[1] + 1))
        elif scope["kind"] == "matrix":
            coords = ((r, c) for r in range(rs[0], rs[1] + 1) for c in range(cs[0], cs[1] + 1))
        else:
            coords = [(rs[0], cs[0])]
        for r, c in coords:
            yield {"scope": scope["id"], "block": block_index, "row": r, "column": c, "rows": rs, "columns": cs}


def compile_plan(plan, tables_by_asset):
    keys(
        plan,
        {"version", "views", "scopes", "objects", "fields", "contexts", "relations", "joins"},
        {"version", "views", "scopes", "objects", "fields"},
    )
    if plan["version"] != "2":
        raise ValueError("Unsupported reading plan version")
    for name in ("views", "scopes", "fields", "contexts", "relations", "joins"):
        if not isinstance(plan.get(name, []), list) or len(plan.get(name, [])) > 512:
            raise ValueError("Plan arrays must have at most 512 entries")
    if not isinstance(plan["objects"], dict) or len(plan["objects"]) > 128:
        raise ValueError("Plan objects must have at most 128 aliases")
    views = {}
    tables = {}
    cells = {}
    for v in plan["views"]:
        keys(v, {"id", "asset_id", "sheet", "transpose"}, {"id", "asset_id", "sheet"})
        if not isinstance(v["id"], str) or not v["id"] or v["id"] in views:
            raise ValueError("Duplicate or invalid view identity")
        if "transpose" in v and type(v["transpose"]) is not bool:
            raise ValueError("transpose must be boolean")
        matches = [t for t in tables_by_asset.get(v["asset_id"], []) if t.get("sheet") == v["sheet"]]
        if len(matches) != 1 or matches[0].get("extraction_safe") is False:
            raise ValueError("View requires one safely read table in the source bundle")
        table = matches[0]
        views[v["id"]] = v
        tables[v["id"]] = table
        cells[v["id"]] = {(c["row"], c["column"]): c for row in table["rows"] for c in row["cells"]}
    scopes = {}
    record_count = 0
    for s in plan["scopes"]:
        keys(
            s,
            {"id", "view", "kind", "rows", "columns", "blocks", "missing_identity"},
            {"id", "view", "kind", "rows", "columns"},
        )
        if not isinstance(s["id"], str) or not s["id"] or s["id"] in scopes or s["view"] not in views:
            raise ValueError("Duplicate scope or unknown view")
        if s.get("missing_identity", "error") not in ("error", "retain_unmapped"):
            raise ValueError("Unknown missing identity policy")
        if s["kind"] not in ("row", "column", "matrix", "block", "curve"):
            raise ValueError("Unknown scope iteration")
        v = views[s["view"]]
        t = tables[s["view"]]
        nr, nc = (t["ncols"], t["nrows"]) if v.get("transpose") else (t["nrows"], t["ncols"])
        bounds(s["rows"], nr)
        bounds(s["columns"], nc)
        if "blocks" in s:
            if not isinstance(s["blocks"], list) or not 1 <= len(s["blocks"]) <= 128:
                raise ValueError("Expected 1..128 blocks")
            prior = []
            for b in s["blocks"]:
                keys(b, {"rows", "columns"}, {"rows", "columns"})
                bounds(b["rows"], nr)
                bounds(b["columns"], nc)
                if (
                    not s["rows"][0] <= b["rows"][0] <= b["rows"][1] <= s["rows"][1]
                    or not s["columns"][0] <= b["columns"][0] <= b["columns"][1] <= s["columns"][1]
                ):
                    raise ValueError("Block outside scope bounds")
                if any(
                    max(b["rows"][0], o["rows"][0]) <= min(b["rows"][1], o["rows"][1])
                    and max(b["columns"][0], o["columns"][0]) <= min(b["columns"][1], o["columns"][1])
                    for o in prior
                ):
                    raise ValueError("Overlapping blocks")
                prior.append(b)
        for block in s.get("blocks") or [s]:
            height = block["rows"][1] - block["rows"][0] + 1
            width = block["columns"][1] - block["columns"][0] + 1
            record_count += (
                height
                if s["kind"] == "row"
                else width
                if s["kind"] in ("column", "curve")
                else height * width
                if s["kind"] == "matrix"
                else 1
            )
        if record_count > MAX_RECORDS:
            raise ValueError("Plan exceeds bounded record budget")
        scopes[s["id"]] = s
    contexts = {}
    for c in plan.get("contexts", []):
        keys(
            c,
            {"id", "scope", "role", "source", "rows", "columns", "target"},
            {"id", "scope", "role", "source", "rows", "columns"},
        )
        if c["id"] in contexts or c["scope"] not in scopes or c["role"] not in ("identity", "condition", "unit"):
            raise ValueError("Invalid context identity, role or scope")
        if "target" in c and (not isinstance(c["target"], str) or not 1 <= len(c["target"]) <= 128):
            raise ValueError("Invalid context target")
        binding(c["source"])
        if "context" in c["source"]:
            raise ValueError("Nested contexts are unsupported")
        s = scopes[c["scope"]]
        bounds(c["rows"], s["rows"][1])
        bounds(c["columns"], s["columns"][1])
        if c["rows"][0] < s["rows"][0] or c["columns"][0] < s["columns"][0]:
            raise ValueError("Context outside scope bounds")
        for old in contexts.values():
            if (
                old["scope"] == c["scope"]
                and old["role"] == c["role"]
                and old.get("target", old["source"].get("column")) == c.get("target", c["source"].get("column"))
                and max(old["rows"][0], c["rows"][0]) <= min(old["rows"][1], c["rows"][1])
                and max(old["columns"][0], c["columns"][0]) <= min(old["columns"][1], c["columns"][1])
            ):
                raise ValueError("Overlapping contexts require disjoint bounds")
        contexts[c["id"]] = c
    objects = plan["objects"]
    kinds = set().union(
        *(set(f["owner_kinds"]) for f in FIELDS.values()),
        *(set(r["allowed_from_kinds"] + r["allowed_to_kinds"]) for r in RELATIONS.values()),
    )
    for alias, d in objects.items():
        keys(d, {"kind", "scope", "key", "label", "identity_scope", "owner", "material"}, {"kind", "scope", "key"})
        if not isinstance(alias, str) or not alias or d["kind"] not in kinds - {"source"} or d["scope"] not in scopes:
            raise ValueError("Invalid object alias, kind or scope")
        if not isinstance(d["key"], list) or len(d["key"]) > 32:
            raise ValueError("Object key must have <=32 bindings")
        for b in d["key"]:
            binding(b)
        if d.get("identity_scope", "record") not in ("record", "scope", "block"):
            raise ValueError("Invalid object identity scope")
        if not d["key"] and d.get("identity_scope", "record") != "record":
            raise ValueError("Local object without identity must stay in its record")
        if "label" in d:
            binding(d["label"])
        for name in ("owner", "material"):
            if name in d and (d[name] not in objects or objects[d[name]]["scope"] != d["scope"]):
                raise ValueError("Object dependency outside record scope")
        if d["kind"] == "component":
            if (
                not {"owner", "material"} <= set(d)
                or objects[d["owner"]]["kind"] not in ("formulation", "patent_claim")
                or objects[d["material"]]["kind"] not in ("material", "formulation")
            ):
                raise ValueError("Component requires valid formulation and material")
    ready = []
    pending = dict(objects)
    while pending:
        available = [a for a, d in pending.items() if all(d[k] in ready for k in ("owner", "material") if k in d)]
        if not available:
            raise ValueError("Cyclic object dependencies")
        for alias in available:
            ready.append(alias)
            pending.pop(alias)

    def relation(r, left, right):
        if r["predicate"] not in RELATIONS or left not in objects or right not in objects:
            raise ValueError("Unknown relation or endpoint")
        rule = RELATIONS[r["predicate"]]
        if (
            objects[left]["kind"] not in rule["allowed_from_kinds"]
            or objects[right]["kind"] not in rule["allowed_to_kinds"]
        ):
            raise ValueError("Relation violates catalogue endpoint kinds")
        if (
            r["predicate"] == "contains_component"
            and objects[right]["kind"] == "component"
            and objects[right]["owner"] != left
        ):
            raise ValueError("Component owner reference conflicts with relationship")
        if (
            r["predicate"] == "refers_to_material"
            and objects[left]["kind"] == "component"
            and objects[left]["material"] != right
        ):
            raise ValueError("Component material reference conflicts with relationship")

    for f in plan["fields"]:
        keys(
            f,
            {
                "scope",
                "target",
                "field_id",
                "source",
                "expected",
                "number_format",
                "unit",
                "basis",
                "qualifiers",
                "x_source",
                "x_unit",
                "extra_sources",
                "mode",
                "header_sources",
                "expected_by",
                "measurement_mode",
            },
            {"scope", "target", "field_id", "source"},
        )
        if (
            f["scope"] not in scopes
            or f["target"] not in objects
            or objects[f["target"]]["scope"] != f["scope"]
            or f["field_id"] not in FIELDS
            or objects[f["target"]]["kind"] not in FIELDS[f["field_id"]]["owner_kinds"]
        ):
            raise ValueError("Field violates catalogue or record scope")
        binding(f["source"], measurement=True)
        for k in ("unit", "basis", "x_source", "x_unit"):
            if k in f:
                binding(f[k], measurement=k == "x_source")
        if f.get("mode", "series" if scopes[f["scope"]]["kind"] == "curve" else "scalar") not in ("scalar", "series"):
            raise ValueError("Unknown field mode")
        if f.get("mode") == "series" and scopes[f["scope"]]["kind"] != "curve":
            raise ValueError("Series field requires curve scope")
        if not isinstance(f.get("extra_sources", {}), dict) or len(f.get("extra_sources", {})) > 32:
            raise ValueError("Invalid extra series sources")
        for b in f.get("extra_sources", {}).values():
            binding(b, measurement=True)
        if not isinstance(f.get("header_sources", []), list) or len(f.get("header_sources", [])) > 128:
            raise ValueError("Invalid header sources")
        for b in f.get("header_sources", []):
            binding(b)
        if not isinstance(f.get("qualifiers", {}), dict) or len(f.get("qualifiers", {})) > 64:
            raise ValueError("Invalid qualifiers")
        for b in f.get("qualifiers", {}).values():
            binding(b)
        field_expected_type(f["field_id"], f.get("expected"))
        if "expected_by" in f:
            dynamic = f["expected_by"]
            keys(dynamic, {"qualifier", "types"}, {"qualifier", "types"})
            if (
                dynamic["qualifier"] not in f.get("qualifiers", {})
                or not isinstance(dynamic["types"], dict)
                or not dynamic["types"]
            ):
                raise ValueError("Expected types require a source qualifier and nonempty mapping")
            for name, expected in dynamic["types"].items():
                if not isinstance(name, str) or not name:
                    raise ValueError("Expected type key must match a source qualifier")
                field_expected_type(f["field_id"], expected)
        if "measurement_mode" in f and (f["measurement_mode"] != "simple" or f["field_id"] != "observation.value"):
            raise ValueError("Unsupported measurement interpretation policy")
        if f.get("number_format", "source") not in ("source", "decimal_dot", "decimal_comma", "en_US", "de_DE"):
            raise ValueError("Unsupported number format")
        if "x_source" in f and scopes[f["scope"]]["kind"] != "curve":
            raise ValueError("Series axes require a curve scope")
    for r in plan.get("relations", []):
        keys(r, {"scope", "from", "to", "predicate"}, {"scope", "from", "to", "predicate"})
        relation(r, r["from"], r["to"])
        if r["scope"] not in scopes or any(objects[r[k]]["scope"] != r["scope"] for k in ("from", "to")):
            raise ValueError("Relation endpoints lack common record scope")
    for j in plan.get("joins", []):
        keys(
            j,
            {"left_scope", "right_scope", "left", "right", "left_on", "right_on", "predicate", "cardinality"},
            {"left_scope", "right_scope", "left", "right", "left_on", "right_on", "predicate", "cardinality"},
        )
        relation(j, j["left"], j["right"])
        if j["cardinality"] not in ("one_to_one", "many_to_one"):
            raise ValueError("Unsupported join cardinality")
        for side in ("left", "right"):
            if j[side + "_scope"] not in scopes or objects[j[side]]["scope"] != j[side + "_scope"]:
                raise ValueError("Join endpoint scope mismatch")
            rules = j[side + "_on"]
            if not isinstance(rules, list) or not 1 <= len(rules) <= 32:
                raise ValueError("Join requires bounded exact source keys")
            for b in rules:
                binding(b)
        if len(j["left_on"]) != len(j["right_on"]):
            raise ValueError("Join key arity differs")
    estimated = 0
    outputs = 0
    for s in scopes.values():
        fields = [f for f in plan["fields"] if f["scope"] == s["id"]]
        object_count = sum(d["scope"] == s["id"] for d in objects.values())
        relation_count = sum(r["scope"] == s["id"] for r in plan.get("relations", []))
        object_ops = sum(len(d["key"]) + ("label" in d) for d in objects.values() if d["scope"] == s["id"])
        join_ops = sum(
            len(j[side + "_on"])
            for j in plan.get("joins", [])
            for side in ("left", "right")
            if j[side + "_scope"] == s["id"]
        )
        for block in s.get("blocks") or [s]:
            height = block["rows"][1] - block["rows"][0] + 1
            width = block["columns"][1] - block["columns"][0] + 1
            count = (
                height
                if s["kind"] == "row"
                else width
                if s["kind"] in ("column", "curve")
                else height * width
                if s["kind"] == "matrix"
                else 1
            )
            outputs += count * (object_count + relation_count + len(fields) + join_ops)
            if outputs > MAX_OUTPUTS:
                raise ValueError("Plan exceeds bounded output budget")
            estimated += count * (object_ops + join_ops + 1)
            for f in fields:
                points = height if f.get("mode", "series" if s["kind"] == "curve" else "scalar") == "series" else 1
                estimated += count * (
                    len(f.get("qualifiers", {}))
                    + len(f.get("header_sources", []))
                    + sum(k in f for k in ("unit", "basis", "x_unit"))
                    + points * (1 + ("x_source" in f) + len(f.get("extra_sources", {})))
                )
    if estimated > MAX_BINDINGS:
        raise ValueError("Plan exceeds bounded binding budget")

    def check_source(b, sid):
        if "semantic" in b:
            return
        if "metadata" in b:
            v = views[scopes[sid]["view"]]
            title = v["sheet"]
            if not isinstance(title, str) or b["span"][1] > len(title):
                raise ValueError("Metadata span outside source bounds")
            return
        if "source_code" in b:
            b = b["source"]
        if "context" in b:
            ctx = contexts.get(b["context"])
            if ctx is None or ctx["scope"] != sid:
                raise ValueError("Unknown or mismatched context scope")
            b = ctx["source"]
        scope = scopes[sid]
        v = views[scope["view"]]
        t = tables[scope["view"]]
        nr, nc = (t["ncols"], t["nrows"]) if v.get("transpose") else (t["nrows"], t["ncols"])
        for name, upper in (("row", nr), ("column", nc)):
            if b[name] != "current" and b[name] > upper:
                raise ValueError("Source binding outside original cell bounds")

    for c in contexts.values():
        check_source(c["source"], c["scope"])
    for d in objects.values():
        for b in d["key"] + ([d["label"]] if "label" in d else []):
            check_source(b, d["scope"])
    for f in plan["fields"]:
        for b in (
            [f["source"]]
            + [f[k] for k in ("unit", "basis", "x_source", "x_unit") if k in f]
            + list(f.get("qualifiers", {}).values())
            + list(f.get("extra_sources", {}).values())
            + f.get("header_sources", [])
        ):
            check_source(b, f["scope"])
    for j in plan.get("joins", []):
        for side in ("left", "right"):
            for b in j[side + "_on"]:
                check_source(b, j[side + "_scope"])
    snapshot = deepcopy(plan)
    return {
        "version": "2",
        "compiler_version": COMPILER_VERSION,
        "quality_version": QUALITY_VERSION,
        "plan": snapshot,
        "plan_hash": fingerprint(snapshot),
        "table_hashes": {v: fingerprint(t) for v, t in tables.items()},
        "record_count": record_count,
        "object_order": ready,
    }
