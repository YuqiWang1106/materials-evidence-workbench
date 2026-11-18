"""Serve complete, byte-limited responses from the read-only knowledge service."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...mcp_contract.protocol import DEFAULT_MAX_BYTES, PREFIX, STATUS, AgentKnowledgeError, error_content
from ...mcp_contract.requests import EntityRequest, EvidenceRequest, ExpandRequest, SearchRequest

if TYPE_CHECKING:
    from ...agent_knowledge_service import AgentKnowledgeService


def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(error_content(code, message), status_code=status_code)


def _serialize(method: Callable[..., object], arguments: dict[str, object]) -> JSONResponse:
    try:
        return JSONResponse(method(**arguments))
    except AgentKnowledgeError:
        raise
    except Exception as exc:
        # Transport errors must never expose storage paths, credentials or provider details.
        raise AgentKnowledgeError("KB_UNAVAILABLE", "Knowledge could not be read. Check service readiness.") from exc


def routes(service: "AgentKnowledgeService", *, max_response_bytes: int = DEFAULT_MAX_BYTES) -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    def invoke(method: Callable[..., object], body: BaseModel | None = None) -> JSONResponse:
        try:
            response = _serialize(method, body.model_dump() if body is not None else {})
        except AgentKnowledgeError as exc:
            return error_response(exc.code, exc.message, exc.status_code)
        if len(response.body) > max_response_bytes:
            return error_response(
                "RESULT_TOO_LARGE",
                "Narrow the query or use offset/limit to read fewer complete items.",
                STATUS["RESULT_TOO_LARGE"],
            )
        return response

    @router.get("/capabilities")
    def capabilities() -> JSONResponse:
        return invoke(service.capabilities)

    @router.post("/search")
    def search(body: SearchRequest) -> JSONResponse:
        return invoke(service.search, body)

    @router.post("/expand")
    def expand(body: ExpandRequest) -> JSONResponse:
        return invoke(service.expand, body)

    @router.post("/entities/read")
    def read_entity(body: EntityRequest) -> JSONResponse:
        return invoke(service.read_entity, body)

    @router.post("/evidence/read")
    def read_evidence(body: EvidenceRequest) -> JSONResponse:
        return invoke(service.read_evidence, body)

    return router
