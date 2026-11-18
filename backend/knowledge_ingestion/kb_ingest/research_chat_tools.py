"""Narrow read tools; model-selected IDs never choose a policy or source scope."""

import time
from copy import deepcopy
from typing import Literal

from pydantic import Field

from .core import fingerprint
from .extraction.entity_search import (
    ENTITY_DESCRIPTIONS,
    ENTITY_MODELS,
    read_entity,
)
from .extraction.entity_search import (
    RESULT_BYTES as ENTITY_RESULT_BYTES,
)
from .extraction.entity_search import (
    expand as entity_expand,
)
from .extraction.entity_search import (
    search as entity_search,
)
from .knowledge_contracts import (
    VERSION,
    CompareRequest,
    ContextValidateRequest,
    GetRequest,
    Id,
    IndexBuildRequest,
    IndexStatusRequest,
    QueryFilter,
    QueryRequest,
    RecordCatalogueRequest,
    SampleTestContextRef,
    SearchRequest,
    StrictModel,
)
from .knowledge_service import READ_DEADLINE
from .knowledge_snapshot import KnowledgeIntegrityError
from .research_chat_contracts import ResearchError
from .research_chat_evidence import EvidenceRegistry
from .research_chat_projection import ReadProjectionLimit, bounded
from .research_chat_unified import (
    DESCRIPTIONS as UNIFIED_DESCRIPTIONS,
)
from .research_chat_unified import (
    MODELS as UNIFIED_MODELS,
)
from .research_chat_unified import (
    TABLE_NAMES,
    UnifiedResearchTools,
)
from .research_chat_vocabulary import FieldsArgs, SelectedSourceVocabulary
from .research_target_wire import TargetArgs, target_profiles, target_schema
from .research_tasks_bridge import ResearchTasksBridge
from .table_chat_tools import TOOL_SCHEMAS
from .wiki_query_service import DESCRIPTIONS as WIKI_DESCRIPTIONS
from .wiki_query_service import MODELS as WIKI_MODELS
from .wiki_query_service import WikiQueryService


class ConstraintsArgs(StrictModel):
    target_id: Id
    context_ids: list[Id] = Field(
        max_length=20,
        description="非空为显式已读候选；[]配非空query_receipt_ids表示从同一完整连续查询链精确选择全部匹配候选，最多20项，不表示全库。",
    )
    query_receipt_ids: list[Id] = Field(max_length=20)


class ReferenceArgs(StrictModel):
    context_ids: list[Id] = Field(min_length=1, max_length=20)
    comparison_ids: list[Id] = Field(max_length=20)


class PlanArgs(StrictModel):
    target_id: Id
    context_ids: list[Id] = Field(min_length=1, max_length=20)


class Empty(StrictModel):
    pass


class QueryArgs(StrictModel):
    filters: list[QueryFilter] = Field(min_length=1, max_length=20)
    page_size: int = Field(ge=1, le=20)
    cursor: Id | None
    selection_ids: list[Id] = Field(
        default_factory=list,
        max_length=20,
        description="仅接受已读record层、可解析为样品/测试上下文的ID（含带context_ref的fact）；不是user_target/count/query_receipt或其他层ID。省略或[]表示整个冻结record范围；用户目标交research_constraints.target_id，错误选择不会自动清空。",
    )


class GetArgs(StrictModel):
    id: Id
    evidence_index: int | None = Field(ge=0, le=19)


class CompareArgs(StrictModel):
    item_ids: list[Id] = Field(min_length=2, max_length=20)
    profile_id: Id
    operations: list[Literal["side_by_side", "condition_diff"]] = Field(min_length=1, max_length=2)


class PassageArgs(StrictModel):
    source_id: Id
    physical_page: int | None = Field(ge=1)
    segment_id: Id | None
    page_size: int = Field(
        ge=1,
        le=20,
        description="仅有页号时先用1小页读取；优先已有具体证据或segment，不为回查一处原文读取全部无关段落。",
    )
    cursor: Id | None


class CatalogueArgs(StrictModel):
    source_id: Id
    context_id: Id | None
    kind: Literal["contexts", "assertions", "evidence"]
    page_size: int = Field(ge=1, le=20)
    cursor: Id | None


class SearchArgs(StrictModel):
    layer: Literal["record", "source_passage", "wiki"]
    query: str = Field(min_length=1, max_length=4096)
    top_k: int = Field(ge=1, le=10)


class ValidateArgs(StrictModel):
    ids: list[Id] = Field(min_length=1, max_length=40)


MODELS = {
    "research_target": TargetArgs,
    "research_constraints": ConstraintsArgs,
    "research_reference": ReferenceArgs,
    "research_plan": PlanArgs,
    "research_search": SearchArgs,
    "research_capabilities": Empty,
    "research_fields": FieldsArgs,
    "research_query": QueryArgs,
    "research_get": GetArgs,
    "research_compare": CompareArgs,
    "research_passages": PassageArgs,
    "research_validate": ValidateArgs,
    "research_catalogue": CatalogueArgs,
}
DESCRIPTIONS = {
    "research_target": (
        "从本轮真实用户输入绑定目标，只提交合法字段profile_id和逐字唯一quote，由程序定位字符位置。"
        "profile是字段声明，不是论文/资料名称；schema枚举及research_capabilities.target_vocabulary给出合法I"
        "D和标签，不证明来源存在该指标。不得改数字/单位/空白或补目标；工具参数错误先按具体错误修正，"
        "只有原句语法不支持才向用户澄清，不把性能百分数自动当配方质量基准。"
    ),
    "research_constraints": (
        "逐项核对已登记用户目标。推荐context_ids=[]并选择同一query_group的完整连续query_receipt_ids，"
        "程序精确选取最多20个已读上下文；缺页先补齐，不混不同过滤/selection。"
        "非空context_ids保留显式候选模式，无回执仅限已检查候选。完整性只针对该query匹配人口，"
        "不代表全部配方。不能自填事实或跨测试拼接。"
    ),
    "research_reference": (
        "给已读sample/test及已登记比较ID生成性能参考；无训练模型，固定不能数值预测，说明就绪缺口，不做性能排名。"
    ),
    "research_plan": (
        "依据用户目标与已读sample/test编制未验证方案，保留来源过程参数与归属，缺参/步骤顺序待确认；"
        "不提交实验或执行任务。"
    ),
    "research_search": (
        "在本轮固定单层来源中做关键词TopK召回。首次缺失时零模型构建同范围派生缓存，共用本轮剩余期限；"
        "失效不换范围。结果仅navigation，必须get后才能引用；非穷尽，不可据数量统计。截断正文的尾部未索引，"
        "来源标签只是资料。"
    ),
    "research_capabilities": (
        "仅在不清楚服务器能力或合法目标profile时读取声明目录；已有足够信息无需重复读取。"
        "声明不是来源实际观测，没有全文搜索时不可假称已检索。"
    ),
    "research_fields": (
        "仅在需要发现查询selector时读取选源实际词汇；已有所需context/fact可直接get或复用。"
        "已知field_id时缩小目录，找到所需query_template即可query，不必读完全部词汇页。"
        "has_more是词汇目录分页，不是样本枚举覆盖；来源标签只是资料。"
    ),
    "research_query": (
        "使用已有实际观测selector或research_fields找到的query_template，在冻结record范围精确查询；"
        "selection_ids省略/[]为全冻结record范围，非空只接受record样品/测试上下文ID，"
        "不接受用户目标ID（交constraints.target_id）；不要把域声明映射的零结果当作概念缺失。"
        "完整count可直接用于最终答案，无需get或枚举每个匹配项；TopK不可统计。"
        "大结果仅返回count及完整本页context导航，facts_included=false时必须get后引用科学值。"
    ),
    "research_get": (
        "仅读取operations含get的已返回ID；evidence_index只选该项公开的evidence_indices；原段/"
        "evidence叶仅读取完整quote，关联facts未读取。count已可直接选作最终count答案，应validate而非get；"
        "null读取实体或事实，不推断测试。"
    ),
    "research_compare": "比较已读sample/test上下文；只执行许可的现有程序比较，不自行算差值或排名。",
    "research_passages": (
        "在独立原段scope内回查所需原文。已有具体证据优先get对应evidence_index，已有segment则定向读取；"
        "仅有页号先page_size=1，不反复扫描无关段落。完整页有界，超限按next_read缩页，不截断或推断缺失；"
        "文本可能是解析或模型转写。"
    ),
    "research_catalogue": "读取已选来源的有界样品/测试、字段qualifier或证据目录；目录项不是事实，需get叶节点读取。",
    "research_validate": "重新验证已读依据在其原层scope和policy下仍然可用。",
}

MODELS.update(dict.fromkeys(TABLE_NAMES))
MODELS.update(UNIFIED_MODELS)
DESCRIPTIONS.update(UNIFIED_DESCRIPTIONS)
MODELS.update(WIKI_MODELS)
DESCRIPTIONS.update(WIKI_DESCRIPTIONS)
MODELS.update(ENTITY_MODELS)
DESCRIPTIONS.update(ENTITY_DESCRIPTIONS)


class ResearchTools:
    def __init__(
        self,
        knowledge,
        selection,
        *,
        prior=None,
        passage_reader=None,
        user_message=None,
        message_reader=None,
        artifact_writer=None,
        artifact_owner=None,
    ):
        self.knowledge = knowledge
        self.registry = EvidenceRegistry(selection, prior=prior)
        self.selection = self.registry.selection
        self.passage_reader = passage_reader
        self.wiki_query = (
            WikiQueryService(knowledge, self.registry)
            if self.selection.get("scope_mode") in ("all_wiki", "unified")
            else None
        )
        self.unified = (
            UnifiedResearchTools(self, artifact_writer=artifact_writer, artifact_owner=artifact_owner)
            if self.selection.get("scope_mode") == "unified"
            else None
        )
        self.tasks = ResearchTasksBridge(self, user_message=user_message, message_reader=message_reader)
        self.registry.query_receipt_navigation = self.tasks.query_navigation

    def schemas(self):
        if self.unified:
            names = set(WIKI_MODELS) | {"research_validate", "research_artifact"}
            if "record" in self.selection["scopes"]:
                names.update(
                    {
                        "research_target",
                        "research_constraints",
                        "research_reference",
                        "research_plan",
                        "research_fields",
                        "research_query",
                        "research_get",
                        "research_compare",
                        "research_catalogue",
                        "research_capabilities",
                    }
                )
            if "source_passage" in self.selection["scopes"]:
                names.update({"research_passages", "research_get"})
            if self.selection["scopes"]:
                names.add("research_search")
            if any("get" in self.registry.operations(e) for e in self.registry.entries.values()):
                names.add("research_get")
            if self.selection["study_unit_refs"]:
                names.update({"study_catalogue", "study_read"})
            if getattr(self.knowledge, "entity_store", None) is not None:
                names.update(ENTITY_MODELS)
            schemas = [
                {
                    "type": "function",
                    "name": name,
                    "description": DESCRIPTIONS[name],
                    "parameters": target_schema() if name == "research_target" else MODELS[name].model_json_schema(),
                    "strict": False,
                }
                for name in sorted(names)
            ]
            if self.selection["table_refs"]:
                schemas.extend(deepcopy(TOOL_SCHEMAS))
            return schemas
        models = (
            {**WIKI_MODELS, "research_validate": ValidateArgs}
            if self.wiki_query
            else {
                k: v
                for k, v in MODELS.items()
                if k not in {*WIKI_MODELS, *UNIFIED_MODELS, *TABLE_NAMES, *ENTITY_MODELS}
            }
        )
        return [
            {
                "type": "function",
                "name": name,
                "description": DESCRIPTIONS[name],
                "parameters": target_schema() if name == "research_target" else model.model_json_schema(),
                "strict": False,
            }
            for name, model in models.items()
        ]

    def prepare_history(self, focus_ids):
        if self.unified:
            for ref in self.selection["table_refs"]:
                self.unified.raw(ref)
            from .study_wiki_evidence import StudySupportContext

            context = StudySupportContext(self.wiki_query.wiki)
            for ref in self.selection["study_unit_refs"]:
                context.unit(ref)
            retired = []
            for key in list(self.registry.entries):
                try:
                    self.validate([key])
                except (ValueError, KeyError):
                    retired.append(key)
            for key in retired:
                self.registry.entries.pop(key, None)
            self.history_unavailable_ids = retired
            return [k for k in focus_ids if k in self.registry.entries]
        if self.wiki_query:
            return self.wiki_query.prepare_history(focus_ids)
        self.prepare(list(dict.fromkeys([*focus_ids, *self.registry.entries])))
        return focus_ids

    def scope(self, layer):
        scope = self.selection["scopes"].get(layer)
        if scope is None:
            raise ResearchError("not_ready")
        return scope

    def _context(self, key):
        item = self.registry.item(key)
        if item["layer"] != "record":
            raise ResearchError("invalid_request")
        ref = item.get("context_ref") or item.get("ref")
        return SampleTestContextRef.model_validate(ref)

    def prepare(self, ids):
        if self.wiki_query:
            return self.validate(ids)
        try:
            for layer, scope in self.selection["scopes"].items():
                roots = scope.get("wiki_refs", []) if layer == "wiki" else scope.get("record_refs", [])
                if layer == "wiki":
                    manifest = self.knowledge.snapshots.read_corpus(scope["corpus_snapshot_id"])
                    if fingerprint(manifest) != scope["manifest_sha256"] or not set(manifest["sources"]) <= set(
                        self.selection["source_ids"]
                    ):
                        raise ResearchError("stale_context")
                for start in range(0, len(roots), 40):
                    result = self.knowledge.validate_context(
                        ContextValidateRequest(
                            schema_version=VERSION,
                            request_id="research-scope",
                            use_policy=scope["use_policy"],
                            refs=roots[start : start + 40],
                        )
                    )
                    if not result.valid:
                        raise ResearchError("stale_context")
                if layer == "source_passage":
                    if self.passage_reader is None:
                        raise ResearchError("not_ready")
                    for source_id in scope["source_ids"]:
                        self.passage_reader(scope, {"source_id": source_id, "page_size": 1, "cursor": None})
            return self.validate(ids)
        except KnowledgeIntegrityError:
            raise ResearchError("stale_context") from None

    def validate(self, ids, *, wiki_pages=None):
        if self.unified:
            wiki_ids = []
            for key in self.registry.closure(ids):
                entry = self.registry.item(key)
                if entry.get("unified_source") or entry["kind"] == "unified_support":
                    self.unified.validate(entry)
                elif entry["kind"].startswith("wiki_"):
                    wiki_ids.append(key)
                else:
                    refs = [entry[k] for k in ("ref", "context_ref", "other_ref") if entry.get(k)]
                    if refs:
                        result = self.knowledge.validate_context(
                            ContextValidateRequest(
                                schema_version=VERSION,
                                request_id="unified-validate",
                                use_policy=entry["scope"]["use_policy"],
                                refs=refs,
                            )
                        )
                        if not result.valid:
                            raise ResearchError("stale_context")
                    if entry["kind"] == "count":
                        query = QueryRequest.model_validate(entry["query"])
                        query = query.model_copy(update={"page": query.page.model_copy(update={"cursor": None})})
                        result = self.knowledge.query(query)
                        if (
                            not result.completeness.matching_complete
                            or result.completeness.total_matches != entry["count"]
                        ):
                            raise ResearchError("stale_context")
                    if entry["kind"] in {"user_target", "task_result", "query_receipt", "comparison_receipt"}:
                        self.tasks.validate([key])
            if wiki_ids:
                self.wiki_query.validate(wiki_ids, pages=wiki_pages)
            return {"valid": True, "ids": list(ids)}
        if self.wiki_query:
            return self.wiki_query.validate(ids)
        try:
            for layer, refs in self.registry.references(ids).items():
                for start in range(0, len(refs), 40):
                    result = self.knowledge.validate_context(
                        ContextValidateRequest(
                            schema_version=VERSION,
                            request_id="research-validate",
                            use_policy=self.scope(layer)["use_policy"],
                            refs=refs[start : start + 40],
                        )
                    )
                    if not result.valid:
                        raise ResearchError("stale_context")
            for key in ids:
                entry = self.registry.item(key)
                if entry["kind"] == "count":
                    query = QueryRequest.model_validate(entry["query"])
                    # Count proves the same matching set, not a persisted cursor.
                    query = query.model_copy(update={"page": query.page.model_copy(update={"cursor": None})})
                    result = self.knowledge.query(query)
                    c = result.completeness
                    if not c.matching_complete or not c.exhaustive_within_scope or c.total_matches != entry["count"]:
                        raise ResearchError("stale_context")
            self.tasks.validate(ids)
        except KnowledgeIntegrityError:
            raise ResearchError("stale_context") from None
        return {"valid": True, "ids": list(ids)}

    def execute(self, name, args):
        if name not in MODELS:
            raise ResearchError("unsupported")
        if self.wiki_query and not self.unified and name not in {*WIKI_MODELS, "research_validate"}:
            raise ResearchError("unsupported")
        if not self.wiki_query and name in WIKI_MODELS:
            raise ResearchError("unsupported")
        if name in {*UNIFIED_MODELS, *TABLE_NAMES} and not self.unified:
            raise ResearchError("unsupported")
        parsed = MODELS[name].model_validate(args) if MODELS[name] is not None else args
        before = dict(self.registry.entries)
        try:
            return bounded(self._execute(name, parsed), ENTITY_RESULT_BYTES if name in ENTITY_MODELS else 12000)
        except ReadProjectionLimit as error:
            self.registry.entries = before
            retry = parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else deepcopy(parsed)
            if name in ("research_catalogue", "research_query", "research_passages") and retry["page_size"] > 1:
                retry.update(page_size=1, cursor=None)
                error.next_read = {"name": name, "arguments": retry}
            elif name == "research_search" and retry["top_k"] > 1:
                retry["top_k"] = 1
                error.next_read = {"name": name, "arguments": retry}
            elif name == "research_get" and retry["evidence_index"] is None:
                item = self.registry.item(retry["id"])
                ref = item.get("context_ref") or item.get("ref", {})
                if item["kind"] != "fact" and ref.get("kind") == "sample_test_context":
                    error.next_read = {
                        "name": "research_catalogue",
                        "arguments": {
                            "source_id": ref["source_id"],
                            "context_id": retry["id"],
                            "kind": "assertions",
                            "page_size": 1,
                            "cursor": None,
                        },
                    }
            raise
        except KnowledgeIntegrityError as error:
            self.registry.entries = before
            if error.code == "REQUEST_CANCELED":
                raise ResearchError("cancelled") from None
            if error.code in ("QUERY_TIMEOUT", "INDEX_TIMEOUT"):
                raise ResearchError("timeout") from None
            if error.code in ("INDEX_NOT_READY", "WIKI_NOT_READY"):
                raise ResearchError("not_ready") from None
            raise ResearchError("stale_context") from None
        except Exception:
            # A failed tool result grants no unseen IDs, including a late
            # receipt artifact/summary failure after observing query rows.
            self.registry.entries = before
            raise

    def _search(self, args):
        scope = self.scope(args.layer)
        selected = {k: scope[k] for k in ("workspace_id", "collection_id", "corpus_snapshot_id", "use_policy")}
        selected["layer"] = args.layer
        # One shared budget includes status, an optional cache build, and search.
        deadline = time.monotonic() + self.knowledge.timeout_seconds
        inherited = READ_DEADLINE.get()
        if inherited is not None:
            deadline = min(deadline, inherited)
        token = READ_DEADLINE.set(deadline)
        try:
            self.knowledge._deadline(deadline)
            state = self.knowledge.index_status(
                IndexStatusRequest(schema_version=VERSION, request_id="research-index-status", scope=selected)
            )
            if state.readiness == "stale":
                raise ResearchError("stale_context")
            if state.readiness == "not_ready":
                index = self.knowledge.lexical_index
                request_id = "research-index-" + fingerprint(
                    [selected, index.tokenizer.version, index.tokenizer.alias_version]
                )
                self.knowledge.index_rebuild(
                    IndexBuildRequest(
                        schema_version=VERSION,
                        request_id=request_id,
                        scope=selected,
                        timeout_seconds=max(0.000001, deadline - time.monotonic()),
                    )
                )
            self.knowledge._deadline(deadline)
            result = self.knowledge.search(
                SearchRequest(
                    schema_version=VERSION,
                    request_id="research-search",
                    domain_profile=self.knowledge.capabilities().domain_profiles[0],
                    query=args.query,
                    collection_id=scope["collection_id"],
                    corpus_snapshot_id=scope["corpus_snapshot_id"],
                    use_policy=scope["use_policy"],
                    source_kinds=[args.layer],
                    top_k=args.top_k,
                )
            )
            actual = {
                "workspace_id": result.workspace_id,
                "collection_id": result.scope.collection_id,
                "corpus_snapshot_id": result.versions.corpus_snapshot_id,
                "use_policy": result.scope.use_policy,
            }
            self.registry._scope(args.layer, actual)
            view = {
                "items": [],
                "clipped": False,
                "exhaustive_within_scope": False,
                "total_matches": None,
                "retrieval": result.retrieval.model_dump(mode="json"),
            }
            staged = []
            for row in result.results:
                if row.kind != "lexical_candidate" or row.scope.model_dump() != selected:
                    raise ResearchError("stale_context")
                ref = row.ref.model_dump(mode="json")
                self.registry._owner(ref, scope)
                entry = {
                    **row.model_dump(mode="json", exclude={"kind", "result_id"}),
                    "kind": "navigation",
                    "layer": args.layer,
                    "scope": actual,
                    "label": None,
                }
                entry["id"] = self.registry._id(entry)
                staged.append(entry)
            self.knowledge._deadline(deadline)
            # Validate the whole batch before making any candidate model-visible.
            return self.registry.project(staged, view)
        finally:
            READ_DEADLINE.reset(token)

    def _entity_workspace(self):
        # The entity graph is written for the website workspace; the model never chooses it.
        workspace = getattr(self.knowledge, "entity_workspace_id", None)
        if workspace:
            return workspace
        found = set()
        for scope in self.selection.get("scopes", {}).values():
            if scope.get("workspace_id"):
                found.add(scope["workspace_id"])
        wiki = self.selection.get("wiki_scope") or {}
        if wiki.get("workspace_id"):
            found.add(wiki["workspace_id"])
        if len(found) != 1:
            raise ResearchError("not_ready")
        return next(iter(found))

    def _execute_entity(self, name, args):
        store = getattr(self.knowledge, "entity_store", None)
        if store is None or not self.unified:
            raise ResearchError("unsupported")
        workspace = self._entity_workspace()
        try:
            if name == "search":
                index = getattr(self.knowledge, "entity_index", None)
                if index is None:
                    raise ResearchError("not_ready")
                return entity_search(index, args.query, args.entity_type, args.section, args.source)
            if name == "expand":
                index = getattr(self.knowledge, "entity_index", None)
                if index is None:
                    raise ResearchError("not_ready")
                return entity_expand(index, args.entity_ids, args.focus, args.to_type, args.sections)
            if name == "read_passage":
                reader = getattr(self.knowledge, "entity_passage", None)
                if reader is None:
                    raise ResearchError("not_ready")
                return reader(args.source_id, args.block_id, args.around)
            if name == "read_entity":
                return read_entity(store, workspace, args.entity_id)
        except (KeyError, ValueError):
            raise ResearchError("invalid_request") from None
        raise ResearchError("unsupported")

    def _execute(self, name, args):
        if name in ENTITY_MODELS:
            return self._execute_entity(name, args)
        if name in {*UNIFIED_MODELS, *TABLE_NAMES}:
            return self.unified.execute(name, args)
        if name in WIKI_MODELS:
            return self.wiki_query.execute(name, args)
        if name in ("research_target", "research_constraints", "research_reference", "research_plan"):
            result = (
                {"items": [self.tasks.target_quote(args.selections)], "clipped": False}
                if name == "research_target"
                else self.tasks.execute(name, args.model_dump(mode="json"))
            )
            result["items"] = [self.registry.model_view(entry) for entry in result["items"]]
            return result
        if name == "research_fields":
            return SelectedSourceVocabulary(self.knowledge, self.selection).read(args)
        if name == "research_capabilities":
            raw = self.knowledge.capabilities().model_dump(mode="json")
            caps = {
                k: raw[k]
                for k in (
                    "readiness",
                    "domain_profiles",
                    "comparison_profiles",
                    "comparison_operations",
                    "exact_query",
                    "lexical_index",
                    "lexical_index_layers",
                    "source_passage",
                    "wiki",
                    "unsupported",
                )
            }
            caps["field_ids"] = [field["field_id"] for field in raw["fields"]]
            caps["collections"] = sorted({scope["collection_id"] for scope in self.selection["scopes"].values()})
            return {
                "capabilities": caps,
                "selected_scopes": {
                    layer: {k: scope[k] for k in ("workspace_id", "collection_id", "corpus_snapshot_id", "use_policy")}
                    for layer, scope in self.selection["scopes"].items()
                },
                "source_ids": self.selection["source_ids"],
                "source_navigation": self.knowledge.source_navigation(self.selection["source_ids"]),
                "target_vocabulary": {
                    "kind": "catalogue_declared_not_observed",
                    "profiles": target_profiles(),
                    "use": "仅绑定用户原文目标的声明语法，不表示资料存在该指标；查询请读research_fields。",
                },
                "vocabulary": {
                    "kind": "catalogue_declared_not_observed",
                    "observed_tool": "research_fields",
                    "zero_meaning": (
                        "声明selector的零结果只说明该条件无匹配；先查看选源实际词汇，不代表资料没有该概念。"
                    ),
                },
                "search": "lexical_candidates" if caps["lexical_index"] else "not_ready",
            }
        if name == "research_search":
            return self._search(args)
        if name == "research_catalogue":
            scope = self.scope("record")
            if args.source_id not in scope["source_ids"]:
                raise ResearchError("invalid_request")
            if args.context_id is not None:
                ref = self._context(args.context_id)
                if ref.source_id != args.source_id:
                    raise ResearchError("invalid_request")
            else:
                roots = [r for r in scope.get("record_refs", []) if r["source_id"] == args.source_id]
                if len(roots) != 1:
                    raise ResearchError("invalid_request")
                ref = roots[0]
            result = self.knowledge.catalogue(
                RecordCatalogueRequest(
                    schema_version=VERSION,
                    request_id="research-catalogue",
                    target=ref,
                    use_policy=scope["use_policy"],
                    kind=args.kind,
                    page={"size": args.page_size, "cursor": args.cursor},
                )
            )
            return self.registry.catalogue(result)
        if name == "research_validate":
            return self.validate(args.ids)
        if name == "research_query":
            selection = []
            for index, key in enumerate(args.selection_ids):
                try:
                    selection.append(self._context(key))
                except ValueError:
                    item = self.registry.entries.get(key, {})
                    return {
                        "error": {
                            **ResearchError("invalid_query_selection").public(),
                            "result_kind": item.get("kind", "unknown"),
                            "layer": item.get("layer"),
                            "selection_index": index,
                        }
                    }
            scope = self.scope("record")
            caps = self.knowledge.capabilities()
            query = QueryRequest(
                schema_version=VERSION,
                request_id="research-query",
                domain_profile=caps.domain_profiles[0],
                collection_id=scope["collection_id"],
                corpus_snapshot_id=scope["corpus_snapshot_id"],
                use_policy=scope["use_policy"],
                include_history=scope["use_policy"] == "history",
                filters=args.filters,
                selection=selection or None,
                page={"size": args.page_size, "cursor": args.cursor},
            )
            result = self.knowledge.query(query)
            view = self.registry.observe(result, "record", query=query.model_dump(mode="json"), max_bytes=10500)
            view["items"].append(self.registry.model_view(self.tasks.receipt("query_receipt", query, result)))
            return view
        if name == "research_get":
            item = self.registry.item(args.id)
            layer = item["layer"]
            ref = item.get("context_ref") or item.get("ref")
            operations = self.registry.operations(item)
            if "get" not in operations:
                return {
                    "error": {
                        "code": "unsupported_result_operation",
                        "result_kind": item["kind"],
                        "allowed_operations": operations,
                        "answer_kind": item["kind"] if "answer" in operations else None,
                        "message": (
                            "该结果不是可读取的叶节点；按allowed_operations使用。"
                            "count可直接选作count答案或validate，不使用evidence_index。"
                        ),
                    }
                }
            if args.evidence_index is not None:
                evidence = (item.get("value", {}) if item["kind"] == "fact" else item).get("evidence_refs", [])
                if args.evidence_index >= len(evidence):
                    raise ResearchError("invalid_request")
                ref = evidence[args.evidence_index]
                self.registry._owner(ref, self.registry.entry_scope(item) if self.unified else self.scope(layer))
            if ref is None:
                raise ResearchError("invalid_request")
            # A fact leaf is narrower than its parent sample context.
            if args.evidence_index is None and item["kind"] == "fact":
                ref = item["ref"]
            scope = item["scope"] if self.unified else self.scope(layer)
            result = self.knowledge.get(
                GetRequest(
                    schema_version=VERSION,
                    request_id="research-get",
                    use_policy=scope["use_policy"],
                    target=ref,
                    view="location",
                )
            )
            return self.registry.observe(
                result, layer, context_complete=ref.get("kind") == "sample_test_context", parent_id=args.id
            )
        if name == "research_compare":
            scope = self.scope("record")
            profiles = self.knowledge.capabilities().comparison_profiles
            profile = next((p for p in profiles if p.id == args.profile_id), None)
            if profile is None:
                raise ResearchError("invalid_request")
            query = CompareRequest(
                schema_version=VERSION,
                request_id="research-compare",
                corpus_snapshot_id=scope["corpus_snapshot_id"],
                use_policy=scope["use_policy"],
                items=[self._context(k) for k in args.item_ids],
                comparison_profile=profile,
                requested_operations=args.operations,
            )
            result = self.knowledge.compare(query)
            view = self.registry.observe(result, "record", max_bytes=10500)
            ids = [item["id"] for item in view["items"] if item["kind"] == "comparison"]
            view["items"].append(
                self.registry.model_view(self.tasks.receipt("comparison_receipt", query, result, comparison_ids=ids))
            )
            return view
        if name == "research_passages":
            scope = self.scope("source_passage")
            if args.source_id not in scope["source_ids"] or self.passage_reader is None:
                raise ResearchError("not_ready")
            return self.registry.observe(self.passage_reader(scope, args.model_dump(mode="json")), "source_passage")
        raise ResearchError("unsupported")
