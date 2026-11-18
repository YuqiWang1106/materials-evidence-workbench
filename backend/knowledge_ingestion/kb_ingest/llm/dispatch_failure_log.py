"""Redacted diagnostics for model dispatch failures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import TracebackType


class RedactedDispatchError(Exception):
    """Logging-only representation; the original exception is not logged."""


def safe_error_id(workspace_id: str, project_id: str, call_id: str) -> str:
    payload = json.dumps([workspace_id, project_id, call_id], ensure_ascii=True, separators=(",", ":"))
    return "err-" + hashlib.sha256(payload.encode("ascii")).hexdigest()[:24]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


_ROOT = Path(__file__).resolve().parent.parent
_SAFE_FILES = {
    str(_ROOT / name): "kb_ingest/" + name
    for name in (
        "project_model_dispatch.py",
        "llm/project_model_dispatch.py",
        "llm/http.py",
        "llm/provider_protocol.py",
        "llm/dispatch_results.py",
    )
}
_SAFE_FUNCTIONS = frozenset(
    {"dispatch", "_notify_dispatch", "_prepare_dispatch", "_decode_response", "post_bounded", "_strict_provider_json"}
)


def safe_stack(trace: TracebackType | None) -> str:
    lines = ["Original traceback frame summary (identifiers hashed; exception text removed):"]
    seen: set[int] = set()
    while trace is not None and len(seen) < 64:
        identity = id(trace)
        if identity in seen:
            break
        seen.add(identity)
        code = trace.tb_frame.f_code
        filename = _SAFE_FILES.get(code.co_filename, "external_file_hash=" + _digest(code.co_filename))
        function = code.co_name if code.co_name in _SAFE_FUNCTIONS else "function_hash=" + _digest(code.co_name)
        lines.append(f"  file={filename} line={trace.tb_lineno} function={function}")
        trace = trace.tb_next
    if trace is not None:
        lines.append("  additional frames omitted")
    return "\n".join(lines)


def safe_exc_info() -> tuple[type[RedactedDispatchError], RedactedDispatchError, None]:
    return RedactedDispatchError, RedactedDispatchError("Original exception details redacted"), None
