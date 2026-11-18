"""Website recovery and shutdown in the application's lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Event
from typing import Protocol

from fastapi import FastAPI
from starlette.types import StatelessLifespan


class _IngestionLifecycle(Protocol):
    _stop: Event

    def start_scheduler(self) -> None: ...
    def close(self) -> None: ...


def build_lifespan(service: _IngestionLifecycle) -> StatelessLifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            app.state.wiki_summary_service.recover()
            app.state.wiki_hybrid_search.recover()
            app.state.research_chat_service.recover()
            app.state.research_wiki_summary_service.recover()
            app.state.document_report_revision_service.recover()
            app.state.study_wiki_jobs.recover()
            service.start_scheduler()
            yield
        finally:
            service._stop.set()
            app.state.study_wiki_jobs.close()
            app.state.research_wiki_summary_service.close()
            app.state.research_chat_service.close()
            service.close()

    return lifespan
