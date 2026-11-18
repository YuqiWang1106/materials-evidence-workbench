"""Loopback-only API with session and origin checks; no arbitrary path endpoints."""

# Preserve the original public imports of this assembly module.
import hmac as hmac
import secrets as secrets
from contextlib import asynccontextmanager as asynccontextmanager
from pathlib import Path
from typing import Literal as Literal
from urllib.parse import quote as quote
from urllib.parse import urlsplit as urlsplit

from fastapi import Body as Body
from fastapi import Depends, FastAPI
from fastapi import HTTPException as HTTPException
from fastapi import Query as Query
from fastapi import Request as Request
from fastapi import UploadFile as UploadFile
from fastapi.responses import FileResponse as FileResponse
from fastapi.responses import JSONResponse as JSONResponse
from fastapi.responses import Response as Response
from pydantic import BaseModel as BaseModel
from pydantic import ConfigDict as ConfigDict
from pydantic import Field as Field
from pydantic import field_validator as field_validator
from pydantic import model_validator as model_validator

from kb_ingest.ingest.service import IngestionService
from kb_ingest.ingest.service import RetiredTableWorkflow as RetiredTableWorkflow

from .auth.resource_access import resource_access as _resource_access
from .auth.session import AGENT_KNOWLEDGE_PREFIX as _AGENT_KNOWLEDGE_PREFIX
from .auth.session import SessionCredentials as _SessionCredentials
from .contract import ContractError as ContractError
from .contract import validate_package as validate_package
from .core.errors import EmptyUploadError as EmptyUploadError
from .core.errors import StateConflict as StateConflict
from .core.errors import UploadTooLargeError as UploadTooLargeError
from .http import app_setup as _setup
from .http import knowledge_setup as _knowledge_setup
from .http import research_setup as _research_setup
from .http.api.documents import DocumentCandidateBatch as DocumentCandidateBatch
from .http.api.documents import DocumentCandidateDecision as DocumentCandidateDecision
from .http.api.documents import DocumentCandidateReview as DocumentCandidateReview
from .http.api.documents import DocumentDisplay as DocumentDisplay
from .http.api.documents import DocumentReview as DocumentReview
from .http.api.documents import DocumentReviewRevision as DocumentReviewRevision
from .http.api.documents import DocumentReviewScope as DocumentReviewScope
from .http.api.publication import PublishConfirmation as PublishConfirmation
from .http.api.raw_tables import BasicAnalysisConfig as BasicAnalysisConfig
from .http.api.raw_tables import RawTableAnalyze as RawTableAnalyze
from .http.api.raw_tables import RawTableSave as RawTableSave
from .http.api.search import Search as Search
from .http.api.sources import SourceCommand as SourceCommand
from .http.api.standard_review import StandardBatchItem as StandardBatchItem
from .http.api.standard_review import StandardConfirm as StandardConfirm
from .http.api.standard_review import StandardForms as StandardForms
from .http.api.standard_review import StandardMeaningCandidate as StandardMeaningCandidate
from .http.api.standard_review import StandardModelBatch as StandardModelBatch
from .http.api.standard_review import StandardPayloadPreview as StandardPayloadPreview
from .http.api.standard_review import StandardPreview as StandardPreview
from .http.api.standard_review import StandardReviewInput as StandardReviewInput
from .http.api.standard_review import StandardSuggestion as StandardSuggestion
from .http.api.standard_review import StandardSuggestionRetry as StandardSuggestionRetry
from .http.api.table_catalogue import TablePreview as TablePreview
from .http.api.table_chats import TableChatCreate as TableChatCreate
from .http.api.table_chats import TableChatTurn as TableChatTurn
from .http.api.table_drafts import ConfirmReading as ConfirmReading
from .http.api.table_drafts import DraftCreate as DraftCreate
from .http.api.table_drafts import DraftUpdate as DraftUpdate
from .http.api.table_drafts import DraftUpgrade as DraftUpgrade
from .http.api.table_drafts import ReadingPreview as ReadingPreview
from .http.api.table_drafts import RecipeReuse as RecipeReuse
from .http.api.table_recipes import RecipeCreate as RecipeCreate
from .http.api.table_regions import ConfirmRegions as ConfirmRegions
from .http.api.table_regions import ConfirmRegionSheet as ConfirmRegionSheet
from .http.api.table_regions import PrepareRegionSheet as PrepareRegionSheet
from .http.api.table_regions import RegionContext as RegionContext
from .http.api.tasks import ALLOWED_OPTIONS as ALLOWED_OPTIONS
from .http.api.tasks import Prepared as Prepared
from .http.api.tasks import Submit as Submit
from .http.errors import register_error_handlers as _register_error_handlers
from .http.errors import register_validation_handler as _register_validation_handler
from .http.lifespan import build_lifespan as _build_lifespan
from .http.task_view import public_task as public_task
from .ingest.model_binding import check_dispatchers as check_dispatchers
from .ingest.model_binding import owns_context as owns_context
from .ingest.tables.raw_table_snapshot import RawTableRangeConflict as RawTableRangeConflict
from .mcp_contract.protocol import DEFAULT_MAX_BYTES as _AGENT_DEFAULT_MAX_BYTES

_UNSET_DOCUMENT_DISPATCHER = _setup.UNSET_DOCUMENT_DISPATCHER


def _website_app(service: IngestionService, credentials: _SessionCredentials) -> FastAPI:
    app = FastAPI(
        title="材料知识摄取",
        version="0.1.0",
        lifespan=_build_lifespan(service),
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(_resource_access(service))],
    )
    app.middleware("http")(credentials.boundary)
    return app


def create_app(
    service: IngestionService,
    session_token=None,
    ui_dir: Path | None = None,
    *,
    project_model_dispatcher=None,
    project_embedding_dispatcher=None,
    knowledge_service=None,
    wiki_service=None,
    wiki_collection_id=None,
    wiki_summary_service=None,
    research_chat_service=None,
    study_wiki_dispatchers=None,
    project_extraction_budget_context=None,
    model_readiness=None,
    document_model_dispatcher=_UNSET_DOCUMENT_DISPATCHER,
    agent_knowledge_token=None,
    agent_knowledge_max_response_bytes=_AGENT_DEFAULT_MAX_BYTES,
):
    research_options = _research_setup.ResearchOptions(
        research_chat_service,
        project_model_dispatcher,
        model_readiness,
        study_wiki_dispatchers,
        document_model_dispatcher,
    )
    research_options = _setup.bind_dispatchers(service, research_options, project_extraction_budget_context)
    credentials = _SessionCredentials(session_token, agent_knowledge_token)
    app = _website_app(service, credentials)
    knowledge_options = _knowledge_setup.KnowledgeOptions(
        knowledge_service, wiki_service, wiki_collection_id, wiki_summary_service, project_embedding_dispatcher
    )
    knowledge = _knowledge_setup.bind_knowledge(app, service, knowledge_options)
    research = _research_setup.bind_research(app, service, knowledge, research_options)
    study = _setup.StudyOptions(
        project_embedding_dispatcher,
        study_wiki_dispatchers,
        agent_knowledge_token is not None,
        agent_knowledge_max_response_bytes,
    )
    _setup.include_service_routers(app, service, knowledge, research, study)
    _register_validation_handler(app, _AGENT_KNOWLEDGE_PREFIX)
    _register_error_handlers(app)
    _setup.include_ingestion_routers(app, service, credentials, ui_dir)
    if ui_dir:
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")
    return app
