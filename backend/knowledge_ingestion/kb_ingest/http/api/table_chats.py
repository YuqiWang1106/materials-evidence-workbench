"""Table chat sessions, turns and artifacts."""

from typing import Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import website_fields as _times


class TableChatCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class TableChatTurn(TableChatCreate):
    expected_revision: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=8000)
    table_version_ids: list[str] = Field(min_length=1, max_length=10)


class _TableChatService(Protocol):
    def list_table_chats(self) -> dict[str, object]: ...

    def create_table_chat(self, payload: dict[str, object], /) -> dict[str, object]: ...

    def get_table_chat(self, chat_id: str, /) -> dict[str, object]: ...

    def enqueue_table_chat_turn(self, chat_id: str, payload: dict[str, object], /) -> dict[str, object]: ...

    def cancel_table_chat_turn(self, chat_id: str, turn_id: str, /) -> dict[str, object]: ...

    def get_table_chat_artifact(self, chat_id: str, turn_id: str, artifact_id: str, /) -> dict[str, object]: ...


def routes(service: _TableChatService) -> APIRouter:
    router = APIRouter()

    @_times.chats.route(router.get, "/api/table-chats")
    def table_chats() -> dict[str, object]:
        return service.list_table_chats()

    @_times.chat.route(router.post, "/api/table-chats")
    def create_table_chat(body: TableChatCreate) -> dict[str, object]:
        return service.create_table_chat(body.model_dump())

    @_times.chat.route(router.get, "/api/table-chats/{chat_id}")
    def table_chat(chat_id: str) -> dict[str, object]:
        return service.get_table_chat(chat_id)

    @_times.timestamps.route(router.post, "/api/table-chats/{chat_id}/turns")
    def table_chat_turn(chat_id: str, body: TableChatTurn) -> dict[str, object]:
        return service.enqueue_table_chat_turn(chat_id, body.model_dump())

    @_times.timestamps.route(router.post, "/api/table-chats/{chat_id}/turns/{turn_id}/cancel")
    def cancel_table_chat_turn(chat_id: str, turn_id: str) -> dict[str, object]:
        return service.cancel_table_chat_turn(chat_id, turn_id)

    @router.get("/api/table-chats/{chat_id}/turns/{turn_id}/artifacts/{artifact_id}", response_model=None)
    def table_chat_artifact(chat_id: str, turn_id: str, artifact_id: str) -> dict[str, object]:
        return service.get_table_chat_artifact(chat_id, turn_id, artifact_id)

    return router
