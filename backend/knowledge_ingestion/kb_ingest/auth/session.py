"""Session credentials and the loopback website's HTTP boundary."""

import hmac
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..mcp_contract import protocol as _agent_contract

AGENT_KNOWLEDGE_PREFIX = _agent_contract.PREFIX


def _session_cookie_name(request: Request) -> str:
    # Browsers share a host's cookies across ports; each local service needs its own name.
    port = request.url.port
    if port is None:
        port = 443 if request.url.scheme == "https" else 80
    return f"kb_session_{port}"


def _check_origin(request: Request) -> Response | None:
    try:
        authority = urlsplit("//" + request.headers.get("host", ""))
        hostname = authority.hostname
        _ = authority.port  # Parsing the port rejects malformed authorities.
    except ValueError:
        return JSONResponse({"detail": "Invalid loopback host"}, status_code=400)
    if hostname not in ("localhost", "127.0.0.1", "::1"):
        return JSONResponse({"detail": "Loopback host required"}, status_code=400)
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        return JSONResponse({"detail": "Cross-origin access is disabled"}, status_code=403)
    # A clicked citation may open the public shell; cross-site API and embedded access stay blocked.
    public_navigation = (
        request.method == "GET"
        and request.url.path == "/"
        and request.headers.get("sec-fetch-mode") == "navigate"
        and request.headers.get("sec-fetch-dest") == "document"
        and request.headers.get("sec-fetch-user") == "?1"
    )
    if request.headers.get("sec-fetch-site") == "cross-site" and not public_navigation:
        return JSONResponse({"detail": "Cross-site access is disabled"}, status_code=403)
    return None


class SessionCredentials:
    def __init__(self, session_token: str | None, agent_knowledge_token: str | None) -> None:
        self._session_token = session_token or secrets.token_urlsafe(32)
        if agent_knowledge_token is not None and (
            not agent_knowledge_token.strip() or agent_knowledge_token == self._session_token
        ):
            message = "Independent read-only knowledge credential required"
            raise ValueError(message)
        self._agent_knowledge_token = agent_knowledge_token

    def set_cookie(self, request: Request, response: Response) -> None:
        response.set_cookie(_session_cookie_name(request), self._session_token, httponly=True, samesite="strict")

    def _check_credentials(self, request: Request, agent_knowledge: bool) -> Response | None:
        token = request.cookies.get(_session_cookie_name(request), request.cookies.get("kb_session", ""))
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        readonly_allowed = (
            agent_knowledge
            and self._agent_knowledge_token is not None
            and auth.startswith("Bearer ")
            and hmac.compare_digest(auth[7:], self._agent_knowledge_token)
        )
        if hmac.compare_digest(token, self._session_token) or readonly_allowed:
            return None
        if agent_knowledge:
            return JSONResponse(
                _agent_contract.error_content(
                    "UNAUTHORIZED", "A read-only knowledge credential or website session is required."
                ),
                status_code=_agent_contract.STATUS["UNAUTHORIZED"],
            )
        return JSONResponse({"detail": "Open the local page to start a session"}, status_code=401)

    async def boundary(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        agent_knowledge = request.url.path == AGENT_KNOWLEDGE_PREFIX or request.url.path.startswith(
            AGENT_KNOWLEDGE_PREFIX + "/"
        )
        rejection = _check_origin(request)
        if rejection is not None:
            return rejection
        if request.url.path.startswith("/api/"):
            rejection = self._check_credentials(request, agent_knowledge)
            if rejection is not None:
                return rejection
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Cache-Control"] = "no-store"
        result.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; object-src 'none'"
        )
        return result
