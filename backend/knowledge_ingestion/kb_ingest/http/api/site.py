"""Website shell, health and capability routes."""

from datetime import datetime
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ...auth.session import SessionCredentials
from ..website_time import public_utc


class _SiteService(Protocol):
    def capabilities(self) -> dict[str, object]: ...
    def health(self) -> dict[str, object]: ...


def _public_times(report: dict[str, object]) -> dict[str, object]:
    public: dict[str, object] = {}
    for key, value in report.items():
        if isinstance(value, datetime):
            public[key] = public_utc(value)
        elif isinstance(value, dict):
            public[key] = _public_times(value)
        else:
            public[key] = value
    return public


def routes(service: _SiteService, credentials: SessionCredentials, ui_dir: Path | None) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_model=None)
    def index(request: Request) -> Response:
        path = ui_dir / "index.html" if ui_dir else None
        response = (
            FileResponse(path)
            if path and path.is_file()
            else JSONResponse({"service": "材料知识摄取", "ui": "pending backend review"})
        )
        credentials.set_cookie(request, response)
        return response

    @router.get("/health", response_model=None)
    def health() -> JSONResponse:
        """Document and model readiness are reported by /api/capabilities."""
        report = service.health()
        code = 503 if report["status"] == "not_ready" else 200
        return JSONResponse({"version": "0.1.0", **_public_times(report)}, status_code=code)

    @router.get("/api/capabilities", response_model=None)
    def capabilities() -> dict[str, object]:
        return service.capabilities()

    return router
