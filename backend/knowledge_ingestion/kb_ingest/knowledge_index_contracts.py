"""Internal lexical index contracts; authority adapters, never model input, attest eligibility."""

from hashlib import sha256
from typing import Literal, Protocol

from pydantic import Field, JsonValue, model_validator

from .core.serialization import canonical, fingerprint
from .knowledge_contracts import Id, KnowledgeRef, Policy, StrictModel


class IndexScope(StrictModel):
    workspace_id: Id
    layer: Literal["record", "source_passage", "wiki"]
    collection_id: Id
    use_policy: Policy
    corpus_snapshot_id: Id

    @model_validator(mode="after")
    def policy_layer(self):
        if self.layer == "record" and self.use_policy == "explore":
            raise ValueError("Formal records require a formal policy")
        if self.layer in ("source_passage", "wiki") and self.use_policy not in ("explore", "source_reviewed"):
            raise ValueError("Source passage / published Wiki policy required")
        return self

    @property
    def key(self):
        return fingerprint(self.model_dump(mode="json"))


KINDS = {
    "record_object": {"record_revision", "sample_test_context"},
    "record_assertion": {"assertion"},
    "record_evidence": {"evidence"},
    "source_passage": {"source_passage"},
    "wiki_revision": {"wiki_revision"},
}


class IndexDocument(StrictModel):
    ref: KnowledgeRef
    content_kind: Literal["record_object", "record_assertion", "record_evidence", "source_passage", "wiki_revision"]
    text: str = Field(min_length=1, max_length=32768)
    eligible: Literal[True]

    @model_validator(mode="after")
    def readable(self):
        if self.ref.kind not in KINDS[self.content_kind]:
            raise ValueError("Content kind differs from typed reference")
        if not self.text.strip() or len(self.text.encode("utf-8")) > 32768:
            raise ValueError("Index body must be nonempty and at most 32768 UTF-8 bytes")
        if self.ref.kind == "source_passage" and self.ref.source_snapshot_ref.workspace_id != self.ref.workspace_id:
            raise ValueError("Nested source workspace differs")
        return self

    def check_scope(self, scope):
        layer = "record" if self.content_kind.startswith("record_") else self.content_kind.removesuffix("_revision")
        if (
            layer != scope.layer
            or self.ref.workspace_id != scope.workspace_id
            or self.ref.corpus_snapshot_id != scope.corpus_snapshot_id
        ):
            raise ValueError("Reference is outside exact index scope")

    @property
    def key(self):
        return fingerprint([self.ref.model_dump(mode="json"), self.content_kind])

    @property
    def content_hash(self):
        return sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def document_hash(self):
        return fingerprint(self.model_dump(mode="json"))

    @property
    def source_identity(self):
        ref = self.ref.model_dump(mode="json")
        if self.ref.kind == "source_passage":
            return ref["source_snapshot_ref"]
        names = (
            ("page_id", "revision_id")
            if self.ref.kind == "wiki_revision"
            else ("task_id", "source_id", "source_revision_id", "record_revision_id", "package_sha256")
        )
        return {name: ref[name] for name in names}


class IndexSnapshot(StrictModel):
    scope: IndexScope
    source_manifest: dict[str, JsonValue]
    documents: list[IndexDocument] = Field(max_length=10000)

    @model_validator(mode="after")
    def complete(self):
        if not self.source_manifest or len(canonical(self.source_manifest).encode()) > 1048576:
            raise ValueError("A bounded source manifest is required")
        for doc in self.documents:
            doc.check_scope(self.scope)
        if len({doc.key for doc in self.documents}) != len(self.documents):
            raise ValueError("Duplicate index document")
        inventory = {doc.key: doc.document_hash for doc in self.documents}
        if self.source_manifest.get("index_documents") != inventory:
            raise ValueError("Source manifest inventory differs from complete snapshot")
        if sum(len(doc.text.encode()) for doc in self.documents) > 16 * 1024 * 1024:
            raise ValueError("Index snapshot body limit exceeded")
        return self


class IndexAuthority(Protocol):
    """Server-owned adapter: recheck permission/currentness/review/hash on EVERY call.

    Manifest must include eligibility-changing revisions/withdrawals and exact
    scope membership, plus index_documents (document key -> document hash).
    Build computes that inventory from the complete immutable snapshot. Search
    may reuse its snapshot-bound inventory, validating current eligibility and
    source-version signatures without re-reading every body. Only TopK refs are
    resolved below; cached inventory alone is never a current eligibility check. Resolve must
    return the same bounded, deterministic text
    projection and full ref used for building. Wiki adapters must validate current
    published revision, approved review and strict publication readback. Neither
    cached index flags nor caller-supplied eligibility may implement this boundary.
    Implementations must bound their I/O by the monotonic deadline; the core also
    rejects a result after that deadline but cannot preempt arbitrary Python I/O.
    """

    def current_manifest(self, scope: IndexScope, *, deadline: float) -> dict: ...
    def resolve(self, scope: IndexScope, ref: KnowledgeRef, content_kind: str, *, deadline: float) -> IndexDocument: ...
