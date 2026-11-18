"""Explicit summary commands; GET and page opening never dispatch a model."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import StateConflict
from .research_chat_engine import strict_json
from .research_summary_contracts import SummaryRequest


def routes(service):
    router = APIRouter(prefix="/api/research-chats/{chat_id}/wiki-summaries")

    def error(code, status, message=None):
        return JSONResponse({"error": {"code": code, "message": message or code}}, status_code=status)

    async def invoke(fn, *args):
        try:
            return await run_in_threadpool(fn, *args)
        except KeyError:
            return error("SUMMARY_NOT_FOUND", 404)
        except StateConflict:
            return error("SUMMARY_CONFLICT", 409, "内容已变化，请保留提案并重新选择窗口或目标版本。")
        except ValueError as exc:
            if str(exc) == "RESEARCH_MODEL_NOT_READY":
                return error("RESEARCH_MODEL_NOT_READY", 503)
            if str(exc).startswith("TURN_NOT_COMPLETED"):
                return error("SUMMARY_WINDOW_NOT_COMPLETED", 422, "窗口包含未完成轮次，请等待完成或调整窗口。")
            if str(exc) == "WIKI_SELF_REFERENCE":
                return error(
                    "WIKI_SELF_REFERENCE", 409, "更新目标页不能引用自身导读；请选取不含目标页引用的原始依据窗口。"
                )
            if str(exc).startswith("SUMMARY_WINDOW_TOO_LARGE"):
                return error("SUMMARY_WINDOW_TOO_LARGE", 413, "请减小 max_turns 后使用新的 request_id。")
            return error("INVALID_SUMMARY_REQUEST", 422)
        except Exception:
            return error("SUMMARY_OPERATION_FAILED", 500)

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                return error("SUMMARY_REQUEST_TOO_LARGE", 413)
        return bytes(raw)

    @router.get("")
    async def jobs(chat_id: str, request: Request):
        params = request.query_params
        if set(params) - {"offset", "limit"} or any(
            len(params.getlist(k)) != 1 or len(v) > 8 or not v.isascii() or not v.isdigit() for k, v in params.items()
        ):
            return error("INVALID_SUMMARY_REQUEST", 422)
        return await invoke(service.list, chat_id, int(params.get("offset", "0")), int(params.get("limit", "25")))

    @router.get("/{job_id}")
    async def job(chat_id: str, job_id: str):
        return await invoke(service.get, chat_id, job_id)

    @router.post("")
    async def create(chat_id: str, request: Request):
        raw = await body(request)
        if isinstance(raw, JSONResponse):
            return raw
        try:
            value = SummaryRequest.model_validate(strict_json(raw)).model_dump(mode="json")
        except (ValueError, TypeError, RecursionError):
            return error("INVALID_SUMMARY_REQUEST", 422)
        return await invoke(service.enqueue, chat_id, value)

    async def command(request, fn, cid, jid):
        raw = await body(request)
        if isinstance(raw, JSONResponse):
            return raw
        if raw not in (b"", b"{}"):
            return error("INVALID_SUMMARY_REQUEST", 422)
        return await invoke(fn, cid, jid)

    @router.post("/{job_id}/cancel")
    async def cancel(chat_id: str, job_id: str, request: Request):
        return await command(request, service.cancel, chat_id, job_id)

    @router.post("/{job_id}/save-retry")
    async def retry(chat_id: str, job_id: str, request: Request):
        return await command(request, service.save_retry, chat_id, job_id)

    @router.post("/{job_id}/apply")
    async def apply(chat_id: str, job_id: str, request: Request):
        return await command(request, service.apply, chat_id, job_id)

    return router
