"""Service-owned Wiki navigation and support capabilities for global research chat."""

import time
from copy import deepcopy
from typing import Annotated, Literal, Union

from pydantic import Field, WithJsonSchema
from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import StateConflict, canonical, fingerprint
from .knowledge_contracts import VERSION, GetRequest, Id, StrictModel
from .knowledge_index_store import IndexFailure
from .knowledge_snapshot import KnowledgeIntegrityError
from .research_chat_contracts import ResearchError
from .storage.models import KnowledgeItem
from .table_chat_tools import _CONFIG
from .wiki_query_index import chunk_key, page_chunks, text_parts

ANALYSIS_PAGE_BYTES = 9000  # Leave room for identity, status and next_read within 12 KB.
ANALYSIS_PAGE_VERSION = "wiki-analysis-pages-v1"


def analysis_page(payload, part_index):
    """Page complete summary columns, splitting only explicit group units if needed.

    Full computed artifacts remain unchanged. Small summaries retain their wire
    shape. A single unsplittable leaf still fails instead of silently losing data.
    """
    if len(canonical(payload).encode()) <= ANALYSIS_PAGE_BYTES:
        if part_index:
            raise ResearchError("invalid_request")
        return deepcopy(payload)
    from .research_chat_projection import ReadProjectionLimit

    comparison = "tables" in payload
    tables = payload["tables"] if comparison else [payload]
    pages = []
    total_columns = sum(len(t["columns"]) for t in tables)
    for table_index, table in enumerate(tables):

        def wrap(column):
            one = {**deepcopy(table), "columns": [deepcopy(column)]}
            if comparison:
                return {**deepcopy(payload), "tables": [one]}
            return one

        for column in table["columns"]:
            candidate = wrap(column)
            if len(canonical(candidate).encode()) <= ANALYSIS_PAGE_BYTES:
                pages.append((candidate, table_index))
                continue
            groups = column.get("group_statistics", {}).get("groups", [])
            if not groups:
                raise ReadProjectionLimit()
            # These are already explicit summary groups; preserve omitted_groups
            # from the original summary separately from page coverage.
            for group_index, group in enumerate(groups):
                part = deepcopy(column)
                part["group_statistics"]["groups"] = [deepcopy(group)]
                part["group_statistics"]["page_coverage"] = {
                    "offset": group_index,
                    "returned": 1,
                    "summary_groups": len(groups),
                    "complete": len(groups) == 1,
                }
                candidate = wrap(part)
                if len(canonical(candidate).encode()) > ANALYSIS_PAGE_BYTES:
                    raise ReadProjectionLimit()
                pages.append((candidate, table_index))
    if not pages:
        raise ReadProjectionLimit()
    if part_index >= len(pages):
        raise ResearchError("invalid_request")
    result, table_index = pages[part_index]
    result["analysis_coverage"] = {
        "part_index": part_index,
        "part_count": len(pages),
        "complete": False,
        "table_index": table_index,
        "table_count": len(tables),
        "total_columns": total_columns,
        "returned_columns": 1,
        "full_result_in_artifact": True,
    }
    return result


def analysis_next(payload, handle):
    coverage = payload.get("analysis_coverage", {})
    if coverage.get("part_index", 0) + 1 < coverage.get("part_count", 0):
        return [
            {
                "name": "wiki_evidence",
                "arguments": {
                    "handle": handle,
                    "selector": {"operation": "read", "part_index": coverage["part_index"] + 1},
                },
            }
        ]
    return []


class WikiSearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=4096)
    relation_depth: Literal[0, 1, 2] = 0
    top_k: int = Field(default=8, ge=1, le=10)


class WikiReadArgs(StrictModel):
    hit_id: Id
    support_offset: int = Field(default=0, ge=0, le=200)


class EvidenceRead(StrictModel):
    operation: Literal["read"] = "read"
    part_index: int = Field(default=0, ge=0, le=10000)
    evidence_index: int | None = Field(default=None, ge=0, le=19)


class TableInspect(StrictModel):
    operation: Literal["inspect_table"]
    columns: list[str] | None = Field(default=None, max_length=32)


class TableRows(StrictModel):
    operation: Literal["read_table_rows"]
    columns: list[str] | None = Field(default=None, max_length=32)
    start_row: int = Field(ge=1)
    limit: int = Field(default=10, ge=1, le=50)


class TableAnalyze(StrictModel):
    operation: Literal["analyze_table"]
    config: Annotated[dict, WithJsonSchema(_CONFIG)]


class ComparisonInput(StrictModel):
    handle: Id
    config: Annotated[dict, WithJsonSchema(_CONFIG)]


class TableCompare(StrictModel):
    operation: Literal["compare_tables"]
    tables: list[ComparisonInput] = Field(min_length=2, max_length=10)


class WikiEvidenceArgs(StrictModel):
    handle: Id
    selector: Annotated[
        Union[EvidenceRead, TableInspect, TableRows, TableAnalyze, TableCompare], Field(discriminator="operation")
    ] = Field(default_factory=EvidenceRead)


MODELS = {"wiki_search": WikiSearchArgs, "wiki_read": WikiReadArgs, "wiki_evidence": WikiEvidenceArgs}
DESCRIPTIONS = {
    "wiki_search": (
        "检索当前工作区可用已发布Wiki的完整分段目录。返回混合或明确降级的TopK导航，不是已读事实、"
        "全量统计或最优结论；用wiki_read读取命中。relation_depth为0/1/2，最多3个种子页加3个相关页；"
        "扩展按原问题筛选，不代表科学关系。"
    ),
    "wiki_read": (
        "读取服务器返回的hit_id，保留固定修订、章节、结论和段落身份，给出受限support handle与next_read。"
        "Wiki是编辑知识；数值应沿support读取原始依据。导航片段不能直接引用。"
    ),
    "wiki_evidence": (
        "仅读取已返回的support handle，不能提交路径、policy或新的来源。read返回固定事实/原段/已有分析；"
        "分析按next_read及part_index续读。已返回的程序证据ID仅可read同一保存结果，不扩展表权限。"
        "仅table_snapshot支持inspect_table/read_table_rows/analyze_table/compare_tables；"
        "单格或原文引用不授权全表。分析使用完整授权快照，匹配已有artifact优先复用；config.units必须留空，"
        "不猜单位。compare每张表须有已读handle。不提供预测数值或科学验证。"
    ),
}

GLOBAL_KINDS = {"wiki_navigation", "wiki_content", "wiki_support", "wiki_evidence"}


class WikiQueryService:
    def __init__(self, knowledge, registry=None):
        self.knowledge = knowledge
        self.registry = registry
        self.adapter = getattr(knowledge, "wiki_adapter", None)
        if self.adapter is None:
            raise ResearchError("not_ready")
        self.wiki = self.adapter.wiki
        self.scope = {
            "workspace_id": self.adapter.workspace_id,
            "collection_id": self.adapter.collection_id,
            "use_policy": "source_reviewed",
        }
        if registry is not None and registry.selection.get("wiki_scope") != self.scope:
            raise ResearchError("stale_context")
        self.hybrid = getattr(knowledge, "wiki_hybrid", None)
        if self.hybrid is None:
            from .wiki_hybrid_search import WikiHybridSearch

            self.hybrid = WikiHybridSearch(self.wiki.ingestion_service.store, self.scope)
            knowledge.wiki_hybrid = self.hybrid
        if self.hybrid.scope != self.scope or self.hybrid.store is not self.wiki.ingestion_service.store:
            raise ResearchError("stale_context")
        self.index = self.hybrid.index
        from .wiki_relations import WikiRelationsService

        self.relations = getattr(knowledge, "wiki_relations", None)
        if self.relations is None:
            self.relations = knowledge.wiki_relations = WikiRelationsService(self.adapter)
        if self.relations.adapter is not self.adapter:
            raise ResearchError("stale_context")
        # ResearchTools owns this query service for exactly one turn. History is
        # validated without consuming current-turn delivered-page slots.
        self._read_bindings = set()

    def selection(self):
        return {"scope_mode": "all_wiki", "source_ids": [], "scopes": {}, "wiki_scope": self.scope}

    def deadline(self):
        from .knowledge_service import READ_CANCELED, READ_DEADLINE

        if READ_CANCELED.get() is not None and READ_CANCELED.get().is_set():
            raise ResearchError("cancelled")
        deadline = READ_DEADLINE.get() or time.monotonic() + self.knowledge.timeout_seconds
        if time.monotonic() >= deadline:
            raise ResearchError("timeout")
        return deadline

    def current(self, binding):
        self.deadline()
        try:
            page = self.adapter._current(binding["page_id"])
        except (KeyError, ValueError, StateConflict) as exc:
            if getattr(exc, "code", None) in {
                "timeout",
                "cancelled",
                "QUERY_TIMEOUT",
                "REQUEST_CANCELED",
                "ARCHIVE_UNAVAILABLE",
            }:
                raise
            raise ResearchError("stale_context") from None
        self.deadline()
        if page is None or self.adapter._identity(page) != binding:
            raise ResearchError("stale_context")
        return page

    def _entry(self, kind, **data):
        entry = {"kind": kind, "layer": "wiki", "scope": deepcopy(self.scope), **deepcopy(data)}
        entry["id"] = self.registry._id(entry)
        return entry

    def _navigation(self, chunk, route=None):
        return self._entry(
            "wiki_navigation",
            wiki_binding=chunk["wiki_binding"],
            **(route or {}),
            section_id=chunk["section_id"],
            claim_id=chunk["claim_id"],
            part_index=chunk["part_index"],
            part_count=chunk["part_count"],
            title=chunk["title"],
            heading=chunk["heading"],
            snippet=chunk["body"][:200],
            chunk_key=chunk_key(chunk),
        )

    def _chunk(self, entry, page):
        for chunk in page_chunks(page):
            if chunk_key(chunk) == entry["chunk_key"]:
                return chunk
        raise ResearchError("stale_context")

    def eligible_pages(self):
        """Server eligibility port shared by lexical and future hybrid retrieval."""
        pages = []
        unavailable = 0
        # Only current published pointers in this workspace are candidates. A
        # revoked unrelated page is excluded, never a global conversation fence.
        with Session(self.wiki.ingestion_service.store.engine) as session:
            ids = list(
                session.scalars(
                    select(KnowledgeItem.page_id)
                    .where(
                        KnowledgeItem.workspace_id == self.scope["workspace_id"],
                        KnowledgeItem.published_revision_id.is_not(None),
                        KnowledgeItem.availability.notin_(["deleted", "withdrawn", "availability_unknown"]),
                    )
                    .order_by(KnowledgeItem.page_id)
                )
            )
        for identifier in ids:
            self.deadline()
            try:
                page = self.adapter._current(identifier)
            except (KeyError, ValueError, StateConflict):
                page = None
            if page is None:
                unavailable += 1
            else:
                pages.append(page)
        return pages, unavailable

    @staticmethod
    def _route(entry):
        return {
            key: deepcopy(entry[key]) for key in ("relation_path", "relation_edges", "relation_depth") if key in entry
        }

    def _validate_route(self, entry):
        path = entry.get("relation_path", [entry["wiki_binding"]])
        edges = entry.get("relation_edges", [])
        if len(path) > 3 or len(edges) != len(path) - 1 or path[-1] != entry["wiki_binding"]:
            raise ResearchError("stale_context")
        try:
            for edge in edges:
                self.relations.validate_edge(edge, self.deadline())
        except (ValueError, KeyError, StateConflict) as exc:
            if getattr(exc, "code", None) in {
                "timeout",
                "cancelled",
                "QUERY_TIMEOUT",
                "REQUEST_CANCELED",
                "ARCHIVE_UNAVAILABLE",
            }:
                raise
            raise ResearchError("stale_context") from None

    def _slots(self, bindings):
        keys = {fingerprint(b) for b in bindings}
        if len(self._read_bindings | keys) > 8:
            raise ResearchError("wiki_page_limit")
        return keys

    def _related(self, hits, pages, query, depth, deadline, top_k):

        seeds = []
        for hit in hits:
            if hit["wiki_binding"] not in seeds:
                seeds.append(hit["wiki_binding"])
            if len(seeds) == 3:
                break
        if not seeds:
            return hits, {}, dict(seed_count=0, expanded_pages=0, truncated=False, truncation_reasons=[])
        from .project_embedding import MAX_INPUT_BYTES, input_identity
        from .wiki_hybrid_search import fuse

        query_terms = set(self.index.cache.tokenizer.tokens(query, query=True))
        chosen = {}
        initial = {c["wiki_binding"]["page_id"] for c in hits}
        query_vector = None
        if self.hybrid.embeddings is not None and len(query.encode()) <= MAX_INPUT_BYTES:
            cache = self.hybrid.embeddings
            key = input_identity(cache.dispatcher.project_id, query)[0]["cache_key"]
            query_vector = cache.ready([key], deadline=deadline).get(key)

        def rank(candidates, limit):
            chunks = []
            lexical = []
            for page in candidates:
                if page["page_id"] in initial:
                    continue
                for chunk in page_chunks(page):
                    if time.monotonic() >= deadline:
                        raise IndexFailure("INDEX_TIMEOUT")
                    chunks.append(chunk)
                    try:
                        terms = set(self.index.cache.tokenizer.tokens(chunk["text"]))
                    except ValueError:
                        continue
                    score = len(query_terms & terms)
                    if score:
                        lexical.append((score, chunk))
            lexical.sort(key=lambda x: (-x[0], chunk_key(x[1])))
            # Read only vectors already prepared by P2. Expansion never calls
            # ensure(), reserves budget, or changes the 16-chunk preparation cap.
            semantic, _ = self.hybrid.score(chunks, query_vector, deadline=deadline)
            ranked = fuse([c for _, c in lexical[:20]], [c for c in semantic if c["score"] > 0], top_k=8)
            selected = []
            for chunk in ranked:
                pid = chunk["wiki_binding"]["page_id"]
                if pid in selected:
                    continue
                if len(selected) >= limit:
                    break
                chosen[pid] = chunk
                selected.append(pid)
            return selected

        graph, paths = self.relations.walk(
            seeds, depth, deadline=deadline, pages=pages, choose=rank, max_extra=min(3, max(0, top_k - len(seeds)))
        )
        extras = [chosen[n["page_id"]] for n in graph["nodes"] if n["page_id"] in chosen]
        routes = {
            pid: dict(relation_path=p, relation_edges=e, relation_depth=len(p) - 1)
            for pid, (p, e) in paths.items()
            if len(p) > 1
        }
        # Keep the initial candidates intact. Reserve at most three of the eight
        # output slots for genuinely additional, question-matching pages.
        combined = hits[: max(0, top_k - len(extras))] + extras
        return (
            combined,
            routes,
            dict(
                seed_count=len(seeds),
                expanded_pages=len(extras),
                truncated=graph["truncated"],
                truncation_reasons=graph["truncation_reasons"],
                unavailable_count=graph["unavailable_count"],
            ),
        )

    def search(self, args):
        deadline = self.deadline()
        pages, unavailable = self.eligible_pages()
        self.deadline()
        try:
            hits, retrieval = self.hybrid.search(pages, args.query, deadline=deadline, top_k=min(args.top_k, 8))
        except IndexFailure as error:
            raise ResearchError("timeout" if error.code == "INDEX_TIMEOUT" else "not_ready") from None
        routes = {}
        relations = dict(seed_count=0, expanded_pages=0, truncated=False, truncation_reasons=[])
        if args.relation_depth:
            try:
                hits, routes, relations = self._related(
                    hits, pages, args.query, args.relation_depth, deadline, min(args.top_k, 8)
                )
            except IndexFailure:
                relations.update(truncated=True, truncation_reasons=["deadline"])
        retrieval = {**retrieval, "relation_depth": args.relation_depth}
        entries = []
        for chunk in hits:
            try:
                self.current(chunk["wiki_binding"])
            except ResearchError as error:
                if error.code != "stale_context":
                    raise
                unavailable += 1
                continue
            entries.append(self._navigation(chunk, routes.get(chunk["wiki_binding"]["page_id"])))
        return self.registry.project(
            entries,
            {
                "items": [],
                "retrieval": retrieval,
                "exhaustive_within_scope": False,
                "unavailable_pages": unavailable,
                "total_matches": None,
                "relations": relations,
            },
        )

    def read(self, args):
        entry = self.registry.item(args.hit_id, "wiki_navigation")
        self._validate_route(entry)
        slots = self._slots([entry["wiki_binding"]])
        page = self.current(entry["wiki_binding"])
        chunk = self._chunk(entry, page)
        content = page["revision"]["content"]
        all_chunks = list(page_chunks(page))
        study = page["revision"]["dependency_manifest"].get("study_unit")
        status = {
            "source_review": "unreviewed_candidate" if study else "reviewed",
            "scientific_validation": False,
            "availability": "current",
            "evidence_level": "published_editorial",
        }
        context = {}
        context_status = {}
        for field in ("conditions", "uncertainties"):
            values = content.get(field, [])
            complete = len(canonical(values).encode()) <= 1600
            context[field] = values if complete else []
            context_status[field] = "read" if values and complete else "read_separately" if values else "not_reported"
        read = self._entry(
            "wiki_content",
            wiki_binding=chunk["wiki_binding"],
            **self._route(entry),
            chunk_key=chunk_key(chunk),
            section_id=chunk["section_id"],
            claim_id=chunk["claim_id"],
            part_index=chunk["part_index"],
            part_count=chunk["part_count"],
            title=chunk["title"],
            heading=chunk["heading"],
            body=chunk["body"],
            claim_type=chunk["claim_type"],
            status=status,
            **context,
            context_status=context_status,
        )
        supports = {s["support_id"]: s for s in page["revision"]["supports"]}
        selected = chunk["support_ids"]
        if args.support_offset > len(selected):
            raise ResearchError("invalid_request")
        entries = [read]
        handles = []
        from .study_wiki_evidence import StudySupportContext, support_navigation

        navigation_context = StudySupportContext(self.wiki)
        for sid in selected[args.support_offset : args.support_offset + 8]:
            if sid not in supports:
                raise ResearchError("stale_context")
            support = self._entry(
                "wiki_support",
                wiki_binding=chunk["wiki_binding"],
                **self._route(entry),
                support=deepcopy(supports[sid]),
            )
            navigation = support_navigation(self.wiki, support["support"], context=navigation_context)
            if navigation is not None:
                self.registry.wiki_support_navigation[support["id"]] = navigation
            entries.append(support)
            handles.append(
                {
                    "handle": support["id"],
                    "support_id": sid,
                    "ref_kind": support["support"]["ref"]["kind"],
                    "support_type": support["support"]["support_type"],
                    "full_snapshot_authorized": support["support"]["ref"]["kind"] == "table_snapshot",
                }
            )
            if navigation is not None:
                handles[-1]["navigation"] = deepcopy(navigation)
        next_reads = []
        for other in all_chunks:
            same = other["section_id"] == chunk["section_id"] and other["claim_id"] == chunk["claim_id"]
            metadata = other["section_id"] in {"wiki-conditions", "wiki-uncertainties", "wiki-research_question"}
            if (same and other["part_index"] == chunk["part_index"] + 1) or (
                metadata and other["part_index"] == 0 and not same
            ):
                nav = self._navigation(other, self._route(entry))
                entries.append(nav)
                next_reads.append(
                    {"name": "wiki_read", "arguments": {"hit_id": nav["id"]}, "section_id": other["section_id"]}
                )
        if args.support_offset + 8 < len(selected):
            next_reads.append(
                {"name": "wiki_read", "arguments": {"hit_id": args.hit_id, "support_offset": args.support_offset + 8}}
            )
        if self.registry.selection.get("scope_mode") == "unified":
            read["dependency_ids"] = [e["id"] for e in entries if e["kind"] == "wiki_support"]
            read["id"] = self.registry._id(read)
        result = self.registry.project(
            entries,
            {
                "items": [],
                "supports": handles,
                "next_read": next_reads,
                "support_coverage": {"returned": len(handles), "total": len(selected), "offset": args.support_offset},
                "conditions_status": context_status["conditions"],
                "uncertainties_status": context_status["uncertainties"],
            },
        )
        self._read_bindings.update(slots)
        return result

    def support(self, handle, *, page=None):
        entry = self.registry.item(handle, "wiki_support")
        self._validate_route(entry)
        page = self.current(entry["wiki_binding"]) if page is None else page
        if self.adapter._identity(page) != entry["wiki_binding"]:
            raise ResearchError("stale_context")
        support = next(
            (s for s in page["revision"]["supports"] if s["support_id"] == entry["support"]["support_id"]), None
        )
        if support != entry["support"]:
            raise ResearchError("stale_context")
        return entry, page, support

    def _raw(self, support):
        ref = support["ref"]
        if ref["kind"] != "table_snapshot":
            raise ResearchError("unsupported")
        raw = self.wiki.ingestion_service._raw_artifacts().load("raw", ref["table_version_id"])["payload"]
        if fingerprint(raw) != ref["snapshot_sha256"] or any(
            raw[k] != ref[k]
            for k in (
                "source_id",
                "asset_id",
                "asset_sha256",
                "table_id",
                "table_version_id",
                "source_region_id",
                "table_range",
            )
        ):
            raise ResearchError("stale_context")
        return raw

    @staticmethod
    def _config(config):
        # Existing tools accept caller labels; global Wiki reads have no user
        # authorization for new units. Source units remain in snapshot metadata.
        if not isinstance(config, dict) or config.get("units", []) != []:
            raise ResearchError("invalid_request")
        return deepcopy(config)

    def _artifact(self, ref):
        from .knowledge_table_artifact import read_table_artifact

        payload = read_table_artifact(self.wiki.ingestion_service, ref)
        if fingerprint(payload) != ref["artifact_sha256"]:
            raise ResearchError("stale_context")
        return payload["artifact"]

    def _table(self, entry, page, support, selector):
        from .table_chat_tools import TableChatTools, _summary

        raw = self._raw(support)
        name = selector.operation
        if name == "read":
            name = "inspect_table"
            arguments = {}
        else:
            arguments = selector.model_dump(mode="json", exclude={"operation"})
        inputs = [(entry, page, support)]
        snapshots = {raw["table_version_id"]: raw}
        if name == "compare_tables":
            arguments = {"tables": []}
            inputs = []
            for selected in selector.tables:
                other, other_page, other_support = self.support(selected.handle)
                snapshot = self._raw(other_support)
                snapshots[snapshot["table_version_id"]] = snapshot
                inputs.append((other, other_page, other_support))
                arguments["tables"].append(
                    {"table_version_id": snapshot["table_version_id"], "config": self._config(selected.config)}
                )
            if entry["id"] not in [i[0]["id"] for i in inputs]:
                raise ResearchError("invalid_request")
        else:
            arguments["table_version_id"] = raw["table_version_id"]
            if name == "analyze_table":
                arguments["config"] = self._config(selector.config)
        tables = TableChatTools(snapshots)
        # Reuse a pinned artifact only when the normalized full input matches.
        result = None
        reused = None
        if name == "analyze_table":
            _, normalized = tables._analysis_args(arguments)
            for candidate in page["revision"]["supports"]:
                if candidate["ref"]["kind"] != "table_analysis_artifact":
                    continue
                artifact = self._artifact(candidate["ref"])
                if (
                    artifact.get("kind") == "table_analysis"
                    and artifact.get("table_version_id") == raw["table_version_id"]
                    and artifact.get("config") == normalized
                ):
                    output = _summary(artifact["result"])
                    if len(canonical(output).encode()) <= ANALYSIS_PAGE_BYTES:
                        return output, "table_analysis", inputs, {"reused_artifact": candidate["ref"]}
                    result = {"output": output, "artifact": artifact}
                    reused = candidate["ref"]
                    break
        if result is None:
            if name in {"analyze_table", "compare_tables"} and getattr(self, "compute_policy", None) is not None:
                self.compute_policy.require(list(snapshots.values()))
            result = tables.run(name, arguments)
        provenance = {
            "program_result_sha256": fingerprint(result.get("artifact", result["output"])),
            "reused_artifact": reused,
        }
        if "artifact" in result:
            if getattr(self, "research_artifact_writer", None) is not None and reused is None:
                provenance["research_artifact_ref"] = self.research_artifact_writer(result["artifact"])
            record = {
                "version": "wiki-local-analysis-v1",
                "scope": self.scope,
                "dependencies": [i[0]["id"] for i in inputs],
                "selector": selector.model_dump(mode="json"),
                "artifact": result["artifact"],
                "output": result["output"],
            }
            if len(canonical(record).encode()) > 2 * 1024 * 1024:
                raise ResearchError("task_record_limit")
            self.deadline()
            provenance["artifact_sha256"] = self.wiki.snapshot_store.put_json(record)
        return result["output"], "table_analysis" if "artifact" in result else "table_cells", inputs, provenance

    def _read_support(self, support, selector):
        ref = support["ref"]
        if ref["kind"] == "table_snapshot":
            if not isinstance(selector, EvidenceRead) or selector.part_index or selector.evidence_index is not None:
                raise ResearchError("invalid_request")
            raw = self._raw(support)
            store = self.wiki.ingestion_service.store
            store.asset_bytes(ref["asset_id"])
            if store.get_asset(ref["asset_id"])["sha256"] != ref["asset_sha256"]:
                raise ResearchError("stale_context")
            from .table_chat_tools import TableChatTools

            payload = TableChatTools({raw["table_version_id"]: raw}).run(
                "read_table_rows",
                {
                    "table_version_id": raw["table_version_id"],
                    "start_row": raw["rows"][0]["row"],
                    "limit": 12,
                    "columns": None,
                },
            )["output"]
            return {**payload, "ref": deepcopy(ref), "scientific_validation": False}, "table_cells"
        if ref["kind"] == "wiki_revision":
            if not isinstance(selector, EvidenceRead) or selector.part_index or selector.evidence_index is not None:
                raise ResearchError("invalid_request")
            try:
                page = self.relations.resolve(ref, self.deadline())
            except (ValueError, KeyError, StateConflict):
                raise ResearchError("stale_context") from None
            chunk = next(page_chunks(page), None)
            if chunk is None:
                raise ResearchError("stale_context")
            payload = {
                key: deepcopy(chunk[key])
                for key in (
                    "wiki_binding",
                    "section_id",
                    "claim_id",
                    "part_index",
                    "part_count",
                    "title",
                    "heading",
                    "body",
                    "claim_type",
                )
            }
            return {**payload, "ref": deepcopy(ref), "scientific_validation": False}, "wiki_content"
        if ref["kind"] == "study_unit_snapshot":
            from .study_wiki_evidence import read_support

            if not isinstance(selector, EvidenceRead):
                raise ResearchError("unsupported")
            binding = getattr(self, "_study_support_manifest", None)
            try:
                return read_support(self.wiki, support, selector, binding or {})
            except (ValueError, StateConflict, KeyError):
                raise ResearchError("stale_context") from None
        if ref["kind"] == "table_analysis_artifact":
            from .table_chat_tools import _summary

            artifact = self._artifact(ref)
            if artifact["kind"] == "table_analysis":
                payload = _summary(artifact["result"])
            elif artifact["kind"] == "table_comparison":
                payload = {
                    "tables": [_summary(t["result"]) for t in artifact["tables"]],
                    "comparison_mode": "side_by_side",
                    "joined": False,
                    "unit_conversion": False,
                }
            else:
                raise ResearchError("unsupported")
            if selector.evidence_index is not None:
                raise ResearchError("invalid_request")
            return analysis_page(payload, selector.part_index), "table_analysis"
        if ref["kind"] not in {"record_revision", "sample_test_context", "assertion", "evidence", "source_passage"}:
            raise ResearchError("unsupported")
        scope = support.get("scope")
        if not scope or scope["workspace_id"] != self.scope["workspace_id"]:
            raise ResearchError("stale_context")
        result = self.knowledge.get(
            GetRequest(
                schema_version=VERSION,
                request_id="wiki-evidence",
                target=ref,
                use_policy=scope["use_policy"],
                view="location",
            )
        )
        row = result.results[0]
        if selector.evidence_index is not None:
            evidence = getattr(row, "evidence_refs", [])
            if selector.evidence_index >= len(evidence):
                raise ResearchError("invalid_request")
            result = self.knowledge.get(
                GetRequest(
                    schema_version=VERSION,
                    request_id="wiki-evidence-leaf",
                    target=evidence[selector.evidence_index],
                    use_policy=scope["use_policy"],
                    view="location",
                )
            )
            row = result.results[0]
        payload = row.model_dump(mode="json")
        quote = (
            payload.get("quote") if row.kind == "source_passage" else (payload.get("evidence_data") or {}).get("quote")
        )
        if quote is not None:
            parts = text_parts(quote)
            index = selector.part_index
            if index >= len(parts):
                raise ResearchError("invalid_request")
            if row.kind == "source_passage":
                payload["quote"] = parts[index]
            else:
                payload["evidence_data"]["quote"] = parts[index]
            payload["quote_coverage"] = {"part_index": index, "part_count": len(parts), "complete": len(parts) == 1}
            return payload, "source_passage" if row.kind == "source_passage" else "document_evidence"
        if selector.part_index:
            raise ResearchError("invalid_request")
        return payload, "record_fact"

    def _wiki_navigation(self, entry, support, selector):
        if selector.operation != "read":
            raise ResearchError("unsupported")
        if selector.part_index or selector.evidence_index is not None:
            raise ResearchError("invalid_request")
        path = entry.get("relation_path", [entry["wiki_binding"]])
        if len(path) >= 3:
            raise ResearchError("wiki_relation_limit")
        try:
            target = self.relations.resolve(support["ref"], self.deadline())
        except (ValueError, KeyError, StateConflict):
            raise ResearchError("stale_context") from None
        target_binding = self.adapter._identity(target)
        if target_binding in path:
            raise ResearchError("wiki_relation_limit")
        self._slots([target_binding])
        row = next(
            (
                r
                for r in self.relations._rows(self.current(entry["wiki_binding"]))
                if r["relation_kind"] == "wiki_reference" and r["basis"]["support_id"] == support["support_id"]
            ),
            None,
        )
        if row is None:
            raise ResearchError("stale_context")
        edge = self.relations._edge(row["basis"], {"binding": target_binding}, "wiki_reference", row["anchor_key"])
        self.relations.validate_edge(edge, self.deadline())
        chunk = next(page_chunks(target), None)
        if chunk is None:
            raise ResearchError("stale_context")
        route = dict(
            relation_path=path + [target_binding],
            relation_edges=entry.get("relation_edges", []) + [edge],
            relation_depth=len(path),
        )
        navigation = self._navigation(chunk, route)
        return self.registry.project(
            [navigation],
            dict(
                items=[],
                next_read=[dict(name="wiki_read", arguments={"hit_id": navigation["id"]})],
                status={
                    "scientific_validation": False,
                    "evidence_level": "published_editorial",
                    "navigation_only": True,
                },
            ),
        )

    def _program_read(self, entry, selector):
        if not isinstance(selector, EvidenceRead):
            raise ResearchError("unsupported")
        if selector.evidence_index is not None:
            raise ResearchError("invalid_request")
        self.validate([entry["id"]])
        record = self.program(entry)
        if record is None:
            raise ResearchError("unsupported")
        # Reopen the same immutable program only after every original support and
        # Wiki route passes current authority. This handle never authorizes a table.
        inputs = [self.support(key)[0] for key in record["dependencies"]]
        slots = self._slots([i["wiki_binding"] for i in inputs])
        payload = analysis_page(record["output"], selector.part_index)
        data = {k: deepcopy(v) for k, v in entry.items() if k not in ("id", "kind", "layer", "scope")}
        data["payload"] = payload
        if "analysis_coverage" in payload:
            data["provenance"]["analysis_projection_version"] = ANALYSIS_PAGE_VERSION
        evidence = self._entry("wiki_evidence", **data)
        result = self.registry.project([evidence], {"items": [], "next_read": analysis_next(payload, evidence["id"])})
        self._read_bindings.update(slots)
        return result

    def evidence(self, args):
        selected = self.registry.item(args.handle)
        if selected["kind"] == "wiki_evidence":
            return self._program_read(selected, args.selector)
        entry, page, support = self.support(args.handle)
        selector = args.selector
        if support["ref"]["kind"] == "wiki_revision":
            return self._wiki_navigation(entry, support, selector)
        slots = self._slots([entry["wiki_binding"]])
        for existing in self.registry.entries.values():
            if (
                existing["kind"] == "wiki_evidence"
                and existing["handle"] == args.handle
                and existing["selector"] == selector.model_dump(mode="json")
                and existing.get("provenance", {}).get("artifact_sha256")
            ):
                if existing["provenance"].get("analysis_projection_version"):
                    return self._program_read(existing, EvidenceRead())
                self.validate([existing["id"]])
                result = self.registry.project(
                    [existing], {"items": [], "next_read": analysis_next(existing["payload"], existing["id"])}
                )
                self._read_bindings.update(slots)
                return result
        if support["ref"]["kind"] == "table_snapshot":
            if selector.operation == "read" and (selector.part_index or selector.evidence_index is not None):
                raise ResearchError("invalid_request")
            payload, kind, inputs, provenance = self._table(entry, page, support, selector)
        else:
            if selector.operation != "read":
                raise ResearchError("unsupported")
            self._study_support_manifest = page["revision"]["dependency_manifest"]
            payload, kind = self._read_support(support, selector)
            inputs = [(entry, page, support)]
            provenance = {}
        if kind == "table_analysis" and provenance.get("artifact_sha256"):
            payload = analysis_page(payload, 0)
            if "analysis_coverage" in payload:
                provenance["analysis_projection_version"] = ANALYSIS_PAGE_VERSION
        # Recheck after computation/readback before making any fact visible.
        for item, _, _ in inputs:
            self.support(item["id"])
        status = {
            "scientific_validation": False,
            "source_review": "unreviewed_candidate" if support["ref"]["kind"] == "study_unit_snapshot" else "reviewed",
            "availability": "current",
            "evidence_level": "local_analysis" if kind == "table_analysis" else "source_read",
            "use_policy": support.get("scope", {}).get("use_policy") if support.get("scope") else "explore",
        }
        evidence = self._entry(
            "wiki_evidence",
            wiki_binding=entry["wiki_binding"],
            **self._route(entry),
            handle=entry["id"],
            selector=selector.model_dump(mode="json"),
            dependency_ids=[i[0]["id"] for i in inputs],
            evidence_type=kind,
            payload=payload,
            status=status,
            provenance=provenance,
        )
        next_read = analysis_next(payload, evidence["id"] if provenance.get("artifact_sha256") else args.handle)
        coverage = payload.get("quote_coverage", {})
        if coverage.get("part_index", 0) + 1 < coverage.get("part_count", 0):
            next_read = [
                {
                    "name": "wiki_evidence",
                    "arguments": {
                        "handle": args.handle,
                        "selector": {**selector.model_dump(mode="json"), "part_index": coverage["part_index"] + 1},
                    },
                }
            ]
        slots = self._slots([i[0]["wiki_binding"] for i in inputs])
        result = self.registry.project([evidence], {"items": [], "next_read": next_read})
        self._read_bindings.update(slots)
        return result

    def program(self, entry):
        digest = entry.get("provenance", {}).get("artifact_sha256")
        if not digest:
            return None
        try:
            store = self.wiki.snapshot_store
            if store._path("objects", digest).stat().st_size > 2 * 1024 * 1024:
                raise ResearchError("task_record_limit")
            record = store.get_json(digest)
            output = record["output"]
            projection = entry["provenance"].get("analysis_projection_version")
            if projection is not None:
                if projection != ANALYSIS_PAGE_VERSION:
                    raise ResearchError("stale_context")
                output = analysis_page(output, entry["payload"]["analysis_coverage"]["part_index"])
            if (
                record["version"] != "wiki-local-analysis-v1"
                or record["scope"] != entry["scope"]
                or record["dependencies"] != entry["dependency_ids"]
                or record["selector"] != entry["selector"]
                or output != entry["payload"]
                or fingerprint(record["artifact"]) != entry["provenance"]["program_result_sha256"]
            ):
                raise ResearchError("stale_context")
            return record
        except (KeyError, OSError, KnowledgeIntegrityError):
            raise ResearchError("stale_context") from None

    def validate(self, ids, *, pages=None):
        # Proofs live only for this validation call and its immediate support export.
        pages = {} if pages is None else pages
        for key in self.registry.closure(ids):
            entry = self.registry.item(key)
            if entry["kind"] not in GLOBAL_KINDS:
                raise ResearchError("stale_context")
            self._validate_route(entry)
            digest = fingerprint(entry["wiki_binding"])
            if digest not in pages:
                pages[digest] = self.current(entry["wiki_binding"])
            page = pages[digest]
            if entry["kind"] in {"wiki_navigation", "wiki_content"}:
                self._chunk(entry, page)
            elif entry["kind"] == "wiki_support":
                self.support(key, page=page)
            elif entry["kind"] == "wiki_evidence":
                # Source/version/policy checked via dependency closure; rendering
                # always uses the immutable saved payload, never model prose.
                self.support(entry["handle"], page=page)
                self.program(entry)
        return {"valid": True, "ids": list(ids)}

    def prepare_history(self, focus_ids):
        from .study_wiki_evidence import StudySupportContext, support_navigation

        navigation_context = StudySupportContext(self.wiki)
        self.registry.wiki_support_navigation.clear()
        retired = []
        for key in list(self.registry.entries):
            try:
                self.validate([key])
                entry = self.registry.item(key)
                if entry["kind"] == "wiki_support":
                    navigation = support_navigation(self.wiki, entry["support"], context=navigation_context)
                    if navigation is not None:
                        self.registry.wiki_support_navigation[key] = navigation
            except (StateConflict, KeyError):
                retired.append(key)
            except ResearchError as error:
                if error.code != "stale_context":
                    raise
                retired.append(key)
        # Stale history is explicitly unavailable. It never vetoes fresh global
        # queries; a final claim cannot select an entry removed from the ledger.
        for key in retired:
            self.registry.entries.pop(key, None)
        return [key for key in focus_ids if key in self.registry.entries]

    def open_support(self, entry, *, evidence_index=None):
        self.validate([entry["id"]])
        result = deepcopy(entry)
        if entry["kind"] == "wiki_support":
            _, page, support = self.support(entry["id"])
            self._study_support_manifest = page["revision"]["dependency_manifest"]
            payload, kind = self._read_support(support, EvidenceRead(evidence_index=evidence_index))
            result.update(location=payload, support_preview={"evidence_type": kind, "payload": deepcopy(payload)})
        elif entry["kind"] == "wiki_evidence":
            support = self.support(entry["handle"])[2]
            program = self.program(entry)
            if program is not None:
                result["program"] = program
            if support["ref"]["kind"] == "study_unit_snapshot":
                page = self.current(entry["wiki_binding"])
                self._study_support_manifest = page["revision"]["dependency_manifest"]
                selector = EvidenceRead.model_validate(entry["selector"])
                if entry["evidence_type"] == "record_fact":
                    selector = EvidenceRead(evidence_index=0 if evidence_index is None else evidence_index)
                result["location"] = self._read_support(support, selector)[0]
            elif support["ref"]["kind"] in {"record_revision", "sample_test_context", "assertion"}:
                selector = EvidenceRead.model_validate(entry["selector"])
                if entry["evidence_type"] == "record_fact":
                    selector = EvidenceRead(evidence_index=0 if evidence_index is None else evidence_index)
                elif evidence_index is not None and evidence_index != selector.evidence_index:
                    raise ResearchError("invalid_request")
                result["location"] = self._read_support(support, selector)[0]
            elif support["ref"]["kind"] in {"evidence", "source_passage"}:
                result["location"] = self._read_support(support, EvidenceRead.model_validate(entry["selector"]))[0]
            else:
                result["location"] = entry["payload"]
        elif entry["kind"] == "wiki_content":
            result["location"] = {
                "kind": "wiki_revision",
                **entry["wiki_binding"],
                "section_id": entry["section_id"],
                "claim_id": entry["claim_id"],
                "part_index": entry["part_index"],
                "body": entry["body"],
                **{key: deepcopy(entry[key]) for key in ("conditions", "uncertainties", "context_status")},
            }
        return result

    def execute(self, name, args):
        from .knowledge_service import READ_DEADLINE

        inherited = READ_DEADLINE.get()
        token = READ_DEADLINE.set(inherited or time.monotonic() + self.knowledge.timeout_seconds)
        try:
            return {"wiki_search": self.search, "wiki_read": self.read, "wiki_evidence": self.evidence}[name](args)
        finally:
            READ_DEADLINE.reset(token)


def evidence_text(entry):
    """Server-authored rendering retains typed cells/results and coverage labels."""
    if entry["kind"] == "wiki_content":
        value = "已发布 Wiki 内容（编辑知识，非原始实测）：" + entry["body"]
        for field, label in (("conditions", "适用条件"), ("uncertainties", "不确定性")):
            if entry[field]:
                value += "\n" + label + "：" + "；".join(entry[field])
            elif entry["context_status"][field] == "read_separately":
                value += "\n" + label + "尚未读取完整，请沿 next_read 继续核对。"
        return value
    if entry.get("unified_source") and entry["payload"].get("kind", "").startswith("study_unit_"):
        return "研究单元候选（未审核、未科学验证）：" + canonical(entry["payload"])
    labels = {
        "study_unit_context": "研究单元上下文（未审核）",
        "table_cells": "原表单元格",
        "table_analysis": "程序描述分析（未科学验证）",
        "record_fact": "已保存事实",
        "document_evidence": "文档证据",
        "source_passage": "来源原段（保留解析状态）",
    }
    payload = entry["payload"]
    kind = entry["evidence_type"]
    if kind == "record_fact":
        from .research_chat_evidence import EvidenceRegistry

        text = (
            (payload.get("label") or payload["ref"].get("source_id", "来源记录"))
            + "："
            + "；".join(
                EvidenceRegistry._field(v) + "为 " + EvidenceRegistry._value_text(v) for v in payload.get("values", [])
            )
        )
        text = text or canonical(payload)
    elif kind in {"document_evidence", "source_passage"}:
        text = payload.get("quote") or (payload.get("evidence_data") or {}).get("quote") or canonical(payload)
    else:
        text = canonical(payload)
    return labels[kind] + "：" + text
