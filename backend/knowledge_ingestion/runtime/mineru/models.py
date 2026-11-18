#!/usr/bin/env python3
"""Prepare the pinned MinerU Standard release in a dedicated model volume.

Only the download command accesses the network. Verification is read-only and
checks the trusted release, every file hash, and MinerU's offline readiness.
This module uses only Python's standard library; mineru-kit is a subprocess.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

RELEASE_FILE = Path(__file__).with_name("standard-models.lock.json")
MARKER = ".mineru_complete"
LOCK = ".standard-download.lock"
CHUNK_SIZE = 1024 * 1024
REPOSITORIES = {
    "MinerU-4_models_onnx": "opendatalab/MinerU-4_models_onnx",
    "MinerU2.5-Pro-2605-1.2B-GGUF": "jinzhenj/MinerU2.5-Pro-2605-1.2B-GGUF",
}


class ModelError(Exception):
    """An installation is incomplete or differs from the trusted release."""


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ModelError("unsafe model path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ModelError("unsafe model path")
    return path


def validate_release(release: dict) -> dict:
    try:
        if (
            release["schema_version"] != 1
            or release["tier"] != "standard"
            or release["small_backend"] != "onnx"
            or release["vlm_engine"] != "llama-cpp"
            or release["mineru_version"] != "4.0.9"
        ):
            raise ModelError("unsupported model release")
        models = release["models"]
        if not isinstance(models, list) or len(models) != len(REPOSITORIES):
            raise ModelError("Standard requires both pinned model repositories")
        seen_repos = set()
        for model in models:
            repo = model["repo"]
            if repo not in REPOSITORIES or repo in seen_repos:
                raise ModelError("unexpected or duplicate model repository")
            seen_repos.add(repo)
            source = model["source"]
            if (
                source["provider"] != "huggingface"
                or source["repo_id"] != REPOSITORIES[repo]
                or not re.fullmatch(r"[0-9a-f]{40}", source["revision"])
            ):
                raise ModelError("model source must use the pinned public repository and commit")
            expected_marker = repo.endswith("-GGUF")
            if model["completion_marker"] is not expected_marker:
                raise ModelError("incorrect model completion protocol")
            if not isinstance(model["files"], list) or not model["files"]:
                raise ModelError("model file list is empty")
            seen_files = set()
            for entry in model["files"]:
                path = str(_relative(entry["path"]))
                if path in seen_files or path == MARKER or path.endswith(".part"):
                    raise ModelError("duplicate or reserved model file path")
                seen_files.add(path)
                if (
                    type(entry["bytes"]) is not int
                    or entry["bytes"] <= 0
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                ):
                    raise ModelError("invalid model size or SHA-256")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ModelError("invalid model release manifest") from exc
    return release


def load_release() -> dict:
    try:
        return validate_release(json.loads(RELEASE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ModelError("trusted model release is unreadable") from exc


def _target(root: Path, relative: str) -> Path:
    """Reject symlinks in every path component within the dedicated volume."""
    path = root
    for part in _relative(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ModelError("symlink is not allowed in model volume")
    return path


def _locations(root: Path, manifest: Path) -> tuple[Path, Path]:
    root = Path(os.path.abspath(root))
    manifest = Path(os.path.abspath(manifest))
    if root.is_symlink() or root == Path(root.anchor):
        raise ModelError("root must be a dedicated directory, not a symlink or filesystem root")
    try:
        relative = manifest.relative_to(root).as_posix()
    except ValueError as exc:
        raise ModelError("manifest must be inside the model root") from exc
    if relative.startswith("models/") or relative in (LOCK, MARKER):
        raise ModelError("manifest cannot overwrite model assets or bootstrap state")
    return root, _target(root, relative)


@contextmanager
def _locked(root: Path, *, write: bool):
    lock = _target(root, LOCK)
    if write:
        root.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open("a+b" if write else "rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
    except FileNotFoundError as exc:
        raise ModelError("model volume has not been prepared") from exc


def _matches(path: Path, entry: dict) -> bool:
    if not path.is_file() or path.stat().st_size != entry["bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest() == entry["sha256"]


def _files(root: Path, release: dict):
    for model in release["models"]:
        for entry in model["files"]:
            yield model, entry, _target(root, f"models/{model['repo']}/{entry['path']}")


def _verify_files(root: Path, release: dict) -> None:
    for model, entry, path in _files(root, release):
        if not _matches(path, entry):
            raise ModelError(f"missing or corrupt model file: {model['repo']}/{entry['path']}")


def _markers(root: Path, release: dict):
    for model in release["models"]:
        if model["completion_marker"]:
            yield _target(root, f"models/{model['repo']}/{MARKER}")


def _invalidate(root: Path, manifest: Path, release: dict) -> None:
    for marker in _markers(root, release):
        marker.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)


def _download_file(path: Path, model: dict, entry: dict, opener) -> None:
    if _matches(path, entry):
        return
    partial = path.with_name(path.name + ".part")
    if partial.is_symlink():
        raise ModelError("symlink partial download is not allowed")
    path.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists() and partial.stat().st_size >= entry["bytes"]:
        if _matches(partial, entry):
            os.replace(partial, path)
            return
        partial.unlink()
    offset = partial.stat().st_size if partial.exists() else 0
    source = model["source"]
    url = f"https://huggingface.co/{source['repo_id']}/resolve/{source['revision']}/" + quote(entry["path"], safe="/")
    headers = {"Accept-Encoding": "identity", "User-Agent": "materials-knowledge-mineru-bootstrap/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        with opener(Request(url, headers=headers), timeout=60) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise ModelError("model download redirected away from HTTPS")
            status = response.status
            if status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if (
                    not match
                    or int(match[1]) != offset
                    or int(match[2]) != entry["bytes"] - 1
                    or int(match[3]) != entry["bytes"]
                ):
                    raise ModelError("invalid range response for partial model download")
            elif status == 200:
                offset = 0  # A server may ignore Range; replace the partial instead of appending.
            else:
                raise ModelError("unexpected HTTP response for model download")
            with partial.open("ab" if offset else "wb") as stream:
                written = offset
                while chunk := response.read(CHUNK_SIZE):
                    written += len(chunk)
                    if written > entry["bytes"]:
                        raise ModelError("download exceeds the pinned model size")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
    except ModelError:
        raise
    except Exception as exc:
        # Do not print redirects, response bodies, or provider URLs that may carry signed tokens.
        raise ModelError(f"download interrupted: {model['repo']}/{entry['path']} ({type(exc).__name__})") from exc
    if not _matches(partial, entry):
        if partial.stat().st_size == entry["bytes"]:
            partial.unlink()  # A complete corrupt partial must not be resumed forever.
        raise ModelError(f"download incomplete or SHA-256 mismatch: {model['repo']}/{entry['path']}")
    os.replace(partial, path)


def _upstream_verify(root: Path, command: str, version: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MINERU_")}
    env.update(
        {
            "MINERU_HOME": str(root / ".verify-home"),
            "MINERU_CONFIG": "",
            "MINERU_MODEL_BASE_DIR": str(root / "models"),
            "MINERU_MODEL_SOURCE": "local",
            "MINERU_MODEL_SMALL_BACKEND": "onnx",
            "MINERU_MODEL_VLM_ENGINE": "llama-cpp",
            "HF_HUB_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    # A nonexistent home prevents loading any model-volume/private configuration.
    if (root / ".verify-home").exists():
        raise ModelError("reserved verification home must not exist")
    try:
        identity = subprocess.run(
            [command, "--version"], env=env, capture_output=True, text=True, timeout=30, check=False
        )
        if identity.returncode or not re.search(r"(?<![\d.])" + re.escape(version) + r"(?![\d.])", identity.stdout):
            raise ModelError("mineru-kit version differs from the pinned release")
        result = subprocess.run(
            [command, "models", "verify", "--tier", "standard", "--small-backend", "onnx", "--vlm-engine", "llama-cpp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelError("could not run the pinned MinerU offline verifier") from exc
    if result.returncode:
        raise ModelError("MinerU offline Standard verification failed")


def _result(release: dict) -> dict:
    entries = [entry for model in release["models"] for entry in model["files"]]
    return {
        "status": "ready",
        "tier": "standard",
        "small_backend": "onnx",
        "vlm_engine": "llama-cpp",
        "files": len(entries),
        "bytes": sum(entry["bytes"] for entry in entries),
        "validation": "file_hashes_and_offline_mineru_verify",
        "inference_tested": False,
    }


def download_models(
    root: Path, manifest: Path, *, release=None, opener=None, upstream=None, command="mineru-kit"
) -> dict:
    release = validate_release(release) if release is not None else load_release()
    root, manifest = _locations(root, manifest)
    # Check all paths before creating the root or invalidating a prior receipt.
    list(_files(root, release))
    with _locked(root, write=True):
        _invalidate(root, manifest, release)
        try:
            for model, entry, path in _files(root, release):
                _download_file(path, model, entry, opener or urlopen)
            _verify_files(root, release)
            # 4.0.9 full-repo readiness requires this marker. It is published only
            # after the complete pinned repository payload has passed SHA-256.
            for marker in _markers(root, release):
                marker.touch()
            (upstream or _upstream_verify)(root, command, release["mineru_version"])
            manifest.parent.mkdir(parents=True, exist_ok=True)
            temporary = _target(root, manifest.relative_to(root).as_posix() + ".part")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(release, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, manifest)
        except BaseException:
            _invalidate(root, manifest, release)
            raise
    return _result(release)


def verify_models(root: Path, manifest: Path, *, release=None, upstream=None, command="mineru-kit") -> dict:
    release = validate_release(release) if release is not None else load_release()
    root, manifest = _locations(root, manifest)
    with _locked(root, write=False):
        try:
            actual = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelError("Standard manifest is missing or unreadable") from exc
        if actual != release:
            raise ModelError("Standard manifest differs from the trusted complete release")
        _verify_files(root, release)
        if any(not marker.is_file() for marker in _markers(root, release)):
            raise ModelError("Standard model completion marker is missing")
        (upstream or _upstream_verify)(root, command, release["mineru_version"])
    return _result(release)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "verify"))
    parser.add_argument("--root", type=Path, required=True, help="Dedicated MinerU model volume root")
    parser.add_argument("--manifest", type=Path, required=True, help="Standard manifest path inside the root")
    parser.add_argument("--mineru-command", default="mineru-kit", help="Pinned mineru-kit executable")
    args = parser.parse_args(argv)
    try:
        operation = download_models if args.action == "download" else verify_models
        result = operation(args.root, args.manifest, command=args.mineru_command)
    except (ModelError, OSError) as exc:
        print(json.dumps({"status": "not_ready", "tier": "standard", "reason": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
