"""Existing buffered gateway input validation, independent of sending and persistence."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from .model_inputs import ModelInputError, validate_controlled_input

if TYPE_CHECKING:
    from .model_requests import ModelRequestContext


class ModelDispatchError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def _fields(value: object, allowed: set[str], required: set[str] | tuple[str, ...] = ()) -> dict[str, object]:
    if type(value) is not dict or set(value) - set(allowed) or not set(required) <= set(value):
        raise ModelDispatchError("invalid_request")
    return value


def _string(value: object, limit: int = 262144) -> None:
    if type(value) is not str or len(value) > limit:
        raise ModelDispatchError("invalid_request")


class DispatchValidation:
    context: ModelRequestContext
    max_body_bytes: int
    max_tools: int
    controlled_inputs: bool

    def _validate_message_content(self, content: object, item: dict[str, object]) -> None:
        if type(content) is str:
            _string(content)
        elif type(content) is list and 0 < len(content) <= 128:
            for part in content:
                if type(part) is dict and part.get("type") in ("input_file", "input_image"):
                    if not self.controlled_inputs or item["role"] != "user":
                        raise ModelDispatchError("unsupported_input")
                    continue
                _fields(part, {"type", "text", "annotations", "logprobs"}, {"type", "text"})
                if part.get("annotations", []) != [] or part.get("logprobs", []) != []:
                    raise ModelDispatchError("unsupported_input")
                if part["type"] not in ("input_text", "output_text"):
                    raise ModelDispatchError("unsupported_input")
                _string(part["text"])
        else:
            raise ModelDispatchError("invalid_request")

    def _validate_message(self, item: dict[str, object]) -> None:
        _fields(item, {"type", "role", "content", "id", "status", "phase"}, {"role", "content"})
        if "id" in item:
            _string(item["id"], 128)
        if "status" in item and item["status"] not in ("in_progress", "completed", "incomplete"):
            raise ModelDispatchError("invalid_request")
        if "phase" in item and item["phase"] not in (None, "commentary", "final_answer"):
            raise ModelDispatchError("invalid_request")
        if item["role"] not in ("user", "assistant", "system", "developer"):
            raise ModelDispatchError("invalid_request")
        content = item["content"]
        self._validate_message_content(content, item)

    def _validate_reasoning(self, item: dict[str, object]) -> None:
        _fields(
            item,
            {"type", "id", "summary", "content", "encrypted_content", "status"},
            {"type", "id", "summary"},
        )
        _string(item["id"], 128)
        if "encrypted_content" in item and item["encrypted_content"] is not None:
            _string(item["encrypted_content"])
        if "status" in item and item["status"] not in ("in_progress", "completed", "incomplete"):
            raise ModelDispatchError("invalid_request")
        for field, part_type in (("summary", "summary_text"), ("content", "reasoning_text")):
            parts = item.get(field, [])
            if type(parts) is not list or len(parts) > 128:
                raise ModelDispatchError("invalid_request")
            for part in parts:
                _fields(part, {"type", "text"}, {"type", "text"})
                if part["type"] != part_type:
                    raise ModelDispatchError("unsupported_input")
                _string(part["text"])

    def _validate_call_values(self, item: dict[str, object]) -> None:
        for key in set(item) - {"type"}:
            _string(item[key])

    def _validate_inputs(self, request: dict[str, object]) -> None:
        inputs = request["input"]
        if type(inputs) is str:
            _string(inputs)
        elif type(inputs) is list and 0 < len(inputs) <= 128:
            for item in inputs:
                if type(item) is not dict:
                    raise ModelDispatchError("invalid_request")
                kind = item.get("type", "message")
                if kind == "message":
                    self._validate_message(item)
                elif kind == "reasoning":
                    self._validate_reasoning(item)
                elif kind == "function_call":
                    _fields(
                        item,
                        {"type", "call_id", "name", "arguments", "id", "status"},
                        {"type", "call_id", "name", "arguments"},
                    )
                    self._validate_call_values(item)
                elif kind == "function_call_output":
                    _fields(item, {"type", "call_id", "output", "id", "status"}, {"type", "call_id", "output"})
                    self._validate_call_values(item)
                else:
                    raise ModelDispatchError("unsupported_input")
        else:
            raise ModelDispatchError("invalid_request")

    def _validate_tools(self, request: dict[str, object]) -> None:
        tools = request.get("tools", [])
        if type(tools) is not list or len(tools) > self.max_tools:
            raise ModelDispatchError("invalid_tools")
        names = set()
        for tool in tools:
            _fields(tool, {"type", "name", "description", "parameters", "strict"}, {"type", "name", "parameters"})
            if (
                tool["type"] != "function"
                or type(tool["name"]) is not str
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool["name"])
            ):
                raise ModelDispatchError("unsupported_tool")
            if tool["name"] in names:
                raise ModelDispatchError("invalid_tools")
            names.add(tool["name"])
            if type(tool["parameters"]) is not dict:
                raise ModelDispatchError("invalid_tools")
            if "description" in tool:
                _string(tool["description"], 4096)
            if "strict" in tool and type(tool["strict"]) is not bool:
                raise ModelDispatchError("invalid_tools")
        self._validate_tool_choice(request, names)

    def _validate_tool_choice(self, request: dict[str, object], names: set[str]) -> None:
        choice = request.get("tool_choice", "auto")
        if type(choice) is dict:
            _fields(choice, {"type", "name"}, {"type", "name"})
            if choice["type"] != "function" or choice["name"] not in names:
                raise ModelDispatchError("invalid_tool_choice")
        elif choice not in ("auto", "none", "required"):
            raise ModelDispatchError("invalid_tool_choice")

    def _validate_controls(self, request: dict[str, object]) -> None:
        if "parallel_tool_calls" in request and type(request["parallel_tool_calls"]) is not bool:
            raise ModelDispatchError("invalid_request")
        if "reasoning" in request:
            reasoning = _fields(request["reasoning"], {"effort"}, {"effort"})
            if reasoning["effort"] not in ("none", "minimal", "low", "medium", "high", "xhigh"):
                raise ModelDispatchError("invalid_request")
        if "text" in request:
            text = _fields(request["text"], {"format"}, {"format"})
            fmt = _fields(text["format"], {"type", "name", "schema", "strict"}, {"type"})
            if fmt["type"] == "text":
                _fields(fmt, {"type"}, {"type"})
            elif fmt["type"] == "json_schema":
                _fields(fmt, {"type", "name", "schema", "strict"}, {"type", "name", "schema"})
                _string(fmt["name"], 64)
                if type(fmt["schema"]) is not dict or ("strict" in fmt and type(fmt["strict"]) is not bool):
                    raise ModelDispatchError("invalid_request")
            else:
                raise ModelDispatchError("invalid_request")

    def _validate_size(self, request: dict[str, object]) -> None:
        try:
            encoded = json.dumps(request, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError, RecursionError):
            raise ModelDispatchError("invalid_request") from None
        if self.controlled_inputs:

            def text_payload(value: object) -> object:
                if isinstance(value, dict):
                    return {k: text_payload(v) for k, v in value.items() if k not in ("file_data", "image_url")}
                if isinstance(value, list):
                    return [text_payload(v) for v in value]
                return value

            if len(json.dumps(text_payload(request), ensure_ascii=False).encode()) > 262144:
                raise ModelDispatchError("body_too_large", 413)
        if len(encoded) > self.max_body_bytes:
            raise ModelDispatchError("body_too_large", 413)

    def validate(self, request: dict[str, object]) -> dict[str, object]:
        _fields(
            request,
            {
                "input",
                "model",
                "service_tier",
                "max_output_tokens",
                "stream",
                "store",
                "instructions",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
                "reasoning",
                "text",
                "include",
            },
            {"input", "max_output_tokens"},
        )
        self._validate_size(request)
        policy = self.context.policy
        if (
            request.get("model", policy.model) != policy.model
            or request.get("service_tier", policy.service_tier) != policy.service_tier
        ):
            raise ModelDispatchError("fixed_model_required")
        if request.get("stream", False) is not False or request.get("store", False) is not False:
            raise ModelDispatchError("buffered_request_required")
        output = request["max_output_tokens"]
        if type(output) is not int or not 0 < output <= (8000 if self.controlled_inputs else policy.max_output_tokens):
            raise ModelDispatchError("invalid_output_bound")
        if "include" in request and request["include"] not in ([], ["reasoning.encrypted_content"]):
            raise ModelDispatchError("unsupported_include")
        if "instructions" in request:
            _string(request["instructions"])
        self._validate_inputs(request)
        self._validate_tools(request)
        self._validate_controls(request)
        if self.controlled_inputs:
            try:
                validate_controlled_input(request, max_pages=4)
            except ModelInputError:
                raise ModelDispatchError("unsupported_input") from None
        return {**request, "model": policy.model, "service_tier": policy.service_tier, "stream": False, "store": False}
