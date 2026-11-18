"""Immutable content addressed resultsets with unguessable server cursors."""

import secrets
import time
from threading import RLock

from .knowledge_snapshot import KnowledgeIntegrityError


class KnowledgeResultStore:
    def __init__(self, snapshots, *, clock=time.time, ttl_seconds=300):
        self.snapshots = snapshots
        self.clock = clock
        self.ttl = ttl_seconds
        self._cursors = {}
        self._lock = RLock()

    def put(self, payload):
        return self.snapshots.put_json({**payload, "expires_at": self.clock() + self.ttl})

    def cursor(self, resultset, offset):
        with self._lock:
            token = secrets.token_urlsafe(32)
            self._cursors[token] = (resultset, offset)
        return token

    def read(self, cursor, fingerprint):
        with self._lock:
            binding = self._cursors.get(cursor)
        if binding is None:
            raise KnowledgeIntegrityError("CURSOR_MISMATCH")
        (resultset, offset) = binding
        payload = self.snapshots.get_json(resultset)
        if self.clock() >= payload["expires_at"]:
            raise KnowledgeIntegrityError("RESULT_SET_EXPIRED")
        if payload["fingerprint"] != fingerprint:
            raise KnowledgeIntegrityError("CURSOR_MISMATCH")
        return (resultset, offset, payload)
