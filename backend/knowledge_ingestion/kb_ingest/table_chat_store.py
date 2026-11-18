"""File-backed chat records; no scientific assertions or coordination schema changes."""

import fcntl
import json
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from .core import StateConflict, atomic_json, fingerprint
from .storage.file_time import record_times
from .storage.types import decode_utc, encode_utc

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
STATUSES = TERMINAL | {"queued", "running"}


def request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("请求标识必须为不超过100字符的字母、数字、短横线或下划线")
    return value


def identity(kind, value):
    return kind + "-" + fingerprint(value)


def checked_id(kind, value):
    if not isinstance(value, str) or not re.fullmatch(kind + r"-[0-9a-f]{64}", value):
        raise ValueError("无效的对话或结果标识")
    return value


class TableChatStore:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "table_chats"

    def _safe(self, path):
        # Inspect every managed ancestor, including broken symlinks, before I/O.
        for part in (self.root, *reversed(path.relative_to(self.root).parents)):
            candidate = part if part == self.root else self.root / part
            if candidate.is_symlink():
                raise StateConflict("对话文件完整性检查失败")
        if path.is_symlink():
            raise StateConflict("对话文件完整性检查失败")
        return path

    def chat_path(self, chat_id):
        return self._safe(self.root / checked_id("chat", chat_id))

    def turn_path(self, chat_id, turn_id):
        return self._safe(self.chat_path(chat_id) / "turns" / checked_id("turn", turn_id))

    def write(self, path, payload):
        path = self._safe(Path(path))
        atomic_json(path, {"format": "table-chat-file-v1", "sha256": fingerprint(payload), "payload": payload})

    def read(self, path):
        path = self._safe(Path(path))
        if not path.exists():
            raise KeyError("对话记录不存在")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope["format"] != "table-chat-file-v1" or fingerprint(envelope["payload"]) != envelope["sha256"]:
                raise ValueError("hash")
            return envelope["payload"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise StateConflict("对话文件完整性检查失败") from exc

    def read_chat(self, chat_id):
        chat = self.read(self.chat_path(chat_id) / "chat.json")
        try:
            if (
                chat["chat_id"] != chat_id
                or identity("chat", request_id(chat["request_id"])) != chat_id
                or type(chat["revision"]) is not int
                or chat["revision"] < 0
                or not isinstance(chat["turn_ids"], list)
                or len(chat["turn_ids"]) > 40
                or len(set(chat["turn_ids"])) != len(chat["turn_ids"])
                or chat["active_turn_id"] is not None
                and chat["active_turn_id"] not in chat["turn_ids"]
            ):
                raise ValueError("identity")
            for tid in chat["turn_ids"]:
                checked_id("turn", tid)
            return record_times(chat, decode_utc)
        except (KeyError, ValueError, TypeError) as exc:
            raise StateConflict("对话文件完整性检查失败") from exc

    def read_turn(self, chat_id, turn_id):
        path = self.turn_path(chat_id, turn_id)
        turn = self.read(path / "turn.json")
        snapshot = self.read(path / "input.json")
        try:
            if (
                turn["chat_id"] != chat_id
                or turn["turn_id"] != turn_id
                or turn_id != identity("turn", [chat_id, request_id(turn["request_id"])])
                or turn["status"] not in STATUSES
                or turn["input_sha256"] != fingerprint(snapshot)
                or any(
                    turn[key] != snapshot[key]
                    for key in ("chat_id", "turn_id", "request_id", "message", "table_version_ids", "tables")
                )
            ):
                raise ValueError("binding")
            return record_times(turn, decode_utc), snapshot
        except (KeyError, ValueError, TypeError) as exc:
            raise StateConflict("对话轮次完整性检查失败") from exc

    def save_chat(self, chat):
        self.write(self.chat_path(chat["chat_id"]) / "chat.json", record_times(chat, encode_utc))

    def save_turn(self, turn):
        self.write(self.turn_path(turn["chat_id"], turn["turn_id"]) / "turn.json", record_times(turn, encode_utc))

    def put_artifact(self, chat_id, turn_id, call_id, artifact):
        payload = {"chat_id": chat_id, "turn_id": turn_id, "call_id": call_id, "artifact": deepcopy(artifact)}
        aid = identity("artifact", payload)
        path = self.turn_path(chat_id, turn_id) / "artifacts" / (aid + ".json")
        if path.exists():
            if self.read(path) != payload:
                raise StateConflict("程序结果完整性检查失败")
        else:
            self.write(path, payload)
        return aid

    def read_artifact(self, chat_id, turn_id, artifact_id):
        checked_id("artifact", artifact_id)
        payload = self.read(self.turn_path(chat_id, turn_id) / "artifacts" / (artifact_id + ".json"))
        if (
            identity("artifact", payload) != artifact_id
            or payload.get("chat_id") != chat_id
            or payload.get("turn_id") != turn_id
        ):
            raise StateConflict("程序结果完整性检查失败")
        return {"artifact_id": artifact_id, **payload}

    def chat_ids(self):
        self._safe(self.root)
        if not self.root.exists():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if re.fullmatch(r"chat-[0-9a-f]{64}", p.name) and (p / "chat.json").exists()
        )

    def acquire_worker(self, turn_id):
        """Same lock namespace as Store.lock; caller transfers this lease to worker."""
        checked_id("turn", turn_id)
        name = "table-chat-worker:" + turn_id
        path = self.store.root / "locks" / (sha256(name.encode()).hexdigest() + ".lock")
        if path.is_symlink():
            raise StateConflict("对话执行锁完整性检查失败")
        stream = path.open("a+b")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        return stream

    @staticmethod
    def release_worker(stream):
        if stream is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
