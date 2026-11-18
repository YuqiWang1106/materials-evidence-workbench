"""Session/origin-protected by create_app; explicit new-revision commands only."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .core import StateConflict
from .document_report_revision_service import ReportRevisionError
from .http import receipt_times as _receipt
from .http import website_fields as _times
from .research_chat_engine import strict_json


class ReportUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str = Field(min_length=1, max_length=300)
    study_ref: str = Field(min_length=1, max_length=300)


class ReportRequest(ReportUnit):
    request_id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,200}$")
    expected_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retry_of: str | None = Field(default=None, max_length=100)


def _report_times(service, *, history=False):
    def binding():
        return service.ingestion.store, (service.dispatcher,)

    fields, shape = (
        (_times.report_history, _receipt.history_receipts)
        if history
        else (_times.report_revision, _receipt.report_receipts)
    )
    return _receipt.with_receipts(fields, binding, shape, ("created_at", "updated_at", "sent_at"))


def routes(service):
    router = APIRouter(prefix="/api/tasks/{task_id}/document-reports")

    def error(code, status):
        messages = {
            "explicit_revision_required": "旧自动报告入口已停用，请显式生成新修订。",
            "model_unavailable": "共享项目报告模型尚未配置。",
            "conflict": "报告输入、归属或状态已变化，请刷新后重试。",
            "invalid_request": "报告请求格式无效。",
            "not_found": "报告修订不存在。",
            "storage_failure": "报告持久化未完成；未自动重发模型。",
        }
        return JSONResponse({"error": {"code": code, "message": messages[code]}}, status_code=status)

    async def invoke(fn, *args, **kwargs):
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except ReportRevisionError:
            return error("model_unavailable", 503)
        except StateConflict:
            return error("conflict", 409)
        except KeyError:
            return error("not_found", 404)
        except (ValueError, TypeError):
            return error("invalid_request", 422)
        except Exception:
            return error("storage_failure", 500)

    async def body(request, model):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                return error("invalid_request", 413)
        try:
            return model.model_validate(strict_json(raw)).model_dump()
        except (ValueError, TypeError, RecursionError):
            return error("invalid_request", 422)

    def unit(task_id, candidate_id, study_ref):
        return {"task_id": task_id, "candidate_id": candidate_id, "study_ref": study_ref}

    @router.get("/capabilities")
    async def capabilities(task_id: str):
        return service.capabilities()

    @_report_times(service).route(router.get, "")
    async def current(
        task_id: str,
        candidate_id: str = Query(min_length=1, max_length=300),
        study_ref: str = Query(min_length=1, max_length=300),
    ):
        return await invoke(service.current, unit(task_id, candidate_id, study_ref))

    @router.post("")
    async def legacy_create(task_id: str):
        return error("explicit_revision_required", 410)

    @router.post("/{job_id}/retry")
    async def legacy_retry(task_id: str, job_id: str):
        return error("explicit_revision_required", 410)

    @_report_times(service, history=True).route(router.get, "/revisions")
    async def history(
        task_id: str,
        candidate_id: str = Query(min_length=1, max_length=300),
        study_ref: str = Query(min_length=1, max_length=300),
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=50),
    ):
        return await invoke(service.history, unit(task_id, candidate_id, study_ref), offset=offset, limit=limit)

    @_report_times(service).route(router.post, "/revisions")
    async def create(task_id: str, request: Request):
        value = await body(request, ReportRequest)
        if isinstance(value, JSONResponse):
            return value
        target = unit(task_id, value.pop("candidate_id"), value.pop("study_ref"))
        return await invoke(service.request, target, **value)

    @_report_times(service).route(router.get, "/revisions/{revision_id}")
    async def revision(
        task_id: str,
        revision_id: str,
        candidate_id: str = Query(min_length=1, max_length=300),
        study_ref: str = Query(min_length=1, max_length=300),
    ):
        return await invoke(service.get, unit(task_id, candidate_id, study_ref), revision_id)

    @_report_times(service).route(router.post, "/revisions/{revision_id}/cancel")
    async def cancel(task_id: str, revision_id: str, request: Request):
        value = await body(request, ReportUnit)
        return (
            value
            if isinstance(value, JSONResponse)
            else await invoke(service.cancel, unit(task_id, **value), revision_id)
        )

    @_report_times(service).route(router.post, "/revisions/{revision_id}/persist")
    async def persist(task_id: str, revision_id: str, request: Request):
        value = await body(request, ReportUnit)
        return (
            value
            if isinstance(value, JSONResponse)
            else await invoke(service.persist, unit(task_id, **value), revision_id)
        )

    return router
