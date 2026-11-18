"""Shared coordination and configuration errors with safe public messages."""

from typing import Never


class StateConflict(RuntimeError):
    pass


class ConfigurationError(ValueError):
    """Safe to display: messages contain no configuration values."""

    def __init__(self, message: str, code: str = "model_config_invalid") -> None:
        self.code = code
        super().__init__(message)


class NativeError(ValueError):
    """Safe public error, deliberately excludes configuration values."""


class ProviderProtocolError(ValueError):
    def __init__(self, code: str = "provider_protocol") -> None:
        self.code = code
        super().__init__(code)


def raise_state_conflict(message: str) -> Never:
    raise StateConflict(message)


class NotRetryableError(StateConflict):
    """Retry was asked for a task that is under review, being saved or already saved."""

    def __init__(self) -> None:
        super().__init__(
            "Only a failed or cancelled extraction can be retried; cancel a draft under review first to run it again"
        )


class EmptyUploadError(ValueError):
    def __init__(self) -> None:
        super().__init__("Empty original file")


class UploadTooLargeError(ValueError):
    def __init__(self) -> None:
        super().__init__("A file must be at most 128 MiB")


class MissingWorkspaceIdError(ValueError):
    def __init__(self) -> None:
        super().__init__("workspace_id required")
