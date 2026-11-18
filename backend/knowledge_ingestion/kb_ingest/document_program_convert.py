"""Deterministic materialization without model review or semantic certification."""

import re
import unicodedata
from copy import deepcopy

from . import contract, core
from .document_issue_scope import project_issue_annotations
from .document_program_contract import CONTRACT_VERSION, PROGRAM_SCHEMA
from .source_token_boundaries import unit_evidence_status


def _norm(value):
    text = unicodedata.normalize("NFKC", str(value)).replace("−", "-").replace("$", "")
    text = re.sub(r"\\(?:mathrm|text|textrm)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:,|;|!|quad| )", " ", text)
    text = text.replace("\\%", "%").replace("^{", "^").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def _literal(value, text, *, role="value"):
    needle = _norm(value)
    if re.fullmatch(r"[+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", needle):
        pattern = r"(?<![A-Za-z0-9_.+-])(?<!\d,)" + re.escape(needle) + r"(?![\d.]|,\d|[eE][+-]?\d)"
    else:
        # Chinese prose does not require space-delimited words. ASCII identifiers do.
        left = r"(?<![A-Za-z0-9_.+-])(?<!\d,)" if re.match(r"[+-]?(?:\d|\.\d)", needle) else r"(?<![A-Za-z0-9_.])"
        right = r"(?![A-Za-z0-9_.]|,\d)" if needle and needle[-1].isdigit() else r"(?![A-Za-z0-9_.])"
        pattern = left + re.escape(needle) + right
    return bool(needle) and bool(re.search(pattern, _norm(text)))


def _unit_norm(value):
    # Layout around explicit products/powers is not part of a unit's spelling.
    return re.sub(r"\s*([/*·^+−-])\s*", r"\1", _norm(value))


_SOURCE_CONTENT_FIELDS = {
    "text",
    "caption",
    "captions",
    "title",
    "label",
    "footnote",
    "footnotes",
    "notes",
    "content",
    "markdown",
    "html",
    "grid",
    "rows",
    "cells",
    "table",
    "linked_notes",
    "source_parts",
    "raw_text",
    "value",
    "header",
}


def _source_text(block):
    """Join source wording across the normalized block's textual components."""
    parts = []

    def collect(value):
        if isinstance(value, str):
            if value.strip():
                parts.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in _SOURCE_CONTENT_FIELDS:
                    collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(block)
    return "\n".join(dict.fromkeys(parts))


def _supported(value, quote, original, role):
    if role == "unit":
        return (
            unit_evidence_status(
                _unit_norm(original if original is not None else quote), _unit_norm(quote), _unit_norm(value)
            )
            == "complete"
        )
    return _literal(value, quote) and (original is None or _literal(value, original))


def _shape(raw):
    if not isinstance(raw, dict):
        raise ValueError("Output must be an object")

    def walk(value, schema, path):
        kinds = schema.get("type")
        kinds = [kinds] if isinstance(kinds, str) else kinds
        valid = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "number": type(value) in (int, float),
            "integer": type(value) is int,
            "null": value is None,
        }
        if not any(valid.get(kind, False) for kind in kinds):
            raise ValueError(path + " has invalid type")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(path + " has invalid enum")
        if isinstance(value, dict):
            properties = schema["properties"]
            for key, item in value.items():
                if key not in properties:
                    raise ValueError(path + " has unknown field " + key)
                walk(item, properties[key], path + "." + key)
            for key in ("ref", "label", "value", "material", "description"):
                if (
                    key in properties
                    and key not in value
                    and not (key == "ref" and properties[key].get("type") == ["string", "null"])
                ):
                    raise ValueError(path + " missing " + key)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, schema["items"], path + "[" + str(i) + "]")

    walk(raw, PROGRAM_SCHEMA, "output")
    for entry in raw.get("issues", []):
        if ("scope" in entry) != ("target_refs" in entry):
            raise ValueError("issue scope and target_refs must be provided together")
    declared = set()

    def refs(value):
        if isinstance(value, dict):
            ref = value.get("ref")
            if ref is not None:
                if not ref or ref in declared:
                    raise ValueError("Duplicate or empty object/fact ref " + str(ref))
                declared.add(ref)
            for child in value.values():
                refs(child)
        elif isinstance(value, list):
            for child in value:
                refs(child)

    for key, value in raw.items():
        if key != "evidence":
            refs(value)


def materialize_program_output(raw, registry, package, *, group_id, target_block_ids):
    _shape(raw)
    p = package
    start = {k: len(p[k]) for k in ("assertions", "relations", "issues", "objects")}
    namespace = p["header"]["source_id"] + ":program:" + core.fingerprint(group_id)[:16]
    evidence = {}
    objects = {}
    checks = {}
    nodes = {}
    touched = set()
    blocked_records = []
    fact_refs = {}
    assets = {a["asset_id"] for a in p["assets"]}

    def issue(message, targets=(), refs=(), **extra):
        return contract.add_issue(p, message, list(targets), list(refs), program_group=group_id, **extra)

    for e in raw.get("evidence", []):
        ref = e.get("ref")
        b = registry.get(e.get("block_id"))
        quote = e.get("quote", "")
        if not isinstance(ref, str) or not ref or ref in evidence:
            raise ValueError("Evidence refs must be unique strings")
        status = "program_checked"
        reasons = []
        if not b and e.get("page") and e.get("asset_ref"):
            matches = [
                x
                for x in registry.values()
                if x.get("asset_ref") == e["asset_ref"]
                and x.get("page") == e["page"]
                and quote
                and _literal(quote, _source_text(x))
            ]
            if len(matches) == 1:
                b = matches[0]
        if (
            not b
            and e.get("asset_ref") in assets
            and type(e.get("page")) is int
            and e["page"] > 0
            and any(
                x.get("asset_ref") == e["asset_ref"]
                and (
                    x.get("page") == e["page"]
                    or x.get("anchor", {}).get("page") == e["page"]
                    or e["page"] in x.get("anchor", {}).get("page_numbers", [])
                )
                for x in registry.values()
            )
        ):
            eid = contract.add_evidence(
                p,
                e["asset_ref"],
                {"page": e["page"], "localization_status": "unresolved"},
                quote,
                program_check={"status": "needs_attention", "reasons": ["unresolved_pdf_quote"]},
            )
            evidence[ref] = (eid, "needs_attention", ["unresolved_pdf_quote"], quote, None)
            issue("Unresolved PDF page quote " + ref, refs=[eid])
            continue
        if not b:
            evidence[ref] = (None, "blocked", ["unresolved_source"], quote, None)
            issue("Unresolved source evidence " + ref)
            continue
        touched.add(b["block_id"])
        source_text = _source_text(b)
        if not quote or not _literal(quote, source_text):
            status = "needs_attention"
            reasons.append("quote_not_in_source")
        if b.get("requires_image_verification") or b.get("structure_reliable") is False:
            status = "needs_attention"
            reasons.append("unreliable_source_reading")
        if b.get("asset_ref") not in assets:
            raise ValueError("Evidence asset not registered")
        eid = contract.add_evidence(
            p,
            b["asset_ref"],
            deepcopy(b.get("anchor") or {"block_id": b["block_id"]}),
            quote or b.get("text", ""),
            program_block_id=b["block_id"],
        )
        evidence[ref] = (eid, status, reasons, quote, source_text)

    def check(refs, literals=(), *, unit=None):
        reasons = []
        ids = []
        texts = []
        status = "program_checked"
        if not refs:
            status = "blocked"
            reasons.append("missing_evidence")
        for ref in refs:
            eid, state, why, text, original = evidence.get(
                ref, (None, "blocked", ["unknown_evidence_ref:" + str(ref)], "", None)
            )
            if eid:
                ids.append(eid)
            texts.append((text, original))
            reasons.extend(why)
            if state == "blocked":
                status = "blocked"
            elif state == "needs_attention" and status != "blocked":
                status = state
        for value, role in [(v, "value") for v in literals] + ([(unit, "unit")] if unit is not None else []):
            if value is not None and not any(_supported(value, t, original, role) for t, original in texts):
                reasons.append("literal_not_in_evidence:" + str(value))
                if status != "blocked":
                    status = "needs_attention"
        return ids, {"status": status, "reasons": list(dict.fromkeys(reasons))}

    def node(item, kind, key, *, literal_identity=False, **extra):
        if key in objects:
            raise ValueError("Duplicate object ref " + str(key))
        refs, checked = check(item.get("evidence", []), [item.get("label")] if literal_identity else [])
        if any(
            checks.get(extra.get(key), {}).get("status") in ("blocked", "needs_attention")
            for key in ("owner_ref", "about_subject_ref", "activity_ref")
        ):
            reasons = list(dict.fromkeys(checked["reasons"] + ["identity_dependency_needs_review"]))
            checked = {"status": "blocked" if checked["status"] == "blocked" else "needs_attention", "reasons": reasons}
        oid = contract.add_object(
            p,
            kind,
            item.get("label", str(key)),
            object_id=namespace + ":" + core.fingerprint(key)[:20],
            evidence_refs=refs,
            label_origin="source_literal" if literal_identity else "model_generated_display",
            program_check=checked,
            program_group=group_id,
            **extra,
        )
        objects[key] = oid
        checks[oid] = checked
        nodes[key] = item
        return oid

    def preserve_blocked(proposal, targets, reasons):
        # A missing optional fact is not invalidation of its containing object.
        # Keep ownership for inspection outside dependency target_refs.
        context = deepcopy(proposal)
        dependency_targets = targets
        if "proposed_fact" in proposal:
            context["proposed_owner_refs"] = list(targets)
            dependency_targets = []
        iid = contract.add_issue(
            p,
            "Blocked proposed record: " + "; ".join(reasons),
            dependency_targets,
            issue_id=namespace + ":blocked:" + core.fingerprint([proposal, targets, reasons])[:24],
            source_context=context,
            program_group=group_id,
            program_check={"status": "blocked", "reasons": reasons},
            use_status="blocked",
        )
        blocked_records.append({"issue_id": iid, "status": "blocked", "reasons": reasons})
        return iid

    def relation(left, predicate, right, refs):
        ev, c = check(refs)
        object_ids = {o["object_id"] for o in p["objects"]}
        if not ev or left not in object_ids or right not in object_ids:
            if left not in object_ids or right not in object_ids:
                c = {"status": "blocked", "reasons": list(dict.fromkeys(c["reasons"] + ["relation_endpoint_missing"]))}
            preserve_blocked(
                {"proposed_relation": {"from_ref": left, "predicate": predicate, "to_ref": right, "evidence": refs}},
                [left, right],
                c["reasons"],
            )
            return None
        for oid in (left, right):
            prior = checks.get(oid, {"status": "program_checked", "reasons": []})
            if prior["status"] != "program_checked":
                if c["status"] != "blocked":
                    c["status"] = "needs_attention"
                c["reasons"].append("endpoint_identity_needs_review")
        rid = contract.add_relation(
            p, left, predicate, right, ev, program_check=c, use_status=c["status"], program_group=group_id
        )
        if c["reasons"]:
            issue("; ".join(c["reasons"]), [rid], ev)
        return rid

    def fact(item, owner, field, *, display_label=False):
        value = item.get("value")
        if value is None or value == "":
            iid = preserve_blocked({"proposed_fact": item}, [owner], ["missing_value"])
            if item.get("ref"):
                fact_refs[item["ref"]] = iid
            return
        paraphrase = (
            item.get("value_representation") == "paraphrase"
            and item.get("fact_type") in ("description", "claim", "interpretation")
            and not item.get("unit")
        )
        value_literals = [] if paraphrase and not re.search(r"\d", _norm(value)) else [value]
        refs, c = check(
            item.get("evidence", []),
            [] if display_label else [*value_literals, item.get("basis"), *item.get("conditions", [])],
            unit=None if display_label else item.get("unit"),
        )
        if not refs:
            iid = preserve_blocked({"proposed_fact": item}, [owner], c["reasons"])
            if item.get("ref"):
                fact_refs[item["ref"]] = iid
            return
        if checks.get(owner, {}).get("status") in ("blocked", "needs_attention"):
            status = "blocked" if c["status"] == "blocked" else "needs_attention"
            c = {"status": status, "reasons": c["reasons"] + ["owner_identity_needs_review"]}
        kind = item.get("fact_type", "description")
        attribution = item.get("attribution", "authors")
        statement = {
            "measurement": "source_reported_measurement",
            "calculation": "source_reported_calculation",
            "interpretation": "author_interpretation",
            "claim": "source_claim",
            "description": "source_report",
        }.get(kind, "source_report")
        name = item.get("name", "")
        qualifiers = {"property": name}
        role = (
            "metric"
            if field.startswith("observation.")
            else "parameter"
            if field in ("condition.parameter", "process.parameter")
            else None
        )
        if role and name:
            qualifiers.update({role + "_label": name, role + "_id": "source_key:" + name})
        aid = namespace + ":assertion:" + core.fingerprint([owner, field, item, refs, display_label])[:24]
        if not any(a["assertion_id"] == aid for a in p["assertions"]):
            aid = contract.add_assertion(
                p,
                owner,
                field,
                value,
                refs,
                assertion_id=aid,
                raw_unit=item.get("unit"),
                raw_basis=item.get("basis"),
                qualifiers=qualifiers,
                statement_kind=statement,
                source_attribution=attribution,
                author_statement_status=item.get("statement_status"),
                source_conditions=deepcopy(item.get("conditions", [])),
                value_representation="display_label"
                if display_label
                else "paraphrase"
                if paraphrase
                else "reported_literal",
                semantic_support="not_checked",
                program_check=c,
                use_status=c["status"],
                program_group=group_id,
            )
        if item.get("ref"):
            fact_refs[item["ref"]] = aid
        saved = next(a["value"] for a in p["assertions"] if a["assertion_id"] == aid)
        if display_label or paraphrase:
            saved["raw_origin"] = "model_generated_display_label" if display_label else "model_paraphrase"
        if c["reasons"]:
            issue("; ".join(c["reasons"]), [aid], refs)

    source = raw.get("source") or {}
    if source.get("title") and not any(
        a["field_id"] == "source.title" and a["value"]["raw_value"] == source["title"] for a in p["assertions"]
    ):
        fact(
            dict(value=source["title"], name="title", evidence=source.get("evidence", [])),
            p["header"]["source_id"] + ":source",
            "source.title",
        )
    for method in raw.get("shared_methods", []):
        oid = node(method, "method_definition", method["ref"])
        fact(
            dict(value=method["label"], name="method", evidence=method.get("evidence", [])),
            oid,
            "method.name",
            display_label=True,
        )
        for f in method.get("details", []):
            fact(f, oid, "condition.parameter")
    pending = []
    test_objects = {}
    for study in raw.get("studies", []):
        st = node(study, "content_unit", study["ref"])
        fact(
            dict(value=study["label"], name="scope", evidence=study.get("evidence", [])),
            st,
            "study.scope",
            display_label=True,
        )
        for sample in study.get("samples", []):
            sa = node(sample, "sample", sample["ref"])
            relation(st, "contains_sample", sa, sample.get("evidence", []))
            fact(
                dict(value=sample["label"], name="identity", evidence=sample.get("evidence", [])),
                sa,
                "material.identity",
                display_label=True,
            )
            if sample.get("composition"):
                fo = node(sample, "formulation", sample["ref"] + ":formulation", owner_ref=sa)
                relation(sa, "has_formulation", fo, sample.get("evidence", []))
                for i, f in enumerate(sample["composition"]):
                    key = sample["ref"] + ":component:" + str(i)
                    ma = node(
                        dict(label=f["material"], evidence=f.get("evidence", [])),
                        "material",
                        key + ":material",
                        literal_identity=True,
                    )
                    co = node(
                        dict(label=f["material"], evidence=f.get("evidence", [])),
                        "component",
                        key,
                        literal_identity=True,
                        owner_ref=fo,
                        material_ref=ma,
                    )
                    fact(f, co, "composition.amount")
                    relation(fo, "contains_component", co, f.get("evidence", []))
                    relation(co, "refers_to_material", ma, f.get("evidence", []))
            for i, f in enumerate(sample.get("preparation", [])):
                pr = node(
                    dict(label=f["name"], evidence=f.get("evidence", [])),
                    "process_definition",
                    sample["ref"] + ":process:" + str(i),
                )
                fact(f, pr, "process.parameter")
                relation(sa, "process_applicability", pr, f.get("evidence", []))
            for test in sample.get("tests", []):
                te = node(test, "reported_test", test["ref"])
                test_objects[test["ref"]] = te
                relation(sa, "tested_by", te, test.get("evidence", []))
                for f in test.get("conditions", []):
                    fact(f, te, "condition.parameter")
                for i, f in enumerate(test.get("results", [])):
                    if (
                        f.get("fact_type") in ("claim", "interpretation", "description")
                        or f.get("attribution") == "product"
                    ):
                        fact(f, te, "claim.text")
                        continue
                    ob = node(
                        dict(label=test["label"], evidence=f.get("evidence", [])),
                        "observation",
                        test["ref"] + ":observation:" + str(i),
                        about_subject_ref=sa,
                        activity_ref=te,
                    )
                    fact(f, ob, "observation.value")
                    relation(te, "produces_observation", ob, f.get("evidence", []))
                    relation(ob, "observes_subject", sa, f.get("evidence", []))
                if test.get("method_ref"):
                    pending.append((test, te))
    for test, te in pending:
        key = test["method_ref"]
        method = nodes.get(key)
        if not method or key not in {m["ref"] for m in raw.get("shared_methods", [])}:
            issue(
                "Unknown method ref " + key,
                [te],
                program_check={"status": "needs_attention", "reasons": ["specified_method_unresolved"]},
            )
            continue
        if test["ref"] not in method.get("applies_to", []):
            issue(
                "Unknown explicit method applicability " + key,
                [te],
                program_check={"status": "needs_attention", "reasons": ["specified_method_unresolved"]},
            )
            continue
    for method in raw.get("shared_methods", []):
        for target in dict.fromkeys(method.get("applies_to", [])):
            if target not in test_objects:
                issue("Unknown method applicability target " + target, [objects[method["ref"]]])
                continue
            relation(test_objects[target], "uses_method", objects[method["ref"]], method.get("evidence", []))
    for i, f in enumerate(raw.get("statements", [])):
        cl = node(
            dict(label=f.get("name", "statement"), evidence=f.get("evidence", [])), "claim", "statement:" + str(i)
        )
        fact(f, cl, "claim.text")
        for ref in f.get("about", []):
            if ref not in objects:
                issue("Unknown statement subject " + ref, [cl])
                continue
            kind = next(o["kind"] for o in p["objects"] if o["object_id"] == objects[ref])
            if kind in ("sample", "content_unit"):
                relation(cl, "about_sample" if kind == "sample" else "about_study", objects[ref], f.get("evidence", []))
            else:
                issue("Unsupported statement subject " + ref, [cl])
    annotations = project_issue_annotations(raw)["items"]
    for index, entry in enumerate(raw.get("issues", [])):
        explicit = "scope" in entry or "target_refs" in entry
        if explicit:
            annotation = annotations[index]
            direct_refs = []
            for pointer in annotation["target_pointers"]:
                node = raw
                for segment in pointer.lstrip("/").split("/"):
                    node = node[int(segment)] if isinstance(node, list) else node[segment]
                if isinstance(node, dict) and isinstance(node.get("ref"), str):
                    direct_refs.append(node["ref"])
            targets = [objects[r] for r in direct_refs if r in objects]
            targets += [fact_refs[r] for r in direct_refs if r in fact_refs]
            unscoped = not targets
        else:
            # Historical v3 records retain their materialization contract.
            targets = [objects[r] for r in entry.get("refs", []) if r in objects]
            targets += [
                o["object_id"]
                for o in p["objects"]
                if o.get("activity_ref") in targets or o.get("about_subject_ref") in targets
            ]
            targets += [fact_refs[r] for r in entry.get("refs", []) if r in fact_refs]
            unscoped = not targets or any(r not in objects and r not in fact_refs for r in entry.get("refs", []))
        group_records = p["assertions"][start["assertions"] :] + p["relations"][start["relations"] :]
        reason = "unscoped_source_issue" if unscoped else "source_reported_issue"
        if unscoped:
            targets = []
        refs, _ = check(entry.get("evidence", []))
        issue(
            entry.get("description", "Source issue"),
            targets,
            refs,
            program_check={"status": "needs_attention", "reasons": [reason]},
        )
        for record in group_records:
            affected = (
                record.get("assertion_id") in targets
                if explicit
                else (
                    record.get("assertion_id") in targets
                    or record.get("subject_ref") in targets
                    or record.get("from_ref") in targets
                    or record.get("to_ref") in targets
                )
            )
            if not unscoped and affected:
                if record["use_status"] != "blocked":
                    record["use_status"] = record["program_check"]["status"] = "needs_attention"
                record["program_check"]["reasons"].append(reason)
    conflicts = {}
    for a in p["assertions"][start["assertions"] :]:
        if a["field_id"] != "observation.value":
            continue
        owner = next(o for o in p["objects"] if o["object_id"] == a["subject_ref"])
        key = core.fingerprint(
            [owner.get("activity_ref"), a["qualifiers"], a.get("source_conditions"), a["value"]["raw_basis"]]
        )
        conflicts.setdefault(key, []).append(a)
    for rows in conflicts.values():
        if len({core.fingerprint([a["value"]["raw_value"], a["value"]["raw_unit"]]) for a in rows}) > 1:
            issue("Conflicting values in same reported test", [a["assertion_id"] for a in rows])
            for a in rows:
                if a["use_status"] != "blocked":
                    a["use_status"] = a["program_check"]["status"] = "needs_attention"
                a["program_check"]["reasons"].append("conflicting_reported_values")
    for bid in target_block_ids:
        block = registry.get(bid, {})
        if block.get("kind") == "table_cell" and bid not in touched and re.search(r"\d", block.get("text", "")):
            issue("Unmapped table value cell " + bid)
    return {
        "contract_version": CONTRACT_VERSION,
        "group_id": group_id,
        "assertion_ids": [r["assertion_id"] for r in p["assertions"][start["assertions"] :]],
        "relation_ids": [r["relation_id"] for r in p["relations"][start["relations"] :]],
        "issue_ids": [r["issue_id"] for r in p["issues"][start["issues"] :]],
        "objects": objects,
        "blocked_records": blocked_records,
        "fact_refs": fact_refs,
    }
