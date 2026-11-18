"""Derived vectors saved before request completion, with no fee dependency."""

import fcntl
import time
from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
from threading import RLock

from .core import fingerprint
from .knowledge_index_store import IndexFailure, KnowledgeIndexStore
from .llm.model_requests import ModelRequestError
from .project_embedding import (
    EmbeddingUnavailable,
    check_time,
    decode_vector,
)
from .storage import embedding_repository as vectors


class WikiEmbeddingCache:
    def __init__(self, store, dispatcher):
        if dispatcher.requests.workspace_id != store.workspace_id:
            raise EmbeddingUnavailable("embedding_workspace_mismatch")
        self.store, self.dispatcher = store, dispatcher
        self.workspace_id = store.workspace_id
        self.db = KnowledgeIndexStore(store)
        self._vectors = OrderedDict()
        self._guard = RLock()

    @contextmanager
    def _lock(self, key):
        # Nonblocking, process-wide claim. A contending query can use lexical now.
        with (self.store.root / "locks" / ("wiki-embedding-" + key + ".lock")).open("a+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EmbeddingUnavailable("embedding_busy") from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _binding(self, row):
        key = fingerprint(
            [
                self.dispatcher.project_id,
                self.dispatcher.model,
                self.dispatcher.dimensions,
                self.dispatcher.input_version,
                row["input_sha256"],
            ]
        )
        if (
            row["cache_key"] != key
            or row["call_id"] != "emb-" + key
            or row["project_id"] != self.dispatcher.project_id
            or row["model"] != self.dispatcher.model
            or row["dimensions"] != self.dispatcher.dimensions
            or row["input_version"] != self.dispatcher.input_version
        ):
            raise EmbeddingUnavailable("embedding_cache_invalid")
        return {
            k: row[k]
            for k in (
                "cache_key",
                "project_id",
                "model",
                "dimensions",
                "input_version",
                "input_sha256",
                "call_id",
                "request_sha256",
            )
        }

    def _decode(self, row):
        self._binding(row)
        blob = row["vector_blob"]
        digest = row["vector_sha256"]
        if type(blob) is not bytes or sha256(blob).hexdigest() != digest:
            raise EmbeddingUnavailable("embedding_cache_invalid")
        key = (row["cache_key"], digest)
        with self._guard:
            if key not in self._vectors:
                self._vectors[key] = decode_vector(blob, digest, self.dispatcher.dimensions)
            self._vectors.move_to_end(key)
            while len(self._vectors) > 10000:
                self._vectors.popitem(last=False)
            return self._vectors[key]

    def _complete(self, row, deadline):
        vector = self._decode(row)
        self.dispatcher.complete(self._binding(row), {**(row["receipt"] or {}), "result_sha256": row["vector_sha256"]})
        with self.db._write(deadline) as session:
            vectors.mark_ready(session, self.workspace_id, row["cache_key"])
        return vector

    def ensure(self, value, *, deadline, cancel=None):
        from .core.clock import IO_DEADLINE

        inherited = IO_DEADLINE.get()
        token = IO_DEADLINE.set(min(inherited, deadline) if inherited is not None else deadline)
        try:
            return self._ensure(value, deadline=deadline, cancel=cancel)
        except (ModelRequestError, IndexFailure):
            raise EmbeddingUnavailable(
                "embedding_deadline" if time.monotonic() >= deadline - 0.005 else "embedding_cache_unavailable"
            ) from None
        finally:
            IO_DEADLINE.reset(token)

    def _ensure(self, value, *, deadline, cancel=None):
        check_time(deadline, cancel)
        binding, _ = self.dispatcher.input_identity(value)
        key = binding["cache_key"]
        with self._lock(key):
            with self.db._write(deadline) as session:
                saved = vectors.prepare(session, self.workspace_id, binding)
                if any(saved[k] != v for k, v in binding.items()):
                    raise EmbeddingUnavailable("embedding_cache_invalid")
            if saved["state"] == "ready":
                return self._decode(saved)
            if saved["state"] == "received":
                try:
                    return self._complete(saved, deadline)
                except (ModelRequestError, ValueError):
                    raise EmbeddingUnavailable("embedding_completion_pending") from None
            if saved["state"] == "unknown":
                raise EmbeddingUnavailable("embedding_outcome_unknown")

            def received(blob, digest, receipt):
                with self.db._write(deadline) as session:
                    vectors.receive(session, self.workspace_id, key, (blob, digest), receipt)

            try:
                vector = self.dispatcher.dispatch(value, deadline=deadline, cancel=cancel, received=received)
                with self.db._write(deadline) as session:
                    vectors.mark_ready(session, self.workspace_id, key)
                return vector
            except EmbeddingUnavailable:
                # Never erase a received vector/receipt, even if execution-state write failed.
                self._mark_unresolved(binding, key, deadline)
                raise

    def _mark_unresolved(self, binding, key, deadline):
        try:
            prior = self.dispatcher.requests.read_call(self.dispatcher.project_id, binding["call_id"])
        except ModelRequestError:
            prior = None
        if prior and prior["state"] in ("sent", "unknown", "completed"):
            try:
                with self.db._write(deadline) as session:
                    vectors.mark_unknown(session, self.workspace_id, key)
            except (IndexFailure, KeyError):
                # Preserve the original dispatch error; recovery rechecks the durable receipt.
                return

    def ready(self, keys, *, deadline):
        """Only load blobs not in the process cache; no network or execution-state writes."""
        result = {}
        keys = list(dict.fromkeys(keys))
        for start in range(0, len(keys), 400):
            with self.db._read(deadline) as session:
                encoding = (self.dispatcher.model, self.dispatcher.dimensions, self.dispatcher.input_version)
                descriptors = vectors.ready(
                    session, self.workspace_id, keys[start : start + 400], self.dispatcher.project_id, encoding
                )
                for key, digest in descriptors:
                    with self._guard:
                        vector = self._vectors.get((key, digest))
                    if vector is None:
                        row = vectors.read(session, self.workspace_id, key)
                        try:
                            vector = self._decode(row)
                        except EmbeddingUnavailable:
                            continue
                    result[key] = vector
        return result

    def blocked(self, keys, *, deadline):
        """Unknown inputs retain their receipt and do not consume the next batch."""
        result: set[str] = set()
        keys = list(dict.fromkeys(keys))
        for start in range(0, len(keys), 400):
            with self.db._read(deadline) as session:
                result.update(
                    vectors.blocked(session, self.workspace_id, keys[start : start + 400], self.dispatcher.project_id)
                )
        return result

    def recover(self):
        """Local only, under each input's live-worker lock. Unknown never resends."""
        deadline = time.monotonic() + 10
        with self.db._read(deadline) as session:
            keys = vectors.unfinished(
                session,
                self.workspace_id,
                self.dispatcher.project_id,
                self.dispatcher.model,
                self.dispatcher.input_version,
            )
        count = 0
        for key in keys:
            if time.monotonic() >= deadline:
                break
            try:
                with self._lock(key):
                    with self.db._read(deadline) as session:
                        row = vectors.read(session, self.workspace_id, key)
                    binding = self._binding(row)
                    if row["state"] == "received":
                        self._complete(row, deadline)
                        count += 1
                    elif row["state"] == "pending":
                        prior = self.dispatcher.requests.read_call(self.dispatcher.project_id, binding["call_id"])
                        if prior and prior["state"] in ("sent", "unknown", "completed"):
                            if prior["state"] == "sent":
                                self.dispatcher.requests.mark_call_unknown(
                                    self.dispatcher.project_id, binding["call_id"], "process_recovery"
                                )
                            with self.db._write(deadline) as session:
                                vectors.mark_unknown(session, self.workspace_id, key)
            except (EmbeddingUnavailable, ModelRequestError):
                continue
        return count
