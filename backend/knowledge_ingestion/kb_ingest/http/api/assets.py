"""Upload and download registered ingestion assets."""

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response

from kb_ingest.ingest.service import IngestionService

from ...core.errors import EmptyUploadError, UploadTooLargeError
from .. import website_fields as _times


def routes(service: IngestionService) -> APIRouter:
    router = APIRouter()

    @_times.created.route(router.post, "/api/assets")
    def upload(file: UploadFile) -> dict[str, object]:
        try:
            if file.size is not None:
                service.resources.check_upload_size(file.size)
            return service.resources.upload(file.file, file.filename or "original.bin")
        except EmptyUploadError as exc:
            raise HTTPException(422, str(exc)) from exc
        except UploadTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc

    @router.get("/api/assets/{asset_id}/download", response_model=None)
    def download(asset_id: str) -> Response:
        asset, content = service.resources.download(asset_id)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=utf-8''" + quote(asset["original_name"], safe=""),
                "X-Content-SHA256": asset["sha256"],
            },
        )

    return router
