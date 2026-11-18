class ModelCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        outcome_unknown: bool = False,
        unknown_retries_exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.outcome_unknown = outcome_unknown or unknown_retries_exhausted
        self.unknown_retries_exhausted = unknown_retries_exhausted


class BudgetExceeded(ModelCallError):
    pass


NOT_PROCESSED = "not_processed"


def allowance_exhausted(exc: BaseException) -> bool:
    """True when a request was not sent because the document's request or time allowance is used up
    (as opposed to a request that ran out of its own time)."""
    text = str(exc)
    return isinstance(exc, BudgetExceeded) and (
        text.startswith(NOT_PROCESSED)
        or "remaining scope not processed" in text
        or "exceeds remaining allowance" in text
    )


class ModelOutcomeUnknownError(RuntimeError):
    """Stop dependent work when a dispatched model request has no confirmed outcome."""

    code = "model_outcome_unknown"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: request retained; no automatic retry")


class ModelRequestBindingError(RuntimeError):
    """Reject missing or conflicting evidence without claiming a provider outcome."""

    code = "model_request_binding_invalid"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: original request binding could not be verified")


def is_model_outcome_unknown(value: object) -> bool:
    """Recognize current and historical task errors without rewriting old records."""
    return isinstance(value, str) and value.startswith(("model_outcome_unknown:", "model_response_lost_after_retries:"))
