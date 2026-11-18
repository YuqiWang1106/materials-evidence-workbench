"""Composition of existing study, Wiki and table ports for one frozen research turn."""

from copy import deepcopy

from pydantic import Field

from .core import fingerprint
from .knowledge_claim_contracts import KnowledgeSupport
from .knowledge_contracts import Id, StrictModel, TableAnalysisArtifactRef, TableSnapshotRef
from .research_chat_contracts import ResearchError, ResearchSelection, SourceFocus
from .study_wiki_evidence import StudySupportContext, read_support, support_navigation
from .table_chat_tools import TOOL_SCHEMAS, TableChatTools
from .table_chat_workflow import _check_selected_capacity
from .wiki_query_service import EvidenceRead, analysis_page

TABLE_NAMES = {s["name"] for s in TOOL_SCHEMAS}


class StudyList(StrictModel):
    source_id: Id
    unit_id: Id | None = None
    query: str | None = Field(default=None, min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)


class StudyRead(StrictModel):
    unit_id: Id
    fact_id: str | None = None
    evidence_index: int | None = Field(default=None, ge=0, le=19)
    citation_id: str | None = None
    part_index: int = Field(default=0, ge=0, le=10000)


class ProgramRead(StrictModel):
    id: Id
    part_index: int = Field(default=0, ge=0, le=10000)


MODELS = {"study_catalogue": StudyList, "study_read": StudyRead, "research_artifact": ProgramRead}
DESCRIPTIONS = {
    "study_catalogue": (
        "仅在未知ID时读取本轮已选文献当前研究单元/事实目录；已知样品或字段用query关键词定向过滤标签/字段/"
        "归属，不扫描全部分页；导航不是已读数值。已知unit_id及fact_id直接study_read；不启动重新抽取。"
    ),
    "study_read": (
        "读取冻结研究单元事实或原文引用；fact_id读取完整事实归属、条件和单位，"
        "evidence_index或citation_id读取原文与注册位置。候选未审核、未科学验证；不能冒充正式保存记录。"
    ),
    "research_artifact": "分页读取已登记的真实研究轮程序结果；历史结果不授予新计算权限。",
}


def freeze_unified(knowledge, focus, freeze_documents=None):
    from .wiki_query_service import WikiQueryService

    focus = SourceFocus.model_validate(focus).model_dump()
    wiki_query = WikiQueryService(knowledge)
    wiki = wiki_query.wiki
    units = StudySupportContext(wiki).units
    sources = focus["document_source_ids"]
    scopes = {}
    refs = []
    if sources:
        if freeze_documents is not None:
            # Existing freeze may have source passages even without saved records.
            try:
                scopes = freeze_documents(sources)["scopes"]
            except ResearchError as error:
                if error.code != "not_ready":
                    raise
        for sid in sources:
            current = [u for u in units.list_source(sid) if u["current_available"]]
            refs.extend(units.reference(u["unit_id"]) for u in current)
            if not current and not any(sid in s["source_ids"] for s in scopes.values()):
                raise ResearchError("not_ready")
    raws = []
    tables = []
    for tid in focus["table_version_ids"]:
        raw = wiki.ingestion_service._raw_artifacts().load("raw", tid)["payload"]
        raws.append(raw)
        _check_selected_capacity(raws)
    if len({r["table_id"] for r in raws}) != len(raws):
        raise ResearchError("invalid_request")
    if raws:
        corpus = wiki._corpus(sorted({r["source_id"] for r in raws}))
        for raw in raws:
            tables.append(
                TableSnapshotRef(
                    kind="table_snapshot",
                    workspace_id=wiki.workspace_id,
                    corpus_snapshot_id=corpus,
                    **{
                        k: raw[k]
                        for k in (
                            "source_id",
                            "asset_id",
                            "asset_sha256",
                            "table_id",
                            "table_version_id",
                            "source_region_id",
                            "table_range",
                        )
                    },
                    snapshot_sha256=fingerprint(raw),
                ).model_dump(mode="json")
            )
    # The unified Wiki scope is global; keep selected record and passage layers.
    scopes.pop("wiki", None)
    return ResearchSelection(
        scope_mode="unified",
        source_ids=sources,
        source_focus=focus,
        scopes=scopes,
        wiki_scope=wiki_query.scope,
        study_unit_refs=refs,
        table_refs=tables,
    ).model_dump(mode="json")


class SelectedTablePolicy:
    def __init__(self, refs):
        self.refs = {r["table_version_id"]: r for r in refs}

    def require(self, snapshots):
        for raw in snapshots:
            ref = self.refs.get(raw["table_version_id"])
            if ref is None:
                raise ResearchError("table_selection_required")
            if fingerprint(raw) != ref["snapshot_sha256"]:
                raise ResearchError("stale_context")


class UnifiedResearchTools:
    def __init__(self, owner, *, artifact_writer=None, artifact_owner=None):
        self.owner = owner
        self.registry = owner.registry
        self.selection = owner.selection
        self.wiki = owner.wiki_query.wiki
        self.policy = SelectedTablePolicy(self.selection["table_refs"])
        self.artifact_writer = artifact_writer
        self.artifact_owner = artifact_owner
        owner.wiki_query.compute_policy = self.policy
        owner.wiki_query.research_artifact_writer = self.save_artifact

    def raw(self, ref):
        raw = self.owner.wiki_query._raw({"ref": ref})
        service = self.wiki.ingestion_service
        service.store.asset_bytes(raw["asset_id"])
        if service.store.get_asset(raw["asset_id"])["sha256"] != raw["asset_sha256"]:
            raise ResearchError("stale_context")
        return raw

    def save_artifact(self, artifact):
        if self.artifact_writer is None:
            raise ResearchError("not_ready")
        payload = self.artifact_writer(artifact)
        tids = (
            [artifact["table_version_id"]]
            if "table_version_id" in artifact
            else [t["table_version_id"] for t in artifact["tables"]]
        )
        first = self.policy.refs[tids[0]]
        return TableAnalysisArtifactRef(
            kind="table_analysis_artifact",
            chat_kind="research",
            workspace_id=self.wiki.workspace_id,
            corpus_snapshot_id=first["corpus_snapshot_id"],
            chat_id=payload["chat_id"],
            turn_id=payload["turn_id"],
            artifact_id=payload["artifact_id"],
            table_version_ids=tids,
            artifact_sha256=fingerprint(payload),
        ).model_dump(mode="json")

    def entry(self, payload, kind, support, *, dependencies=None, program=None, selector=None):
        entry = {
            "kind": "wiki_evidence",
            "unified_source": True,
            "layer": "wiki",
            "scope": deepcopy(self.selection["wiki_scope"]),
            "wiki_binding": {},
            "payload": payload,
            "evidence_type": kind,
            "support": support,
            "ref": deepcopy(support["ref"]),
            "dependency_ids": dependencies or [],
            "selector": selector,
            "status": {
                "scientific_validation": False,
                "source_review": "unreviewed_candidate"
                if support["ref"]["kind"] == "study_unit_snapshot"
                else "reviewed",
                "availability": "current",
            },
            "source_role": "focus",
            "program": program,
        }
        entry["id"] = self.registry._id(entry)
        return entry

    def support(self, ref, kind="reported_evidence", scope=None):
        return KnowledgeSupport(
            support_id="support-" + fingerprint([ref, scope]), ref=ref, support_type=kind, scope=scope
        ).model_dump(mode="json")

    def table(self, name, args):
        ids = (
            [args["table_version_id"]] if name != "compare_tables" else [t["table_version_id"] for t in args["tables"]]
        )
        refs = []
        raws = []
        for tid in ids:
            ref = self.policy.refs.get(tid)
            if ref is None:
                raise ResearchError("table_selection_required")
            refs.append(ref)
            raws.append(self.raw(ref))
        self.policy.require(raws)
        result = TableChatTools({r["table_version_id"]: r for r in raws}).run(name, args)
        entries = []
        for ref in refs:
            # These dependencies are true snapshot supports, not fabricated reads.
            support = self.support(ref, "source_description")
            dep = {
                "kind": "unified_support",
                "layer": "wiki",
                "scope": deepcopy(self.selection["wiki_scope"]),
                "support": support,
                "ref": ref,
            }
            dep["id"] = self.registry._id(dep)
            entries.append(dep)
        program = None
        kind = "table_cells"
        support = entries[0]["support"]
        if "artifact" in result:
            ref = self.save_artifact(result["artifact"])
            program = {"artifact_ref": ref, "output": result["output"]}
            support = self.support(ref, "calculation")
            kind = "table_analysis"
        payload = analysis_page(result["output"], 0) if program else result["output"]
        entry = self.entry(payload, kind, support, dependencies=[e["id"] for e in entries], program=program)
        entries.append(entry)
        return self.registry.project(entries, {"items": [], "next_read": self.next_read(entry)})

    @staticmethod
    def next_read(entry):
        c = entry["payload"].get("analysis_coverage", {})
        return (
            [{"name": "research_artifact", "arguments": {"id": entry["id"], "part_index": c["part_index"] + 1}}]
            if c.get("part_index", 0) + 1 < c.get("part_count", 0)
            else []
        )

    def execute(self, name, args):
        if name in TABLE_NAMES:
            return self.table(name, args)
        if name == "research_artifact":
            entry = self.registry.item(args.id, "wiki_evidence")
            self.validate(entry)
            if not entry.get("unified_source") or not entry.get("program"):
                raise ResearchError("unsupported")
            new = deepcopy(entry)
            new["payload"] = analysis_page(entry["program"]["output"], args.part_index)
            new["id"] = self.registry._id(new)
            return self.registry.project([new], {"items": [], "next_read": self.next_read(new)})
        refs = self.selection["study_unit_refs"]
        context = StudySupportContext(self.wiki)
        if name == "study_catalogue":
            selected = [
                r
                for r in refs
                if r["source_id"] == args.source_id and (args.unit_id is None or r["unit_id"] == args.unit_id)
            ]
            if not selected:
                raise ResearchError("invalid_request")
            if args.unit_id is None and args.query is None:
                rows = [
                    {
                        "unit_id": r["unit_id"],
                        "source_id": r["source_id"],
                        "study_label": context.unit(r)["snapshot"]["payload"]["projection"].get("study_label"),
                    }
                    for r in selected
                ]
            else:
                rows = []
                for selected_ref in selected:
                    unit = context.unit(selected_ref)
                    projection = unit["snapshot"]["payload"]["projection"]
                    for fact in projection["facts"]:
                        support = self.support({**selected_ref, "fact_id": fact["id"]})
                        navigation = support_navigation(self.wiki, support, context=context) or {}
                        rows.append(
                            {"unit_id": unit["unit_id"], "fact_id": fact["id"], "name": fact.get("name"), **navigation}
                        )
                    rows.extend(
                        {"unit_id": unit["unit_id"], "citation_id": c["id"], "source_pointer": c.get("source_pointer")}
                        for c in projection["citations"]
                    )
            if args.query:
                terms = args.query.casefold().split()
                rows = [
                    r
                    for r in rows
                    if all(
                        t
                        in " ".join(
                            str(r.get(k, ""))
                            for k in ("study_label", "name", "object_label", "owner_label", "source_pointer")
                        ).casefold()
                        for t in terms
                    )
                ]
            return {
                "navigation": rows[args.offset : args.offset + args.limit],
                "navigation_matches": len(rows),
                "navigation_complete": True,
                "scientific_population_count": False,
                "has_more": args.offset + args.limit < len(rows),
                "next_offset": args.offset + args.limit if args.offset + args.limit < len(rows) else None,
                "facts_read": False,
            }
        ref = next((deepcopy(r) for r in refs if r["unit_id"] == args.unit_id), None)
        if ref is None:
            raise ResearchError("invalid_request")
        if args.fact_id and args.citation_id:
            raise ResearchError("invalid_request")
        ref["fact_id"] = args.fact_id
        scope = {"snapshot_entry_kind": "citation", "snapshot_entry_id": args.citation_id} if args.citation_id else None
        support = self.support(ref, scope=scope)
        selector = EvidenceRead(part_index=args.part_index, evidence_index=args.evidence_index)
        payload, kind = self.study_payload(support, selector)
        entry = self.entry(payload, kind, support, selector=selector.model_dump())
        return self.registry.project([entry], {"items": []})

    def study_payload(self, support, selector):
        payload, kind = read_support(self.wiki, support, selector, {})
        if payload["kind"] == "study_unit_fact":
            context = StudySupportContext(self.wiki)
            unit = context.unit(support["ref"])
            fact = payload["fact"]
            pointer = fact["source_pointer"]
            owner = fact["owner_pointer"]
            projection = unit["snapshot"]["payload"]["projection"]
            owned = [
                c
                for c in projection["context"]
                if c.get("candidate_id") == fact.get("candidate_id")
                and c.get("study_ref") == fact.get("study_ref")
                and (
                    pointer.startswith(c["source_pointer"] + "/")
                    or c["source_pointer"] == owner
                    or c["source_pointer"] in fact.get("applies_to_pointers", [])
                )
            ]
            methods = {p for c in owned for p in c.get("applicable_method_pointers", [])}
            methods.update(c["method_pointer"] for c in owned if c.get("method_pointer"))
            payload["owner_context"] = deepcopy(
                owned
                + [
                    c
                    for c in projection["context"]
                    if c not in owned
                    and c.get("candidate_id") == fact.get("candidate_id")
                    and c["source_pointer"] in methods
                ]
            )
            payload["navigation"] = support_navigation(self.wiki, support, context=context)
        return payload, kind

    def validate(self, entry):
        ref = entry.get("ref") or entry["support"]["ref"]
        if ref["kind"] == "study_unit_snapshot":
            from .study_wiki_evidence import resolve_support

            resolve_support(self.wiki, entry["support"])
            if entry.get("payload") is not None:
                payload, _ = self.study_payload(entry["support"], EvidenceRead.model_validate(entry["selector"]))
                if payload != entry["payload"]:
                    raise ResearchError("stale_context")
        elif ref["kind"] == "table_snapshot":
            self.raw(ref)
        elif ref["kind"] == "table_analysis_artifact":
            from .knowledge_table_artifact import read_table_artifact

            payload = read_table_artifact(
                self.wiki.ingestion_service, ref, allow_running=self.artifact_owner == (ref["chat_id"], ref["turn_id"])
            )
            if fingerprint(payload) != ref["artifact_sha256"]:
                raise ResearchError("stale_context")
        else:
            raise ResearchError("stale_context")
        return True

    def open_support(self, entry, evidence_index=None):
        self.validate(entry)
        value = deepcopy(entry)
        if entry["kind"] == "unified_support":
            payload, kind = self.owner.wiki_query._read_support(
                entry["support"], EvidenceRead(evidence_index=evidence_index)
            )
            value.update(location=payload, support_preview={"evidence_type": kind, "payload": deepcopy(payload)})
        elif entry["ref"]["kind"] == "study_unit_snapshot":
            selector = EvidenceRead.model_validate(entry["selector"])
            if evidence_index is not None:
                selector = selector.model_copy(update={"evidence_index": evidence_index})
            value["location"] = self.study_payload(entry["support"], selector)[0]
        else:
            value["location"] = entry.get("payload")
        if entry.get("program"):
            from .knowledge_table_artifact import read_table_artifact

            value["program"] = read_table_artifact(self.wiki.ingestion_service, entry["program"]["artifact_ref"])
        return value


def query_population_refs(entry, wiki):
    """Read the exact frozen population proving a count/coverage, including zero hits.

    Filters change which rows matched, not which records had to be inspected to
    establish an exhaustive total. An explicit context selection narrows that
    population. These are publishing dependencies, never additional read facts.
    """
    from .knowledge_contracts import QueryRequest, RecordRef

    query = entry.get("query")
    if entry["kind"] == "query_receipt":
        from .research_tasks_contracts import POLICY_VERSION

        saved = wiki.snapshot_store.get_json(entry["artifact_sha256"])
        if (saved["version"], saved["kind"], saved["scope"], saved["dependencies"]) != (
            POLICY_VERSION,
            entry["kind"],
            entry["scope"],
            entry["dependency_ids"],
        ):
            raise ResearchError("stale_context")
        query = saved["data"]["request"]
    request = QueryRequest.model_validate(query)
    scope = entry["scope"]
    if any(getattr(request, key) != scope[key] for key in ("collection_id", "corpus_snapshot_id", "use_policy")):
        raise ResearchError("stale_context")
    manifest = wiki.snapshot_store.read_corpus(request.corpus_snapshot_id)
    if any(manifest[key] != scope[key] for key in ("workspace_id", "collection_id", "use_policy")):
        raise ResearchError("stale_context")
    refs = [
        RecordRef(
            kind="record_revision",
            workspace_id=scope["workspace_id"],
            corpus_snapshot_id=request.corpus_snapshot_id,
            **{
                key: record[key]
                for key in (
                    "task_id",
                    "source_id",
                    "source_revision_id",
                    "record_revision_id",
                    "record_snapshot_id",
                    "package_sha256",
                )
            },
        ).model_dump(mode="json")
        for record in manifest["records"]
    ]
    if request.selection:
        selected = [ref.model_dump(mode="json") for ref in request.selection]
        for ref in selected:
            if not any(all(ref[key] == value for key, value in root.items() if key != "kind") for root in refs):
                raise ResearchError("stale_context")
        return selected
    return refs


def used_supports(registry, ids, *, wiki=None, validated_wiki_pages=None):
    """Real support closure shared with manual summary; never changes tool scope."""
    out = []
    seen = set()
    wiki_refs = {}

    def add(ref, key, scope=None, kind="reported_evidence"):
        support = KnowledgeSupport(
            support_id="support-" + fingerprint([key, ref, scope]),
            ref=ref,
            scope=scope,
            research_entry_id=key,
            support_type=kind,
        ).model_dump(mode="json")
        if support["support_id"] not in seen:
            seen.add(support["support_id"])
            out.append(support)

    for key in registry.closure(ids):
        entry = registry.item(key)
        if entry["kind"] in {"count", "query_receipt"}:
            if wiki is None:
                raise ResearchError("not_ready")
            for ref in query_population_refs(entry, wiki):
                add(ref, key, entry["scope"], "source_description")
        if entry.get("support"):
            support = entry["support"]
            add(support["ref"], key, support.get("scope"), support["support_type"])
        if entry.get("wiki_binding"):
            b = entry["wiki_binding"]
            if wiki is None:
                raise ResearchError("not_ready")
            digest = fingerprint(b)
            if digest not in wiki_refs:
                from .knowledge_publication import KnowledgePublicationService
                from .knowledge_wiki_adapter import KnowledgeWikiAdapter

                adapter = KnowledgeWikiAdapter(wiki, KnowledgePublicationService(wiki))
                page = (validated_wiki_pages or {}).get(digest)
                if page is not None and adapter._identity(page) != b:
                    raise ResearchError("stale_context")
                frozen = adapter._freeze_pages([page]) if page is not None else adapter.freeze_bindings([b])
                scope = {k: frozen[k] for k in ("workspace_id", "collection_id", "corpus_snapshot_id", "use_policy")}
                wiki_refs[digest] = (frozen["wiki_refs"][0], scope)
            ref, scope = wiki_refs[digest]
            add(ref, key, scope, "source_description")
        for field in ("ref", "other_ref", "context_ref"):
            ref = entry.get(field)
            if ref and not entry.get("support"):
                add(ref, key, entry["scope"])
        artifact = entry.get("provenance", {}).get("research_artifact_ref")
        if artifact:
            add(artifact, key, kind="calculation")
    return out
