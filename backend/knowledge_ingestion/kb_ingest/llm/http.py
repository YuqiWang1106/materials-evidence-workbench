"""The only place where a connection to a model or embedding provider is opened.

Every request uses the transport injected by the caller, ignores environment proxies, never
follows a redirect and is never retried here. Callers keep their own timeout and retry policy.
"""

from collections.abc import Callable

import httpx

from .identity import Json


class ResponseTooLargeError(Exception):
    """The provider sent more bytes than the caller accepts. Nothing beyond the bound is read."""


def _headers(api_key: str, idempotency_key: str | None) -> dict[str, str]:
    headers = {"Authorization": "Bearer " + api_key}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def post(
    url: str,
    body: Json,
    *,
    api_key: str,
    seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
    idempotency_key: str | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=seconds, transport=transport, follow_redirects=False, trust_env=False
    ) as client:
        return await client.post(url, json=body, headers=_headers(api_key, idempotency_key))


async def post_bounded(
    url: str,
    body: Json,
    *,
    api_key: str,
    seconds: float,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
    before_chunk: Callable[[], None] | None = None,
) -> tuple[int, httpx.Headers, bytes]:
    """Read the response as it arrives. `before_chunk` lets the caller enforce its deadline."""
    async with (
        httpx.AsyncClient(timeout=seconds, transport=transport, follow_redirects=False, trust_env=False) as client,
        client.stream("POST", url, json=body, headers=_headers(api_key, None)) as response,
    ):
        received = bytearray()
        async for chunk in response.aiter_bytes():
            if before_chunk is not None:
                before_chunk()
            received.extend(chunk)
            if len(received) > max_bytes:
                raise ResponseTooLargeError
        return response.status_code, response.headers, bytes(received)


def post_sync(
    url: str,
    body: Json,
    *,
    api_key: str,
    seconds: float,
    transport: httpx.BaseTransport | None = None,
    idempotency_key: str | None = None,
) -> httpx.Response:
    with httpx.Client(timeout=seconds, transport=transport, follow_redirects=False, trust_env=False) as client:
        return client.post(url, json=body, headers=_headers(api_key, idempotency_key))
