"""Validated private model results shared by storage and the model gateway."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, StringConstraints, field_validator

from ..core.serialization import canonical

MODEL_RESULT_VERSION = 1
MAX_RESULT_BYTES = 32 * 1024 * 1024
Identity = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"\S")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def normalized_receipt(value: object) -> str | None:
    if type(value) is not str or len(value) > 256 or any(ord(char) < 32 for char in value):
        return None
    return value


class ModelResultProtocol(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class ModelRequestState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    FAILED = "failed"


class ModelResultRecoveryState(StrEnum):
    PRESERVED_PENDING = "preserved_pending"
    COMPLETED = "completed"


class NormalizedResult(RootModel[dict[str, JsonValue]]):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, hide_input_in_errors=True)


@dataclass(frozen=True, slots=True)
class ModelResultBinding:
    workspace_id: str
    project_id: str
    call_id: str
    provider: str
    service_identity: str
    model: str
    request_sha256: str


class StoredResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    workspace_id: Identity
    project_id: Identity
    call_id: Identity
    provider: Identity
    service_identity: Identity
    model: Identity
    request_sha256: Digest
    format_version: Annotated[int, Field(ge=1, le=1)]
    protocol: ModelResultProtocol
    response: NormalizedResult = Field(repr=False)
    result_sha256: Digest
    result_bytes: Annotated[int, Field(gt=0, le=MAX_RESULT_BYTES)]
    receipt_id: Annotated[str, Field(max_length=256)] | None
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def utc_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            message = "Invalid result timestamp"
            raise ValueError(message)
        return value

    @field_validator("receipt_id")
    @classmethod
    def safe_receipt(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 for char in value):
            message = "Invalid result receipt"
            raise ValueError(message)
        return value

    def stored(self) -> "StoredModelResult":
        encoded = canonical(self.response.root)
        if self.receipt_id != normalized_receipt(self.response.root.get("id")):
            message = "Invalid result receipt binding"
            raise ValueError(message)
        if len(encoded.encode()) != self.result_bytes or sha256(encoded.encode()).hexdigest() != self.result_sha256:
            message = "Invalid result integrity"
            raise ValueError(message)
        return StoredModelResult(
            ModelResultBinding(
                self.workspace_id,
                self.project_id,
                self.call_id,
                self.provider,
                self.service_identity,
                self.model,
                self.request_sha256,
            ),
            self.format_version,
            self.protocol,
            encoded,
            self.result_sha256,
            self.result_bytes,
            self.received_at,
            self.receipt_id,
        )


@dataclass(frozen=True, slots=True)
class StoredModelResult:
    binding: ModelResultBinding
    format_version: int
    protocol: ModelResultProtocol
    canonical_response: str = field(repr=False)
    result_sha256: str
    result_bytes: int
    received_at: datetime
    receipt_id: str | None

    def response(self) -> dict[str, JsonValue]:
        """Return a fresh decoded object; callers never mutate the retained value."""
        return NormalizedResult.model_validate_json(self.canonical_response).root


@dataclass(frozen=True, slots=True)
class SavedModelResult:
    stored: StoredModelResult
    request_state: ModelRequestState
    recovery_state: ModelResultRecoveryState

    def response(self) -> dict[str, JsonValue]:
        return self.stored.response()
