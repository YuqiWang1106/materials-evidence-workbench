"""Exception responses for website and read-only knowledge requests."""

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kb_ingest.contract import ContractError
from kb_ingest.core.errors import StateConflict
from kb_ingest.ingest.service import RetiredTableWorkflow
from kb_ingest.ingest.tables.raw_table_snapshot import RawTableRangeConflict
from kb_ingest.mcp_contract import protocol as _agent_contract


def register_validation_handler(app: FastAPI, agent_knowledge_prefix: str) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith(agent_knowledge_prefix + "/"):
            return JSONResponse(
                _agent_contract.error_content("INVALID_ARGUMENT", "Check required fields and parameter types."),
                status_code=_agent_contract.STATUS["INVALID_ARGUMENT"],
            )
        return await request_validation_exception_handler(request, exc)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": "Requested record or asset does not exist"}, status_code=404)

    @app.exception_handler(StateConflict)
    async def conflict(request: Request, exc: StateConflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RetiredTableWorkflow)
    async def retired_table_workflow(request: Request, exc: RetiredTableWorkflow) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=410)

    @app.exception_handler(ContractError)
    async def invalid(request: Request, exc: ContractError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RawTableRangeConflict)
    async def raw_table_range_conflict(request: Request, exc: RawTableRangeConflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "range_conflict": exc.detail}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)
