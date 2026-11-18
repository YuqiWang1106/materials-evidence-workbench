"""Website research jobs with durable ownership and explicit, bounded reads."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from threading import Event, Lock

from .core import fingerprint
from .knowledge_contracts import VERSION, GetRequest
from .llm.project_model_dispatch import ProjectModelDispatcher
from .research_chat_contracts import ChatCreate, ResearchError, TurnCreate
from .research_chat_engine import execute_research_chat
from .research_chat_store import ResearchChatStore, identity
from .research_chat_tools import ResearchTools


def support_records(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from support_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from support_records(child)


PUBLIC_TURN = (
    "chat_id",
    "turn_id",
    "request_id",
    "message",
    "input_hash",
    "status",
    "answer",
    "events",
    "error",
    "model",
    "created_at",
    "updated_at",
)


class ResearchChatService:
    def __init__(
        self,
        store,
        knowledge,
        *,
        dispatcher=None,
        freeze_selection=None,
        passage_reader=None,
        pool=None,
        model_readiness=None,
    ):
        self.files = ResearchChatStore(store)
        self.knowledge = knowledge
        self.dispatcher = dispatcher
        self.model_readiness = model_readiness
        if getattr(knowledge, "wiki_adapter", None) is not None and getattr(knowledge, "wiki_hybrid", None) is None:
            from .wiki_hybrid_search import WikiHybridSearch

            adapter = knowledge.wiki_adapter
            knowledge.wiki_hybrid = WikiHybridSearch(
                store,
                {
                    "workspace_id": adapter.workspace_id,
                    "collection_id": adapter.collection_id,
                    "use_policy": "source_reviewed",
                },
            )
        self.freeze_selection = freeze_selection
        self.passage_reader = passage_reader
        self._own_pool = pool is None
        self.pool = pool or ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-chat")
        self._stop = Event()
        self._guard = Lock()
        self._futures = set()

    def capabilities(self):
        ready = isinstance(self.dispatcher, ProjectModelDispatcher)
        model = self.dispatcher.context.policy.model if ready else None
        state = self.model_readiness or {
            "status": "configured_unverified" if ready else "not_ready",
            "reason": "configured_unverified" if ready else "model_not_configured",
            "provider_tested": False,
        }
        return {
            "version": "research-chat-v1",
            "model": model,
            "model_ready": ready,
            "model_status": state,
            "configuration_refresh": "restart_required",
            "scope_modes": ["selected_sources", "all_wiki", "unified"]
            if getattr(self.knowledge, "wiki_adapter", None) is not None
            else ["selected_sources"],
            "wiki_retrieval": self.knowledge.wiki_hybrid.status()
            if getattr(self.knowledge, "wiki_hybrid", None)
            else None,
            "sources_ready": self.freeze_selection is not None,
            "automatic_retries": False,
            "layers": {
                "record": "available" if self.knowledge else "not_ready",
                "source_passage": "available" if self.passage_reader else "not_ready",
                "wiki": "available" if getattr(self.knowledge, "wiki_adapter", None) is not None else "not_ready",
            },
            "search": "lexical_candidates"
            if getattr(self.knowledge, "lexical_index", None) is not None
            else "not_ready",
            "research_tasks": {
                "question_answering": "available",
                "constraints": "verified_contexts_only",
                "performance_reference": "reference_only",
                "prediction": "model_not_available",
                "research_plan": "draft_not_submitted",
            },
            "search_index_readiness": "check_selected_scope",
            "limits": {"model_calls": 8, "tool_calls": 16, "output_tokens": 4096, "wall_seconds": 180},
        }

    def create(self, payload):
        request = ChatCreate.model_validate(payload)
        cid = identity("chat", request.request_id)
        if (self.files.chat_path(cid) / "chat.json").exists():
            chat = self.files.read_chat(cid)
            if (request.scope_mode != "unified" and chat["selection"]["source_ids"] != request.source_ids) or chat[
                "selection"
            ].get("scope_mode", "selected_sources") != request.scope_mode:
                from .core import StateConflict

                raise StateConflict("Request identity conflict")
            return self.get(cid)
        if request.scope_mode == "unified":
            selected = self.freeze_unified({"document_source_ids": [], "table_version_ids": []})
        elif request.scope_mode == "all_wiki":
            from .wiki_query_service import WikiQueryService

            selected = WikiQueryService(self.knowledge).selection()
        else:
            if self.freeze_selection is None:
                raise ResearchError("sources_not_ready")
            selected = self.freeze_selection(request.source_ids)
            if set(selected["source_ids"]) != set(request.source_ids):
                raise ResearchError("stale_context")
        chat = self.files.create(request.request_id, selected)
        return self.get(chat["chat_id"])

    def freeze_unified(self, focus):
        from .research_chat_unified import freeze_unified

        return freeze_unified(self.knowledge, focus, self.freeze_selection)

    def list(self, *, offset=0, limit=25):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ResearchError("invalid_request")
        ids = self.files.chat_ids()
        rows = []
        for cid in ids[offset : offset + limit]:
            with self.files.lock(cid):
                chat = self.files.read_chat(cid)
                row = {
                    k: deepcopy(chat[k]) for k in ("chat_id", "revision", "created_at", "updated_at", "active_turn_id")
                }
                title = "新研究对话"
                latest = None
                if chat["turn_ids"]:
                    first, _ = self.files.read_turn(cid, chat["turn_ids"][0])
                    last, _ = self.files.read_turn(cid, chat["turn_ids"][-1])
                    title = " ".join(first["message"].split())[:80] or title
                    latest = last["status"]
                row.update(
                    title=title,
                    latest_turn_status=latest,
                    selected_source_count=len(chat["selection"]["source_ids"]),
                    scope_mode=chat["selection"].get("scope_mode", "selected_sources"),
                )
                rows.append(row)
        return {
            "chats": rows,
            "has_more": offset + len(rows) < len(ids),
            "next_offset": offset + len(rows) if offset + len(rows) < len(ids) else None,
        }

    def get(self, cid, *, offset=0, limit=20):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ResearchError("invalid_request")
        with self.files.lock(cid):
            chat = self.files.read_chat(cid)
            rows = [
                self._public(self.files.read_turn(cid, tid)[0]) for tid in chat["turn_ids"][offset : offset + limit]
            ]
            return {
                **{
                    k: deepcopy(chat[k])
                    for k in (
                        "chat_id",
                        "revision",
                        "selection",
                        "focus_ids",
                        "active_turn_id",
                        "created_at",
                        "updated_at",
                    )
                },
                "source_warnings": self._source_warnings(
                    set(chat["selection"].get("source_ids", []))
                    | {sid for row in rows for sid in (row.get("source_focus") or {}).get("document_source_ids", [])}
                ),
                "turns": rows,
                "has_more": offset + len(rows) < len(chat["turn_ids"]),
                "next_offset": offset + len(rows) if offset + len(rows) < len(chat["turn_ids"]) else None,
            }

    def _source_warnings(self, source_ids):
        result = []
        for sid in source_ids:
            row = self.files.store.source_state(sid)
            if row and (
                row["lifecycle"] != "active" or row.get("visibility_generation") and not row["published_task_id"]
            ):
                result.append(
                    {
                        "source_id": sid,
                        "message": "来源已删除"
                        if row["lifecycle"] in {"deleting", "deleted"}
                        else "来源正在重新解析或已撤下",
                    }
                )
        return result

    def _historical_support_warning(self, entry, created_at):
        # Check immutable task/snapshot ownership, not only today's published pointer.
        from sqlalchemy.orm import Session

        from .document_derived_reset import _sources
        from .storage.lifecycle_repository import is_stale_task, reparses, snapshot_task
        from .storage.source_repository import get_source

        records = list(support_records(entry))
        ids = _sources(entry)
        workspace = self.files.store.workspace_id
        with Session(self.files.store.engine) as session:
            states = [get_source(session, workspace, sid) for sid in ids]
            if not any(s and s.visibility_generation for s in states):
                return None
            tasks = {v[k] for v in records for k in ("task_id", "source_task_id") if isinstance(v.get(k), str)}
            for v in records:
                if v.get("kind") == "study_unit_snapshot":
                    tid = snapshot_task(session, workspace, v.get("snapshot_id"))
                    if tid:
                        tasks.add(tid)
            if any(is_stale_task(session, workspace, tid) for tid in tasks):
                return "来源已重新解析，旧版本依据已失效"
            versions = {v["table_version_id"] for v in records if isinstance(v.get("table_version_id"), str)}
            for op in reparses(session, workspace):
                if versions.intersection(op.manifest.get("withdrawn_artifacts", [])):
                    return "来源已重新解析，旧版本依据已失效"
                # Legacy source-passage entries lack a task ID. A later full reparse
                # invalidates every support in that historical turn for this source.
                if (
                    not tasks
                    and not versions
                    and isinstance(created_at, (int, float))
                    and op.created_at > created_at
                    and ids.intersection(op.manifest.get("source_ids", []))
                ):
                    return "来源已重新解析，旧版本依据已失效"
        return None

    def _public(self, turn):
        from .document_derived_reset import _sources

        value = deepcopy({k: turn[k] for k in (*PUBLIC_TURN, "source_focus") if k in turn})
        registry = (turn.get("answer") or {}).get("registry") or {}
        unavailable = {}
        for key, entry in registry.items():
            warnings = self._source_warnings(_sources(entry))
            if warnings:
                unavailable[key] = (
                    "来源已删除" if any(w["message"] == "来源已删除" for w in warnings) else warnings[0]["message"]
                )
            else:
                reason = self._historical_support_warning(entry, turn.get("created_at"))
                if reason:
                    unavailable[key] = reason
        # Calculated supports inherit the availability of their saved evidence.
        for _ in range(len(registry)):
            added = False
            for key, entry in registry.items():
                reasons = [unavailable[d] for d in entry.get("dependency_ids", []) if d in unavailable]
                if key not in unavailable and reasons:
                    unavailable[key] = "来源已删除" if "来源已删除" in reasons else reasons[0]
                    added = True
            if not added:
                break
        if unavailable:
            value["unavailable_supports"] = unavailable
        return value

    def turn(self, cid, tid):
        with self.files.lock(cid):
            if tid not in self.files.read_chat(cid)["turn_ids"]:
                raise KeyError("Turn outside chat")
            return self._public(self.files.read_turn(cid, tid)[0])

    def enqueue(self, cid, payload):
        request = TurnCreate.model_validate(payload).model_dump(mode="json")
        if self._stop.is_set():
            raise ResearchError("interrupted")
        if not self.capabilities()["model_ready"]:
            raise ResearchError(self.capabilities()["model_status"]["reason"])
        turn = self.files.enqueue(cid, request, freeze_selection=self.freeze_unified)
        tid = turn["turn_id"]
        if turn["status"] == "queued":
            lease = self.files.acquire_worker(tid)
            if lease is not None:

                def run():
                    try:
                        self._run(cid, tid)
                    finally:
                        self.files.release_worker(lease)

                try:
                    future = self.pool.submit(run)
                except RuntimeError:
                    self.files.release_worker(lease)
                    return self._public(turn)
                with self._guard:
                    self._futures.add(future)

                def finished(f):
                    if f.cancelled():
                        self.files.release_worker(lease)
                    with self._guard:
                        self._futures.discard(f)

                future.add_done_callback(finished)
        return self._public(turn)

    def user_message(self, cid, tid):
        # Turn IDs are only resolved inside the authenticated conversation.
        if tid not in self.files.read_chat(cid)["turn_ids"]:
            raise ResearchError("stale_context")
        turn, snapshot = self.files.read_turn(cid, tid)
        text = snapshot["message"]
        return {"message_id": tid, "role": "user", "text": text, "text_sha256": sha256(text.encode()).hexdigest()}

    def _run(self, cid, tid):
        token = self.files.start(cid, tid)
        if token is None:
            return

        def cancelled():
            if self._stop.is_set():
                return True
            with self.files.lock(cid):
                return not self.files.owned(cid, tid, token)[2]

        def event(value):
            self.files.event(cid, tid, token, value)

        try:
            with self.files.lock(cid):
                turn, snapshot = self.files.read_turn(cid, tid)
                history, prior = self.files.resolve_history(cid, tid)
            tools = ResearchTools(
                self.knowledge,
                snapshot["selection"],
                prior=prior,
                passage_reader=self.passage_reader,
                user_message=self.user_message(cid, tid),
                message_reader=lambda mid: self.user_message(cid, mid),
                artifact_writer=lambda artifact: self.files.put_artifact(
                    cid, tid, "program-" + fingerprint(artifact), artifact
                ),
                artifact_owner=(cid, tid),
            )
            answer = asyncio.run(
                execute_research_chat(
                    dispatcher=self.dispatcher,
                    tools=tools,
                    history=history,
                    message=snapshot["message"],
                    focus_ids=snapshot["focus_ids"],
                    on_event=event,
                    cancel_check=cancelled,
                )
            )
            if cancelled():
                return
            self.files.finish(cid, tid, token, answer=answer, input_hash=turn["input_hash"])
        except Exception as error:
            if self._stop.is_set():
                safe = ResearchError("interrupted")
            else:
                safe = error if isinstance(error, ResearchError) else ResearchError("execution_failed")
            self.files.finish(cid, tid, token, error=safe.public())

    def cancel(self, cid, tid):
        return self._public(self.files.cancel(cid, tid))

    def recover(self):
        from .llm.model_requests import ModelRequestError

        recovered = self.files.recover()
        for turn in recovered:
            unknown = False
            events = [
                event for event in turn["events"] if event.get("type") == "model_request" and event.get("call_id")
            ]
            if events and not isinstance(self.dispatcher, ProjectModelDispatcher):
                # Credentials may now be absent; that does not prove an older
                # recorded provider request was never sent.
                unknown = True
            elif isinstance(self.dispatcher, ProjectModelDispatcher):
                context = self.dispatcher.context
                for event in events:
                    row = context.service.read_call(context.project_id, event["call_id"])
                    if row is None:
                        unknown = True  # Legacy send evidence without imported state.
                    elif row["state"] in ("sent", "unknown"):
                        unknown = True
                        try:
                            context.service.mark_call_unknown(context.project_id, event["call_id"], "process_recovery")
                        except ModelRequestError as error:
                            if error.code != "invalid_transition":
                                raise
            if unknown:
                with self.files.lock(turn["chat_id"]):
                    current, _ = self.files.read_turn(turn["chat_id"], turn["turn_id"])
                    if current["status"] == "interrupted":
                        current["error"] = ResearchError("request_outcome_unknown").public()
                        self.files.save_turn(current)
        return len(recovered)

    def support(self, cid, tid, support_id, *, evidence_index=None):
        # None preserves the distinction between an omitted index and an
        # explicit zero until a global citation's saved selector is resolved.
        if evidence_index is not None and (type(evidence_index) is not int or not 0 <= evidence_index < 20):
            raise ResearchError("invalid_request")
        with self.files.lock(cid):
            if tid not in self.files.read_chat(cid)["turn_ids"]:
                raise KeyError("Turn outside chat")
            turn, snapshot = self.files.read_turn(cid, tid)
            if turn["status"] != "completed" or not turn["answer"]:
                raise KeyError("No committed evidence")
            tools = ResearchTools(
                self.knowledge,
                snapshot["selection"],
                prior=turn["answer"]["registry"],
                message_reader=lambda mid: self.user_message(cid, mid),
            )
            entry = deepcopy(tools.registry.item(support_id))
        # Completed turns are immutable. Resolve artifact authority outside the
        # chat lock because the common artifact reader acquires its owner lock.
        from .document_derived_reset import _sources

        for sid in _sources(entry):
            self.files.store.require_source(sid)
        tools.validate([support_id])
        if tools.unified is not None and (entry.get("unified_source") or entry["kind"] == "unified_support"):
            return tools.unified.open_support(entry, evidence_index=evidence_index)
        if tools.wiki_query is not None and entry["kind"].startswith("wiki_"):
            return tools.wiki_query.open_support(entry, evidence_index=evidence_index)
        evidence_index = 0 if evidence_index is None else evidence_index
        if entry["kind"] == "task_result":
            entry["program"] = tools.tasks.read(support_id)
        if entry["kind"] == "user_target":
            entry["user_target"] = tools.tasks.read(support_id)
        ref = entry.get("ref")
        if ref is not None:
            scope = entry["scope"] if tools.unified else tools.scope(entry["layer"])
            result = self.knowledge.get(
                GetRequest(
                    schema_version=VERSION,
                    request_id="research-support",
                    use_policy=scope["use_policy"],
                    target=ref,
                    view="location",
                )
            )
            row = result.results[0]
            evidence = getattr(row, "evidence_refs", [])
            if evidence and ref["kind"] not in ("evidence", "source_passage"):
                if evidence_index >= len(evidence):
                    raise ResearchError("invalid_request")
                result = self.knowledge.get(
                    GetRequest(
                        schema_version=VERSION,
                        request_id="research-preview",
                        use_policy=scope["use_policy"],
                        target=evidence[evidence_index],
                        view="location",
                    )
                )
                row = result.results[0]
            entry["location"] = row.model_dump(mode="json")
        return entry

    def close(self):
        self._stop.set()
        with self._guard:
            futures = list(self._futures)
        for future in futures:
            future.cancel()
        if self._own_pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
