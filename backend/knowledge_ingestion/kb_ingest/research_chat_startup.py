"""Website-owned model services; startup validates locally and never sends HTTP."""

from dataclasses import dataclass, field

from .core.config import credential_status, load_model_settings
from .llm.model_requests import ModelRequestContext, ModelRequestPolicy, ModelRequestService
from .llm.project_model_dispatch import (
    ProjectModelDispatcher,
    make_document_extraction_context,
    make_study_wiki_dispatchers,
)
from .materials_host import MaterialsConfigurationError, MaterialsHostConfig
from .project_embedding import ProjectEmbeddingDispatcher
from .storage import revision_check as _revisions
from .storage.engine import file_engine


@dataclass(repr=False)
class ResearchModelBinding:
    config: MaterialsHostConfig
    engine: object
    dispatcher: ProjectModelDispatcher | None
    embedding_dispatcher: object | None
    study_wiki_dispatchers: dict = field(default_factory=dict)
    extraction_context: object | None = None
    document_dispatcher: object | None = None
    owns_engine: bool = False
    closed: bool = False

    def app_kwargs(self):
        if self.closed:
            raise RuntimeError("research_model_closed")
        return {
            "session_token": self.config.session_token,
            "project_model_dispatcher": self.dispatcher,
            "project_embedding_dispatcher": self.embedding_dispatcher,
            "study_wiki_dispatchers": self.study_wiki_dispatchers,
            "project_extraction_budget_context": self.extraction_context,
            "document_model_dispatcher": self.document_dispatcher,
        }

    def close(self):
        if not self.closed:
            if self.owns_engine:
                self.engine.dispose()
            self.closed = True


def _import_legacy_requests(requests, config, engine):
    # Legacy fee files are retained. Only execution states are copied into the
    # website database; the legacy connection cannot write or create a file.
    from pathlib import Path

    with _revisions.external_database(config.budget_root, Path(engine.url.database).parent) as copied:
        if copied is not None:
            old_engine = file_engine(copied)
            try:
                requests.import_legacy(old_engine, project_id=config.project_id)
            finally:
                old_engine.dispose()


def _request_engine(config):
    from .store import Store

    runtime = config.snapshot_root / "request-runtime"
    with _revisions.external_database(config.budget_root, runtime):
        pass
    return Store(runtime, workspace_id=config.workspace_id).engine


def bind_research_model(config, *, engine=None, model_settings=None, environ=None, transport=None):
    if not isinstance(config, MaterialsHostConfig):
        raise MaterialsConfigurationError("Invalid trusted research model configuration.")
    settings = (
        model_settings
        if model_settings is not None
        else load_model_settings(config.model_config, environ={} if environ is None else environ)
    )
    owns_engine = engine is None
    if owns_engine:
        engine = _request_engine(config)
    try:
        requests = ModelRequestService(engine, workspace_id=config.workspace_id)
        _import_legacy_requests(requests, config, engine)

        def dispatcher(credentials):
            if not credential_status(credentials)["ready"]:
                return None
            policy = ModelRequestPolicy(
                provider="compatible",
                model=credentials.model or "gpt-6-luna",
                base_url=credentials.base_url,
                protocol=credentials.protocol,
            )
            return ProjectModelDispatcher(
                ModelRequestContext(requests, config.project_id, policy),
                api_key=credentials.api_key,
                timeout_seconds=60,
                max_body_bytes=1048576,
                max_tools=16,
                transport=transport,
                reasoning_effort=credentials.reasoning_effort,
            )

        research = dispatcher(settings.research)
        wiki_base = dispatcher(settings.wiki)
        document_base = dispatcher(settings.documents)
        wiki = make_study_wiki_dispatchers(wiki_base, credentials=settings.wiki) if wiki_base else {}
        extraction = (
            make_document_extraction_context(document_base, credentials=settings.documents) if document_base else None
        )
        embedding = None
        if credential_status(settings.embedding)["ready"]:
            values = settings.embedding
            embedding = ProjectEmbeddingDispatcher(
                requests,
                config.project_id,
                api_key=values.api_key,
                base_url=values.base_url,
                model=values.model,
                dimensions=values.dimensions,
                transport=transport,
            )
        return ResearchModelBinding(config, engine, research, embedding, wiki, extraction, document_base, owns_engine)
    except BaseException:
        if owns_engine:
            engine.dispose()
        raise
