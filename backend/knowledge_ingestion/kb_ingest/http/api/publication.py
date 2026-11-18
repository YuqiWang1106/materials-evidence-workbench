"""Validate a candidate package and publish the reviewed result."""

from collections.abc import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from kb_ingest.contract import validate_package
from kb_ingest.core.errors import StateConflict
from kb_ingest.http.task_view import public_task
from kb_ingest.ingest.service import IngestionService


class PublishConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def routes(service: IngestionService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks/{task_id}/validation", response_model=None)
    def validation(task_id: str) -> dict[str, object]:
        package = service.records(task_id)
        result = validate_package(package)
        if any(s.get("source_type") in ("paper", "patent") for s in package["sources"]):
            effective: dict[str, object] = {}
            if package["header"].get("document_pipeline_version") == "2":
                artifact: Callable[[str, str], dict[str, object]] = service.document_artifact
                effective = artifact(task_id, "quality")
            result["document_quality"] = effective.get(
                "data", effective or {"status": "legacy_not_rechecked", "scientific_validation": False}
            )
        return result

    @router.post("/api/tasks/{task_id}/publish", response_model=None)
    def publish(task_id: str, body: PublishConfirmation | None = None) -> dict[str, object]:
        try:
            return public_task(
                service.publish(
                    task_id,
                    expected_package_sha256=body.expected_package_sha256 if body else None,
                    expected_review_sha256=body.expected_review_sha256 if body else None,
                )
            )
        except StateConflict:
            raise
        except RuntimeError as exc:
            raise HTTPException(503, "local archive is unavailable or publication needs reconciliation") from exc

    return router
