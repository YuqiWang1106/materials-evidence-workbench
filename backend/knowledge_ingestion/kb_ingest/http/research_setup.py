"""Bind website research and report services to the ingestion coordinator."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict, Unpack

from fastapi import FastAPI

if TYPE_CHECKING:
    from kb_ingest.ingest.service import IngestionService

    from ..document_report_revision_service import DocumentReportRevisionService
    from ..knowledge_contracts import KnowledgeResult
    from ..knowledge_wiki import WikiService
    from ..research_chat_service import ResearchChatService
    from ..research_chat_sources import ResearchSources
    from ..research_summary_service import ResearchWikiSummaryService
    from ..store import Store
    from .knowledge_setup import KnowledgeServices, WebsiteKnowledge


@dataclass(frozen=True, slots=True)
class ResearchOptions:
    chat: ResearchChatService | None
    model_dispatcher: object
    model_readiness: Mapping[str, object] | None
    wiki_dispatchers: Mapping[str, object] | None
    document_dispatcher: object


@dataclass(frozen=True, slots=True)
class ResearchServices:
    chat: ResearchChatService
    summary: ResearchWikiSummaryService
    report: DocumentReportRevisionService


class _ChatOptions(TypedDict):
    dispatcher: object
    model_readiness: Mapping[str, object] | None
    freeze_selection: Callable[[list[str]], dict[str, object]] | None
    passage_reader: Callable[[dict[str, object], dict[str, object]], KnowledgeResult] | None


class _ChatFactory(Protocol):
    def __call__(
        self, store: Store, knowledge: WebsiteKnowledge, /, **options: Unpack[_ChatOptions]
    ) -> ResearchChatService: ...


class _SummaryFactory(Protocol):
    def __call__(
        self, research: ResearchChatService, wiki: WikiService, /, *, dispatcher: object = ...
    ) -> ResearchWikiSummaryService: ...


class _ReportFactory(Protocol):
    def __call__(self, ingestion: IngestionService, /, *, dispatcher: object) -> DocumentReportRevisionService: ...


def _bind_chat(store: Store, knowledge: WebsiteKnowledge, options: ResearchOptions) -> ResearchChatService:
    chat = options.chat
    if chat is None:
        from ..research_chat_service import ResearchChatService
        from ..research_chat_sources import ResearchSources

        build_sources: Callable[[WebsiteKnowledge], ResearchSources] = ResearchSources
        build_chat: _ChatFactory = ResearchChatService
        sources = build_sources(knowledge) if knowledge.documents is not None else None
        chat = build_chat(
            store,
            knowledge,
            dispatcher=options.model_dispatcher,
            model_readiness=options.model_readiness,
            freeze_selection=sources.freeze if sources else None,
            passage_reader=sources.passages if sources else None,
        )
    if chat.files.store is not store:
        message = "Research storage must belong to this website"
        raise ValueError(message)
    return chat


def _bind_report(service: IngestionService, dispatcher: object) -> DocumentReportRevisionService:
    report = service.document_report_revision_service
    if report is None:
        from ..document_report_revision_service import DocumentReportRevisionService

        build_report: _ReportFactory = DocumentReportRevisionService
        report = build_report(service, dispatcher=dispatcher)
        service.document_report_revision_service = report
    elif report.dispatcher is not dispatcher:
        message = "Report revisions must use this website shared dispatcher"
        raise ValueError(message)
    return report


def bind_research(
    app: FastAPI, service: IngestionService, knowledge: KnowledgeServices, options: ResearchOptions
) -> ResearchServices:
    chat = _bind_chat(service.store, knowledge.knowledge, options)
    app.state.research_chat_service = chat

    from ..research_summary_service import ResearchWikiSummaryService

    build_summary: _SummaryFactory = ResearchWikiSummaryService
    if options.wiki_dispatchers is None:
        summary = build_summary(chat, knowledge.wiki)
    else:
        summary = build_summary(chat, knowledge.wiki, dispatcher=next(iter(options.wiki_dispatchers.values()), None))
    service.research_wiki_summary_services = [*service.research_wiki_summary_services, summary]
    app.state.research_wiki_summary_service = summary

    report = _bind_report(service, options.document_dispatcher)
    app.state.document_report_revision_service = report
    return ResearchServices(chat, summary, report)
