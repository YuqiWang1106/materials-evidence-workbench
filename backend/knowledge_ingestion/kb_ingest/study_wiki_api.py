"""Explicit study Wiki requests and proposal CAS; no model selection or result injection."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .http import receipt_times as _receipt
from .http import website_fields as _times


class RequestJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    expected_snapshot_id: str = Field(min_length=1, max_length=128)


class AdoptJob(RequestJob):
    expected_page_revision: int = Field(ge=1)


def routes(jobs):
    router = APIRouter(prefix="/api")

    def binding():
        return jobs.store, jobs.dispatchers.values()

    job_times = _receipt.with_receipts(_times.study_job, binding, _receipt.study_receipts)
    proposal_times = _receipt.with_receipts(_times.study_proposal, binding, _receipt.proposal_receipts)

    @job_times.route(router.post, "/study-units/{unit_id}/wiki-jobs")
    async def request(unit_id: str, payload: RequestJob):
        return await run_in_threadpool(jobs.request, unit_id, **payload.model_dump())

    @proposal_times.route(router.get, "/study-units/{unit_id}/wiki-update")
    async def proposal(unit_id: str):
        return {"proposal": await run_in_threadpool(jobs.proposal, unit_id)}

    @job_times.route(router.get, "/study-wiki-jobs/{job_id}")
    async def get(job_id: str):
        return await run_in_threadpool(jobs.get, job_id)

    @job_times.route(router.post, "/study-wiki-jobs/{job_id}/retry-publication")
    async def retry(job_id: str):
        return await run_in_threadpool(jobs.retry_publication, job_id)

    @job_times.route(router.post, "/study-wiki-jobs/{job_id}/adopt")
    async def adopt(job_id: str, payload: AdoptJob):
        return await run_in_threadpool(jobs.adopt, job_id, **payload.model_dump())

    return router
