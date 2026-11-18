"""Bind direct source issues to exact records without treating them as facts."""

from __future__ import annotations

import re
from copy import deepcopy

from .contract import add_evidence, add_issue
from .core import fingerprint


def materialize_direct_issues(package, accepted, blocks, mapping):
    """Retain categories, unsupported citations and unresolved object bindings.

    The caller materializes objects/facts with ``issues=[]`` first. Issue text
    is never an identity lookup. Valid citations are literal locators only;
    unresolved citations remain audit data and cannot manufacture evidence.
    """
    registry = {b["block_id"]: b for b in blocks}
    if len(registry) != len(blocks):
        raise ValueError("duplicate source region identity")
    objects = {o["object_id"] for o in package["objects"]}
    assets = {a["asset_id"] for a in package["assets"]}
    receipt = {"issue_refs": []}
    for index, issue in enumerate(accepted.get("issues", [])):
        targets, unresolved_objects, evidence_refs, unresolved_evidence = [], [], [], []
        for key in issue.get("object_keys", []):
            target = mapping.get("targets", {}).get("o:" + key)
            if target in objects:
                targets.append(target)
            else:
                unresolved_objects.append(key)
        for evidence in issue.get("evidence", []):
            block = registry.get(evidence.get("block_id"))
            quote = evidence.get("quote")
            valid = (
                block is not None
                and isinstance(quote, str)
                and bool(quote.strip())
                and re.sub(r"\s+", "", quote) in re.sub(r"\s+", "", block.get("text", ""))
                and block.get("asset_ref") in assets
                and bool(block.get("anchor"))
            )
            if not valid:
                unresolved_evidence.append(deepcopy(evidence))
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
                    e
                    for e in package["evidence"]
                    if e["asset_ref"] == block["asset_ref"]
                    and e["anchor"] == block["anchor"]
                    and e["content"] == quote
                    and e["content_kind"] == kind
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
                    quote,
                    content_kind=kind,
                    source_block_id=block.get("source_block_id", block["block_id"]),
                    support_review="locator_literal_check_only",
                )
            )
        targets = list(dict.fromkeys(targets))
        evidence_refs = list(dict.fromkeys(evidence_refs))
        identity = fingerprint([index, issue, targets, evidence_refs])[:20]
        iid = add_issue(
            package,
            issue["description"],
            targets,
            evidence_refs,
            severity="error" if unresolved_objects or unresolved_evidence else issue.get("severity", "warning"),
            issue_id=f"{package['header']['source_id']}:issue:direct:{identity}",
            category=issue.get("category", "extraction_uncertain"),
            source_context={
                "candidate_issue": deepcopy(issue),
                "candidate_issue_index": index,
                "unresolved_object_keys": unresolved_objects,
                "unresolved_evidence": unresolved_evidence,
                "evidence_check": "locator_literal_check_only_semantics_unreviewed",
            },
        )
        receipt["issue_refs"].append(iid)
    return receipt
