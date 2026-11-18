"""Small buffered OpenAI-style protocol adapter; never a provider capability probe.

Internal callers use Responses shapes. Chat Completions supports text/images,
function tools/results and json_schema output. It cannot carry Responses files,
opaque reasoning or streaming; no external service compatibility is inferred.
"""

from copy import deepcopy
from typing import Any, Never

from ..core.endpoints import PROTOCOLS
from ..core.errors import ProviderProtocolError

type Payload = dict[str, Any]


def _reject(code: str = "provider_protocol") -> Never:
    raise ProviderProtocolError(code)


def _content(value: object) -> str | list[Payload]:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        _reject()
    parts: list[Payload] = []
    for part in value:
        if not isinstance(part, dict):
            _reject()
        if part.get("type") in ("input_text", "output_text") and isinstance(part.get("text"), str):
            parts.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "input_image" and isinstance(part.get("image_url"), str):
            image = {"url": part["image_url"]}
            if "detail" in part:
                image["detail"] = part["detail"]
            parts.append({"type": "image_url", "image_url": image})
        else:
            _reject("unsupported_model_capability")
    return parts


def _append_tool_call(messages: list[Payload], calls: set[str], item: Payload) -> None:
    cid, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
    if (
        not isinstance(cid, str)
        or not cid
        or not all(isinstance(v, str) and v for v in (name, arguments))
        or cid in calls
    ):
        _reject()
    calls.add(cid)
    call = {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}
    if messages and messages[-1]["role"] == "assistant":
        messages[-1].setdefault("tool_calls", []).append(call)
    else:
        messages.append({"role": "assistant", "content": None, "tool_calls": [call]})


def _append_item(messages: list[Payload], calls: set[str], results: set[str], item: Payload) -> None:
    kind = item.get("type")
    if kind == "reasoning":
        return  # Responses-only opaque items cannot be sent to Chat.
    if kind == "function_call":
        _append_tool_call(messages, calls, item)
    elif kind == "function_call_output":
        cid = item.get("call_id")
        if cid not in calls or cid in results or not isinstance(item.get("output"), str):
            _reject()
        results.add(cid)
        messages.append({"role": "tool", "tool_call_id": cid, "content": item["output"]})
    elif kind in (None, "message") and item.get("role") in ("system", "developer", "user", "assistant"):
        messages.append({"role": item["role"], "content": _content(item.get("content"))})
    else:
        _reject("unsupported_model_capability")


def _chat_messages(request: Payload) -> list[Payload]:
    messages: list[Payload] = []
    if request.get("instructions"):
        if not isinstance(request["instructions"], str):
            _reject()
        messages.append({"role": "system", "content": request["instructions"]})
    items = request.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list):
        _reject()
    calls: set[str] = set()
    results: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            _reject()
        _append_item(messages, calls, results, item)
    if calls != results:
        _reject()
    return messages


def _tools(request: Payload, wire: Payload) -> None:
    if "tools" in request:
        if not isinstance(request["tools"], list):
            _reject()
        tools = []
        for tool in request["tools"]:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                _reject("unsupported_model_capability")
            if not isinstance(tool.get("name"), str) or not isinstance(tool.get("parameters"), dict):
                _reject()
            tools.append(
                {
                    "type": "function",
                    "function": {
                        k: deepcopy(tool[k]) for k in ("name", "description", "parameters", "strict") if k in tool
                    },
                }
            )
        wire["tools"] = tools
    if "tool_choice" in request:
        choice = request["tool_choice"]
        if choice in ("auto", "none", "required"):
            wire["tool_choice"] = choice
        elif isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
            wire["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        else:
            _reject("unsupported_model_capability")


def _format(request: Payload, wire: Payload) -> None:
    if "text" in request:
        text = request["text"]
        fmt = text.get("format") if isinstance(text, dict) else None
        if not isinstance(fmt, dict):
            _reject()
        if fmt.get("type") == "json_schema" and isinstance(fmt.get("schema"), dict):
            wire["response_format"] = {
                "type": "json_schema",
                "json_schema": {k: deepcopy(fmt[k]) for k in ("name", "description", "strict", "schema") if k in fmt},
            }
        elif fmt.get("type") in ("json_object", "text"):
            wire["response_format"] = {"type": fmt["type"]}
        else:
            _reject("unsupported_model_capability")


def encode_request(request: Payload, protocol: str = "responses") -> tuple[str, Payload]:
    """Return endpoint and wire request; reject unsupported features before send."""
    if protocol not in PROTOCOLS:
        _reject("unsupported_model_protocol")
    if not isinstance(request, dict) or not isinstance(request.get("model"), str) or not request["model"]:
        _reject()
    if request.get("stream") not in (None, False):
        _reject("unsupported_model_capability")
    if protocol == "responses":
        return "/responses", deepcopy(request)
    if request.get("previous_response_id") or request.get("background"):
        _reject("unsupported_model_capability")
    wire: Payload = {"model": request["model"], "messages": _chat_messages(request), "stream": False}
    if "max_output_tokens" in request:
        wire["max_completion_tokens"] = request["max_output_tokens"]
    for name in ("temperature", "top_p", "parallel_tool_calls"):
        if name in request:
            wire[name] = request[name]
    # The standard Chat field preserves the requested effort. A compatible
    # provider must support it when its selected model uses reasoning controls.
    if isinstance(request.get("reasoning"), dict) and request["reasoning"].get("effort"):
        wire["reasoning_effort"] = request["reasoning"]["effort"]
    _tools(request, wire)
    _format(request, wire)
    return "/chat/completions", wire


def _message_content(message: Payload) -> list[Payload]:
    output: list[Payload] = []
    content = message.get("content")
    if content is not None:
        if not isinstance(content, str):
            _reject()
        if content:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": content}],
                }
            )
    return output


def _tool_output(message: Payload, output: list[Payload]) -> list[Payload]:
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        _reject()
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
            _reject()
        cid, name, arguments = call.get("id"), call["function"].get("name"), call["function"].get("arguments")
        if not all(isinstance(v, str) and v for v in (cid, name, arguments)) or cid in seen:
            _reject()
        seen.add(cid)
        output.append(
            {"type": "function_call", "call_id": cid, "name": name, "arguments": arguments, "status": "completed"}
        )
    return calls


def _metadata(payload: Payload, result: Payload) -> None:
    for key in ("id", "model", "service_tier"):
        if isinstance(payload.get(key), str):
            result[key] = payload[key]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        counts = {
            new: usage.get(old)
            for old, new in (
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
                ("total_tokens", "total_tokens"),
            )
        }
        if all(type(v) is int and v >= 0 for v in counts.values()):
            result["usage"] = counts


def _choice(payload: Payload) -> tuple[Payload, str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        _reject()
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        _reject()
    finish = choice.get("finish_reason")
    if finish not in ("stop", "tool_calls", "length", "content_filter"):
        _reject()
    if message.get("refusal"):
        _reject("provider_refusal")
    return message, str(finish)


def decode_response(payload: object, protocol: str = "responses", requested_model: str | None = None) -> Payload:
    """Normalize successful JSON without requiring usage, receipt IDs or prices.

    The requested model is not substituted for missing provider identity metadata.
    Callers retain the requested model independently in their execution record.
    """
    if protocol not in PROTOCOLS:
        _reject("unsupported_model_protocol")
    if not isinstance(payload, dict):
        _reject()
    if protocol == "responses":
        if not isinstance(payload.get("output"), list) or payload.get("status") not in (
            "completed",
            "incomplete",
            "failed",
            "cancelled",
        ):
            _reject()
        return deepcopy(payload)
    message, finish = _choice(payload)
    output = _message_content(message)
    calls = _tool_output(message, output)
    if not output or (finish == "tool_calls" and not calls) or (calls and finish == "stop"):
        _reject()
    result: Payload = {"status": "completed" if finish in ("stop", "tool_calls") else "incomplete", "output": output}
    if finish in ("length", "content_filter"):
        result["incomplete_details"] = {"reason": "max_output_tokens" if finish == "length" else "content_filter"}
    _metadata(payload, result)
    return result


def provider_error_code(status: int, payload: object = None) -> str:
    """Public error category; never returns provider text, URL, body or secrets."""
    if status == 401:
        return "provider_authentication"
    if status == 403:
        return "provider_permission"
    if status == 404:
        return "provider_model_unavailable"
    if status == 429:
        return "provider_rate_limit"
    if status >= 500:
        return "provider_server_error"
    return "provider_request_rejected"
