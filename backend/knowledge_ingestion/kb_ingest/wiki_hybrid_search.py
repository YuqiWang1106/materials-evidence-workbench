"""Shared, bounded Wiki recall. Authority and evidence remain in WikiQueryService."""

import math
import time
from copy import deepcopy
from threading import RLock

from .project_embedding import MAX_INPUT_BYTES, EmbeddingUnavailable
from .research_chat_contracts import ResearchError
from .wiki_embeddings import WikiEmbeddingCache
from .wiki_query_index import WikiQueryIndex, chunk_key


def embedding_text(chunk):
    # Only metadata may be shortened; the complete P1 2400-byte body is retained.
    title = chunk["title"].encode()[:512].decode("utf-8", "ignore")
    heading = chunk["heading"].encode()[:512].decode("utf-8", "ignore")
    return title + "\n" + heading + "\n" + chunk["body"]


def fuse(lexical, semantic, *, top_k=8):
    scores = {}
    chunks = {}
    for route in (lexical[:20], semantic[:20]):
        seen = set()
        for rank, chunk in enumerate(route, 1):
            key = chunk_key(chunk)
            if key in seen:
                continue
            seen.add(key)
            chunks[key] = chunk
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    result = []
    pages = {}
    for key in sorted(scores, key=lambda k: (-scores[k], k)):
        chunk = chunks[key]
        page = chunk["wiki_binding"]["page_id"]
        if pages.get(page, 0) >= 2:
            continue
        pages[page] = pages.get(page, 0) + 1
        result.append({**deepcopy(chunk), "score": scores[key]})
        if len(result) >= min(top_k, 8):
            break
    return result


class WikiHybridSearch:
    def __init__(self, store, scope, dispatcher=None):
        self.store, self.scope = store, deepcopy(scope)
        self.index = WikiQueryIndex(store, scope)
        self.embeddings = WikiEmbeddingCache(store, dispatcher) if dispatcher is not None else None
        self._guard = RLock()
        self._status = {
            "requested_mode": "hybrid",
            "actual_mode": "lexical",
            "semantic_available": False,
            "indexed_chunks": 0,
            "eligible_chunks": 0,
            "degraded_reason": "index_not_prepared",
            "relation_depth": 0,
        }

    def status(self):
        # Last completed retrieval, never a claim about present source eligibility.
        with self._guard:
            return {
                **deepcopy(self._status),
                "coverage_basis": "last_query",
                "embedding_configured": self.embeddings is not None,
            }

    def recover(self):
        return self.embeddings.recover() if self.embeddings else 0

    def _keys(self, chunks):
        _project = self.embeddings.dispatcher.project_id
        return {
            chunk_key(c): self.embeddings.dispatcher.input_identity(embedding_text(c))[0]["cache_key"] for c in chunks
        }

    def score(self, chunks, query_vector, *, deadline):
        """Rank an already authorized subset. No preparation and no paid requests."""
        if self.embeddings is None:
            return [], 0
        keys = self._keys(chunks)
        vectors = self.embeddings.ready(keys.values(), deadline=deadline)
        if query_vector is None:
            return [], sum(keys[chunk_key(c)] in vectors for c in chunks)
        scored = []
        for chunk in chunks:
            if time.monotonic() >= deadline:
                raise ResearchError("context_limit")
            vector = vectors.get(keys[chunk_key(chunk)])
            if vector is not None:
                score = math.fsum(a * b for a, b in zip(query_vector, vector))
                scored.append((score, chunk_key(chunk), chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [{**deepcopy(c), "score": score} for score, _, c in scored[:20]], len(scored)

    def search(self, pages, query, *, deadline, top_k=8):
        from .knowledge_service import READ_CANCELED

        if type(top_k) is not int or not 1 <= top_k <= 8:
            raise ResearchError("invalid_request")
        generation = self.index.refresh(pages, deadline=deadline)
        chunks = self.index.documents(generation, deadline=deadline)
        # Obtain lexical before spending any preparation time; provider failure
        # therefore cannot suppress usable lexical results.
        try:
            lexical = self.index.lexical(generation, query, deadline=deadline, top_k=20)
        except ValueError:
            raise ResearchError("query_limit") from None
        reason = None
        query_vector = None
        if self.embeddings is None:
            reason = "embedding_not_configured"
        elif len(query.encode()) > MAX_INPUT_BYTES:
            reason = "embedding_input_limit"
        elif not chunks:
            reason = "scope_empty"
        else:
            # Reserve room for SQLite ranking and final authority validation.
            available = max(0, deadline - time.monotonic())
            prep_deadline = min(time.monotonic() + 30, deadline - min(2.0, available * 0.25))
            try:
                query_vector = self.embeddings.ensure(query, deadline=prep_deadline, cancel=READ_CANCELED.get())
                keys = self._keys(chunks)
                ready = self.embeddings.ready(keys.values(), deadline=prep_deadline)
                blocked = self.embeddings.blocked(keys.values(), deadline=prep_deadline)
                if blocked:
                    reason = "embedding_outcome_unknown"
                missing = [c for c in chunks if keys[chunk_key(c)] not in ready and keys[chunk_key(c)] not in blocked]
                for chunk in missing[:16]:
                    try:
                        self.embeddings.ensure(
                            embedding_text(chunk), deadline=prep_deadline, cancel=READ_CANCELED.get()
                        )
                    except EmbeddingUnavailable as exc:
                        reason = exc.code
                        if exc.code in (
                            "budget_denied",
                            "embedding_deadline",
                            "embedding_provider_failure",
                            "cancelled",
                        ):
                            break
                if len(missing) > 16 and reason is None:
                    reason = "embedding_preparation_limit"
            except EmbeddingUnavailable as exc:
                reason = exc.code
        if READ_CANCELED.get() is not None and READ_CANCELED.get().is_set():
            raise ResearchError("cancelled")
        semantic, indexed = self.score(chunks, query_vector, deadline=deadline)
        actual = (
            "hybrid"
            if query_vector is not None and indexed == len(chunks) and indexed
            else "partial"
            if semantic
            else "lexical"
        )
        if actual == "hybrid":
            reason = None
        elif reason is None:
            reason = "embedding_partial" if indexed else "embedding_not_ready"
        status = {
            "requested_mode": "hybrid",
            "actual_mode": actual,
            "semantic_available": bool(semantic),
            "indexed_chunks": indexed,
            "eligible_chunks": len(chunks),
            "degraded_reason": reason,
            "relation_depth": 0,
        }
        with self._guard:
            self._status = deepcopy(status)
        return fuse(lexical, semantic, top_k=top_k), status
