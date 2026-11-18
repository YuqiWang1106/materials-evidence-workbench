"""Agent request bodies and argument validation at the service boundary."""

from pydantic import BaseModel, Field, StrictStr, field_validator

from .protocol import AgentKnowledgeError


def checked_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentKnowledgeError("INVALID_ARGUMENT", f"{label} must be nonempty text.")
    return value.strip()


def checked_strings(values: object, label: str) -> list[str] | None:
    if values is None:
        return None
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise AgentKnowledgeError("INVALID_ARGUMENT", f"{label} must contain nonempty strings.")
    return list(dict.fromkeys(value.strip() for value in values))


def source_list(values: object, label: str) -> list[str] | None:
    result = checked_strings(values, label)
    if result == []:
        raise AgentKnowledgeError("INVALID_ARGUMENT", f"{label} must not be empty.")
    return result


def check_pagination(offset: object, limit: object) -> None:
    if type(offset) is not int or offset < 0 or (limit is not None and (type(limit) is not int or limit <= 0)):
        raise AgentKnowledgeError("INVALID_ARGUMENT", "offset must be nonnegative and limit must be positive.")


def checked_around(around: object) -> int:
    if type(around) is not int or around < 0:
        raise AgentKnowledgeError("INVALID_ARGUMENT", "around must be a nonnegative integer.")
    return around


class ScopeRequest(BaseModel):
    source_ids: list[StrictStr] | None = Field(default=None, min_length=1)


class PageRequest(ScopeRequest):
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, gt=0)


class SearchRequest(PageRequest):
    query: StrictStr
    entity_type: StrictStr | None = None
    section: StrictStr | None = None
    source: StrictStr | None = None
    source_names: list[StrictStr] | None = Field(default=None, min_length=1)

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        # type: (str) -> str
        if not value.strip():
            message = "query must not be empty"
            raise ValueError(message)
        return value


class ExpandRequest(PageRequest):
    entity_ids: list[StrictStr] = Field(min_length=1)
    focus: StrictStr
    to_type: StrictStr | None = None
    sections: list[StrictStr] | None = None

    @field_validator("focus")
    @classmethod
    def nonempty(cls, value):
        # type: (str) -> str
        if not value.strip():
            message = "focus must not be empty"
            raise ValueError(message)
        return value


class EntityRequest(PageRequest):
    entity_id: StrictStr = Field(min_length=1)
    sections: list[StrictStr] | None = None


class EvidenceRequest(ScopeRequest):
    evidence_ref: StrictStr = Field(min_length=1)
    around: int = Field(default=2, ge=0)
