"""Turn an accepted candidate into package objects, assertions and relations."""

from __future__ import annotations

from kb_ingest.contract import FIELDS, ContractError, add_assertion, add_issue, add_object, add_relation
from kb_ingest.core import fingerprint

from .documents_inventory import UnitObjectInventory, _evidence_refs
from .documents_validate import validate_candidate


def _materialize(
    pkg: dict,
    candidate: dict,
    blocks: list[dict],
    unit_id: str,
    part_key: str = "",
    inventory: UnitObjectInventory | None = None,
) -> dict:
    inventory = inventory or UnitObjectInventory(unit_id)
    if inventory.unit_id != unit_id:
        raise ValueError("inventory cannot cross source units")
    materialized = {
        "candidate_sha256": fingerprint(candidate),
        "targets": {},
        "existing_objects": {key: key for key in inventory.entries},
    }
    relation_positions = {id(item): i for i, item in enumerate(candidate.get("relations", []))}
    registry = {**inventory.blocks, **{b["block_id"]: b for b in blocks}}
    candidate, problems = validate_candidate(candidate, list(registry.values()), inventory.as_prompt())
    for problem in sorted(set(problems)):
        add_issue(pkg, f"Candidate validation: {problem}", [unit_id], severity="error")
    object_ids = {key: key for key in inventory.entries}
    new_by_key = {item["key"]: item for item in candidate["objects"]}
    proofs, resolving = {}, set()

    def resolve(key):
        if key in object_ids:
            return object_ids[key]
        if key not in new_by_key or key in resolving:
            return None
        resolving.add(key)
        item = new_by_key[key]
        owner_ref = resolve(item.get("owner_key")) if item.get("owner_key") else None
        oid, proof, ambiguous = inventory.identify(item, owner_ref, registry, part_key)
        object_ids[key], proofs[key] = oid, proof
        resolving.remove(key)
        if ambiguous:
            add_issue(pkg, "Ambiguous repeated object label; identities were not merged", [unit_id], severity="error")
        return oid

    for key in new_by_key:
        resolve(key)
    # Components with unresolvable parent/material would create misleading composition facts.
    removed = {
        o["key"]
        for o in candidate["objects"]
        if o["kind"] == "component"
        and (o.get("owner_key") not in object_ids or o.get("material_key") not in object_ids)
    }
    for key in removed:
        object_ids.pop(key, None)
        add_issue(pkg, "Component owner/material could not be resolved", [unit_id], severity="error")
    for item in candidate["objects"]:
        if item["key"] not in object_ids:
            continue
        extra = {
            "unit_ref": unit_id,
            "source_scope": item["scope"],
            "evidence_refs": _evidence_refs(pkg, item["evidence"], registry),
            "object_id": object_ids[item["key"]],
        }
        for key in ("owner", "material"):
            if item.get(key + "_key") in object_ids:
                extra[key + "_ref"] = object_ids[item[key + "_key"]]
        existing_row = next((o for o in pkg["objects"] if o["object_id"] == object_ids[item["key"]]), None)
        if existing_row is None:
            add_object(pkg, item["kind"], item["label"], **extra)
        else:
            existing_row["evidence_refs"] = list(dict.fromkeys(existing_row["evidence_refs"] + extra["evidence_refs"]))
        inventory.register(object_ids[item["key"]], item, object_ids, proofs[item["key"]], registry)
        materialized["targets"]["o:" + item["key"]] = object_ids[item["key"]]
    fact_ids, pending_derivations = {}, []
    for ordinal, item in enumerate(candidate["facts"]):
        if item["object_key"] not in object_ids:
            continue
        if item.get("formation") == "explicit_inheritance" and not item.get("input_fact_keys"):
            add_issue(
                pkg,
                "Inherited value lacks explicit input facts; not expanded",
                [object_ids[item["object_key"]]],
                severity="error",
            )
            continue
        qualifiers = item.get("qualifiers") or {}
        if isinstance(qualifiers, list):
            qualifiers = {q["key"]: q["value"] for q in qualifiers}
        value_kind = item.get("value_kind")
        if value_kind not in FIELDS[item["field_id"]]["value_kinds"]:
            inferred = (
                "scalar"
                if isinstance(item["raw_value"], (float, int)) and not isinstance(item["raw_value"], bool)
                else "text"
            )
            value_kind = inferred if inferred in FIELDS[item["field_id"]]["value_kinds"] else None
        if value_kind is None:
            add_issue(
                pkg, "Value kind does not fit registered field", [object_ids[item["object_key"]]], severity="error"
            )
            continue
        try:
            fact_id = add_assertion(
                pkg,
                object_ids[item["object_key"]],
                item["field_id"],
                item["raw_value"],
                _evidence_refs(pkg, item["evidence"], registry),
                raw_unit=item.get("raw_unit"),
                raw_basis=item.get("raw_basis"),
                qualifiers=qualifiers,
                statement_kind=item.get("statement_kind", "source_report"),
                formation=item.get("formation", "direct"),
                value_kind=value_kind,
                source_context={
                    "unit_ref": unit_id,
                    "evidence_check": "locator_and_literal_quote_only_semantics_unreviewed",
                    **(
                        {"program_value_source": item["program_value_source"]}
                        if item.get("program_value_source")
                        else {}
                    ),
                },
            )
            fact_ids[item.get("key", str(ordinal))] = fact_id
            if item.get("input_fact_keys"):
                pending_derivations.append((fact_id, item))
        except ContractError:
            add_issue(
                pkg, "Field mapping rejected by common contract", [object_ids[item["object_key"]]], severity="error"
            )
    # Resolve the graph to a fixed point: a withheld input also withholds all
    # descendants. Cycles never constitute an independently sourced base fact.
    pending_by_id = {fid: item for fid, item in pending_derivations}
    resolved_ids = set(fact_ids.values()) - set(pending_by_id)
    while True:
        newly_resolved = {
            fid
            for fid, item in pending_by_id.items()
            if fid not in resolved_ids
            and all(k in fact_ids and fact_ids[k] in resolved_ids for k in item["input_fact_keys"])
        }
        if not newly_resolved:
            break
        resolved_ids.update(newly_resolved)
    for fact_id, item in pending_derivations:
        inputs = [fact_ids[k] for k in item["input_fact_keys"] if k in fact_ids and fact_ids[k] != fact_id]
        resolved = fact_id in resolved_ids and len(inputs) == len(item["input_fact_keys"])
        if not resolved:
            pkg["assertions"] = [a for a in pkg["assertions"] if a["assertion_id"] != fact_id]
            add_issue(pkg, "Inheritance inputs not resolved; output fact withheld", [unit_id], severity="error")
            continue
        pkg["derivations"].append(
            {
                "derivation_id": fact_id + ":derivation",
                "output_assertion_ref": fact_id,
                "kind": item.get("formation", "explicit_inheritance"),
                "input_assertion_refs": inputs,
                "evidence_refs": _evidence_refs(pkg, item["evidence"], registry),
                "resolution": "model_proposed_source_chain_requires_review",
            }
        )
    remaining_fact_ids = {row["assertion_id"] for row in pkg["assertions"]}
    materialized["targets"].update({"f:" + key: fid for key, fid in fact_ids.items() if fid in remaining_fact_ids})
    for item in candidate["relations"]:
        if item["from_key"] not in object_ids or item["to_key"] not in object_ids:
            continue
        relation_id = add_relation(
            pkg,
            object_ids[item["from_key"]],
            item["predicate"],
            object_ids[item["to_key"]],
            _evidence_refs(pkg, item["evidence"], registry),
            applicability=item.get("applicability", "requires_review"),
            basis_kind=item.get("basis_kind", "model_source_proposal"),
        )
        materialized["targets"]["r:" + str(relation_positions[id(item)])] = relation_id
    for issue in candidate["issues"]:
        add_issue(
            pkg,
            issue["description"],
            [object_ids[k] for k in issue.get("object_keys", []) if k in object_ids],
            _evidence_refs(pkg, issue.get("evidence", []), registry),
            severity=issue.get("severity", "warning"),
        )
    from kb_ingest.document_mapping_proof import record_content_hash

    records = {
        row[key]: row
        for section, key in (("objects", "object_id"), ("assertions", "assertion_id"), ("relations", "relation_id"))
        for row in pkg[section]
    }
    materialized["record_sha256"] = {
        target: record_content_hash(records[ref]) for target, ref in materialized["targets"].items()
    }
    return materialized


def _check_observation_closure(pkg: dict) -> None:
    """Report missing edges, never invent a test or infer a method from proximity."""
    relations = pkg["relations"]
    for obj in pkg["objects"]:
        if obj["kind"] != "observation":
            continue
        oid = obj["object_id"]
        facts = [a for a in pkg["assertions"] if a["subject_ref"] == oid]
        if not facts:
            continue
        subjects = {r["to_ref"] for r in relations if r["from_ref"] == oid and r["predicate"] == "observes_subject"}
        tests = {r["from_ref"] for r in relations if r["to_ref"] == oid and r["predicate"] == "produces_observation"}
        explicit_test_subjects = {
            r["to_ref"] for r in relations if r["from_ref"] in tests and r["predicate"] == "tests_subject"
        }
        explicit_test_subjects.update(
            r["from_ref"] for r in relations if r["to_ref"] in tests and r["predicate"] == "tested_by"
        )
        if not subjects and not explicit_test_subjects:
            add_issue(
                pkg,
                "Observation subject relation is unresolved; value cannot be assigned by page proximity",
                [oid],
                severity="error",
            )
        if any(a["statement_kind"] == "source_reported_measurement" for a in facts):
            method_edges = [
                r
                for r in relations
                if (r["from_ref"] in tests | subjects and r["predicate"] == "uses_method")
                or (r["from_ref"] in subjects and r["predicate"] == "method_applicability")
            ]
            if not method_edges:
                add_issue(
                    pkg,
                    "Measured observation has no explicit path to a reported method; method coverage unresolved",
                    [oid],
                    severity="error",
                )
