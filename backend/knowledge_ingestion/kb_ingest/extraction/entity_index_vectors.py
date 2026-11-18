"""On-disk embedding cache for entity index units, bound to workspace and model identity."""

from __future__ import annotations

import json
import math
import struct
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from kb_ingest.core import fingerprint


class _EmbeddingDispatcher(Protocol):
    def dispatch(self, value: str, *, deadline: float, received: Callable[..., object]) -> object: ...


class VectorCache:
    """Vectors bound to service/model/dimensions and verified against saved hashes."""

    def __init__(
        self, root: str | Path, dispatcher: _EmbeddingDispatcher | None, *, workspace_id: object = None
    ) -> None:

        self.dispatcher = dispatcher
        self.binding: dict[str, object] | None = None
        self._dimensions = 0
        if dispatcher is not None:
            binding = {
                key: getattr(dispatcher, key, None)
                for key in ("model", "dimensions", "service_identity", "input_version", "project_id")
            }
            binding["workspace_id"] = workspace_id
            dimensions = binding["dimensions"]
            if not binding["model"] or not binding["service_identity"] or type(dimensions) is not int or dimensions < 1:
                raise ValueError("embedding_cache_identity_required")
            self.binding = binding
            self._dimensions = dimensions
        self.root = Path(root) / (fingerprint(self.binding) if self.binding else "unconfigured")
        self._memo: dict[str, list[float]] = {}

    @staticmethod
    def digest(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / (digest + ".f32")

    def get(self, text: str) -> list[float] | None:
        if self.binding is None:
            return None
        digest = self.digest(text)
        try:
            path = self._path(digest)
            blob = path.read_bytes()
            metadata = json.loads(path.with_suffix(".json").read_text())
        except (OSError, ValueError):
            return None
        if (
            not isinstance(metadata, dict)
            or metadata.get("binding") != self.binding
            or metadata.get("input_sha256") != digest
            or metadata.get("result_sha256") != sha256(blob).hexdigest()
            or len(blob) != self._dimensions * 4
        ):
            return None
        values = struct.unpack("<%df" % (len(blob) // 4), blob)
        if any(not math.isfinite(value) for value in values):
            return None
        norm = math.sqrt(math.fsum(v * v for v in values))
        vector = [v / norm for v in values] if norm > 0 else None
        if vector is not None:
            self._memo[digest] = vector
        return vector

    def ensure(self, texts: Sequence[str], *, deadline: float, workers: int = 8) -> tuple[int, list[str]]:
        """Embed the texts that have no vector yet. Returns (newly embedded, failure codes)."""
        dispatcher = self.dispatcher
        if dispatcher is None:
            return 0, ["embedding_not_configured"]
        from kb_ingest.project_embedding import EmbeddingUnavailable

        missing = list(dict.fromkeys(text for text in texts if text.strip() and self.get(text) is None))
        failures: list[str] = []
        done = 0
        dimensions = self._dimensions

        def one(text: str) -> str | None:
            path = self._path(self.digest(text))

            def received(blob: object, digest: object, receipt: object) -> None:
                from kb_ingest.core import atomic_json
                from kb_ingest.project_embedding import decode_vector

                if not isinstance(receipt, dict) or receipt.get("result_sha256") != digest:
                    raise EmbeddingUnavailable("embedding_receipt_conflict")
                decode_vector(blob, digest, dimensions)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
                # decode_vector already rejects non-bytes with this same code; this only narrows the type.
                if type(blob) is not bytes:
                    raise EmbeddingUnavailable("embedding_cache_invalid")
                temporary.write_bytes(blob)
                temporary.replace(path)
                atomic_json(
                    path.with_suffix(".json"),
                    {"binding": self.binding, "input_sha256": self.digest(text), "result_sha256": digest},
                )

            try:
                dispatcher.dispatch(text, deadline=deadline, received=received)
                return None
            except EmbeddingUnavailable as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for code in pool.map(one, missing):
                if code:
                    failures.append(code)
                else:
                    done += 1
        return done, sorted(set(failures))
