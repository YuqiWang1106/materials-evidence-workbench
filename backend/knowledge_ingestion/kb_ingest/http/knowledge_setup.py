"""Bind the website knowledge and Wiki services to their shared trusted scope."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict, Unpack

from fastapi import FastAPI

if TYPE_CHECKING:
    from kb_ingest.extraction.entity_index import EntityIndex
    from kb_ingest.ingest.service import IngestionService
    from kb_ingest.knowledge_publication import KnowledgePublicationService
    from kb_ingest.knowledge_scope import KnowledgeScopeRegistry
    from kb_ingest.knowledge_snapshot import KnowledgeSnapshotStore
    from kb_ingest.knowledge_wiki import WikiService
    from kb_ingest.knowledge_wiki_adapter import KnowledgeWikiAdapter
    from kb_ingest.knowledge_wiki_summary import WikiSummaryService
    from kb_ingest.project_embedding import ProjectEmbeddingDispatcher
    from kb_ingest.storage.entity_store import EntityStore
    from kb_ingest.store import Store
    from kb_ingest.wiki_hybrid_search import WikiHybridSearch
    from kb_ingest.wiki_relations import WikiRelationsService


class WebsiteKnowledge(Protocol):
    """Knowledge service bindings shared by the website routers."""

    workspace_id: str
    registry: KnowledgeScopeRegistry
    snapshots: KnowledgeSnapshotStore
    adapters: Mapping[str, object]
    documents: object | None
    wiki_adapter: KnowledgeWikiAdapter
    wiki_hybrid: WikiHybridSearch
    wiki_relations: WikiRelationsService
    entity_store: EntityStore
    entity_workspace_id: str
    entity_index: EntityIndex[EntityStore]
    entity_passage: Callable[[str, str, int], object]


@dataclass(frozen=True, slots=True)
class KnowledgeOptions:
    knowledge_service: WebsiteKnowledge | None
    wiki_service: WikiService | None
    wiki_collection_id: str | None
    wiki_summary_service: WikiSummaryService | None
    project_embedding_dispatcher: ProjectEmbeddingDispatcher | None


@dataclass(frozen=True, slots=True)
class KnowledgeServices:
    knowledge: WebsiteKnowledge
    wiki: WikiService
    publisher: KnowledgePublicationService
    summary: WikiSummaryService
    relations: WikiRelationsService


class _WikiScope(TypedDict, total=False):
    workspace_id: str
    registry: KnowledgeScopeRegistry
    snapshot_store: KnowledgeSnapshotStore
    collection_id: str


class _WikiFactory(Protocol):
    def __call__(self, service: IngestionService, /, **scope: Unpack[_WikiScope]) -> WikiService: ...


def _bind_wiki(service: IngestionService, options: KnowledgeOptions) -> WikiService:
    if options.wiki_service is not None:
        return options.wiki_service
    from kb_ingest.knowledge_wiki import WikiService

    scope: _WikiScope = {}
    knowledge = options.knowledge_service
    if knowledge is not None:
        scope = {
            "workspace_id": knowledge.workspace_id,
            "registry": knowledge.registry,
            "snapshot_store": knowledge.snapshots,
        }
        # Reuse an existing trusted collection without extending its membership.
        if options.wiki_collection_id is not None:
            scope["collection_id"] = options.wiki_collection_id
        elif len(knowledge.adapters) == 1:
            scope["collection_id"] = next(iter(knowledge.adapters))
    make_wiki: _WikiFactory = WikiService
    return make_wiki(service, **scope)


def _bind_adapter(
    service: IngestionService, wiki: WikiService, adapter: KnowledgeWikiAdapter, knowledge: WebsiteKnowledge | None
) -> WebsiteKnowledge:
    if knowledge is None:
        from kb_ingest.knowledge_website import website_knowledge

        make_knowledge: Callable[[IngestionService, WikiService, KnowledgeWikiAdapter], WebsiteKnowledge] = (
            website_knowledge
        )
        return make_knowledge(service, wiki, adapter)
    if (
        adapter.workspace_id != knowledge.workspace_id
        or adapter.registry is not knowledge.registry
        or adapter.snapshots is not knowledge.snapshots
    ):
        message = "Wiki scope must reuse the configured knowledge service"
        raise ValueError(message)
    knowledge.wiki_adapter = adapter
    return knowledge


def bind_knowledge(app: FastAPI, service: IngestionService, options: KnowledgeOptions) -> KnowledgeServices:
    from kb_ingest.knowledge_publication import KnowledgePublicationService
    from kb_ingest.knowledge_wiki_adapter import KnowledgeWikiAdapter
    from kb_ingest.knowledge_wiki_summary import WikiSummaryService
    from kb_ingest.wiki_hybrid_search import WikiHybridSearch
    from kb_ingest.wiki_relations import WikiRelationsService

    wiki = _bind_wiki(service, options)
    service.study_unit_workspace_id = wiki.workspace_id
    make_publisher: Callable[[WikiService], KnowledgePublicationService] = KnowledgePublicationService
    publisher = make_publisher(wiki)
    make_adapter: Callable[[WikiService, KnowledgePublicationService], KnowledgeWikiAdapter] = KnowledgeWikiAdapter
    adapter = make_adapter(wiki, publisher)
    knowledge = _bind_adapter(service, wiki, adapter, options.knowledge_service)
    hybrid_search: Callable[[Store, dict[str, str], ProjectEmbeddingDispatcher | None], WikiHybridSearch] = (
        WikiHybridSearch
    )
    scope = {
        "workspace_id": adapter.workspace_id,
        "collection_id": adapter.collection_id,
        "use_policy": "source_reviewed",
    }
    knowledge.wiki_hybrid = hybrid_search(service.store, scope, options.project_embedding_dispatcher)
    app.state.wiki_hybrid_search = knowledge.wiki_hybrid
    make_relations: Callable[[KnowledgeWikiAdapter], WikiRelationsService] = WikiRelationsService
    knowledge.wiki_relations = make_relations(adapter)
    app.state.wiki_relations_service = knowledge.wiki_relations
    app.state.wiki_service = wiki
    app.state.knowledge_service = knowledge
    make_summary: Callable[[WikiService], WikiSummaryService] = WikiSummaryService
    summary = options.wiki_summary_service or make_summary(wiki)
    if summary.wiki is not wiki:
        message = "SUMMARY_WORKSPACE_MISMATCH"
        raise ValueError(message)
    app.state.wiki_summary_service = summary
    return KnowledgeServices(knowledge, wiki, publisher, summary, knowledge.wiki_relations)
