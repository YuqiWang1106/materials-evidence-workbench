"""Read published artifacts with version-bound locators, without a second evidence store."""

from __future__ import annotations

import base64 as base64
import json
import logging
import os
import re as re
import time
from hashlib import sha256 as sha256
from pathlib import Path as Path

from .core import StateConflict as StateConflict
from .core import atomic_json as atomic_json
from .core import canonical as canonical
from .core import fingerprint as fingerprint
from .core.contract_entity import normalize_name as normalize_name
from .extraction.entity_contract import ENTITY_TYPES as ENTITY_TYPES
from .extraction.entity_index_expand import expand_graph as expand_graph
from .extraction.entity_resolve import highlight_regions as highlight_regions
from .knowledge import agent_retrieval as _retrieval
from .knowledge.agent_evidence import AgentEvidence as _AgentEvidence
from .knowledge.agent_publications import AgentPublications as _AgentPublications
from .knowledge.agent_publications import PublicationCache as _PublicationCache
from .mcp_contract import protocol as _protocol
from .mcp_contract.protocol import TOOLS as TOOLS
from .mcp_contract.protocol import AgentKnowledgeError as AgentKnowledgeError
from .mcp_contract.protocol import encode_evidence_ref as encode_evidence_ref
from .mcp_contract.requests import checked_around as _checked_around
from .mcp_contract.requests import checked_text as _text
from .mcp_contract.requests import source_list as _checked_source_list

_STATUS = _protocol.STATUS
_LOGGER = logging.getLogger(__name__)


class _SearchTiming:
    """Collect opt-in timing without retaining query or source identities."""

    def __init__(self):
        self.enabled = os.environ.get("KBS_AGENT_SEARCH_TIMING") == "1"
        self.values = {}
        self.started = 0
        if self.enabled:
            try:
                self.started = time.perf_counter_ns()
            except Exception:
                self.enabled = False

    def stage(self, name):
        if self.enabled:
            try:
                now = time.perf_counter_ns()
                self.values[name + "_ms"] = max(0, (now - self.started) // 1_000_000)
                self.started = now
            except Exception:
                self.enabled = False

    def finish(self, scope, raw, page, mode):
        if self.enabled:
            try:
                values = {
                    **self.values,
                    "candidate_count": len(raw["entities"]),
                    "returned_count": page["returned"],
                    "omitted_count": page["omitted"],
                    "source_count": len(scope["source_ids"] or []),
                    "scope_is_all": int(scope["source_ids"] is None),
                    "retrieval_mode": mode,
                }
                _LOGGER.info("agent_search_timing %s", json.dumps(values, sort_keys=True))
            except Exception:
                pass


class AgentKnowledgeService:
    def __init__(self, ingestion_service, entity_store, entity_index, workspace_id):
        self.ingestion_service, self.store = ingestion_service, ingestion_service.store
        self.entity_store, self.entity_index = entity_store, entity_index
        self.workspace_id = _text(workspace_id, "workspace_id")
        if entity_index.workspace_id != workspace_id:
            raise ValueError("Entity index workspace does not match the Agent workspace")

        self._publications = _AgentPublications(self.store, self.entity_store, self.workspace_id)
        self._evidence = _AgentEvidence(self.store, self.workspace_id)

    def _response(self, data, *, page=None, warnings=()):
        return {
            "schema_version": _protocol.SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "data": data,
            "page": page or {"returned": 1, "omitted": 0, "next_offset": None},
            "warnings": list(dict.fromkeys(warnings)),
        }

    def capabilities(self):
        vectors = self.entity_index.vectors
        return self._response(
            {
                "tools": list(TOOLS),
                "ready": True,
                "features": {"source_scope_v1": True, "short_citation_links_v1": True},
                "retrieval_mode": "hybrid" if vectors and vectors.dispatcher else "lexical",
            }
        )

    _source_list = staticmethod(_checked_source_list)

    def _view(self, scope, cache):
        view = self._publications.contributions(scope, cache)
        if view is None:
            return None, self.entity_index
        index = self.entity_index.request_view(
            list(view.rows.values()),
            view.attributes,
            view,
            {row["source_id"]: row["title"] for row in scope["sources"]},
            source_ids=scope["source_ids"],
        )
        return view, index

    def _published(self, source_id, cache, task_id=None):
        return self._publications.published(source_id, cache, task_id)

    def _projection(self):
        return _retrieval.AgentRetrieval(
            self.workspace_id, self.entity_store, self._publications, self._evidence, self._view
        )

    def _result(self, projected):
        return self._response(projected.data, page=projected.page, warnings=projected.warnings)

    def read_entity(self, entity_id, sections=None, offset=0, limit=None, source_ids=None):
        request = _retrieval.EntityQuery(entity_id, sections, offset, limit, source_ids)
        return self._result(self._projection().read_entity(request))

    def search(
        self,
        query,
        entity_type=None,
        section=None,
        source=None,
        offset=0,
        limit=None,
        source_names=None,
        source_ids=None,
    ):
        request = _retrieval.SearchQuery(
            query=query,
            entity_type=entity_type,
            section=section,
            source=source,
            offset=offset,
            limit=limit,
            source_names=source_names,
            source_ids=source_ids,
        )
        return self._result(self._projection().search(request, _SearchTiming))

    def expand(self, entity_ids, focus, to_type=None, sections=None, offset=0, limit=None, source_ids=None):
        request = _retrieval.ExpandQuery(
            entity_ids=entity_ids,
            focus=focus,
            to_type=to_type,
            sections=sections,
            offset=offset,
            limit=limit,
            source_ids=source_ids,
        )
        return self._result(self._projection().expand(request))

    def _link_path(self, short):
        return self._evidence.link_path(short)

    def read_evidence(self, evidence_ref, around=2, source_ids=None):
        source_ids = self._source_list(source_ids, "source_ids")
        evidence_ref = _text(evidence_ref, "evidence_ref")
        if evidence_ref.startswith("kbref_"):
            evidence_ref = self._evidence.resolve_link(evidence_ref)
        around = _checked_around(around)
        payload = _protocol.decode_evidence_ref(evidence_ref, self.workspace_id)
        if source_ids is not None and payload["source_id"] not in source_ids:
            raise AgentKnowledgeError(
                "SOURCE_OUT_OF_SCOPE", "The evidence belongs to a source outside the selected scope."
            )
        if self.entity_store.entity(self.workspace_id, payload["entity_id"]) is None:
            raise AgentKnowledgeError("REFERENCE_NOT_FOUND", "The evidence entity is unavailable in this workspace.")
        cache: _PublicationCache = {}
        pub = self._published(payload["source_id"], cache, payload["task_id"])
        self._publications.scope(source_ids, cache)
        evidence = self._evidence.readback(pub, payload, around)
        self._published(payload["source_id"], {}, payload["task_id"])
        self._evidence.remember_links(evidence)
        return self._response({"evidence": evidence})
