"""Fixed evidence identities and readback from workspace-bound publications."""

import json
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, TypedDict

from ..core import StateConflict, atomic_json, canonical, fingerprint
from ..extraction.entity_resolve_locations import highlight_regions
from ..mcp_contract.protocol import EVIDENCE_REF_VERSION, AgentKnowledgeError, encode_evidence_ref
from .agent_publications import Publication, PublicationPart, Record, records


class _EvidenceStore(Protocol):
    root: Path

    def get_asset(self, asset_id: str) -> Record: ...
    def asset_bytes(self, asset_id: str) -> bytes: ...
    def lock(self, name: str) -> AbstractContextManager[object]: ...


class EvidencePayload(TypedDict):
    version: int
    workspace_id: str
    source_id: object
    task_id: object
    processing_run_id: str
    candidate_sha256: object
    entity_id: object
    attribute_key: str
    location_key: str
    asset_id: str
    asset_sha256: object
    graph_sha256: str
    blocks_sha256: str
    block_ids: list[str]
    quote_sha256: str
    content_block_id: str | None


def location_key(place: Mapping[str, object]) -> str:
    return fingerprint(
        {key: place.get(key) for key in ("asset_ref", "page", "block_id", "quote")}
        | {"block_ids": place.get("block_ids") or []}
    )


@dataclass(frozen=True, slots=True)
class _Location:
    publication: PublicationPart
    attribute: Mapping[str, object]
    place: Mapping[str, object]

    def block(self, key: str, asset_id: str, *, context: bool = False) -> Record:
        value = self.publication["blocks"].get(key)
        if (
            isinstance(value, dict)
            and value.get("asset_ref") == asset_id
            and (not context or value.get("page") == self.place.get("page"))
        ):
            return value
        if context:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "Context does not belong to the evidence page.")
        raise AgentKnowledgeError("INTEGRITY_FAILURE", "Evidence blocks do not belong to the registered original.")

    def passage(self, asset_id: str, content_block_id: str | None) -> tuple[list[str], str]:
        block_ids = _block_ids(self.place)
        blocks = [self.block(key, asset_id) for key in block_ids]
        if blocks[0].get("page") != self.place.get("page"):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "Evidence page does not match its published block.")
        quote = self.place.get("quote")
        if content_block_id is not None:
            quote = self.block(content_block_id, asset_id, context=True).get("text")
            block_ids = [content_block_id]
        if not isinstance(quote, str) or not quote.strip():
            raise AgentKnowledgeError("SOURCE_NOT_READY", "The published evidence has no readable text.")
        return block_ids, quote


def _block_ids(place: Mapping[str, object]) -> list[str]:
    extra = place.get("block_ids") or []
    if not isinstance(extra, list):
        raise AgentKnowledgeError("INTEGRITY_FAILURE", "Evidence blocks do not belong to the registered original.")
    unique: dict[str, None] = {}
    for key in [place.get("block_id"), *extra]:
        if not isinstance(key, str):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "Evidence blocks do not belong to the registered original.")
        unique[key] = None
    return list(unique)


def _fixed_location(pub: Publication, payload: Record) -> _Location:
    part: PublicationPart = next(
        (p for p in pub["publications"] if p["graph_hash"] == payload.get("graph_sha256")), pub
    )
    expected = {
        "graph_sha256": part["graph_hash"],
        "blocks_sha256": part["blocks_hash"],
        "candidate_sha256": part["task"]["package_sha256"],
        "processing_run_id": part["run"],
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AgentKnowledgeError("INTEGRITY_FAILURE", "The fixed evidence artifacts have changed.")
    attribute = next(
        (
            row
            for row in part["graph"]["attributes"]
            if fingerprint(row) == payload["attribute_key"] and row.get("entity_id") == payload["entity_id"]
        ),
        None,
    )
    if attribute is None:
        raise AgentKnowledgeError("REFERENCE_NOT_FOUND", "The exact published attribute was not found.")
    for place in [attribute, *records(attribute.get("citations", []))]:
        if location_key(place) == payload["location_key"]:
            return _Location(part, attribute, place)
    raise AgentKnowledgeError("REFERENCE_NOT_FOUND", "The published evidence location was not found.")


def _content_kind(blocks: list[Record]) -> str:
    if any(
        row.get("source") in ("visual", "handwriting") or row.get("kind") == "visual_transcription" for row in blocks
    ):
        return "model_transcription"
    if all(row.get("source") in ("mineru", "pymupdf", "docvortex", "native", "local_ocr") for row in blocks):
        return "parsed"
    return "unknown"


def _passage(key: str, block: Record) -> Record:
    return {
        "block_id": key,
        "page": block.get("page"),
        "text": block.get("text", ""),
        "table_region": block.get("table_region"),
    }


def _context_ids(loc: _Location, payload: EvidencePayload, around: int) -> list[str]:
    # Expand the page window once. Table headers and captions belong to the evidence even at zero radius.
    page: dict[str, Record] = {
        key: row
        for key, row in loc.publication["blocks"].items()
        if isinstance(row, dict)
        and row.get("asset_ref") == payload["asset_id"]
        and row.get("page") == loc.place.get("page")
    }
    keys = list(page)
    positions = {key: index for index, key in enumerate(keys)}
    selected: set[str] = set()
    table_ids: set[object] = set()
    for key in payload["block_ids"]:
        if key in positions:
            at = positions[key]
            selected.update(keys[max(0, at - around) : at + around + 1])
        block = loc.block(key, payload["asset_id"])
        table_id = block.get("table_region") or (key if block.get("kind") == "table" else None)
        if table_id:
            table_ids.add(table_id)
    for table_id in table_ids:
        selected.update(
            key for key, block in page.items() if key == table_id or block.get("table_context_for") == table_id
        )
    selected.difference_update(payload["block_ids"])
    return [key for key, row in page.items() if key in selected and row.get("text")]


def _short_ref(reference: str) -> str:
    return "kbref_" + sha256(reference.encode()).hexdigest()[:32]


def _read_link(path: Path, short: str) -> str:
    if path.is_symlink():
        message = "Invalid locator file"
        raise ValueError(message)
    value: object = json.loads(path.read_text(encoding="utf-8"))
    ref = value["evidence_ref"] if isinstance(value, dict) else None
    if not isinstance(ref, str) or _short_ref(ref) != short:
        message = "Mismatched reference"
        raise ValueError(message)
    return ref


class AgentEvidence:
    def __init__(self, store: _EvidenceStore, workspace_id: str) -> None:
        self.store = store
        self.workspace_id = workspace_id

    def _asset(self, pub: PublicationPart, asset_id: str) -> Record:
        if asset_id in pub["assets"]:
            return pub["assets"][asset_id]
        registered = next(
            (row for row in records(pub["package"].get("assets", [])) if row.get("asset_id") == asset_id), None
        )
        asset_ids = pub["task"]["asset_ids"]
        if not isinstance(asset_ids, list) or asset_id not in asset_ids or registered is None:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The evidence original is not part of its published task.")
        try:
            asset = self.store.get_asset(asset_id)
            self.store.asset_bytes(asset_id)
        except (KeyError, StateConflict, OSError) as exc:
            raise AgentKnowledgeError(
                "INTEGRITY_FAILURE", "The evidence original is unavailable or has changed."
            ) from exc
        if asset["asset_id"] != asset_id or asset["sha256"] != registered.get("sha256"):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The original does not match the published candidate.")
        pub["assets"][asset_id] = asset
        return asset

    def payload(
        self,
        pub: PublicationPart,
        attribute: Mapping[str, object],
        place: Mapping[str, object],
        content_block_id: str | None = None,
    ) -> EvidencePayload:
        asset_id = place.get("asset_ref")
        if not isinstance(asset_id, str):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The evidence original is not part of its published task.")
        asset = self._asset(pub, asset_id)
        block_ids, quote = _Location(pub, attribute, place).passage(asset_id, content_block_id)
        return {
            "version": EVIDENCE_REF_VERSION,
            "workspace_id": self.workspace_id,
            "source_id": pub["task"]["source_id"],
            "task_id": pub["task"]["task_id"],
            "processing_run_id": pub["run"],
            "candidate_sha256": pub["task"]["package_sha256"],
            "entity_id": attribute["entity_id"],
            "attribute_key": fingerprint(attribute),
            "location_key": location_key(place),
            "asset_id": asset_id,
            "asset_sha256": asset["sha256"],
            "graph_sha256": pub["graph_hash"],
            "blocks_sha256": pub["blocks_hash"],
            "block_ids": block_ids,
            "quote_sha256": sha256(quote.encode("utf-8")).hexdigest(),
            "content_block_id": content_block_id,
        }

    def _project(self, loc: _Location, payload: EvidencePayload, fields: Mapping[str, object]) -> Record:
        ref = encode_evidence_ref(payload)
        ids, asset_id = payload["block_ids"], payload["asset_id"]
        selected = [loc.block(key, asset_id) for key in ids]
        quote = selected[0]["text"] if payload["content_block_id"] is not None else loc.place["quote"]
        regions = loc.place.get("highlight_regions") if payload["content_block_id"] is None else None
        source = next(
            (
                row
                for row in records(loc.publication["package"].get("sources", []))
                if row.get("source_id") == payload["source_id"]
            ),
            {},
        )
        result = {
            key: value for key, value in fields.items() if key not in ("version", "location_key", "content_block_id")
        }
        result.update(
            evidence_ref=ref,
            citation_key=sha256(ref.encode()).hexdigest()[:20],
            title=source.get("title") or self._asset(loc.publication, asset_id)["original_name"],
            page=loc.place.get("page"),
            quote=quote,
            content_kind=_content_kind(selected),
            highlight_regions=regions or [region for block in selected for region in highlight_regions(block)],
            publication_status="entity_published",
            scientific_validation=False,
            passages=[_passage(key, block) for key, block in zip(ids, selected, strict=True)],
            context=[],
        )
        return result

    def readback(self, pub: Publication, payload: Record, around: int) -> Record:
        loc = _fixed_location(pub, payload)
        content_id = payload.get("content_block_id")
        if content_id is not None and not isinstance(content_id, str):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The evidence identity or content hash does not match.")
        expected = self.payload(loc.publication, loc.attribute, loc.place, content_id)
        if canonical(payload) != canonical(expected):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The evidence identity or content hash does not match.")
        result = self._project(loc, expected, payload)
        context = []
        for key in _context_ids(loc, expected, around):
            neighbor = self.payload(loc.publication, loc.attribute, loc.place, key)
            context.append(self._project(loc, neighbor, neighbor))
        result["context"] = context
        return result

    def link_path(self, short: str) -> Path:
        if not re.fullmatch(r"kbref_[0-9a-f]{32}", short):
            raise AgentKnowledgeError("INVALID_ARGUMENT", "Invalid short evidence reference.")
        directory = self.store.root / "citation-links" / sha256(self.workspace_id.encode()).hexdigest()
        if directory.resolve() != directory:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid citation locator directory.")
        return directory / (short + ".json")

    def resolve_link(self, short: str) -> str:
        path = self.link_path(short)
        try:
            return _read_link(path, short)
        except FileNotFoundError as exc:
            raise AgentKnowledgeError(
                "REFERENCE_NOT_FOUND", "The short citation is unavailable; read the evidence again."
            ) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The short citation mapping is invalid.") from exc

    def remember_links(self, evidence: Record) -> None:
        # Persist locators only. Opening one always rechecks the original and its publication.
        with self.store.lock("agent-citation-links"):
            for item in [evidence, *records(evidence["context"])]:
                ref = item["evidence_ref"]
                if not isinstance(ref, str):
                    raise AgentKnowledgeError("INTEGRITY_FAILURE", "The short citation mapping is invalid.")
                short = _short_ref(ref)
                path = self.link_path(short)
                if path.exists() or path.is_symlink():
                    if self.resolve_link(short) != ref:
                        raise AgentKnowledgeError("INTEGRITY_FAILURE", "Short citation collision.")
                else:
                    try:
                        atomic_json(path, {"evidence_ref": ref})
                    except OSError as exc:
                        raise AgentKnowledgeError("KB_UNAVAILABLE", "Cannot persist the citation locator.") from exc
                item["link_ref"] = short
