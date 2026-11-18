"""Read-only study inputs inherit existing website session authorization."""

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from .core.workspace import OMITTED_WORKSPACE
from .http import receipt_times as _receipt
from .http import website_fields as _times
from .study_units import StudyUnitService


def routes(service, *, workspace_id=OMITTED_WORKSPACE):
    router = APIRouter(prefix="/api")

    def binding():
        return service.store, service.study_wiki_jobs.dispatchers.values()

    unit_times = _receipt.with_receipts(_times.study_unit, binding, _receipt.unit_receipts)
    list_times = _receipt.with_receipts(_times.study_units, binding, _receipt.units_receipts)
    units = (
        StudyUnitService(service)
        if workspace_id is OMITTED_WORKSPACE
        else StudyUnitService(service, workspace_id=workspace_id)
    )

    @list_times.route(router.get, "/sources/{source_id}/study-units")
    async def source_units(source_id: str):
        rows = await run_in_threadpool(units.list_source, source_id)
        jobs = getattr(service, "study_wiki_jobs", None)
        for row in rows:
            row["wiki_generation"] = (
                jobs.capabilities() if jobs else {"ready": False, "unavailable_reason": "study_wiki_not_ready"}
            )
            row["wiki_jobs"] = await run_in_threadpool(jobs.for_unit, row["unit_id"]) if jobs else []
        return {"units": rows}

    @unit_times.route(router.get, "/study-units/{unit_id}")
    async def unit(unit_id: str):
        row = await run_in_threadpool(units.get, unit_id)
        jobs = getattr(service, "study_wiki_jobs", None)
        row["wiki_generation"] = (
            jobs.capabilities() if jobs else {"ready": False, "unavailable_reason": "study_wiki_not_ready"}
        )
        row["wiki_jobs"] = await run_in_threadpool(jobs.for_unit, unit_id) if jobs else []
        return row

    return router
