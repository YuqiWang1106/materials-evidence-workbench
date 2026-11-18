import httpx

# Transport failures before the request reached the provider: nothing can have been billed.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)
# A failed write can mean the final bytes arrived but their acknowledgement was lost.
_WRITE_FAILED = (httpx.WriteError, httpx.WriteTimeout)


# Provider overload or transient server errors; the request was not run to completion.
def transport_status(exc: httpx.TransportError) -> str:
    """Ledger status of a transport failure: not sent, partly written, or sent with an unknown outcome."""
    if isinstance(exc, _NOT_SENT):
        return "transport_not_sent"
    if isinstance(exc, _WRITE_FAILED):
        return "transport_write_failed"  # the last chunk may have arrived: treated as possibly sent
    return "transport_unknown"


# Outcomes after which the provider never had the request.
_NOT_REACHED = ("model_not_dispatched", "project_budget_not_dispatched", "transport_not_sent")
