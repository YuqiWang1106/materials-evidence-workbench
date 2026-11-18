"""Read-only knowledge protocol identifiers, errors and canonical evidence locators."""

import base64
import json

from ..core.serialization import canonical

PREFIX = "/api/agent-knowledge/v1"
SCHEMA_VERSION = "1.0"
EVIDENCE_REF_VERSION = 1
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
TOOLS = ["kb_search", "kb_expand", "kb_read_entity", "kb_read_evidence"]
STATUS = {
    "INVALID_ARGUMENT": 422,
    "UNAUTHORIZED": 401,
    "REFERENCE_NOT_FOUND": 404,
    "SOURCE_NOT_READY": 409,
    "SOURCE_CHANGED": 409,
    "INTEGRITY_FAILURE": 409,
    "KB_UNAVAILABLE": 503,
    "RESULT_TOO_LARGE": 413,
    "SOURCE_AMBIGUOUS": 409,
    "SOURCE_OUT_OF_SCOPE": 409,
}


class AgentKnowledgeError(ValueError):
    def __init__(self, code, message, status_code=None):
        # type: (str, str, int | None) -> None
        self.code, self.message = code, message
        self.status_code = status_code or STATUS.get(code, 500)
        super().__init__(f"{code}: {message}")


def error_content(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def encode_evidence_ref(payload):
    # type: (object) -> str
    return base64.urlsafe_b64encode(canonical(payload).encode("utf-8")).decode("ascii").rstrip("=")


def _checked_reference(payload: object, workspace_id: str, evidence_ref: str) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != EVIDENCE_REF_VERSION
        or payload.get("workspace_id") != workspace_id
    ):
        message = "Wrong reference format or workspace"
        raise ValueError(message)
    if encode_evidence_ref(payload) != evidence_ref:
        message = "Noncanonical reference"
        raise ValueError(message)
    for key in ("source_id", "task_id", "entity_id", "attribute_key", "location_key"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            message = "Missing identity"
            raise ValueError(message)
    if payload.get("content_block_id") is not None and not isinstance(payload["content_block_id"], str):
        message = "Invalid context identity"
        raise ValueError(message)
    return payload


def decode_evidence_ref(evidence_ref: str, workspace_id: str) -> dict[str, object]:
    try:
        raw = base64.b64decode(evidence_ref + "=" * (-len(evidence_ref) % 4), altchars=b"-_", validate=True)
        payload: object = json.loads(raw)
        return _checked_reference(payload, workspace_id, evidence_ref)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise AgentKnowledgeError("INVALID_ARGUMENT", "Invalid evidence reference for this workspace.") from exc
