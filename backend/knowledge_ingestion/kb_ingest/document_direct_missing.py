"""Review and retain direct-extraction absence claims without manufacturing facts.

The common record contract already has assessments and issues. These helpers add
no storage schema and never create assertions, values, subjects or measurements.
"""

from __future__ import annotations

import re
from copy import deepcopy

from .contract import FIELDS, _append, _profile, add_evidence, add_issue
from .core import fingerprint
from .document_mapping_proof import record_content_hash
from .ingest.documents.document_quality_verify import requires_image_review

_STATUSES = ("not_reported", "reading_failed", "uncertain", "conflict")
_HANDLING = {
    "not_reported": "source_not_reported",
    "reading_failed": "reading_failed",
    "uncertain": "extraction_uncertain",
    "conflict": "source_conflict",
}
MISSING_REVIEW_PROPERTIES = {
    "missing_status": {"type": ["string", "null"], "enum": [*_STATUSES, None]},
    "missing_scope_complete": {"type": ["boolean", "null"]},
}
MISSING_REVIEW_INSTRUCTIONS = """Targets prefixed m: are proposed missing-field assessments, never measurements.
For each, return missing_status as not_reported, reading_failed, uncertain or conflict, and
missing_scope_complete as true only when the COMPLETE relevant source scope is available and
checked for that subject and field. supported + not_reported + missing_scope_complete=true
means the cited source and full checked scope establish that this field was not reported.
A missing number in one quote, page, batch or extraction does not establish source absence.
Unreadable/OCR-unverified content is reading_failed; conflicting source statements are conflict;
incomplete context or uncertain meaning is uncertain. Keep these separate from not_reported.
Review exactly the submitted citations and subject/field attribution. Never claim measured zero,
failed experiment, independent scientific validation or human approval from a missing field.
For ordinary non-missing targets return null for both missing-specific properties.
"""


def missing_review_targets(candidate) -> dict:
    """Return additional compact targets for the same independent source review."""
    return {
        f"m:{i}": {"kind": "missing_field", "proposed_status": "not_reported", **deepcopy(item)}
        for i, item in enumerate(candidate.get("unreported_fields", []))
    }


def _registry(blocks):
    result = {b["block_id"]: b for b in blocks}
    if len(result) != len(blocks):
        raise ValueError("duplicate source region identity")
    return result


def _source_fingerprint(blocks):
    return fingerprint(
        [
            {
                k: b.get(k)
                for k in ("block_id", "asset_ref", "kind", "text", "anchor", "source", "requires_image_verification")
            }
            for b in sorted(blocks, key=lambda b: b["block_id"])
        ]
    )


def _literal_support(evidence, registry):
    def text(value):
        return re.sub(r"\s+", "", value)

    return [
        deepcopy(e)
        for e in evidence or []
        if isinstance(e, dict)
        and e.get("block_id") in registry
        and isinstance(e.get("quote"), str)
        and e["quote"].strip()
        and text(e["quote"]) in text(registry[e["block_id"]].get("text", ""))
    ]


def _binding(record):
    return fingerprint({k: v for k, v in record.items() if k != "verification_sha256"})


def verify_missing(candidate, blocks, review, *, accepted_object_keys, visual_reviewed=()) -> tuple[list, dict]:
    """Check each absence proposal; retain failed proposals with their actual status.

    ``accepted_object_keys`` must come from common object verification. The
    compact reviewer explicitly judges the submitted citations; callers provide
    those unchanged citations as each check's evidence, just as for ordinary
    direct-review targets. Missing or duplicate checks fail closed.
    """
    registry = _registry(blocks)
    source_hash = _source_fingerprint(blocks)
    objects = {o["key"]: o for o in candidate.get("objects", [])}
    accepted_objects, visual = set(accepted_object_keys), set(visual_reviewed)
    checks, duplicates = {}, set()
    for check in review.get("checks", []):
        target = check.get("target")
        if target in checks:
            duplicates.add(target)
        checks[target] = check
    records, rejected, direct_rejected = [], [], []
    for target, item in missing_review_targets(candidate).items():
        check = checks.get(target, {})
        status = check.get("missing_status")
        status = status if status in _STATUSES else "uncertain"
        evidence = item.get("evidence") or []
        valid_evidence = _literal_support(evidence, registry)
        review_evidence = check.get("evidence") or []
        valid_review = _literal_support(review_evidence, registry)
        owner = item.get("object_key")
        field = FIELDS.get(item.get("field_id"), {})
        reason = None
        if target in duplicates:
            reason = "Duplicate missing-field review target"
        elif owner not in objects:
            reason = "Missing-field subject has not passed object review"
        elif objects[owner]["kind"] not in field.get("owner_kinds", []):
            reason = "Missing-field catalogue or owner is invalid"
        elif not evidence or len(valid_evidence) != len(evidence):
            reason = "Missing-field evidence is not literal source content"
        elif not review_evidence or len(valid_review) != len(review_evidence):
            reason = "Missing-field review evidence is not literal source content"
        elif not {e["block_id"] for e in evidence} & {e["block_id"] for e in review_evidence}:
            reason = "Missing-field review does not support the submitted source region"
        elif any(
            requires_image_review(registry[e["block_id"]]) and e["block_id"] not in visual
            for e in evidence + review_evidence
        ):
            reason = "Missing-field source requires original image review"
            status = "reading_failed"
        elif check.get("verdict") != "supported" or not str(check.get("reason", "")).strip():
            reason = "Independent source review absent, uncertain or unsupported"
        elif status == "not_reported" and check.get("missing_scope_complete") is not True:
            reason = "Complete relevant source scope was not checked; absence is unestablished"
        elif status != "not_reported":
            reason = check.get("reason") or "Source does not establish a reported absence"
        if reason:
            direct_rejected.append({"target": target, "reason": reason})
        elif owner not in accepted_objects:
            reason = "Missing-field subject has not passed object review"
        if reason:
            # Never preserve not_reported after any failed condition. Explicit
            # reading failures/conflicts remain distinctions, not empty values.
            if status == "not_reported":
                status = "uncertain"
            rejected.append({"target": target, "reason": reason, "missing_status": status})
        record = {
            "target": target,
            "object_key": owner,
            "field_id": item.get("field_id"),
            "reason": item.get("reason", ""),
            "status": status,
            "review_reason": reason or check["reason"],
            "evidence": deepcopy(evidence),
            "valid_evidence": valid_evidence,
            "review": deepcopy(check),
            "source_sha256": source_hash,
            "checked_block_ids": sorted(registry),
            "candidate_item_sha256": fingerprint(item),
            "candidate_sha256": fingerprint(candidate),
            "review_sha256": fingerprint(review),
        }
        record["verification_sha256"] = _binding(record)
        records.append(record)
    return records, {
        "rejected": rejected,
        "direct_rejected": direct_rejected,
        "reviewed_missing_count": len(records),
        "not_reported_count": sum(r["status"] == "not_reported" for r in records),
    }


def materialize_missing(package, records, blocks, materialized) -> dict:
    """Bind reviewed missing targets to existing subjects and common assessments.

    Preflight seals prevent a later edit to the item, verdict or original source
    from reusing an earlier verification. Literal support is retained even for
    an unresolved assessment, but it is never promoted to semantic fact evidence.
    """
    registry = _registry(blocks)
    source_hash = _source_fingerprint(blocks)
    for record in records:
        if record.get("verification_sha256") != _binding(record):
            raise ValueError("missing-field review binding changed")
        if record.get("source_sha256") != source_hash:
            raise ValueError("missing-field original source changed after review")
    objects = {o["object_id"]: o for o in package["objects"]}
    assets = {a["asset_id"] for a in package["assets"]}
    receipt = {"targets": {}, "record_sha256": {}, "issue_refs": []}
    for record in records:
        target = materialized.get("targets", {}).get("o:" + str(record["object_key"]))
        subject = objects.get(target)
        status = record["status"]
        field_id = record["field_id"]
        evidence_refs = []
        for e in record["valid_evidence"]:
            block = registry[e["block_id"]]
            if block.get("asset_ref") not in assets or not block.get("anchor"):
                continue
            visual = block.get("kind") in ("visual_page", "visual_transcription") or block.get("source") == "visual"
            kind = (
                "model_visual_transcription"
                if visual
                else "ocr_transcription"
                if block.get("source") in ("mineru", "ocr")
                else "verbatim"
            )
            existing = next(
                (
                    x
                    for x in package["evidence"]
                    if x["asset_ref"] == block["asset_ref"]
                    and x["anchor"] == block["anchor"]
                    and x["content"] == e["quote"]
                    and x["content_kind"] == kind
                ),
                None,
            )
            evidence_refs.append(
                existing["evidence_id"]
                if existing
                else add_evidence(
                    package,
                    block["asset_ref"],
                    block["anchor"],
                    e["quote"],
                    content_kind=kind,
                    source_block_id=block.get("source_block_id", block["block_id"]),
                    support_review="locator_literal_check_only",
                )
            )
        reason = record["review_reason"]
        if not subject or subject["kind"] not in FIELDS.get(field_id, {}).get("owner_kinds", []):
            iid = add_issue(
                package,
                "Missing-field subject/field has no verified materialized binding: " + str(field_id),
                [target] if subject else [],
                evidence_refs,
                severity="error",
                category="extraction_uncertain",
            )
            receipt["issue_refs"].append(iid)
            continue
        if status == "not_reported" and not evidence_refs:
            status, reason = "uncertain", "Missing-field evidence has no registered original source anchor"
        if status == "not_reported" and any(
            a["subject_ref"] == target and a["field_id"] == field_id for a in package["assertions"]
        ):
            status, reason = "conflict", "Field-level absence conflicts with a reported value on the same subject"
        row = {
            "assessment_id": f"{package['header']['source_id']}:assessment:"
            f"{fingerprint([target, record['verification_sha256'], status])[:20]}",
            "subject_ref": target,
            "target_ref": target,
            "field_id": field_id,
            "profile_id": _profile(package, field_id, subject["kind"], {}),
            "qualifiers": {},
            "handling": _HANDLING[status],
            "status": "source_missing" if status == "not_reported" else "unresolved",
            "raw_value": None,
            "reason": record["reason"] + "; " + reason,
            "evidence_refs": list(dict.fromkeys(evidence_refs)),
            "source_revision_id": package["header"]["source_revision_id"],
            "review_state": "model_source_reviewed" if status == "not_reported" else "unresolved",
            "use_status": "not_a_measurement",
            "independent_validation": False,
            "source_context": {
                "missing_target": record["target"],
                "missing_status": status,
                "verification_sha256": record["verification_sha256"],
                "source_sha256": source_hash,
                "candidate_sha256": record["candidate_sha256"],
                "review_sha256": record["review_sha256"],
                "checked_block_ids": record["checked_block_ids"],
                "review": deepcopy(record["review"]),
            },
        }
        aid = _append(package, "assessments", row)
        receipt["targets"][record["target"]] = aid
        receipt["record_sha256"][record["target"]] = record_content_hash(row)
        if status != "not_reported":
            iid = add_issue(
                package,
                row["reason"],
                [aid, target],
                row["evidence_refs"],
                severity="error",
                category=_HANDLING[status],
            )
            receipt["issue_refs"].append(iid)
    return receipt
