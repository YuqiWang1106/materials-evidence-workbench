"""Read validated table-chat records into the service's epoch-valued views."""

from collections.abc import Callable
from copy import deepcopy

from .storage.file_time import public_times
from .storage.time_source import sourced_epoch
from .table_chat_store import TableChatStore

REQUEST_METADATA = ("request_state", "call_id", "request_hash", "service_identity", "result_sha256")
_CHAT_KEYS = ("chat_id", "title", "revision", "active_turn_id")
PUBLIC_TURN_KEYS = (
    "turn_id",
    "request_id",
    "message",
    "table_version_ids",
    "tables",
    "status",
    "assistant_text",
    "error",
    "model",
    "tool_calls",
    "usage",
)


def public_turn(turn: dict) -> dict:
    fallback = "unknown" if turn.get("request_started") or turn.get("model_requests") else "not_sent"
    return deepcopy(
        {
            **{key: turn[key] for key in PUBLIC_TURN_KEYS},
            **public_times(turn, sourced_epoch),
            **{key: turn.get(key, fallback if key == "request_state" else None) for key in REQUEST_METADATA},
        }
    )


def list_chats(files: TableChatStore) -> dict:
    chats = [files.read_chat(cid) for cid in files.chat_ids()]
    chats.sort(key=lambda chat: (chat["updated_at"], chat["chat_id"]), reverse=True)
    return {
        "chats": [
            {**{key: chat[key] for key in _CHAT_KEYS}, "updated_at": sourced_epoch(chat["updated_at"])}
            for chat in chats
        ]
    }


def chat_detail(files: TableChatStore, chat_id: str, source_warnings: Callable[[dict], list]) -> dict:
    chat = files.read_chat(chat_id)
    turns = [public_turn(files.read_turn(chat_id, tid)[0]) for tid in chat["turn_ids"]]
    for turn in turns:
        if warnings := source_warnings(turn):
            turn["source_warnings"] = warnings
    return {**{k: chat[k] for k in _CHAT_KEYS}, **public_times(chat, sourced_epoch), "turns": turns}
