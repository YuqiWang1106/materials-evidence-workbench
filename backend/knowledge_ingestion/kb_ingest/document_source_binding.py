"""Exact server packet bindings for immutable model evidence anchors."""

import json
from copy import deepcopy

from .core import fingerprint
from .storage.task_files import checked_child


def canonical_location_evidence(item, metadata, assets):
    """Resolve a submitted whole-PDF filename only from its exact group manifest.

    Do not guess filenames or reinterpret page numbers in a split PDF. The raw
    model evidence remains untouched; this alias is only for source navigation.
    """
    if not isinstance(item.get("asset_ref"), str) or type(item.get("page")) is not int:
        return item
    if item["asset_ref"] in assets:
        return item
    manifest = metadata.get("manifest")
    if not isinstance(manifest, dict):
        return item
    source, group = manifest.get("source"), manifest.get("group")
    if not isinstance(source, dict) or not isinstance(group, dict) or group.get("pdf_is_original") is not True:
        return item
    if not isinstance(source.get("asset_ref"), str):
        return item
    registered = assets.get(source["asset_ref"])
    if not registered or registered.get("sha256") != source.get("sha256"):
        return item
    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        return item
    names = {
        a.get("filename")
        for a in attachments
        if isinstance(a, dict) and a.get("media_type") == "application/pdf" and isinstance(a.get("filename"), str)
    }
    pages = group.get("original_pages")
    if not isinstance(pages, list) or item["asset_ref"] not in names or item["page"] not in pages:
        return item
    return {**item, "asset_ref": source["asset_ref"]}


def _packet_metadata(service, task, candidate_id):
    path = checked_child(service._document_artifact_root(task), "input-summary.json")
    try:
        rows = json.loads(path.read_text()) if path.is_file() else []
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    matching = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("group_id"), str)
        and candidate_id == "raw-" + fingerprint(row["group_id"])[:24]
    ]
    if len(matching) != 1:
        return None
    row = matching[0]
    manifest = row.get("manifest") or {}
    if not isinstance(manifest, dict):
        return None
    group = manifest.get("group")
    if not isinstance(group, dict) or group.get("id") != row["group_id"]:
        return None
    return row


def resolve_evidence_anchor(service, *, task_id, candidate_id, evidence):
    """Return additional registered location; never modify source/snapshot JSON."""
    original = deepcopy(evidence)
    task = service.store.get_task(task_id)
    assets = service._document_source_assets(task)
    metadata = _packet_metadata(service, task, candidate_id)
    resolved = canonical_location_evidence(deepcopy(evidence), metadata or {}, assets)
    asset = assets.get(resolved.get("asset_ref"))
    valid = asset is not None and type(resolved.get("page")) is int and resolved["page"] > 0
    basis = {}
    if valid:
        registered = asset["asset_id"]
        if registered not in task["asset_ids"]:
            valid = False
        else:
            resolved = {**resolved, "asset_ref": registered}
            basis = {"kind": "registered_asset", "asset_sha256": asset["sha256"]}
            if evidence.get("asset_ref") not in assets:
                basis.update(
                    kind="exact_transmitted_whole_pdf",
                    candidate_id=candidate_id,
                    group_id=metadata["group_id"],
                    transmitted_filename=evidence["asset_ref"],
                )
    return {
        "status": "resolved" if valid else "unresolved",
        "original_anchor": original,
        "resolved_anchor": resolved if valid else deepcopy(evidence),
        "resolution_basis": basis if valid else {},
    }
