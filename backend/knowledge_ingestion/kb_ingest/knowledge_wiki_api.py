"""Wiki routes inherit the app's loopback/session/origin authorization."""

import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .core import StateConflict
from .http import website_fields as _times
from .knowledge_publication_contract import PublicationPreviewRequest, PublicationRequest

_ACCEPTED = _times.timestamps.documentation(202, description="Accepted")


def _summary_accepted(result: dict | JSONResponse) -> JSONResponse:
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(_times.timestamps.project(result), status_code=202)


def routes(wiki, publisher, summaries=None, relations=None):
    router = APIRouter(prefix="/api/wiki")

    def fail(exc):
        if isinstance(exc, KeyError):
            return JSONResponse({"error": {"code": "REFERENCE_NOT_FOUND", "gaps": []}}, status_code=404)
        code = getattr(exc, "code", None)
        if code is None:
            code = str(exc) if isinstance(exc, StateConflict) else "INVALID_REQUEST"
        status = 404 if isinstance(exc, KeyError) else 409
        if isinstance(exc, (ValidationError, json.JSONDecodeError)) or code in {
            "INVALID_REQUEST",
            "SUMMARY_CONTEXT_LIMIT",
        }:
            status = 422
        if code in {"WORKSPACE_MISMATCH", "SOURCE_USE_POLICY_DENIED"}:
            status = 403
        if code == "ARCHIVE_UNAVAILABLE":
            status = 503
        if isinstance(exc, KeyError):
            code = "REFERENCE_NOT_FOUND"
        return JSONResponse({"error": {"code": code, "gaps": getattr(exc, "gaps", [])}}, status_code=status)

    async def invoke(method, *args, request=None, model=None):
        try:
            if request is not None:
                data = bytearray()
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > 262144:
                        raise ValueError("Request too large")
                payload = json.loads(data)
                if model is not None:
                    payload = model.model_validate(payload).model_dump()
                elif not isinstance(payload, dict):
                    raise ValueError("Object required")
                return await run_in_threadpool(method, *args, payload)
            return await run_in_threadpool(method, *args)
        except (ValueError, StateConflict, KeyError) as exc:
            return fail(exc)

    if summaries is not None:
        summary_errors = {
            "SUMMARY_CONTEXT_LIMIT": "整理输入超过 256 KB（含对话与表格证据），未发送模型，也未删减问答",
            "SUMMARY_WINDOW_NOT_COMPLETED": "最近窗口中有未完成、失败或取消的问答，请完成纠正后重试",
            "SUMMARY_END_TURN_NOT_FOUND": "截止轮不属于此对话，请刷新后重试",
            "SUMMARY_INPUT_CHANGED": "对话或来源已变化，请核对资料后重试",
            "SUMMARY_INTEGRITY_FAILED": "总结记录完整性检查失败，请检查服务",
            "SUMMARY_ALREADY_RUNNING": "此总结正在执行，请刷新任务状态",
            "REQUEST_ID_CONFLICT": "同一请求标识已用于不同内容，请刷新后重试",
            "REFERENCE_NOT_FOUND": "对话或总结记录不存在",
            "INVALID_REQUEST": "总结请求格式无效",
            "WORKSPACE_MISMATCH": "工作区不匹配",
            "SOURCE_USE_POLICY_DENIED": "当前资料不可用于此操作",
        }

        async def summary_invoke(method, *args, request=None):
            result = await invoke(method, *args, request=request)
            if isinstance(result, JSONResponse):
                code = json.loads(result.body)["error"]["code"]
                if code not in summary_errors:
                    code = "SUMMARY_REQUEST_FAILED"
                return JSONResponse(
                    {"error": {"code": code, "message": summary_errors.get(code, "总结请求未完成，请检查服务后重试")}},
                    status_code=result.status_code,
                )
            return result

        @router.get("/summaries/capabilities")
        async def summary_capabilities():
            return summaries.capabilities()

        @router.post("/summaries", openapi_extra=_ACCEPTED)
        async def create_summary(request: Request):
            result = await summary_invoke(summaries.enqueue, request=request)
            return _summary_accepted(result)

        @_times.summaries.route(router.get, "/summaries")
        async def list_summaries(chat_id: str):
            return await summary_invoke(summaries.list, chat_id)

        @_times.timestamps.route(router.get, "/summaries/{job_id}")
        async def get_summary(job_id: str):
            return await summary_invoke(summaries.get, job_id)

        @router.post("/summaries/{job_id}/retry", openapi_extra=_ACCEPTED)
        async def retry_summary(job_id: str, request: Request):
            result = await summary_invoke(summaries.retry, job_id, request=request)
            return _summary_accepted(result)

    @_times.wiki_pages.route(router.get, "/pages")
    async def pages():
        return await invoke(wiki.list_pages)

    if relations is not None:

        @router.get("/pages/{page_id}/relations")
        async def page_relations(page_id: str, depth: int = Query(default=1, ge=0, le=2)):
            return await invoke(relations.graph, page_id, depth)

    @_times.wiki_page.route(router.get, "/pages/{page_id}")
    async def page(page_id: str, revision: int | None = None):
        return await invoke(wiki.get_page, page_id, revision)

    @_times.wiki_page.route(router.post, "/drafts/from-chat")
    async def draft(request: Request):
        return await invoke(wiki.draft_from_chat, request=request)

    @_times.wiki_page.route(router.post, "/pages/{page_id}/revisions")
    async def revision(page_id: str, request: Request):
        return await invoke(wiki.save_revision, page_id, request=request)

    @router.post("/pages/{page_id}/delete")
    async def delete(page_id: str, request: Request):
        return await invoke(wiki.delete, page_id, request=request)

    @_times.wiki_page.route(router.post, "/pages/{page_id}/review")
    async def review(page_id: str, request: Request):
        return await invoke(wiki.review, page_id, request=request)

    @router.post("/pages/{page_id}/publication-preview")
    async def preview(page_id: str, request: Request):
        return await invoke(
            lambda p, b: publisher.preview(p, b["expected_revision"]),
            page_id,
            request=request,
            model=PublicationPreviewRequest,
        )

    @router.post("/pages/{page_id}/publish")
    async def publish(page_id: str, request: Request):
        return await invoke(publisher.publish, page_id, request=request, model=PublicationRequest)

    @router.post("/publications/{publication_id}/reconcile")
    async def reconcile(publication_id: str, request: Request):
        def run(pid, body):
            if body:
                raise ValueError("No reconcile overrides allowed")
            return publisher.reconcile(pid)

        return await invoke(run, publication_id, request=request)

    @_times.wiki_page.route(router.post, "/pages/{page_id}/withdraw")
    async def withdraw(page_id: str, request: Request):
        return await invoke(wiki.withdraw, page_id, request=request)

    return router
