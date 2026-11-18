"""Session-protected website research routes, mounted by the existing API app."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .core import StateConflict
from .research_chat_contracts import ChatCreate, ResearchError, TurnCreate
from .research_chat_engine import strict_json


def routes(service):
    router = APIRouter(prefix="/api/research-chats")

    def error(code, status):
        return JSONResponse({"error": ResearchError(code).public()}, status_code=status)

    async def invoke(fn, *args, **kwargs):
        try:
            if service is None:
                raise ResearchError("not_ready")
            return await run_in_threadpool(fn, *args, **kwargs)
        except ResearchError as e:
            return error(
                e.code,
                503
                if e.code
                in (
                    "not_ready",
                    "model_not_configured",
                    "api_key_missing",
                    "model_config_invalid",
                    "unsupported_protocol",
                    "legacy_identity_mismatch",
                )
                else 409
                if e.code in ("stale_context", "request_unknown")
                else 422,
            )
        except StateConflict:
            return error("stale_context", 409)
        except KeyError:
            return error("invalid_request", 404)
        except (ValueError, TypeError, ValidationError):
            return error("invalid_request", 422)
        except Exception:
            return error("execution_failed", 500)

    async def body(request, model):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                return error("context_limit", 413)
        try:
            return model.model_validate(strict_json(raw)).model_dump(mode="json")
        except (ValueError, TypeError, RecursionError):
            return error("invalid_request", 422)

    def page(request, *, support=False):
        allowed = {"evidence_index"} if support else {"offset", "limit"}
        if set(request.query_params) - allowed:
            raise ResearchError("invalid_request")
        values = {}
        for key, value in request.query_params.items():
            if (
                len(request.query_params.getlist(key)) != 1
                or len(value) > 8
                or not value.isascii()
                or not value.isdigit()
            ):
                raise ResearchError("invalid_request")
            values[key] = int(value)
        return values

    def method(name):
        if service is None:
            return lambda *a, **k: None
        return getattr(service, name)

    @router.get("/capabilities")
    async def capabilities(request: Request):
        result = await invoke(method("capabilities"))
        if isinstance(result, dict):
            from .llm.project_model_dispatch import ProjectModelDispatcher

            summary = getattr(request.app.state, "research_wiki_summary_service", None)
            ready = isinstance(getattr(summary, "dispatcher", None), ProjectModelDispatcher) and not getattr(
                summary, "_closed", True
            )
            result.update(
                wiki_model_ready=ready,
                wiki_model_status={
                    "status": "configured_unverified" if ready else "not_ready",
                    "reason": "configured_unverified" if ready else "model_not_configured",
                    "provider_tested": False,
                },
            )
        return result

    @router.get("")
    async def chats(request: Request):
        try:
            params = page(request)
        except ResearchError:
            return error("invalid_request", 422)
        return await invoke(method("list"), **params)

    @router.post("")
    async def create(request: Request):
        value = await body(request, ChatCreate)
        return value if isinstance(value, JSONResponse) else await invoke(method("create"), value)

    @router.get("/{chat_id}")
    async def chat(chat_id: str, request: Request):
        try:
            params = page(request)
        except ResearchError:
            return error("invalid_request", 422)
        return await invoke(method("get"), chat_id, **params)

    @router.post("/{chat_id}/turns")
    async def enqueue(chat_id: str, request: Request):
        value = await body(request, TurnCreate)
        return value if isinstance(value, JSONResponse) else await invoke(method("enqueue"), chat_id, value)

    @router.get("/{chat_id}/turns/{turn_id}")
    async def turn(chat_id: str, turn_id: str):
        return await invoke(method("turn"), chat_id, turn_id)

    @router.post("/{chat_id}/turns/{turn_id}/cancel")
    async def cancel(chat_id: str, turn_id: str, request: Request):
        # Cancellation has no caller-selected model, path or scope fields.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                return error("context_limit", 413)
        if raw not in (b"", b"{}"):
            return error("invalid_request", 422)
        return await invoke(method("cancel"), chat_id, turn_id)

    @router.get("/{chat_id}/turns/{turn_id}/supports/{support_id}")
    async def support(chat_id: str, turn_id: str, support_id: str, request: Request):
        try:
            params = page(request, support=True)
        except ResearchError:
            return error("invalid_request", 422)
        return await invoke(method("support"), chat_id, turn_id, support_id, **params)

    return router
