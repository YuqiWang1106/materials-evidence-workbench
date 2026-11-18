"""Validate server-supplied model capabilities against the ingestion coordinator."""

from collections.abc import Mapping
from typing import TypeGuard

from ..llm.model_requests import ModelRequestContext
from ..llm.project_model_dispatch import ProjectModelDispatcher


def owns_context(workspace_id: str, context: object) -> TypeGuard[ModelRequestContext]:
    return isinstance(context, ModelRequestContext) and context.service.workspace_id == workspace_id


def check_dispatchers(workspace_id: str, wiki: Mapping[str, object] | None, *dispatchers: object) -> None:
    for dispatcher in (*dispatchers, *(wiki or {}).values()):
        if isinstance(dispatcher, ProjectModelDispatcher) and not owns_context(workspace_id, dispatcher.context):
            message = "model_workspace_mismatch"
            raise ValueError(message)
