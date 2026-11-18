"""Lifetime ownership of the native data root and local reader environment."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Self

from .core.config import NativeConfig
from .core.errors import NativeError


class DataRootOwner:
    """A nonblocking lifetime flock acquired before Store construction/recovery."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.stream: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.root / ".native-owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self.stream = os.fdopen(fd, "r+b")
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError):
            if self.stream:
                self.stream.close()
                self.stream = None
            message = "Data root already has an owner or its owner lock is unavailable."
            raise NativeError(message) from None
        return self

    def __exit__(self, *_: object) -> None:
        if self.stream is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


@contextmanager
def reader_environment(config: NativeConfig) -> Iterator[None]:
    if config.model_root is None or not config.mineru_command:
        yield
        return
    manifest = str(config.model_root / "standard-model-manifest.json")
    values = {
        "MATERIALS_MINERU_HOME": str(config.model_root),
        "MINERU_HOME": str(config.model_root),
        "MATERIALS_MINERU_MODEL_MANIFEST": manifest,
        "MATERIALS_MINERU_STANDARD_MODEL_MANIFEST": manifest,
        "MATERIALS_MINERU_LOCK_PATH": str(config.data_root / ".mineru-parse.lock"),
        "MINERU_CONFIG": "",
        "MINERU_MODEL_BASE_DIR": str(config.model_root / "models"),
        "MINERU_MODEL_SOURCE": "local",
        "MINERU_MODEL_SMALL_BACKEND": "onnx",
        "MINERU_MODEL_VLM_ENGINE": "llama-cpp",
        "HF_HUB_OFFLINE": "1",
        "MINERU_PROCESSING_WINDOW_SIZE": "1",
        "MINERU_PDF_RENDER_THREADS": "1",
        "MINERU_INTRA_OP_NUM_THREADS": "1",
        "MINERU_INTER_OP_NUM_THREADS": "1",
    }
    prior = {k: v for k, v in os.environ.items() if k.startswith(("MINERU_", "MATERIALS_MINERU_")) or k == "HF_HUB_OFFLINE"}
    for key in prior:
        os.environ.pop(key, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for key in values:
            os.environ.pop(key, None)
        os.environ.update(prior)
