"""Configuration validation shared by model settings and provider clients."""

from urllib.parse import urlsplit

from .errors import ProviderProtocolError

PROTOCOLS = frozenset({"responses", "chat_completions"})


def normalize_base_url(value: object) -> str:
    if type(value) is not str or not value or any(ord(c) <= 32 for c in value):
        raise ProviderProtocolError("invalid_model_endpoint")
    try:
        parsed = urlsplit(value)
        # Accessing the port validates malformed and out-of-range values before any request.
        _ = parsed.port
        hostname = parsed.hostname
    except (ValueError, TypeError):
        raise ProviderProtocolError("invalid_model_endpoint") from None
    if (
        parsed.scheme not in ("http", "https")
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/").endswith(("/responses", "/chat/completions", "/embeddings"))
    ):
        raise ProviderProtocolError("invalid_model_endpoint")
    return value.rstrip("/")
