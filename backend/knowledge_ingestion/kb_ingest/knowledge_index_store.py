"""Atomic, rebuildable FTS candidates. The index is never a source of authority."""

import fcntl
import math
import time
import uuid
from contextlib import contextmanager
from hashlib import sha256

from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .core import clock, fingerprint
from .knowledge_index_contracts import IndexAuthority, IndexDocument, IndexScope, IndexSnapshot
from .knowledge_index_tokens import IndexTokenizer
from .storage import sqlite
from .storage.models import (
    KnowledgeIndexCurrent as Current,
)
from .storage.models import (
    KnowledgeIndexEntry as Entry,
)
from .storage.models import (
    KnowledgeIndexGeneration as Generation,
)


class IndexFailure(ValueError):
    def __init__(self, code, reason=None):
        self.code = code
        self.reason = reason or code.lower()
        super().__init__(code)


def _deadline(timeout, maximum):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= maximum:
        raise ValueError("Invalid index time limit")
    return time.monotonic() + timeout


def _check_time(deadline):
    if time.monotonic() >= deadline:
        raise IndexFailure("INDEX_TIMEOUT")


class KnowledgeIndexStore:
    """Use an already migrated Store. Construction performs no migration or scan.

    Rebuild jobs are idempotent by (exact scope, request_id). A failed/interrupted
    build can be explicitly repeated with the SAME immutable snapshot. No startup
    background work or model request is scheduled. Source adapters are not exposed
    as user-configurable callbacks; wiring them is the service's responsibility.
    """

    def __init__(self, store, *, tokenizer=None):
        self.store = store
        self.tokenizer = tokenizer or IndexTokenizer()

    @contextmanager
    def _connection(self, deadline):
        from .core.clock import IO_DEADLINE

        inherited = IO_DEADLINE.get()
        if inherited is not None:
            deadline = min(deadline, inherited)
        token = IO_DEADLINE.set(deadline)
        try:
            _check_time(deadline)
            with self.store.engine.connect() as connection:
                yield connection, deadline
        except DBAPIError as exc:
            _check_time(deadline)
            message = str(exc.orig).lower()
            if "locked" in message or "busy" in message:
                raise IndexFailure("INDEX_TIMEOUT", "database_connection_timeout") from None
            raise IndexFailure("INDEX_UNAVAILABLE") from None
        except TimeoutError:
            raise IndexFailure("INDEX_TIMEOUT") from None
        finally:
            IO_DEADLINE.reset(token)

    @contextmanager
    def _read(self, deadline):
        with self._connection(deadline) as (conn, deadline):
            if not sqlite.is_sqlite(conn):
                raise IndexFailure("FTS5_UNAVAILABLE")
            try:
                with sqlite.query_deadline(conn, deadline), Session(bind=conn) as session:
                    yield session
                _check_time(deadline)
            except DBAPIError as exc:
                _check_time(deadline)
                message = str(exc.orig).lower()
                if "locked" in message or "busy" in message:
                    raise IndexFailure("INDEX_TIMEOUT", "database_lock_timeout") from None
                if "no such table" in message:
                    raise IndexFailure("INDEX_NOT_READY", "migration_required") from None
                if "fts5" in message:
                    raise IndexFailure("FTS5_UNAVAILABLE") from None
                raise IndexFailure("INDEX_UNAVAILABLE") from None

    @contextmanager
    def _write(self, deadline):
        # Bound lock acquisition and SQL to the inherited read deadline.
        with self._read(deadline) as session:
            _check_time(deadline)
            sqlite.begin_immediate(session)
            try:
                yield session
                _check_time(deadline)
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @contextmanager
    def _build_lock(self, scope, deadline):
        name = "knowledge-index-" + scope.key
        path = self.store.root / "locks" / (sha256(name.encode()).hexdigest() + ".lock")
        with path.open("a+b") as stream:
            while True:
                _check_time(deadline)
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _manifest(self, authority, scope, expected, deadline):
        _check_time(deadline)
        try:
            current = authority.current_manifest(scope.model_copy(deep=True), deadline=deadline)
            matches = fingerprint(current) == expected
        except Exception:
            _check_time(deadline)
            raise IndexFailure("INDEX_STALE", "source_manifest_unverifiable") from None
        _check_time(deadline)
        if not matches:
            raise IndexFailure("INDEX_STALE", "source_manifest_changed")

    def _resolve(self, authority, scope, doc, deadline):
        _check_time(deadline)
        try:
            fresh = authority.resolve(
                scope.model_copy(deep=True), doc.ref.model_copy(deep=True), doc.content_kind, deadline=deadline
            )
            # Revalidate even model instances: model_copy/update can bypass validation.
            fresh = IndexDocument.model_validate(
                fresh.model_dump(mode="json") if isinstance(fresh, IndexDocument) else fresh
            )
            fresh.check_scope(scope)
            matches = fresh.document_hash == doc.document_hash
        except Exception:
            _check_time(deadline)
            raise IndexFailure("INDEX_STALE", "source_reference_unverifiable") from None
        _check_time(deadline)
        if not matches:
            raise IndexFailure("INDEX_STALE", "source_content_changed")

    @staticmethod
    def _build_result(row):
        return {
            "generation_id": row.generation_id,
            "state": row.state,
            "entry_count": row.entry_count,
            "error_code": row.error_code,
            "source_manifest_hash": row.source_manifest_hash,
        }

    def build_status(self, scope, request_id):
        with self._read(_deadline(2, 10)) as session:
            row = session.scalar(
                select(Generation).where(Generation.scope_key == scope.key, Generation.request_id == request_id)
            )
            return self._build_result(row) if row else None

    def status(self, scope, *, deadline=None):
        """Build status only; 'ready' does not certify present source eligibility."""
        with self._read(min(_deadline(2, 10), deadline) if deadline is not None else _deadline(2, 10)) as session:
            pointer = session.get(Current, scope.key)
            generation = session.get(Generation, pointer.generation_id) if pointer else None
            return {
                "readiness": "ready" if generation and generation.state == "ready" else "not_ready",
                "generation": generation.generation_id if generation else None,
                "eligibility": "requires_read_validation",
                "mode": "lexical",
            }

    def binding(self, scope, *, deadline):
        """Internal, hash-checked persistent authority binding; never eligibility."""
        with self._read(deadline) as session:
            pointer = session.get(Current, scope.key)
            row = session.get(Generation, pointer.generation_id) if pointer else None
            if row is None:
                raise IndexFailure("INDEX_NOT_READY")
            if (
                row.state != "ready"
                or row.scope != scope.model_dump(mode="json")
                or fingerprint(row.source_manifest) != row.source_manifest_hash
            ):
                raise IndexFailure("INDEX_STALE")
            return row.source_manifest

    def rebuild(self, snapshot, *, request_id, authority: IndexAuthority, timeout=30):
        from pydantic import TypeAdapter

        from .knowledge_contracts import Id

        TypeAdapter(Id).validate_python(request_id)
        # Freeze the caller's mutable Pydantic/dict containers before any source I/O.
        snapshot = IndexSnapshot.model_validate(
            snapshot.model_dump(mode="json") if isinstance(snapshot, IndexSnapshot) else snapshot
        )
        scope = snapshot.scope
        deadline = _deadline(timeout, 120)
        manifest_hash = fingerprint(snapshot.source_manifest)
        request_hash = fingerprint(
            [
                scope.model_dump(),
                manifest_hash,
                sorted(d.document_hash for d in snapshot.documents),
                self.tokenizer.version,
                self.tokenizer.alias_version,
            ]
        )
        with self._build_lock(scope, deadline):
            _check_time(deadline)
            with self._write(deadline) as session:
                generation = session.scalar(
                    select(Generation).where(Generation.scope_key == scope.key, Generation.request_id == request_id)
                )
                if generation:
                    if generation.request_hash != request_hash:
                        raise IndexFailure("INDEX_REQUEST_CONFLICT")
                    if generation.state == "ready":
                        return self._build_result(generation)
                    # A crash may have left a staged job. Only this generation is reset.
                    sqlite.delete_fts_generation(session, generation.generation_id)
                    session.execute(delete(Entry).where(Entry.generation_id == generation.generation_id))
                    generation.state = "building"
                    generation.error_code = None
                    generation.entry_count = 0
                    generation.updated_at = clock.now()
                else:
                    generation = Generation(
                        generation_id="index-" + uuid.uuid4().hex,
                        scope_key=scope.key,
                        scope=scope.model_dump(mode="json"),
                        request_id=request_id,
                        request_hash=request_hash,
                        source_manifest=snapshot.source_manifest,
                        source_manifest_hash=manifest_hash,
                        tokenizer_version=self.tokenizer.version,
                        alias_version=self.tokenizer.alias_version,
                        state="building",
                        entry_count=0,
                        error_code=None,
                        created_at=clock.now(),
                        updated_at=clock.now(),
                    )
                    session.add(generation)
                session.flush()
                generation_id = generation.generation_id
            try:
                self._manifest(authority, scope, manifest_hash, deadline)
                prepared = []
                for doc in snapshot.documents:
                    self._resolve(authority, scope, doc, deadline)
                    prepared.append((doc, self.tokenizer.document_tokens(doc.text)))
                self._manifest(authority, scope, manifest_hash, deadline)
                with self._write(deadline) as session:
                    for doc, tokens in prepared:
                        _check_time(deadline)
                        row = Entry(
                            generation_id=generation_id,
                            entry_key=doc.key,
                            scope_key=scope.key,
                            **scope.model_dump(mode="json"),
                            ref=doc.ref.model_dump(mode="json"),
                            source_identity=doc.source_identity,
                            content_kind=doc.content_kind,
                            text=doc.text,
                            content_hash=doc.content_hash,
                            document_hash=doc.document_hash,
                            tokenizer_version=self.tokenizer.version,
                            alias_version=self.tokenizer.alias_version,
                        )
                        session.add(row)
                        session.flush()
                        sqlite.insert_fts_entry(session, tokens, generation_id, row.entry_id)
                    # The exact source set must still be eligible before publication.
                    self._manifest(authority, scope, manifest_hash, deadline)
                    generation = session.get(Generation, generation_id)
                    generation.state = "ready"
                    generation.entry_count = len(prepared)
                    generation.updated_at = clock.now()
                    pointer = session.get(Current, scope.key)
                    if pointer:
                        pointer.generation_id = generation_id
                    else:
                        session.add(Current(scope_key=scope.key, generation_id=generation_id))
                    result = self._build_result(generation)
                return result
            except Exception as exc:
                code = exc.code if isinstance(exc, IndexFailure) else "INDEX_BUILD_FAILED"
                try:
                    with self._write(deadline) as session:
                        generation = session.get(Generation, generation_id)
                        generation.state = "failed"
                        generation.error_code = code
                        generation.updated_at = clock.now()
                except IndexFailure:
                    # An expired deadline cannot authorize an unbounded cleanup.
                    # The durable 'building' job remains explicitly resumable.
                    pass
                raise IndexFailure(code, getattr(exc, "reason", None)) from None

    @staticmethod
    def _result(scope, generation, results, reason=None):
        return {
            "readiness": "ready" if generation else "not_ready",
            "mode": "lexical",
            "scope": scope.model_dump(mode="json"),
            "generation": generation,
            "results": results,
            "reason": reason,
            "exhaustive_within_scope": False,
            "total_matches": None,
            "ranking_meaning": "text_relevance_only",
        }

    def search(self, scope, query, *, authority: IndexAuthority, top_k=10, timeout=2):
        scope = IndexScope.model_validate(scope.model_dump(mode="json") if isinstance(scope, IndexScope) else scope)
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        expression = self.tokenizer.match_expression(query)
        deadline = _deadline(timeout, 10)
        with self._read(deadline) as session:
            pointer = session.get(Current, scope.key)
            if pointer is None:
                return self._result(scope, None, [], "index_not_ready")
            generation = session.get(Generation, pointer.generation_id)
            if (
                generation is None
                or generation.state != "ready"
                or generation.scope_key != scope.key
                or generation.scope != scope.model_dump(mode="json")
                or fingerprint(generation.source_manifest) != generation.source_manifest_hash
            ):
                raise IndexFailure("INDEX_STALE", "generation_integrity_failure")
            if (
                generation.tokenizer_version != self.tokenizer.version
                or generation.alias_version != self.tokenizer.alias_version
            ):
                raise IndexFailure("INDEX_STALE", "tokenizer_or_alias_changed")
            self._manifest(authority, scope, generation.source_manifest_hash, deadline)
            if not expression or not generation.entry_count:
                return self._result(
                    scope, generation.generation_id, [], "no_query_terms" if not expression else "scope_empty"
                )
            # Bound rows in SQL; never materialize the corpus for Python ranking.
            hits = sqlite.rank_scoped_entries(session, expression, generation.generation_id, scope.key, top_k)
            results = []
            for entry_id, score in hits:
                row = session.get(Entry, entry_id)
                try:
                    doc = IndexDocument(ref=row.ref, content_kind=row.content_kind, text=row.text, eligible=True)
                    doc.check_scope(scope)
                    if (
                        row.entry_key != doc.key
                        or row.content_hash != doc.content_hash
                        or row.document_hash != doc.document_hash
                        or row.source_identity != doc.source_identity
                        or any(getattr(row, key) != value for key, value in scope.model_dump().items())
                        or row.tokenizer_version != self.tokenizer.version
                        or row.alias_version != self.tokenizer.alias_version
                    ):
                        raise ValueError("Index row integrity mismatch")
                except (ValueError, TypeError):
                    raise IndexFailure("INDEX_STALE", "entry_integrity_failure") from None
                self._resolve(authority, scope, doc, deadline)
                results.append(
                    {
                        "ref": doc.ref.model_dump(mode="json"),
                        "scope": scope.model_dump(mode="json"),
                        "content_kind": doc.content_kind,
                        "source_identity": doc.source_identity,
                        "snippet": doc.text[:800],
                        "snippet_truncated": len(doc.text) > 800,
                        "content_hash": doc.content_hash,
                        "generation": generation.generation_id,
                        "readiness": "ready",
                        "requires_authoritative_read": True,
                        "score": -float(score),
                        "mode": "lexical",
                    }
                )
            self._manifest(authority, scope, generation.source_manifest_hash, deadline)
            return self._result(scope, generation.generation_id, results, None if results else "no_lexical_match")
