"""Check route resources against the ingestion workspace."""

from collections.abc import Callable

from fastapi import Request

from kb_ingest.ingest.service import IngestionService

# These handlers enforce ownership themselves after checking lifecycle or feature availability.
_HANDLER_CHECKS_SOURCE = {
    "source_management",
    "source_delete",
    "source_reparse",
    "confirm_draft",
    "reading_preview",
    "upgrade_reading",
    "reuse_recipe",
}


def resource_access(service: IngestionService) -> Callable[[Request], None]:
    def check_source_access(request: Request) -> None:
        params = request.path_params
        if task_id := params.get("task_id"):
            service.resources.task(task_id)
        if params.get("source_id") and request.scope["route"].name not in _HANDLER_CHECKS_SOURCE:
            service.resources.require_source(params["source_id"])
        if asset_id := params.get("asset_id"):
            service.resources.require_asset(asset_id)

    return check_source_access
