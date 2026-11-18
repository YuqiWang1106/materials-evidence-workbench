"""Shared pure effective projection; does not fetch or infer relations."""

from copy import deepcopy

from .ingest.documents.document_quality_assess import definite_package, invalidated_references


def project_saved_package(package, blocked_refs):
    result = definite_package(package)
    if package["header"].get("document_pipeline_version") != "2":
        return result
    blocked = set(blocked_refs)
    result = deepcopy(result)
    result["query_context"] = {"current_review_required": bool(blocked)}
    if not blocked:
        return result
    blocked = invalidated_references(package, blocked)
    result["assertions"] = [a for a in result["assertions"] if not {a["assertion_id"], a["subject_ref"]} & blocked]
    result["relations"] = [
        r for r in result["relations"] if not {r["relation_id"], r["from_ref"], r["to_ref"]} & blocked
    ]
    while True:
        valid = {a["assertion_id"] for a in result["assertions"]}
        invalid = {
            d["output_assertion_ref"] for d in result["derivations"] if not set(d["input_assertion_refs"]) <= valid
        }
        if not invalid & valid:
            break
        result["assertions"] = [a for a in result["assertions"] if a["assertion_id"] not in invalid]
    valid = {a["assertion_id"] for a in result["assertions"]}
    result["derivations"] = [
        d
        for d in result["derivations"]
        if d["output_assertion_ref"] in valid and set(d["input_assertion_refs"]) <= valid
    ]
    return result
