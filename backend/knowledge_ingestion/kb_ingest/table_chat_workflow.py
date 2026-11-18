"""Durable per-turn selected-table authorization and bounded background execution."""

import json
import re
import uuid
from copy import deepcopy

from .contract import strict_equal
from .core import StateConflict, canonical, clock, fingerprint
from .ingest.resources import IngestionResources
from .ingest.tables.raw_table_store import RawTableArtifactStore, newest_artifacts_first
from .llm import BudgetExceeded, ModelCallError
from .table_chat_projection import REQUEST_METADATA, chat_detail, list_chats, public_turn
from .table_chat_store import TERMINAL, TableChatStore, checked_id, identity, request_id

MODEL = "gpt-6-sol"
MAX_SELECTED_CELLS = 2_000_000
MAX_HISTORY_BYTES = 128_000
SUMMARY_KEYS = (
    "table_version_id",
    "table_id",
    "source_id",
    "name",
    "original_name",
    "sheet",
    "table_range",
    "row_count",
    "column_count",
)
SAFE_ERRORS = {
    "model_not_configured": "尚未配置表格对话模型，请完成服务端配置后重试",
    "provider_http": "模型服务返回请求错误，请检查服务可用性后主动重试",
    "timeout": "模型请求超时，结果未知；本轮没有自动重发",
    "cancelled": "本轮已取消；此前程序结果仍保留",
    "budget_exhausted": "本轮模型调用、输入输出或执行时间已达到上限，请缩小问题或新开一轮",
    "unsupported_protocol": "该表格模型协议暂不受支持，请修改私有模型配置并重启网站",
    "unsupported_model_protocol": "该表格模型协议暂不受支持，请修改私有模型配置并重启网站",
    "unsupported_model_capability": "该模型接口不支持本次所需能力，请检查配置；本轮未发送请求",
    "provider_authentication": "模型服务鉴权失败，请检查私有配置中的 API key",
    "provider_permission": "模型服务拒绝访问，请检查账户和模型权限",
    "provider_model_unavailable": "模型或接口不存在，请检查服务地址及模型 ID",
    "provider_rate_limit": "模型服务限流；本轮没有自动重发，请稍后再试",
    "provider_server_error": "模型服务暂时失败；本轮没有自动重发",
    "provider_request_rejected": "模型服务拒绝本次协议或参数，请检查该模型支持的能力",
    "context_limit": "对话上下文超过上限，请新建对话或缩小问题",
    "refusal": "模型拒绝了本轮请求，请调整问题后重试",
    "incomplete": "模型回复未完成；此前程序结果仍保留，请缩小问题后重试",
    "provider_protocol": "模型服务返回的响应格式不受支持，请检查服务后重试",
    "tool_limit": "本轮工具调用超过上限，请缩小问题或继续下一轮",
    "cancellation_check_failed": "无法验证本轮执行权限，已停止请求以避免重复执行",
    "tool_failed": "程序分析未完成，请检查原表或缩小分析范围；此前结果仍保留",
    "integrity_failed": "选表版本或对话文件完整性检查失败，已停止分析",
    "execution_failed": "本轮分析执行失败；此前程序结果仍保留，请检查服务后主动重试",
    "model_outcome_unknown": "模型请求已发送但结果尚无法确认；本轮不会自动重发，请先核查请求状态",
    "legacy_model_outcome_unknown": "历史模型请求结果尚无法确认；本轮不会自动重发，请先核查请求状态",
    "completed_without_answer": "模型结果已保存，但本轮回复未发布；此前程序结果仍保留，本轮不会自动重发",
    "model_request_in_progress": "相同模型请求仍有执行者，本轮不会重复发送",
    "saved_result_binding_invalid": "已保存模型结果完整性检查失败，本轮不会重新发送",
    "request_identity_mismatch": "模型请求身份不一致，本轮不会重新发送",
}


def _safe_error(exc):
    code = "execution_failed"
    if isinstance(exc, BudgetExceeded):
        code = "budget_exhausted"
    elif isinstance(exc, ModelCallError):
        reason = getattr(exc, "reason", None)
        if reason in SAFE_ERRORS:
            code = reason
        elif str(exc) in SAFE_ERRORS:
            code = str(exc)
        else:
            code = "provider_protocol"
    elif isinstance(exc, StateConflict):
        code = "integrity_failed"
    return {"code": code, "message": SAFE_ERRORS[code]}


def _bounded_json(value, maximum, message):
    if len(canonical(value).encode("utf-8")) > maximum:
        raise ValueError(message)


def _fields(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError("对话请求字段无效")


def _check_selected_capacity(tables):
    total = 0
    for table in tables:
        rows, columns = table["row_count"], table["column_count"]
        if type(rows) is not int or type(columns) is not int or rows < 1 or columns < 1:
            raise StateConflict("已保存表尺寸无效")
        total += rows * columns
        if total > MAX_SELECTED_CELLS:
            raise ValueError("本轮选表总单元格数超过上限，请分批选择")


def _historical_output(output):
    """Summarize past tool evidence explicitly; retain original turn files intact."""
    if not isinstance(output, dict):
        return {"summary_unavailable": True}
    result = {
        key: deepcopy(output[key])
        for key in (
            "ok",
            "error",
            "table_version_id",
            "engine_version",
            "config",
            "overview",
            "coverage",
            "comparison_mode",
            "joined",
            "unit_conversion",
            "default_header_row_count",
        )
        if key in output
    }
    if isinstance(output.get("tables"), list):
        result["tables"] = [_historical_output(item) for item in output["tables"]]
    columns = []
    for column in output.get("columns", []):
        if not isinstance(column, dict) or "statistics" not in column:
            continue
        item = {
            key: deepcopy(column[key])
            for key in ("column_id", "label", "unit", "counts", "statistics", "warnings")
            if key in column
        }
        for operation in ("histogram", "scatter", "line", "group_statistics"):
            value = column.get(operation)
            if isinstance(value, dict):
                item[operation] = {
                    key: deepcopy(value[key])
                    for key in ("status", "reason", "point_count", "excluded_count", "group_count", "bin_count")
                    if key in value
                }
        columns.append(item)
    if columns:
        result["columns"] = columns
    return result


def _historical_turn(turn):
    result = {key: deepcopy(turn[key]) for key in ("message", "assistant_text", "tables", "status")}
    result["historical_tool_outputs_summarized"] = True
    result["tool_calls"] = [
        {
            **{
                key: deepcopy(call[key])
                for key in ("call_id", "name", "arguments", "status", "artifact_id")
                if key in call
            },
            "output": _historical_output(call.get("output")),
        }
        for call in turn["tool_calls"]
    ]
    return result


def _execution_metadata(files, turn):
    """Read execution truth without opening/resetting budgets or replaying a request."""
    metadata = {key: turn.get(key) for key in REQUEST_METADATA}
    metadata["request_state"] = (
        "unknown" if (turn.get("request_started") or turn.get("model_requests") or turn.get("call_id")) else "not_sent"
    )
    try:
        directory = files.turn_path(turn["chat_id"], turn["turn_id"]) / "execution"
        path = files._safe(directory / "budget.json")
        attempts = files._safe(directory / "attempts.jsonl")
        if not path.exists():
            if attempts.exists() and attempts.stat().st_size:
                metadata["request_state"] = "unknown"
            return metadata
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("execution state")
        requests = state.get("requests", {})
        if not isinstance(requests, dict):
            raise ValueError("execution records")
        if not requests:
            if state.get("pending") or state.get("settled") or (attempts.exists() and attempts.stat().st_size):
                metadata["request_state"] = "unknown"
            return metadata
        rows = []
        for key, row in requests.items():
            if (
                not isinstance(row, dict)
                or row.get("request_hash") != key
                or not isinstance(key, str)
                or not re.fullmatch(r"[0-9a-f]{64}", key)
                or row.get("model") != turn["model"]
                or row.get("call_id") != "model-" + key
                or not isinstance(row.get("service_identity"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["service_identity"])
                or type(row.get("sequence", 0)) is not int
            ):
                raise ValueError("execution identity")
            rows.append(row)
        # A process can die after committing a new request but before its turn event.
        row = max(rows, key=lambda value: value.get("sequence", 0))
        metadata.update({key: row.get(key) for key in REQUEST_METADATA if key != "request_state"})
        if row["state"] == "completed":
            if not isinstance(row.get("result"), dict) or fingerprint(row["result"]) != row.get("result_sha256"):
                raise ValueError("result binding")
            metadata["request_state"] = "completed" if turn["status"] == "completed" else "completed_without_answer"
        elif row["state"] in {"pending", "failed"} and row.get("dispatched") is False:
            metadata["request_state"] = "not_sent"
        elif row["state"] == "failed" and row.get("dispatched") is True:
            metadata["request_state"] = "failed"
        else:
            metadata["request_state"] = "unknown"
    except (OSError, ValueError, KeyError, TypeError, StateConflict):
        metadata.update(request_state="unknown", result_sha256=None)
    return metadata


class TableChatWorkflowMixin:
    resources: IngestionResources
    table_options: dict

    def _table_model_status(self):
        from .core.config import ModelCredentials, credential_status

        return credential_status(
            ModelCredentials(
                api_key=self.table_options.get("api_key", ""),
                base_url=self.table_options.get("base_url") or "https://api.openai.com/v1",
                model=self.table_options.get("model") or MODEL,
                protocol=self.table_options.get("protocol", "responses"),
            )
        )

    def _table_chats(self):
        return TableChatStore(self.store)

    def list_saved_table_versions(self):
        artifacts = RawTableArtifactStore(self.store)
        tables = []
        for tid in artifacts.identifiers("raw"):
            try:
                snapshot = artifacts.load("raw", tid)["payload"]
            except (StateConflict, KeyError):
                continue
            row = {key: snapshot[key] for key in SUMMARY_KEYS}
            row["created_at"] = artifacts.audit("raw", tid)["created_at"]
            tables.append(row)
        return {"tables": newest_artifacts_first(tables, "table_version_id")}

    def create_table_chat(self, payload):
        _fields(payload, {"request_id"})
        rid = request_id(payload["request_id"])
        cid = identity("chat", rid)
        files = self._table_chats()
        with self.store.lock("table-chat:" + cid):
            if not (files.chat_path(cid) / "chat.json").exists():
                now = clock.now()
                files.save_chat(
                    {
                        "chat_id": cid,
                        "request_id": rid,
                        "title": "新对话",
                        "revision": 0,
                        "created_at": now,
                        "updated_at": now,
                        "active_turn_id": None,
                        "turn_ids": [],
                    }
                )
        return self.get_table_chat(cid)

    def list_table_chats(self):
        return list_chats(self._table_chats())

    def get_table_chat(self, chat_id):
        checked_id("chat", chat_id)
        with self.store.lock("table-chat:" + chat_id):
            return chat_detail(self._table_chats(), chat_id, self._table_source_warnings)

    def _table_source_warnings(self, turn):
        warnings = []
        for table in turn.get("tables", []):
            sid = table.get("source_id")
            state = self.store.source_state(sid) if sid else None
            message = None
            if state and state["lifecycle"] in {"deleting", "deleted"}:
                message = "来源已删除"
            elif state and (state["lifecycle"] != "active" or state.get("visibility_generation")):
                try:
                    RawTableArtifactStore(self.store).load("raw", table["table_version_id"])
                except (KeyError, ValueError, OSError, StateConflict):
                    message = "来源已重新解析，旧表格版本已失效"
            if message:
                warnings.append(
                    {"source_id": sid, "table_version_id": table.get("table_version_id"), "message": message}
                )
        return warnings

    def enqueue_table_chat_turn(self, chat_id, payload):
        _fields(payload, {"request_id", "expected_revision", "message", "table_version_ids"})
        checked_id("chat", chat_id)
        rid = request_id(payload["request_id"])
        message, ids = payload["message"], payload["table_version_ids"]
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ValueError("请输入1至8000字符的分析需求")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 10
            or any(not isinstance(tid, str) for tid in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("每轮请选择1至10个不同的已保存表版本")
        for tid in ids:
            checked_id("rawtable", tid)
        if type(payload["expected_revision"]) is not int or payload["expected_revision"] < 0:
            raise ValueError("对话版本必须是非负整数")
        turn_id = identity("turn", [chat_id, rid])
        files = self._table_chats()
        lease = None
        with self.store.lock("table-chat:" + chat_id):
            chat = files.read_chat(chat_id)
            # A lost response must remain retryable even after revision advanced.
            if turn_id in chat["turn_ids"]:
                old, _ = files.read_turn(chat_id, turn_id)
                if old["message"] != message or old["table_version_ids"] != ids:
                    raise StateConflict("同一请求标识不能用于不同消息或选表")
                return public_turn(old)
            if chat["revision"] != payload["expected_revision"]:
                raise StateConflict("对话已变化，请刷新后发送")
            if chat["active_turn_id"] is not None:
                raise StateConflict("当前对话仍在分析，请等待完成或取消")
            if len(chat["turn_ids"]) >= 40:
                raise ValueError("当前对话已达到40轮，请新建对话")
            artifacts = RawTableArtifactStore(self.store)
            # Validate one immutable snapshot at a time; never retain all of them
            # until their combined size is known to fit the execution budget.
            tables, table_hashes = [], {}
            for tid in ids:
                value = artifacts.load("raw", tid)["payload"]
                tables.append({key: value[key] for key in SUMMARY_KEYS})
                _check_selected_capacity(tables)
                table_hashes[tid] = fingerprint(value)
                del value
            history = []
            for previous_id in chat["turn_ids"]:
                previous, _ = files.read_turn(chat_id, previous_id)
                if previous["status"] not in TERMINAL:
                    raise StateConflict("对话状态不一致，请刷新")
                history.append(_historical_turn(previous))
            _bounded_json(history, MAX_HISTORY_BYTES, "历史对话超过本轮上下文上限，请新建对话")
            snapshot = {
                "chat_id": chat_id,
                "turn_id": turn_id,
                "request_id": rid,
                "message": message,
                "table_version_ids": list(ids),
                "tables": tables,
                "history": history,
                "table_hashes": table_hashes,
                "version": "table-chat-v1",
            }
            now = clock.now()
            turn = {
                key: deepcopy(snapshot[key])
                for key in ("chat_id", "turn_id", "request_id", "message", "table_version_ids", "tables")
            }
            turn.update(
                input_sha256=fingerprint(snapshot),
                status="queued",
                assistant_text="",
                error=None,
                created_at=now,
                updated_at=now,
                model=self.table_options.get("model") or MODEL,
                tool_calls=[],
                usage={},
                attempt_token=uuid.uuid4().hex,
                model_requests=0,
                request_started=False,
                request_state="not_sent",
                call_id=None,
                request_hash=None,
                service_identity=None,
                result_sha256=None,
            )
            lease = files.acquire_worker(turn_id)
            if lease is None:
                raise StateConflict("该请求已有执行者，请刷新")
            try:
                directory = files.turn_path(chat_id, turn_id)
                files.write(directory / "input.json", snapshot)
                files.save_turn(turn)
                chat["turn_ids"].append(turn_id)
                chat.update(active_turn_id=turn_id, revision=chat["revision"] + 1, updated_at=now)
                if len(chat["turn_ids"]) == 1:
                    chat["title"] = message.strip()[:80]
                # Only this final pointer publication makes the turn executable.
                files.save_chat(chat)
            except BaseException:
                files.release_worker(lease)
                raise
        self._start_table_chat_turn(chat_id, turn_id, lease)
        return public_turn(turn)

    def _start_table_chat_turn(self, chat_id, turn_id, lease):
        key = "table-chat:" + turn_id
        with self._guard:
            self._running.add(key)

        def run():
            try:
                self._run_table_chat_turn(chat_id, turn_id)
            finally:
                self._table_chats().release_worker(lease)
                with self._guard:
                    self._running.discard(key)

        try:
            future = self.pool.submit(run)

            # A queued executor future cancelled by service.close never enters run.
            def cancelled(future):
                if future.cancelled():
                    self._table_chats().release_worker(lease)
                    with self._guard:
                        self._running.discard(key)

            future.add_done_callback(cancelled)
        except RuntimeError:
            self._table_chats().release_worker(lease)
            with self._guard:
                self._running.discard(key)
            # The durable queued turn is marked interrupted by recovery, not resent.

    def _run_table_chat_turn(self, chat_id, turn_id):
        files = self._table_chats()
        with self.store.lock("table-chat:" + chat_id):
            chat = files.read_chat(chat_id)
            turn, snapshot = files.read_turn(chat_id, turn_id)
            if chat["active_turn_id"] != turn_id or turn["status"] != "queued":
                return
            token = turn["attempt_token"]
            turn.update(status="running", updated_at=clock.now())
            files.save_turn(turn)

        def current():
            chat = files.read_chat(chat_id)
            turn, _ = files.read_turn(chat_id, turn_id)
            return (
                chat,
                turn,
                (chat["active_turn_id"] == turn_id and turn["attempt_token"] == token and turn["status"] == "running"),
            )

        def cancelled():
            if self._stop.is_set():
                return True
            with self.store.lock("table-chat:" + chat_id):
                return not current()[2]

        def on_event(event):
            if not isinstance(event, dict):
                raise ValueError("无效工具事件")
            with self.store.lock("table-chat:" + chat_id):
                _, live, owned = current()
                if not owned or self._stop.is_set():
                    raise StateConflict("当前分析已取消或失去执行权")
                kind = event.get("type")
                if kind == "model_request":
                    state = event.get("request_state")
                    if (
                        state not in {"pending", "sent", "completed"}
                        or event.get("model") != live["model"]
                        or any(
                            not isinstance(event.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", event[key])
                            for key in ("request_hash", "service_identity")
                        )
                        or event.get("call_id") != "model-" + event["request_hash"]
                    ):
                        raise ValueError("模型请求身份无效")
                    if state == "pending":
                        if live.get("call_id") == event["call_id"] or live["model_requests"] >= 8:
                            raise ValueError("重复模型请求或超过请求上限")
                        live["model_requests"] += 1
                    elif any(
                        live.get(key) != event[key] for key in ("call_id", "request_hash", "service_identity")
                    ) or live.get("request_state") != ("pending" if state == "sent" else "sent"):
                        raise ValueError("模型请求状态没有对应执行记录")
                    if state == "completed" and (
                        not isinstance(event.get("result_sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", event["result_sha256"])
                    ):
                        raise ValueError("模型结果缺少完整性绑定")
                    live.update({key: event.get(key) for key in REQUEST_METADATA if key != "request_state"})
                    live.update(request_state=state, request_started=state == "sent")
                elif kind in ("tool_started", "tool_completed"):
                    cid, name, arguments = event.get("call_id"), event.get("name"), event.get("arguments")
                    if (
                        not isinstance(cid, str)
                        or not 1 <= len(cid) <= 200
                        or not isinstance(name, str)
                        or not 1 <= len(name) <= 100
                        or not isinstance(arguments, dict)
                    ):
                        raise ValueError("无效工具调用")
                    _bounded_json(arguments, 16000, "工具参数超过上限")
                    prior = next((call for call in live["tool_calls"] if call["call_id"] == cid), None)
                    if kind == "tool_started":
                        if prior is not None or len(live["tool_calls"]) >= 16:
                            raise ValueError("重复工具调用或超过调用上限")
                        live["tool_calls"].append(
                            {
                                "call_id": cid,
                                "name": name,
                                "arguments": deepcopy(arguments),
                                "output": None,
                                "status": "running",
                            }
                        )
                    else:
                        if (
                            prior is None
                            or prior["status"] != "running"
                            or prior["name"] != name
                            or not strict_equal(prior["arguments"], arguments)
                        ):
                            raise ValueError("工具结果没有对应调用")
                        output = event.get("output")
                        _bounded_json(output, 64000, "工具结果摘要超过上限")
                        if event.get("artifact") is not None:
                            prior["artifact_id"] = files.put_artifact(chat_id, turn_id, cid, event["artifact"])
                        prior.update(output=deepcopy(output), status="completed")
                else:
                    raise ValueError("未知执行事件")
                live["updated_at"] = clock.now()
                files.save_turn(live)

        try:
            _check_selected_capacity(snapshot["tables"])
            artifacts = RawTableArtifactStore(self.store)
            snapshots = {tid: artifacts.load("raw", tid)["payload"] for tid in snapshot["table_version_ids"]}
            if {tid: fingerprint(value) for tid, value in snapshots.items()} != snapshot["table_hashes"]:
                raise StateConflict("选表版本已损坏")
            from .table_chat_engine import execute_table_chat

            directory = files._safe(files.turn_path(chat_id, turn_id) / "execution")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            result = execute_table_chat(
                options=self.resources.model_options(self.table_options),
                snapshots=snapshots,
                history=deepcopy(snapshot["history"]),
                message=snapshot["message"],
                run_dir=directory,
                on_event=on_event,
                cancel_check=cancelled,
            )
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("assistant_text"), str)
                or len(result["assistant_text"]) > 32000
                or result.get("model") != turn["model"]
                or not isinstance(result.get("usage"), (dict, list))
            ):
                raise ValueError("无效模型结果")
            _bounded_json(result["usage"], 32000, "用量记录超过上限")
            with self.store.lock("table-chat:" + chat_id):
                chat, live, owned = current()
                if not owned:
                    return
                live.update(
                    status="interrupted" if self._stop.is_set() else "completed",
                    assistant_text="" if self._stop.is_set() else result["assistant_text"],
                    usage=result["usage"],
                    request_started=False,
                    error={"code": "interrupted", "message": "服务已停止，本轮未自动重发"}
                    if self._stop.is_set()
                    else None,
                )
                self._finish_table_chat(files, chat, live)
        except Exception as exc:
            with self.store.lock("table-chat:" + chat_id):
                chat, live, owned = current()
                if not owned:
                    return
                live.update(
                    status="interrupted" if self._stop.is_set() else "failed",
                    error={"code": "interrupted", "message": "服务已停止，本轮未自动重发"}
                    if self._stop.is_set()
                    else _safe_error(exc),
                )
                self._finish_table_chat(files, chat, live)

    @staticmethod
    def _finish_table_chat(files, chat, turn):
        now = clock.now()
        turn.update(_execution_metadata(files, turn))
        if turn["request_state"] in {"unknown", "completed_without_answer"}:
            code = "model_outcome_unknown" if turn["request_state"] == "unknown" else "completed_without_answer"
            turn["error"] = {"code": code, "message": SAFE_ERRORS[code]}
            if turn["status"] == "completed":
                turn.update(status="failed", assistant_text="")
        turn["request_started"] = turn["request_state"] == "unknown"
        turn.update(updated_at=now, attempt_token=None)
        files.save_turn(turn)
        chat.update(active_turn_id=None, revision=chat["revision"] + 1, updated_at=now)
        files.save_chat(chat)

    def cancel_table_chat_turn(self, chat_id, turn_id):
        files = self._table_chats()
        checked_id("chat", chat_id)
        checked_id("turn", turn_id)
        with self.store.lock("table-chat:" + chat_id):
            chat = files.read_chat(chat_id)
            if turn_id not in chat["turn_ids"]:
                raise KeyError("对话轮次不存在")
            turn, _ = files.read_turn(chat_id, turn_id)
            if turn["status"] not in TERMINAL:
                turn.update(
                    status="cancelled", error={"code": "cancelled", "message": "本轮已取消；已产生的程序结果仍保留"}
                )
                self._finish_table_chat(files, chat, turn)
            return public_turn(turn)

    def get_table_chat_artifact(self, chat_id, turn_id, artifact_id):
        files = self._table_chats()
        checked_id("chat", chat_id)
        checked_id("turn", turn_id)
        checked_id("artifact", artifact_id)
        with self.store.lock("table-chat:" + chat_id):
            chat = files.read_chat(chat_id)
            if turn_id not in chat["turn_ids"]:
                raise KeyError("对话轮次不存在")
            turn, _ = files.read_turn(chat_id, turn_id)
            warnings = self._table_source_warnings(turn)
            if warnings:
                raise StateConflict(warnings[0]["message"])
            if not any(call.get("artifact_id") == artifact_id for call in turn["tool_calls"]):
                raise KeyError("程序结果不属于当前对话轮次")
            return files.read_artifact(chat_id, turn_id, artifact_id)

    def recover_table_chat_turns(self):
        files = self._table_chats()
        for chat_id in files.chat_ids():
            try:
                with self.store.lock("table-chat:" + chat_id):
                    chat = files.read_chat(chat_id)
                    tid = chat["active_turn_id"]
                    if tid is None:
                        continue
                    lease = files.acquire_worker(tid)
                    if lease is None:
                        continue
                    try:
                        turn, _ = files.read_turn(chat_id, tid)
                        if turn["status"] not in TERMINAL:
                            turn.update(
                                status="interrupted",
                                error={
                                    "code": "interrupted",
                                    "message": "服务中断，本轮没有自动重新发送模型请求。可发送新消息重试。",
                                },
                            )
                        self._finish_table_chat(files, chat, turn)
                    finally:
                        files.release_worker(lease)
            except (OSError, ValueError, KeyError, StateConflict):
                # Damaged records fail closed when requested; never regenerate them.
                continue
