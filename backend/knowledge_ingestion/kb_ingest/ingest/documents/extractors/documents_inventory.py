"""Conservative object identities and evidence locators within one source unit."""

from __future__ import annotations

import re

from kb_ingest.contract import ContractError, _id, add_evidence, strict_equal
from kb_ingest.core import fingerprint

from .documents_validate import _comparison_text


def _prompt_blocks(blocks: list[dict]) -> list[dict]:
    return [
        {
            "block_id": b["block_id"],
            "asset_ref": b.get("asset_ref"),
            "page": b.get("page"),
            "kind": b.get("kind"),
            "text": b["text"],
        }
        for b in blocks
    ]


def _evidence_refs(pkg: dict, support: list[dict], registry: dict) -> list[str]:
    result = []
    for item in support:
        block = registry[item["block_id"]]
        visual = block.get("kind") in ("visual_page", "visual_transcription") or block.get("source") == "visual"
        content_kind = (
            "model_visual_transcription"
            if visual
            else "ocr_transcription"
            if block.get("source") in ("mineru", "ocr")
            else "verbatim"
        )
        source_block_id = block.get("source_block_id", block["block_id"])
        if pkg["header"].get("document_extraction_mode") == "direct-six-v1":
            identity = [block["asset_ref"], block["anchor"], item["quote"], content_kind]
            eid = _id(pkg, "evidence", identity)
            existing = next((e for e in pkg["evidence"] if e["evidence_id"] == eid), None)
            if existing is not None:
                if not strict_equal(
                    [existing.get(k) for k in ("asset_ref", "anchor", "content", "content_kind")], identity
                ):
                    raise ContractError("Conflicting direct evidence source content")
                if existing.get("source_block_id") not in (None, source_block_id):
                    raise ContractError("Conflicting direct evidence source block binding")
                # Review state belongs to the existing evidence. Reusing its
                # immutable source locator neither downgrades nor approves it.
                result.append(eid)
                continue
        result.append(
            add_evidence(
                pkg,
                block["asset_ref"],
                block["anchor"],
                item["quote"],
                content_kind=content_kind,
                source_block_id=source_block_id,
                support_review="not_performed",
            )
        )
    return result


class UnitObjectInventory:
    """Conservative identities within one discovered source unit, never across units.

    Labels are lookup candidates, not sufficient evidence. Merge requires an
    explicit identifier quoted in source or an identical source evidence anchor.
    Model-proposed identity remains unreviewed even when references are closed.
    """

    merge_kinds = {"sample", "formulation", "method_definition"}

    def __init__(self, unit_id: str):
        self.unit_id = unit_id
        self.entries: dict[str, dict] = {}
        self.blocks: dict[str, dict] = {}
        self.proofs: dict[str, dict] = {}

    def as_prompt(self) -> list[dict]:
        return [dict(row) for row in self.entries.values()]

    def identify(self, item: dict, owner_ref: str | None, registry: dict, part_key: str) -> tuple[str, dict, bool]:
        base = [self.unit_id, item["scope"], item["kind"], _comparison_text(item["label"]), owner_ref]
        support = item.get("identity_evidence") or item.get("evidence", [])
        supported = [
            e
            for e in support
            if e.get("block_id") in registry
            and e.get("quote")
            and (
                registry[e["block_id"]].get("kind") == "visual_page"
                or _comparison_text(e["quote"]) in _comparison_text(registry[e["block_id"]]["text"])
            )
        ]
        identifier = item.get("source_identifier")
        if identifier:
            # Short table identifiers must be separate tokens, not substrings of words/numbers.
            pattern = r"(?<![A-Za-z0-9])" + re.escape(identifier.strip()) + r"(?![A-Za-z0-9])"
            if not any(re.search(pattern, e["quote"]) for e in supported):
                identifier = None
        proof = {
            "base": base,
            "identifier": identifier,
            "anchors": [
                fingerprint(
                    [registry[e["block_id"]].get("asset_ref"), registry[e["block_id"]].get("anchor"), e["quote"]]
                )
                for e in supported
            ],
        }
        possible = [(oid, p) for oid, p in self.proofs.items() if p["base"] == base]
        matches = (
            [
                oid
                for oid, p in possible
                if (identifier and p["identifier"] == identifier) or set(proof["anchors"]) & set(p["anchors"])
            ]
            if item["kind"] in self.merge_kinds
            else []
        )
        if len(matches) == 1:
            return matches[0], proof, False
        ambiguous = (
            item["kind"] in self.merge_kinds
            and bool(possible)
            and (len(matches) > 1 or not identifier or any(not p["identifier"] for _, p in possible))
        )
        # A partition namespace is only the fallback for an unresolved/new identity.
        oid = f"{self.unit_id}:object:{fingerprint([base, part_key, item['key'], proof])[:20]}"
        return oid, proof, ambiguous

    def register(self, object_id: str, item: dict, object_ids: dict, proof: dict, blocks: dict) -> None:
        self.blocks.update(blocks)
        entry = self.entries.setdefault(
            object_id,
            {
                "key": object_id,
                "kind": item["kind"],
                "label": item["label"],
                "scope": item["scope"],
                "owner_key": object_ids.get(item.get("owner_key")),
                "material_key": object_ids.get(item.get("material_key")),
                "source_identifier": proof["identifier"],
                "evidence": [],
                "identity_evidence": [],
            },
        )
        for name in ("evidence", "identity_evidence"):
            for e in item.get(name, []):
                if e not in entry[name]:
                    entry[name].append(e)
        old_proof = self.proofs.setdefault(object_id, proof)
        old_proof["anchors"] = list(dict.fromkeys(old_proof["anchors"] + proof["anchors"]))
