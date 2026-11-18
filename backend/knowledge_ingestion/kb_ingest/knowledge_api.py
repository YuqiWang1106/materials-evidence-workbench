"""Fixed read tools inheriting the local API session/origin boundary."""

import asyncio
import json
from threading import Event

from fastapi import APIRouter
from fastapi import Request as HTTPRequest
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .knowledge_contracts import (
    VERSION,
    Capabilities,
    CompareRequest,
    ContextValidateRequest,
    ContextValidateResult,
    CorpusRequest,
    CorpusResult,
    EvidencePreviewResult,
    GetRequest,
    Id,
    IndexBuildRequest,
    IndexBuildResult,
    IndexStatusRequest,
    IndexStatusResult,
    KnowledgeError,
    KnowledgeResult,
    PassageBrowseRequest,
    QueryRequest,
    RecordCatalogueRequest,
    RecordCatalogueResult,
    SearchRequest,
    StrictModel,
    safe_error,
)
from .knowledge_service import READ_CANCELED
from .knowledge_snapshot import KnowledgeIntegrityError

PREFIX = "/api/knowledge/v1"
TOOL_MODELS = {
    "materials_query": QueryRequest,
    "materials_search": SearchRequest,
    "materials_get": GetRequest,
    "materials_compare": CompareRequest,
    "materials_context_validate": ContextValidateRequest,
    "materials_freeze_corpus": CorpusRequest,
    "materials_passages": PassageBrowseRequest,
    "materials_catalogue": RecordCatalogueRequest,
}


def tool_schemas():
    return {
        "index_management": {
            "rebuild": {
                "method": "POST",
                "path": PREFIX + "/index/rebuild",
                "input_schema": IndexBuildRequest.model_json_schema(),
                "output_schema": IndexBuildResult.model_json_schema(),
            },
            "status": {
                "method": "POST",
                "path": PREFIX + "/index/status",
                "input_schema": IndexStatusRequest.model_json_schema(),
                "output_schema": IndexStatusResult.model_json_schema(),
            },
        },
        "resource_preview": {
            "method": "GET",
            "path": "/api/knowledge/v1/resources/{handle}/preview",
            "query_schema": {
                "type": "object",
                "properties": {"page": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            "output_schema": EvidencePreviewResult.model_json_schema(),
            "error_schema": KnowledgeError.model_json_schema(),
        },
        "schema_version": VERSION,
        "tools": {
            **{
                name: {
                    "input_schema": model.model_json_schema(),
                    "output_schema": (
                        ContextValidateResult
                        if model is ContextValidateRequest
                        else CorpusResult
                        if model is CorpusRequest
                        else RecordCatalogueResult
                        if model is RecordCatalogueRequest
                        else KnowledgeResult
                    ).model_json_schema(),
                    "error_schema": KnowledgeError.model_json_schema(),
                }
                for (name, model) in TOOL_MODELS.items()
            },
            "materials_capabilities": {
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "output_schema": Capabilities.model_json_schema(),
            },
        },
    }


STATUS = {
    "INVALID_QUERY": 422,
    "CURSOR_MISMATCH": 409,
    "SNAPSHOT_MISMATCH": 409,
    "SNAPSHOT_REVIEW_CHANGED": 409,
    "WORKSPACE_MISMATCH": 403,
    "SOURCE_USE_POLICY_DENIED": 403,
    "REFERENCE_NOT_FOUND": 404,
    "RESULT_SET_EXPIRED": 410,
    "ARCHIVE_UNAVAILABLE": 503,
    "INDEX_NOT_READY": 503,
    "INDEX_STALE": 409,
    "INDEX_TIMEOUT": 504,
    "FTS5_UNAVAILABLE": 503,
    "INDEX_UNAVAILABLE": 503,
    "INDEX_BUILD_FAILED": 503,
    "INDEX_REQUEST_CONFLICT": 409,
    "WIKI_NOT_READY": 503,
    "QUERY_TIMEOUT": 504,
    "INTEGRITY_FAILURE": 409,
    "SOURCE_INDEX_MISMATCH": 409,
    "PREVIEW_UNSUPPORTED": 422,
}


def routes(service):
    router = APIRouter(prefix=PREFIX)

    def error(code, request_id="invalid-request"):
        return JSONResponse(
            safe_error(
                code,
                request_id,
                remedy="retry_later" if code in {"ARCHIVE_UNAVAILABLE", "QUERY_TIMEOUT"} else "correct_request",
            ).model_dump(mode="json"),
            status_code=STATUS.get(code, 422),
        )

    @router.get("/capabilities")
    def capabilities():
        if service is None:
            return error("REFERENCE_NOT_FOUND")
        try:
            return service.capabilities().model_dump(mode="json")
        except KnowledgeIntegrityError as exc:
            return error(exc.code)

    @router.get("/schemas")
    def schemas():
        return tool_schemas()

    @router.get("/document-sources")
    def document_sources():
        try:
            if service is None:
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            return service.document_sources()
        except KnowledgeIntegrityError as exc:
            return error(exc.code)
        except Exception:
            return error("INTEGRITY_FAILURE")

    async def dispatch(request, model, method):
        request_id = "invalid-request"
        try:
            length = request.headers.get("content-length")
            if length and int(length) > 262144:
                return error("INVALID_QUERY")
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 262144:
                    return error("INVALID_QUERY")
            data = json.loads(body)
            if isinstance(data, dict):
                candidate = data.get("request_id")
                try:
                    request_id = RequestId(request_id=candidate).request_id
                except ValidationError:
                    pass
            parsed = model.model_validate(data)
            if service is None:
                return error("REFERENCE_NOT_FOUND", request_id)
            if await request.is_disconnected():
                return error("REQUEST_CANCELED", request_id)
            canceled = Event()
            token = READ_CANCELED.set(canceled)

            async def watch_disconnect():
                while not canceled.is_set():
                    await asyncio.sleep(0.02)
                    if await request.is_disconnected():
                        canceled.set()

            watcher = asyncio.create_task(watch_disconnect())
            try:
                result = await run_in_threadpool(getattr(service, method), parsed)
            except asyncio.CancelledError:
                canceled.set()
                raise
            finally:
                watcher.cancel()
                READ_CANCELED.reset(token)
            if await request.is_disconnected():
                return error("REQUEST_CANCELED", request_id)
            return result.model_dump(mode="json")
        except KnowledgeIntegrityError as exc:
            return error(exc.code, request_id)
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            return error("INVALID_QUERY", request_id)
        except Exception:
            return error("INTEGRITY_FAILURE", request_id)

    class RequestId(StrictModel):
        request_id: Id

    @router.post("/index/rebuild")
    async def index_rebuild(request: HTTPRequest):
        return await dispatch(request, IndexBuildRequest, "index_rebuild")

    @router.post("/index/status")
    async def index_status(request: HTTPRequest):
        return await dispatch(request, IndexStatusRequest, "index_status")

    @router.post("/query")
    async def query(request: HTTPRequest):
        return await dispatch(request, QueryRequest, "query")

    @router.post("/corpora")
    async def corpora(request: HTTPRequest):
        return await dispatch(request, CorpusRequest, "freeze_corpus")

    @router.post("/passages")
    async def passages(request: HTTPRequest):
        return await dispatch(request, PassageBrowseRequest, "passages")

    @router.post("/catalogue")
    async def catalogue(request: HTTPRequest):
        return await dispatch(request, RecordCatalogueRequest, "catalogue")

    @router.post("/get")
    async def get(request: HTTPRequest):
        return await dispatch(request, GetRequest, "get")

    @router.post("/compare")
    async def compare(request: HTTPRequest):
        return await dispatch(request, CompareRequest, "compare")

    @router.post("/search")
    async def search(request: HTTPRequest):
        return await dispatch(request, SearchRequest, "search")

    @router.post("/context/validate")
    async def validate(request: HTTPRequest):
        return await dispatch(request, ContextValidateRequest, "validate_context")

    @router.get("/resources/{handle}/preview")
    async def preview(handle: str, request: HTTPRequest):
        if service is None:
            return error("REFERENCE_NOT_FOUND")
        try:
            if set(request.query_params) - {"page"}:
                return error("INVALID_QUERY")
            raw = request.query_params.getlist("page")
            if len(raw) > 1 or raw and (not raw[0].isascii() or not raw[0].isdigit() or len(raw[0]) > 8):
                return error("INVALID_QUERY")
            page = int(raw[0]) if raw else None
            if page is not None and page < 1:
                return error("INVALID_QUERY")
            if await request.is_disconnected():
                return error("REQUEST_CANCELED")
            canceled = Event()
            token = READ_CANCELED.set(canceled)

            async def watch_disconnect():
                while not canceled.is_set():
                    await asyncio.sleep(0.02)
                    if await request.is_disconnected():
                        canceled.set()

            watcher = asyncio.create_task(watch_disconnect())
            try:
                result = await run_in_threadpool(service.preview, handle, page)
            except asyncio.CancelledError:
                canceled.set()
                raise
            finally:
                watcher.cancel()
                READ_CANCELED.reset(token)
            if await request.is_disconnected():
                return error("REQUEST_CANCELED")
            return result.model_dump(mode="json")
        except KnowledgeIntegrityError as exc:
            return error(exc.code)
        except Exception:
            return error("INTEGRITY_FAILURE")

    @router.get("/resources/{handle}")
    def resource(handle: str):
        if service is None:
            return error("REFERENCE_NOT_FOUND")
        try:
            return Response(service.resource(handle), media_type="application/octet-stream")
        except KnowledgeIntegrityError as exc:
            return error(exc.code)
        except Exception:
            return error("INTEGRITY_FAILURE")

    return router
