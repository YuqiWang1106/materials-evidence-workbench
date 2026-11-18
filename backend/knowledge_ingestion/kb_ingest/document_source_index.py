"""Read-only source indexes shared by candidate display and publication.

The index is derived locally; it is never part of model input or checkpoint
identity. Existing block IDs remain authoritative for legacy artifact bundles.
"""

import json
from copy import deepcopy
from dataclasses import dataclass, field

from .storage.task_files import checked_child


@dataclass
class SourceIndex:
    registry: dict
    page_inventory: dict
    asset_errors: dict
    error: str | None = None
    block_errors: dict = field(default_factory=dict)

    def evidence_error(self, evidence, assets):
        asset = evidence.get("asset_ref")
        block_id = evidence.get("block_id")
        if block_id is not None and not isinstance(block_id, str):
            return "source_reference_invalid"
        block = self.registry.get(block_id)
        if asset is not None and (not isinstance(asset, str) or asset not in assets):
            return "source_asset_unregistered"
        if block_id in self.block_errors:
            return self.block_errors[block_id]
        ref = asset or (block or {}).get("asset_ref")
        if ref in self.asset_errors:
            return self.asset_errors[ref]
        if self.error:
            return self.error
        page = evidence.get("page")
        if page is not None and (
            type(page) is not int or page < 1 or (ref and page not in self.page_inventory.get(ref, set()))
        ):
            return "source_page_unavailable"
        if block and (
            (asset is not None and asset != block["asset_ref"]) or (page is not None and page != block["page"])
        ):
            return "source_reference_mismatch"
        return None


def build_source_index(assets, registry, reading=None, *, reading_error=False):
    """Filter all locations through task assets, source hashes and physical pages.

    Legacy nonempty registries without IR keep their IDs and positive page
    anchors. If IR is present it must validate them; legacy data cannot bypass a
    mismatched source hash or an absent physical page.
    """
    if reading_error or not isinstance(registry, dict):
        return SourceIndex({}, {}, {}, "source_index_unreadable")
    pages, errors, derived = {}, {}, {}
    if reading is not None:
        try:
            documents = reading["documents"]
            if not isinstance(documents, list):
                raise ValueError("documents must be an array")
            for document in documents:
                ref = document["asset_ref"]
                if ref not in assets:
                    errors[ref] = "source_asset_unregistered"
                    continue
                if document.get("source_sha256") != assets[ref]["sha256"]:
                    errors[ref] = "source_hash_mismatch"
                    continue
                for row in document["pages"]:
                    page = row["page"]
                    if type(page) is not int or page < 1 or page in pages.get(ref, set()):
                        raise ValueError("physical pages must be positive and unique")
                    pages.setdefault(ref, set()).add(page)
            # Build before filtering to retain original bundle-local block IDs.
            from .ingest.documents.document_pipeline_blocks import ir_blocks

            derived = {block["block_id"]: block for block in ir_blocks(documents)}
        except (ValueError, TypeError, KeyError, AttributeError):
            return SourceIndex({}, {}, errors, "source_index_unreadable")
        for ref in errors:
            pages.pop(ref, None)
    source = registry or derived
    trusted, block_errors = {}, {}
    for key, block in source.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(block, dict):
            block_errors[key] = "source_index_unreadable"
            continue
        ref, page = block.get("asset_ref"), block.get("page")
        reason = None
        if not isinstance(ref, str) or ref not in assets:
            reason = "source_asset_unregistered"
        elif ref in errors:
            reason = errors[ref]
        elif block.get("source_sha256", assets[ref]["sha256"]) != assets[ref]["sha256"]:
            reason = errors[ref] = "source_hash_mismatch"
        elif block.get("block_id") != key:
            reason = "source_reference_mismatch"
        elif type(page) is not int or page < 1 or (reading is not None and page not in pages.get(ref, set())):
            reason = "source_page_unavailable"
        else:
            anchor = block.get("anchor")
            if anchor is not None and not isinstance(anchor, dict):
                reason = "source_index_unreadable"
            else:
                anchor = anchor or {}
                anchor_pages = anchor.get("page_numbers", [anchor.get("page", page)])
                if (
                    not isinstance(anchor_pages, list)
                    or not anchor_pages
                    or any(type(p) is not int or p != page for p in anchor_pages)
                    or ("page" in anchor and (type(anchor["page"]) is not int or anchor["page"] != page))
                ):
                    reason = "source_page_unavailable"
        if reason:
            block_errors[key] = reason
            continue
        trusted[key] = deepcopy(block)
        pages.setdefault(ref, set()).add(page)
    for key, block in list(trusted.items()):
        if block["asset_ref"] in errors:
            block_errors[key] = errors[block["asset_ref"]]
            del trusted[key]
    for ref in errors:
        pages.pop(ref, None)
    error = None if trusted or errors or block_errors else "source_index_missing"
    return SourceIndex(trusted, pages, errors, error, block_errors)


def load_source_index(root, assets):
    try:
        path = checked_child(root, "source-registry.json")
        registry = json.loads(path.read_text()) if path.is_file() else {}
        ir_path = checked_child(root, "document-ir.json")
        reading = None
        if ir_path.is_file():
            reading = json.loads(ir_path.read_text())
            if not isinstance(reading, dict):
                raise ValueError("reading index must be an object")
    except (OSError, ValueError, TypeError):
        return SourceIndex({}, {}, {}, "source_index_unreadable")
    return build_source_index(assets, registry, reading)
