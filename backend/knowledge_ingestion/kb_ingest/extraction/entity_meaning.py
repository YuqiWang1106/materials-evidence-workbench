"""Stored entities closest in meaning to the incoming ones, for the resolve step."""

from __future__ import annotations

from kb_ingest.llm import ModelCallError


def meaning_candidates(embedder, entity_store, workspace_id, merged, unresolved=None):
    """similar() for resolve_graph: stored entities of the same type closest in meaning, by embeddings."""
    from .entity_embedding import entity_text, similar_entities
    from .entity_index import EntityIndex

    def similar(entities):
        descriptions = {}
        for row in merged["attributes"]:
            if row["section"] == "说明":
                descriptions.setdefault(row["local_id"], row["text"])
        incoming = [
            (
                entity["local_id"],
                entity["type"],
                entity_text(
                    entity["type"], entity["name"], entity.get("aliases"), descriptions.get(entity["local_id"])
                ),
            )
            for entity in entities
        ]
        stored = [
            (unit.entity_id, unit.type, unit.index_text)
            for unit in EntityIndex(entity_store, workspace_id)._load()["units"]
            if unit.kind == "entity" and unit.type != "formulation"
        ]
        try:
            return similar_entities(embedder, incoming, stored)
        except (ModelCallError, ValueError) as exc:
            if unresolved is not None:
                unresolved.append(
                    {"phase": "resolve", "reason": "embedding candidates unavailable: " + type(exc).__name__}
                )
            return {}

    return similar
