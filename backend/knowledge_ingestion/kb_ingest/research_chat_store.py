"""Atomic research records and worker ownership; never automatically replay work."""

import fcntl
import json
import re
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from .core import StateConflict, atomic_json, canonical, fingerprint
from .research_chat_contracts import ResearchError, ResearchSelection, TurnCreate

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
FORMAT = "research-chat-file-v1"
INPUT_VERSION = "research-chat-input-v2"
UNIFIED_INPUT_VERSION = "research-chat-input-v3"
MAX_FILE_BYTES = 2_097_152


def identity(kind, value):
    return kind + "-" + fingerprint(value)


def checked_id(kind, value):
    if type(value) is not str or not re.fullmatch(kind + r"-[0-9a-f]{64}", value):
        raise ValueError("Invalid research identity")
    return value


def bounded(value, limit=MAX_FILE_BYTES):
    if len(canonical(value).encode()) > limit:
        raise ResearchError("context_limit")
    return value


class ResearchChatStore:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "research_chats"

    def safe(self, path):
        path = Path(path)
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise StateConflict("Invalid research path") from None
        if ".." in relative.parts or self.root.is_symlink():
            raise StateConflict("Invalid research path")
        for current in [path, *path.parents]:
            if current.is_symlink():
                raise StateConflict("Invalid research link")
            if current == self.root:
                break
        return path

    def chat_path(self, cid):
        return self.safe(self.root / checked_id("chat", cid))

    def turn_path(self, cid, tid):
        return self.safe(self.chat_path(cid) / "turns" / checked_id("turn", tid))

    def write(self, path, value):
        bounded(value)
        atomic_json(self.safe(path), {"format": FORMAT, "sha256": fingerprint(value), "payload": value})

    def read(self, path):
        path = self.safe(path)
        if not path.exists():
            raise KeyError("Research record missing")
        try:
            if path.stat().st_size > MAX_FILE_BYTES + 256:
                raise ValueError()
            envelope = json.loads(path.read_text())
            if (
                set(envelope) != {"format", "sha256", "payload"}
                or envelope["format"] != FORMAT
                or fingerprint(envelope["payload"]) != envelope["sha256"]
            ):
                raise ValueError()
            return envelope["payload"]
        except (ValueError, TypeError, OSError, RecursionError):
            raise StateConflict("Research record integrity failed") from None

    @contextmanager
    def lock(self, cid):
        checked_id("chat", cid)
        name = "research-chat:" + cid
        directory = self.store.root / "locks"
        path = directory / (sha256(name.encode()).hexdigest() + ".lock")
        if directory.is_symlink() or path.is_symlink():
            raise StateConflict("Invalid research lock")
        with self.store.lock(name):
            yield

    def save_chat(self, chat):
        self.write(self.chat_path(chat["chat_id"]) / "chat.json", chat)

    def read_chat(self, cid):
        chat = self.read(self.chat_path(cid) / "chat.json")
        try:
            if (
                chat["chat_id"] != cid
                or identity("chat", chat["request_id"]) != cid
                or type(chat["revision"]) is not int
                or chat["revision"] < 0
                or len(chat["turn_ids"]) > 200
                or len(set(chat["turn_ids"])) != len(chat["turn_ids"])
                or chat["active_turn_id"] is not None
                and chat["active_turn_id"] not in chat["turn_ids"]
            ):
                raise ValueError()
            ResearchSelection.model_validate(chat["selection"])
            for tid in chat["turn_ids"]:
                checked_id("turn", tid)
        except (KeyError, ValueError, TypeError):
            raise StateConflict("Research chat integrity failed") from None
        return chat

    def save_turn(self, turn):
        self.write(self.turn_path(turn["chat_id"], turn["turn_id"]) / "turn.json", turn)

    def read_turn(self, cid, tid):
        path = self.turn_path(cid, tid)
        turn = self.read(path / "turn.json")
        snapshot = self.read(path / "input.json")
        try:
            if (
                turn["chat_id"] != cid
                or turn["turn_id"] != tid
                or identity("turn", [cid, turn["request_id"]]) != tid
                or turn["input_hash"] != fingerprint(snapshot)
                or turn["status"] not in TERMINAL | {"queued", "running"}
                or any(turn[k] != snapshot[k] for k in ("chat_id", "turn_id", "request_id", "message"))
            ):
                raise ValueError()
            if "input_version" in snapshot:
                if (
                    snapshot["input_version"] not in (INPUT_VERSION, UNIFIED_INPUT_VERSION)
                    or "history" in snapshot
                    or type(snapshot["history_refs"]) is not list
                    or len(snapshot["history_refs"]) > 200
                ):
                    raise ValueError()
                for ref in snapshot["history_refs"]:
                    if (
                        type(ref) is not dict
                        or set(ref) != {"turn_id", "binding_sha256"}
                        or type(ref["binding_sha256"]) is not str
                        or not re.fullmatch(r"[0-9a-f]{64}", ref["binding_sha256"])
                    ):
                        raise ValueError()
                    checked_id("turn", ref["turn_id"])
            if snapshot.get("input_version") == UNIFIED_INPUT_VERSION:
                selected = ResearchSelection.model_validate(snapshot["selection"])
                if (
                    selected.scope_mode != "unified"
                    or selected.source_focus.model_dump() != snapshot["request"]["source_focus"]
                ):
                    raise ValueError()
            elif "input_version" not in snapshot and (
                "history_refs" in snapshot or type(snapshot["history"]) is not list or len(snapshot["history"]) > 200
            ):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise StateConflict("Research turn integrity failed") from None
        return turn, snapshot

    def resolve_history(self, cid, tid):
        """Resolve one ordered prefix, never recursively hydrate earlier inputs.

        Legacy inputs retain their frozen embedded meaning. V2 references bind
        complete terminal turns, including unused evidence and failed outcomes.
        Callers hold the chat lock when resolving for execution or completion.
        """
        chat = self.read_chat(cid)
        _, snapshot = self.read_turn(cid, tid)
        try:
            unified = snapshot.get("input_version") == UNIFIED_INPUT_VERSION
            if tid not in chat["turn_ids"] or (not unified and snapshot["selection"] != chat["selection"]):
                raise ValueError()
            if unified and (
                snapshot["selection"].get("scope_mode") != "unified" or chat["selection"].get("scope_mode") != "unified"
            ):
                raise ValueError()
            prefix = chat["turn_ids"][: chat["turn_ids"].index(tid)]
            if snapshot.get("input_version") in (INPUT_VERSION, UNIFIED_INPUT_VERSION):
                refs = snapshot["history_refs"]
                if [ref["turn_id"] for ref in refs] != prefix:
                    raise ValueError()

                def referenced_turns():
                    for ref in refs:
                        turn, previous = self.read_turn(cid, ref["turn_id"])
                        if (
                            turn["status"] not in TERMINAL
                            or (not unified and previous["selection"] != snapshot["selection"])
                            or fingerprint(turn) != ref["binding_sha256"]
                        ):
                            raise ValueError()
                        yield turn

                rows = referenced_turns()
            else:
                rows = snapshot["history"]
                if [row["turn_id"] for row in rows] != prefix:
                    raise ValueError()
            history = []
            prior = {}
            for row in rows:
                if row["status"] not in TERMINAL:
                    raise ValueError()
                successful = row["answer"] if row["status"] == "completed" else None
                if successful:
                    registry = successful.get("registry", {})
                    if type(registry) is not dict:
                        raise ValueError()
                    for key, entry in registry.items():
                        if key in prior:
                            if canonical(prior[key]) != canonical(entry):
                                raise ValueError()
                        else:
                            prior[key] = deepcopy(entry)
                history.append(
                    {k: deepcopy(row[k]) for k in ("turn_id", "message", "status")}
                    | {
                        "answer": {k: deepcopy(successful[k]) for k in ("text", "focus_ids") if k in successful}
                        if successful
                        else None
                    }
                )
            if not set(snapshot["focus_ids"]) <= set(prior):
                raise ValueError()
            return history, prior
        except (KeyError, ValueError, TypeError):
            raise StateConflict("Research history integrity failed") from None

    def create(self, rid, selection):
        from .research_chat_contracts import ChatCreate

        selection = ResearchSelection.model_validate(selection).model_dump(mode="json")
        ChatCreate(
            request_id=rid,
            source_ids=selection["source_ids"],
            scope_mode=selection.get("scope_mode", "selected_sources"),
        )
        cid = identity("chat", rid)
        with self.lock(cid):
            if (self.chat_path(cid) / "chat.json").exists():
                chat = self.read_chat(cid)
                if chat["selection"] != selection:
                    raise StateConflict("Request identity conflict")
                return chat
            now = time.time()
            chat = {
                "version": "research-chat-v1",
                "chat_id": cid,
                "request_id": rid,
                "selection": selection,
                "revision": 0,
                "turn_ids": [],
                "active_turn_id": None,
                "focus_ids": [],
                "created_at": now,
                "updated_at": now,
            }
            self.save_chat(chat)
            return deepcopy(chat)

    def enqueue(self, cid, payload, *, freeze_selection=None):
        request = TurnCreate.model_validate(payload).model_dump(mode="json")
        tid = identity("turn", [cid, request["request_id"]])
        with self.lock(cid):
            chat = self.read_chat(cid)
            if tid in chat["turn_ids"]:
                turn, snapshot = self.read_turn(cid, tid)
                if snapshot["request"] != request:
                    raise StateConflict("Request identity conflict")
                return turn
            if chat["revision"] != request["expected_revision"] or chat["active_turn_id"] is not None:
                raise StateConflict("Research revision conflict")
            if len(chat["turn_ids"]) >= 200:
                raise ResearchError("context_limit")
            unified = chat["selection"].get("scope_mode") == "unified"
            if unified != ("source_focus" in request):
                raise ResearchError("invalid_request")
            selection = (
                (freeze_selection(request["source_focus"]) if freeze_selection else None)
                if unified
                else chat["selection"]
            )
            if selection is None:
                raise ResearchError("not_ready")
            selection = ResearchSelection.model_validate(selection).model_dump(mode="json")
            history_refs = []
            allowed = set()
            for previous in chat["turn_ids"]:
                turn, previous_input = self.read_turn(cid, previous)
                if turn["status"] not in TERMINAL or (not unified and previous_input["selection"] != chat["selection"]):
                    raise StateConflict("Research history incomplete")
                history_refs.append({"turn_id": previous, "binding_sha256": fingerprint(turn)})
                if turn["status"] == "completed" and turn["answer"]:
                    allowed.update(turn["answer"].get("registry", {}))
            bounded(history_refs, 128_000)
            focus = chat["focus_ids"] if request["focus_ids"] is None else request["focus_ids"]
            if not set(focus) <= allowed:
                raise StateConflict("Unknown research focus")
            snapshot = {
                "input_version": UNIFIED_INPUT_VERSION if unified else INPUT_VERSION,
                "chat_id": cid,
                "turn_id": tid,
                "request_id": request["request_id"],
                "message": request["message"],
                "request": request,
                "selection": deepcopy(selection),
                "history_refs": history_refs,
                "focus_ids": deepcopy(focus),
            }
            now = time.time()
            turn = {
                "chat_id": cid,
                "turn_id": tid,
                "request_id": request["request_id"],
                "message": request["message"],
                "input_hash": fingerprint(snapshot),
                "status": "queued",
                "attempt_token": uuid.uuid4().hex,
                "answer": None,
                "events": [],
                "error": None,
                "model": "gpt-6-luna",
                "created_at": now,
                "updated_at": now,
            }
            if unified:
                turn["source_focus"] = deepcopy(request["source_focus"])
            self.write(self.turn_path(cid, tid) / "input.json", snapshot)
            self.save_turn(turn)
            if unified:
                chat["selection"] = deepcopy(selection)
            chat["turn_ids"].append(tid)
            chat.update(active_turn_id=tid, revision=chat["revision"] + 1, updated_at=now)
            self.save_chat(chat)
            return deepcopy(turn)

    def put_artifact(self, cid, tid, call_id, artifact):
        payload = {"chat_id": cid, "turn_id": tid, "call_id": call_id, "artifact": deepcopy(artifact)}
        aid = identity("artifact", payload)
        self.write(self.turn_path(cid, tid) / "artifacts" / (aid + ".json"), payload)
        return self.read_artifact(cid, tid, aid)

    def read_artifact(self, cid, tid, aid):
        checked_id("artifact", aid)
        chat = self.read_chat(cid)
        if tid not in chat["turn_ids"]:
            raise StateConflict("Artifact turn outside chat")
        turn, snapshot = self.read_turn(cid, tid)
        if snapshot["selection"].get("scope_mode") != "unified":
            raise StateConflict("Artifact requires unified turn")
        payload = self.read(self.turn_path(cid, tid) / "artifacts" / (aid + ".json"))
        if identity("artifact", payload) != aid or payload["chat_id"] != cid or payload["turn_id"] != tid:
            raise StateConflict("Artifact identity mismatch")
        used = (
            [payload["artifact"]["table_version_id"]]
            if "table_version_id" in payload["artifact"]
            else [t["table_version_id"] for t in payload["artifact"].get("tables", [])]
        )
        if not used or not set(used) <= set(snapshot["selection"]["source_focus"]["table_version_ids"]):
            raise StateConflict("Artifact selection mismatch")
        return {"artifact_id": aid, **payload}

    def owned(self, cid, tid, token):
        chat = self.read_chat(cid)
        turn, _ = self.read_turn(cid, tid)
        return (
            chat,
            turn,
            (chat["active_turn_id"] == tid and turn["status"] == "running" and turn["attempt_token"] == token),
        )

    def start(self, cid, tid):
        with self.lock(cid):
            chat = self.read_chat(cid)
            if tid not in chat["turn_ids"] or chat["active_turn_id"] != tid:
                return None
            turn, _ = self.read_turn(cid, tid)
            if turn["status"] != "queued":
                return None
            turn.update(status="running", updated_at=time.time())
            self.save_turn(turn)
            return turn["attempt_token"]

    def event(self, cid, tid, token, event):
        bounded(event, 262144)
        with self.lock(cid):
            _, turn, owned = self.owned(cid, tid, token)
            if not owned:
                raise ResearchError("cancelled")
            if len(turn["events"]) >= 64:
                raise ResearchError("context_limit")
            turn["events"].append(deepcopy(event))
            turn["updated_at"] = time.time()
            self.save_turn(turn)

    def _finish(self, chat, turn):
        now = time.time()
        turn.update(attempt_token=None, updated_at=now)
        self.save_turn(turn)
        chat.update(active_turn_id=None, revision=chat["revision"] + 1, updated_at=now)
        if turn["status"] == "completed":
            chat["focus_ids"] = turn["answer"].get("focus_ids", [])
        self.save_chat(chat)

    def finish(self, cid, tid, token, *, answer=None, error=None, input_hash=None):
        with self.lock(cid):
            chat, turn, owned = self.owned(cid, tid, token)
            if not owned:
                return False
            if not error:
                if input_hash is not None and turn["input_hash"] != input_hash:
                    raise StateConflict("Research input changed")
                self.resolve_history(cid, tid)
            status = (
                error["code"]
                if error and error.get("code") in ("interrupted", "cancelled")
                else "failed"
                if error
                else "completed"
            )
            turn.update(status=status, answer=deepcopy(answer), error=error)
            self._finish(chat, turn)
            return True

    def cancel(self, cid, tid):
        with self.lock(cid):
            chat = self.read_chat(cid)
            if tid not in chat["turn_ids"]:
                raise KeyError("Turn outside chat")
            turn, _ = self.read_turn(cid, tid)
            if turn["status"] not in TERMINAL:
                turn.update(status="cancelled", error=ResearchError("cancelled").public())
                self._finish(chat, turn)
            return turn

    def chat_ids(self):
        self.safe(self.root)
        return (
            sorted(
                p.name
                for p in self.root.iterdir()
                if re.fullmatch(r"chat-[0-9a-f]{64}", p.name) and (p / "chat.json").exists()
            )
            if self.root.exists()
            else []
        )

    def acquire_worker(self, tid):
        checked_id("turn", tid)
        directory = self.store.root / "locks"
        path = directory / (sha256(("research-worker:" + tid).encode()).hexdigest() + ".lock")
        if directory.is_symlink() or path.is_symlink():
            raise StateConflict("Invalid worker lock")
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

    def recover(self):
        recovered = []
        for cid in self.chat_ids():
            try:
                with self.lock(cid):
                    chat = self.read_chat(cid)
                    tid = chat["active_turn_id"]
                    if tid is None:
                        continue
                    lease = self.acquire_worker(tid)
                    if lease is None:
                        continue
                    try:
                        turn, _ = self.read_turn(cid, tid)
                        if turn["status"] not in TERMINAL:
                            turn.update(status="interrupted", error=ResearchError("interrupted").public())
                        self._finish(chat, turn)
                        if turn["status"] == "interrupted":
                            recovered.append(deepcopy(turn))
                    finally:
                        self.release_worker(lease)
            except (KeyError, ValueError, OSError, StateConflict):
                continue
        return recovered
