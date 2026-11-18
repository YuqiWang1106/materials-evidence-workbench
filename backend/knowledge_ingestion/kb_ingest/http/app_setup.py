"""Validate startup capabilities and register the website's routers in contract order."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, FastAPI

from kb_ingest.ingest.service import IngestionService

from ..auth.session import SessionCredentials
from ..ingest.model_binding import check_dispatchers, owns_context
from ..ingest.tables.draft_reads import DraftReads
from .api import (
    assets,
    documents,
    evidence,
    publication,
    raw_tables,
    search,
    site,
    sources,
    standard_review,
    table_catalogue,
    table_chats,
    table_drafts,
    table_recipes,
    table_regions,
    tasks,
)
from .knowledge_setup import KnowledgeServices, WebsiteKnowledge
from .research_setup import ResearchOptions, ResearchServices

if TYPE_CHECKING:
    from ..agent_knowledge_service import AgentKnowledgeService
    from ..document_report_revision_service import DocumentReportRevisionService
    from ..extraction.entity_index import EntityIndex
    from ..knowledge_publication import KnowledgePublicationService
    from ..knowledge_wiki import WikiService
    from ..knowledge_wiki_summary import WikiSummaryService
    from ..llm.model_requests import ModelRequestContext
    from ..project_embedding import ProjectEmbeddingDispatcher
    from ..research_chat_service import ResearchChatService
    from ..research_summary_service import ResearchWikiSummaryService
    from ..storage.entity_store import EntityStore
    from ..study_wiki_jobs import StudyWikiJobs
    from ..wiki_relations import WikiRelationsService


UNSET_DOCUMENT_DISPATCHER = object()


def bind_dispatchers(
    service: IngestionService, options: ResearchOptions, extraction_context: ModelRequestContext | None
) -> ResearchOptions:
    if options.document_dispatcher is UNSET_DOCUMENT_DISPATCHER:
        options = replace(options, document_dispatcher=options.model_dispatcher)
    check_dispatchers(
        service.workspace_id, options.wiki_dispatchers, options.model_dispatcher, options.document_dispatcher
    )
    if extraction_context is not None:
        if not owns_context(service.workspace_id, extraction_context):
            message = "trusted_extraction_context_required"
            raise ValueError(message)
        service.project_budget_context = extraction_context
    return options


@dataclass(frozen=True, slots=True)
class StudyOptions:
    embedding: ProjectEmbeddingDispatcher | None
    wiki_dispatchers: Mapping[str, object] | None
    is_agent_enabled: bool
    max_response_bytes: int


class _UnitRoutes(Protocol):
    def __call__(self, service: IngestionService, /, *, workspace_id: str) -> APIRouter: ...


class _EntityRoutes(Protocol):
    def __call__(
        self, service: IngestionService, /, *, workspace_id: str, index: EntityIndex[EntityStore]
    ) -> APIRouter: ...


class _StudyJobsFactory(Protocol):
    def __call__(
        self, service: IngestionService, wiki: WikiService, /, *, dispatchers: Mapping[str, object] | None
    ) -> StudyWikiJobs: ...


def _entity_router(service: IngestionService, graph: KnowledgeServices, options: StudyOptions) -> APIRouter:
    from ..entity_api import routes
    from ..extraction.entity_index import EntityIndex
    from ..storage.entity_store import EntityStore

    knowledge = graph.knowledge
    knowledge.entity_store = EntityStore(service.store)
    knowledge.entity_workspace_id = graph.wiki.workspace_id

    def source_labels() -> dict[str, object]:
        citation_index: Callable[[], dict[str, dict[str, object]]] = service.source_citation_index
        return {key: row["title"] for key, row in citation_index().items()}

    knowledge.entity_index = EntityIndex(
        knowledge.entity_store,
        graph.wiki.workspace_id,
        vectors_root=service.store.root / "cache" / "entity-vectors",
        dispatcher=options.embedding,
        source_labels=source_labels,
    )
    knowledge.entity_passage = service.source_passage
    build_router: _EntityRoutes = routes
    return build_router(service, workspace_id=graph.wiki.workspace_id, index=knowledge.entity_index)


def _agent_router(
    app: FastAPI, service: IngestionService, graph: KnowledgeServices, max_response_bytes: int
) -> APIRouter:
    from ..agent_knowledge_service import AgentKnowledgeService
    from .api.agent_knowledge import routes

    if graph.wiki.workspace_id != service.workspace_id:
        message = "Read-only access requires the configured ingestion workspace"
        raise ValueError(message)
    build_agent: Callable[[IngestionService, EntityStore, EntityIndex[EntityStore], str], AgentKnowledgeService] = (
        AgentKnowledgeService
    )
    agent = build_agent(service, graph.knowledge.entity_store, graph.knowledge.entity_index, service.workspace_id)
    app.state.agent_knowledge_service = agent
    return routes(agent, max_response_bytes=max_response_bytes)


def _study_router(
    app: FastAPI, service: IngestionService, wiki: WikiService, dispatchers: Mapping[str, object] | None
) -> APIRouter:
    from ..study_wiki_api import routes
    from ..study_wiki_jobs import StudyWikiJobs

    build_jobs: _StudyJobsFactory = StudyWikiJobs
    jobs = build_jobs(service, wiki, dispatchers=dispatchers)
    app.state.study_wiki_jobs = jobs
    build_router: Callable[[StudyWikiJobs], APIRouter] = routes
    return build_router(jobs)


def include_service_routers(
    app: FastAPI, service: IngestionService, graph: KnowledgeServices, research: ResearchServices, study: StudyOptions
) -> None:
    from .. import (
        document_report_revision_api,
        knowledge_api,
        knowledge_wiki_api,
        research_chat_api,
        research_summary_api,
        study_unit_api,
    )

    wiki: Callable[[WikiService, KnowledgePublicationService, WikiSummaryService, WikiRelationsService], APIRouter] = (
        knowledge_wiki_api.routes
    )
    knowledge: Callable[[WebsiteKnowledge], APIRouter] = knowledge_api.routes
    chat: Callable[[ResearchChatService], APIRouter] = research_chat_api.routes
    summary: Callable[[ResearchWikiSummaryService], APIRouter] = research_summary_api.routes
    report: Callable[[DocumentReportRevisionService], APIRouter] = document_report_revision_api.routes
    units: _UnitRoutes = study_unit_api.routes

    for router in (
        wiki(graph.wiki, graph.publisher, graph.summary, graph.relations),
        knowledge(graph.knowledge),
        chat(research.chat),
        summary(research.summary),
        report(research.report),
        units(service, workspace_id=graph.wiki.workspace_id),
        _entity_router(service, graph, study),
    ):
        app.include_router(router)
    if study.is_agent_enabled:
        app.include_router(_agent_router(app, service, graph, study.max_response_bytes))
    app.include_router(_study_router(app, service, graph.wiki, study.wiki_dispatchers))


def include_ingestion_routers(
    app: FastAPI, service: IngestionService, credentials: SessionCredentials, ui_dir: Path | None
) -> None:
    reads = DraftReads(service.store)
    for router in (
        site.routes(service, credentials, ui_dir),
        assets.routes(service),
        tasks.routes(service),
        table_catalogue.routes(service),
        table_drafts.routes(service, reads),
        table_regions.routes(service),
        raw_tables.routes(service),
        table_chats.routes(service),
        raw_tables.analysis_routes(service),
        standard_review.routes(service),
        table_drafts.reading_routes(service),
        table_recipes.routes(service, reads),
        tasks.result_routes(service),
        sources.routes(service),
        tasks.operation_routes(service),
        documents.routes(service),
        publication.routes(service),
        evidence.routes(service),
        search.routes(service),
    ):
        app.include_router(router)
