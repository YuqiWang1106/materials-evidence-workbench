"""Current published Wiki chunks in the existing migrated, rebuildable FTS cache.

The global namespace never collides with frozen KnowledgeIndexStore scopes.
No runtime DDL: this uses the models/tables provided by migration 0006.
"""

import re
import time
from copy import deepcopy

from sqlalchemy import delete, select

from .core import clock, fingerprint
from .knowledge_index_store import KnowledgeIndexStore
from .research_chat_contracts import ResearchError
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

CHUNK_BYTES = 2400
FORMAT = "global-wiki-chunks-v1"


def text_parts(value, limit=CHUNK_BYTES):
    """Preserve every character; prefer paragraph boundaries, split oversized ones."""
    result = []
    current = ""
    for paragraph in re.split(r"(\n\n)", value):
        if current and len((current + paragraph).encode()) > limit:
            result.append(current)
            current = ""
        for character in paragraph:
            if len(current.encode()) + len(character.encode()) > limit:
                result.append(current)
                current = ""
            current += character
    if current or not result:
        result.append(current)
    return result


def page_chunks(page):
    revision = page["revision"]
    content = revision["content"]
    binding = {k: revision[k] for k in ("revision_id", "content_hash", "dependency_manifest_hash")}
    binding["page_id"] = page["page_id"]
    for section in content["sections"]:
        claims = section["claims"] or [
            {"claim_id": None, "text": section["heading"], "support_ids": [], "claim_type": "editorial_synthesis"}
        ]
        for claim in claims:
            parts = text_parts(claim["text"])
            for index, body in enumerate(parts):
                yield {
                    "wiki_binding": deepcopy(binding),
                    "section_id": section["section_id"],
                    "claim_id": claim["claim_id"],
                    "part_index": index,
                    "part_count": len(parts),
                    "title": content["title"],
                    "heading": section["heading"],
                    "body": body,
                    "claim_type": claim["claim_type"],
                    "support_ids": claim["support_ids"],
                    "text": content["title"] + "\n" + section["heading"] + "\n" + body,
                }
    # Conditions/question/uncertainties are searchable in full as separate parts.
    for field in ("research_question", "conditions", "uncertainties"):
        values = content.get(field, [])
        value = values if isinstance(values, str) else "\n".join(values)
        if not value:
            continue
        parts = text_parts(value)
        for index, body in enumerate(parts):
            yield {
                "wiki_binding": deepcopy(binding),
                "section_id": "wiki-" + field,
                "claim_id": None,
                "part_index": index,
                "part_count": len(parts),
                "title": content["title"],
                "heading": field,
                "body": body,
                "claim_type": "editorial_synthesis",
                "support_ids": [],
                "text": content["title"] + "\n" + field + "\n" + body,
            }


def chunk_key(chunk):
    return fingerprint([chunk["wiki_binding"], chunk["section_id"], chunk["claim_id"], chunk["part_index"]])


def invalidate_current(session, workspace_id):
    """Single local invalidation path inside the existing publication event write.

    Publication/edit/delete callers do no rebuild or provider I/O. Every search
    refreshes from current published authority, including after a process restart.
    """
    scopes = select(Generation.scope_key).where(
        Generation.scope["workspace_id"].as_string() == workspace_id,
        Generation.source_manifest["format"].as_string() == FORMAT,
    )
    session.execute(delete(Current).where(Current.scope_key.in_(scopes)))


class WikiQueryIndex:
    def __init__(self, store, scope):
        self.store = store
        self.scope = scope
        self.cache = KnowledgeIndexStore(store)
        self.key = fingerprint([FORMAT, scope])

    def refresh(self, pages, *, deadline):
        """Publish one complete eligible chunk generation; reusable by hybrid retrieval."""
        chunks = [chunk for page in pages for chunk in page_chunks(page)]
        if len(chunks) > 10000 or sum(len(c["text"].encode()) for c in chunks) > 16 * 1024 * 1024:
            raise ResearchError("context_limit")
        tokenizer = self.cache.tokenizer
        manifest = {
            "format": FORMAT,
            "revisions": [c["wiki_binding"] for c in chunks],
            "documents": {chunk_key(c): fingerprint(c) for c in chunks},
        }
        digest = fingerprint([manifest, tokenizer.version, tokenizer.alias_version])
        generation = "wiki-index-" + fingerprint([self.key, digest])
        # One SQLite write transaction publishes only the complete generation.
        with self.cache._write(deadline) as session:
            if session.get(Generation, generation) is None:
                now = clock.now()
                session.add(
                    Generation(
                        generation_id=generation,
                        scope_key=self.key,
                        scope=self.scope,
                        request_id=digest,
                        request_hash=digest,
                        source_manifest=manifest,
                        source_manifest_hash=fingerprint(manifest),
                        tokenizer_version=tokenizer.version,
                        alias_version=tokenizer.alias_version,
                        state="ready",
                        entry_count=len(chunks),
                        error_code=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
                for chunk in chunks:
                    if time.monotonic() >= deadline:
                        raise ResearchError("context_limit")
                    body = chunk["text"]
                    key = chunk_key(chunk)
                    row = Entry(
                        generation_id=generation,
                        entry_key=key,
                        scope_key=self.key,
                        workspace_id=self.scope["workspace_id"],
                        layer="wiki",
                        collection_id=self.scope["collection_id"],
                        use_policy="source_reviewed",
                        corpus_snapshot_id=FORMAT,
                        ref=chunk,
                        source_identity=chunk["wiki_binding"],
                        content_kind="wiki_chunk",
                        text=body,
                        content_hash=fingerprint(body),
                        document_hash=fingerprint(chunk),
                        tokenizer_version=tokenizer.version,
                        alias_version=tokenizer.alias_version,
                    )
                    session.add(row)
                    session.flush()
                    # Pathological source words do not make the complete page disappear.
                    lexical = re.sub(
                        r"[A-Za-z0-9_-]{129,}",
                        lambda m: " ".join(m[0][i : i + 100] for i in range(0, len(m[0]), 100)),
                        body,
                    )
                    sqlite.insert_fts_entry(session, tokenizer.document_tokens(lexical), generation, row.entry_id)
            current = session.get(Current, self.key)
            if current is None:
                session.add(Current(scope_key=self.key, generation_id=generation))
            else:
                current.generation_id = generation
        return generation

    def documents(self, generation, *, deadline):
        """Internal cache content only. Callers must still validate current authority."""
        with self.cache._read(deadline) as session:
            row = session.get(Generation, generation)
            if row is None or row.scope_key != self.key or row.state != "ready":
                raise ResearchError("stale_context")
            return [
                deepcopy(e.ref)
                for e in session.scalars(
                    select(Entry).where(Entry.generation_id == generation).order_by(Entry.entry_id)
                )
            ]

    def lexical(self, generation, query, *, deadline, top_k=5):
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ResearchError("invalid_request")
        tokenizer = self.cache.tokenizer
        expression = tokenizer.match_expression(query)
        if not expression:
            return []
        with self.cache._read(deadline) as session:
            results = []
            for entry_id, rank in sqlite.rank_generation(session, expression, generation, top_k):
                entry = session.get(Entry, entry_id)
                if (
                    entry is None
                    or entry.scope_key != self.key
                    or entry.generation_id != generation
                    or fingerprint(entry.ref) != entry.document_hash
                ):
                    raise ResearchError("stale_context")
                results.append({**deepcopy(entry.ref), "score": -float(rank)})
            return results

    def search(self, pages, query, *, deadline, top_k=5):
        generation = self.refresh(pages, deadline=deadline)
        return self.lexical(generation, query, deadline=deadline, top_k=top_k)
