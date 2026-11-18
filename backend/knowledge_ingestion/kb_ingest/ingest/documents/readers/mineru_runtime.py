"""Read the selected CLI's installed package identity without loading models.

Distribution RECORD hashes identify installed wheel metadata. They do not claim
to verify every installed executable byte; model weights have a separate check.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

_PINNED = {"mineru": "4.0.9", "docvortex": "0.5.5"}
_OPTIONAL = ("onnxruntime", "mineru-llama-cpp", "mineru-vl-utils")
_PROBE = r"""
import hashlib
from importlib import metadata
import json
import sys

packages = {}
for name in ("mineru", "docvortex", "onnxruntime", "mineru-llama-cpp", "mineru-vl-utils"):
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        packages[name] = {"status": "not_installed"}
        continue
    record = dist.read_text("RECORD")
    packages[name] = {"version": dist.version,
                      "record_sha256": hashlib.sha256(record.encode()).hexdigest() if record else None}
print(json.dumps({"python": {"version": ".".join(map(str, sys.version_info[:3])),
                             "implementation": sys.implementation.name}, "packages": packages}))
"""


def _interpreter(command: list[str]) -> str | None:
    if not command:
        return None
    try:
        with Path(command[0]).open("rb") as stream:
            line = stream.readline(4097)
            trampoline = None
            if line == b"#!/bin/sh\n":
                trampoline = (stream.readline(4097), stream.readline(4097))
        if len(line) > 4096 or not line.startswith(b"#!"):
            return None
        if trampoline is not None:
            # uv/pip use this Python/shell preamble for paths with spaces or
            # long shebangs. Inspect its literal interpreter; never run shell.
            launch, close = trampoline
            prefix, suffix = b"'''exec' ", b' "$0" "$@"\n'
            if (
                len(launch) > 4096
                or not launch.startswith(prefix)
                or not launch.endswith(suffix)
                or close != b"' '''\n"
            ):
                return None
            quoted = launch[len(prefix) : -len(suffix)].decode("utf-8")
            parts = shlex.split(quoted)
            if len(parts) != 1 or any(ord(c) < 32 or ord(c) == 127 for c in parts[0]):
                return None
            executable = parts[0]
            # Accept uv's shell-quoted literal or pip's simple double quotes.
            # Double-quoted substitutions/escapes must not become a different
            # executable when the real CLI is subsequently launched.
            if quoted != shlex.quote(executable) and not (
                quoted == '"' + executable + '"' and not any(c in executable for c in ('"', "$", "`", "\\"))
            ):
                return None
            if not Path(executable).is_absolute():
                return None
        else:
            parts = shlex.split(line[2:].decode("utf-8").strip())
            if not parts:
                return None
            if Path(parts[0]).name == "env":
                parts = parts[1:]
                if parts and parts[0] == "-S":
                    parts = parts[1:]
                if len(parts) != 1 or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", parts[0]):
                    return None
                executable = shutil.which(parts[0])
            else:
                if len(parts) != 1 or not Path(parts[0]).is_absolute():
                    return None
                executable = parts[0]
        if (
            not executable
            or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(executable).name)
            or not Path(executable).is_file()
            or not os.access(executable, os.X_OK)
        ):
            return None
        # Keep the venv symlink path: resolving it would probe global packages.
        return executable
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _not_ready(reason: str) -> dict:
    return {"status": "not_ready", "reason": reason}


def probe_mineru_runtime(command: list[str], *, timeout_seconds: float = 5.0) -> dict:
    """Return bounded, sanitized identity from the selected Python environment."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 5
    ):
        return _not_ready("mineru_runtime_probe_timeout_invalid")
    interpreter = _interpreter(command)
    if interpreter is None:
        return _not_ready("mineru_python_launcher_unsupported")
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [interpreter, "-I", "-B", "-c", _PROBE],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                return _not_ready("mineru_runtime_probe_timeout")
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            if process.returncode != 0:
                return _not_ready("mineru_runtime_probe_failed")
            output.seek(0)
            raw = output.read(16385)
        if len(raw) > 16384:
            return _not_ready("mineru_runtime_metadata_invalid")
        data = json.loads(raw)
        python = data["python"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", python["version"]) or python["implementation"] not in {"cpython", "pypy"}:
            raise ValueError("invalid interpreter metadata")
        packages = {}
        for name in (*_PINNED, *_OPTIONAL):
            package = data["packages"][name]
            if package == {"status": "not_installed"} and name in _OPTIONAL:
                packages[name] = package
                continue
            version, record = package["version"], package["record_sha256"]
            if (
                not isinstance(version, str)
                or not re.fullmatch(r"[A-Za-z0-9.!+_-]{1,100}", version)
                or not isinstance(record, str)
                or not re.fullmatch(r"[a-f0-9]{64}", record)
            ):
                raise ValueError("invalid distribution metadata")
            if name in _PINNED and version != _PINNED[name]:
                return _not_ready("mineru_runtime_version_unpinned")
            packages[name] = {"version": version, "record_sha256": record}
        return {
            "status": "available",
            "python": {"version": python["version"], "implementation": python["implementation"]},
            "packages": packages,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return _not_ready("mineru_runtime_metadata_invalid")
