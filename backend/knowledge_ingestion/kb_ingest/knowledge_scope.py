"""Collection membership is configured by the server, never the request."""

from dataclasses import dataclass
from threading import RLock

from .knowledge_snapshot import KnowledgeIntegrityError


@dataclass(frozen=True)
class CollectionScope:
    workspace_id: str
    collection_id: str
    version: str
    source_ids: frozenset[str]
    policy_version: str


class KnowledgeScopeRegistry:
    def __init__(self, scopes=()):
        self._scopes = {}
        self._lock = RLock()
        for scope in scopes:
            self.replace(scope)

    def replace(self, scope):
        if not isinstance(scope, CollectionScope) or not isinstance(scope.source_ids, frozenset):
            raise ValueError("Trusted immutable collection configuration required")
        with self._lock:
            self._scopes[(scope.workspace_id, scope.collection_id)] = scope

    def resolve(self, workspace_id, collection_id):
        with self._lock:
            scope = self._scopes.get((workspace_id, collection_id))
        if scope is None:
            raise KnowledgeIntegrityError(
                "WORKSPACE_MISMATCH" if any(k[1] == collection_id for k in self._scopes) else "REFERENCE_NOT_FOUND"
            )
        return scope

    def check_manifest(self, manifest):
        scope = self.resolve(manifest["workspace_id"], manifest["collection_id"])
        if manifest["collection_version"] != scope.version or manifest["policy_version"] != scope.policy_version:
            raise KnowledgeIntegrityError("SNAPSHOT_REVIEW_CHANGED")
        if not set(manifest["sources"]) <= scope.source_ids:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        return scope
