"""Strict website research inputs and fixed, safe public failures."""

from typing import Annotated, Literal

from pydantic import Field, model_serializer, model_validator

from .knowledge_contracts import (
    Hash,
    Id,
    RecordRef,
    StrictModel,
    StudyUnitSnapshotRef,
    TableSnapshotRef,
    WikiRevisionRef,
)

RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
Layer = Literal["record", "source_passage", "wiki"]


class ChatCreate(StrictModel):
    request_id: RequestId
    scope_mode: Literal["selected_sources", "all_wiki", "unified"] = "selected_sources"
    source_ids: list[Id] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Duplicate source")
        if (self.scope_mode == "selected_sources") != bool(self.source_ids):
            raise ValueError("Explicit scope required")
        return self


class SourceFocus(StrictModel):
    document_source_ids: list[Id] = Field(max_length=100)
    table_version_ids: list[Id] = Field(max_length=10)

    @model_validator(mode="after")
    def unique(self):
        for values in (self.document_source_ids, self.table_version_ids):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate source focus")
        return self


class TurnCreate(StrictModel):
    request_id: RequestId
    expected_revision: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=8000)
    source_focus: SourceFocus | None = None

    @model_serializer(mode="wrap")
    def compatible_request(self, handler):
        value = handler(self)
        if self.source_focus is None:
            value.pop("source_focus", None)
        return value

    focus_ids: list[Id] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.message.strip():
            raise ValueError("Empty message")
        if self.focus_ids is not None and len(set(self.focus_ids)) != len(self.focus_ids):
            raise ValueError("Duplicate focus")
        return self


class LayerScope(StrictModel):
    workspace_id: Id
    collection_id: Id
    corpus_snapshot_id: Id
    use_policy: Literal["saved_extractions", "source_reviewed", "explore", "history"]
    source_ids: list[Id] = Field(min_length=1, max_length=100)
    manifest_sha256: Hash | None = None
    record_refs: list[RecordRef] = Field(default_factory=list, max_length=100)
    wiki_refs: list[WikiRevisionRef] = Field(default_factory=list, max_length=100)


class GlobalWikiScope(StrictModel):
    workspace_id: Id
    collection_id: Id
    use_policy: Literal["source_reviewed"] = "source_reviewed"


class ResearchSelection(StrictModel):
    scope_mode: Literal["selected_sources", "all_wiki", "unified"] = "selected_sources"
    source_ids: list[Id] = Field(default_factory=list, max_length=100)
    scopes: dict[Layer, LayerScope]
    wiki_scope: GlobalWikiScope | None = None
    source_focus: SourceFocus | None = None
    study_unit_refs: list[StudyUnitSnapshotRef] = Field(default_factory=list)
    table_refs: list[TableSnapshotRef] = Field(default_factory=list, max_length=10)

    @model_serializer(mode="wrap")
    def compatible_selection(self, handler):
        value = handler(self)
        if self.scope_mode != "unified":
            for key in ("source_focus", "study_unit_refs", "table_refs"):
                value.pop(key, None)
        if self.scope_mode == "selected_sources":
            value.pop("scope_mode", None)
            value.pop("wiki_scope", None)
        return value

    @model_validator(mode="after")
    def bound(self):
        if self.scope_mode == "unified":
            if self.wiki_scope is None or self.source_focus is None:
                raise ValueError("Unified scope required")
            if self.source_ids != self.source_focus.document_source_ids:
                raise ValueError("Document focus mismatch")
            if [r.table_version_id for r in self.table_refs] != self.source_focus.table_version_ids:
                raise ValueError("Table focus mismatch")
            if len({r.table_id for r in self.table_refs}) != len(self.table_refs):
                raise ValueError("Duplicate logical table")
            if any(
                r.source_id not in self.source_ids or r.workspace_id != self.wiki_scope.workspace_id
                for r in self.study_unit_refs
            ):
                raise ValueError("Study scope mismatch")
            if any(r.workspace_id != self.wiki_scope.workspace_id for r in self.table_refs):
                raise ValueError("Table scope mismatch")
            for scope in self.scopes.values():
                if (
                    not set(scope.source_ids) <= set(self.source_ids)
                    or scope.workspace_id != self.wiki_scope.workspace_id
                ):
                    raise ValueError("Layer scope mismatch")
            return self
        if self.source_focus is not None or self.study_unit_refs or self.table_refs:
            raise ValueError("Legacy selection extensions")
        if self.scope_mode == "all_wiki":
            if self.source_ids or self.scopes or self.wiki_scope is None:
                raise ValueError("Invalid global Wiki selection")
            return self
        if self.wiki_scope is not None or not self.source_ids:
            raise ValueError("Invalid selected sources")
        if not self.scopes or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Invalid selection")
        for layer, scope in self.scopes.items():
            if not set(scope.source_ids) <= set(self.source_ids):
                raise ValueError("Selection differs from layer")
            for ref in scope.record_refs:
                if (
                    layer != "record"
                    or ref.source_id not in scope.source_ids
                    or ref.corpus_snapshot_id != scope.corpus_snapshot_id
                    or ref.workspace_id != scope.workspace_id
                ):
                    raise ValueError("Record outside layer")
            for ref in scope.wiki_refs:
                if (
                    layer != "wiki"
                    or ref.corpus_snapshot_id != scope.corpus_snapshot_id
                    or ref.workspace_id != scope.workspace_id
                ):
                    raise ValueError("Wiki outside layer")
            if layer == "wiki" and (
                scope.use_policy != "source_reviewed" or not scope.wiki_refs or not scope.manifest_sha256
            ):
                raise ValueError("Invalid Wiki policy")
            if layer == "record" and scope.use_policy not in ("saved_extractions", "source_reviewed", "history"):
                raise ValueError("Invalid record policy")
            if layer == "source_passage" and scope.use_policy not in ("explore", "source_reviewed"):
                raise ValueError("Invalid passage policy")
        return self


ERRORS = {
    "provider_permission": "模型服务拒绝访问，请核对账户或模型权限。",
    "provider_rate_limit": "模型服务限流，本轮未自动重发。",
    "provider_request_rejected": "模型服务拒绝请求，请核对模型和所需能力；本轮未自动重发。",
    "provider_refusal": "模型服务拒绝生成本次结果，尚未保存研究答案。",
    "unsupported_model_protocol": "模型服务协议不受支持，请修改私有配置后重启网站。",
    "unsupported_model_capability": "当前模型接口不支持本次请求所需的输入或输出能力，本次未自动替换模型。",
    "request_outcome_unknown": "请求已发送但结果未知，已保存请求记录；刷新或重启不会自动重发。",
    "model_not_configured": (
        "尚未配置模型服务，请在本实例私有 model-config.toml 中填写服务地址、协议和模型，保存后重启网站。"
    ),
    "api_key_missing": (
        "模型 API key 尚未填写；请在本实例私有模型配置文件中填写，保存后重启网站。不要把密钥发到聊天中。"
    ),
    "model_config_invalid": "模型配置格式无效，请检查本实例私有 TOML 的字段、地址和凭据格式，保存后重启网站。",
    "unsupported_protocol": (
        "所选服务协议不受支持；请使用已支持的 Responses 或 Chat Completions 能力，保存配置后重启网站。"
    ),
    "unsupported_capability": "所选模型接口不支持本次请求需要的能力，本次未自动替换模型。",
    "provider_authentication": "模型服务拒绝鉴权，请在私有配置中核对 API key 和账户权限。",
    "provider_model_unavailable": "模型不存在或当前账户无访问权限，请核对服务商提供的模型 ID。",
    "provider_rate_limited": "模型服务限流，本轮未自动重发；请稍后提交新的请求。",
    "provider_server_error": "模型服务返回服务端错误，本轮未自动重发。",
    "provider_timeout": "模型请求超时，结果可能未知；不会自动重发，请先核对本轮请求记录。",
    "request_unknown": "请求已发送但结果未知，已保留请求记录；刷新或重启不会自动重发。",
    "parser_not_ready": "本地 Standard 解析环境未就绪；已有可读取资料仍可用于研究。",
    "no_sources": "资料库暂无可用研究资料，请先上传并解析资料，或查询已有 Wiki。",
    "sources_not_ready": "所选资料尚未解析完成或暂无可读取内容，请等待解析完成或重新选择资料。",
    "index_not_ready": "研究索引尚未就绪，请完成资料解析后刷新来源。",
    "legacy_identity_mismatch": "旧研究配置与网站工作区身份不一致，请保留原身份并核对配置。",
    "table_selection_required": (
        "本轮未选择该保存表版本，不能新计算；可读取已有原表或保存结果。请重新选表后提交新消息。"
    ),
    "wiki_page_limit": "本轮已读取8个Wiki版本；请在下一轮继续。",
    "wiki_relation_limit": "Wiki关联导航最多两跳，不能继续沿此路径展开。",
    "query_selection_required": (
        "context_ids与query_receipt_ids不能都为空。请选择已读候选，"
        "或用context_ids=[]搭配同一完整查询链回执；不会自动选择全库。"
    ),
    "query_receipt_invalid": (
        "该回执不是当前范围内可验证的query记录。请使用真实query返回的query_receipt ID，不传目标、上下文或自行构造记录。"
    ),
    "query_receipt_mixed": (
        "回执属于不同查询定义、selection或结果集，不能合并。请按query_navigation.query_group选择同一查询链"
        "；不会自动忽略回执。"
    ),
    "query_receipt_incomplete": (
        "回执未形成从首至末的完整连续查询链。请沿该查询补齐有序分页并确认matching_complete；本次未生成筛选结果。"
    ),
    "query_receipt_duplicate": "回执页或完整上下文引用重复。请每页仅选择一次并保留原查询顺序；本次未生成筛选结果。",
    "query_receipt_empty": "该精确查询没有匹配候选，可直接引用count或检查selector；这不表示全体配方不满足目标。",
    "query_receipt_limit": (
        "该查询链超过20个候选。请明确缩小query/selection，或显式分批候选并保留部分覆盖；不会自动截取前20项。"
    ),
    "query_receipt_unread": (
        "查询回执中的上下文未登记为已读，或与已读完整来源身份不符。请重新显式读取对应查询页；不会凭名称或隐藏资料补齐。"
    ),
    "finalization_protocol": "模型在仅允许最终答复的阶段返回了工具调用；本次未执行该工具，本轮未自动续跑或付费重试。",
    "invalid_query_selection": (
        "selection_ids仅接受已读record层样品/测试上下文ID，不接受目标、计数、query receipt或其他层引用。"
        "全冻结record范围请显式用[]，用户目标传research_constraints.target_id。本次未执行查询，"
        "不会自动清空限制。"
    ),
    "model_turn_limit": "本轮已用尽8次模型请求，尚未形成通过校验的答案；已保存读取结果，没有自动继续或付费重试。",
    "target_unknown_profile": (
        "工具参数profile_id不是合法字段声明ID；它不是资料名，也不表示用户缺少目标。"
        "请按schema枚举或research_capabilities.target_vocabulary选择匹配原句的字段。"
    ),
    "target_quote_not_found": (
        "工具参数quote未逐字匹配本轮用户原文。请复制实际连续片段，不改数字、单位、空白或标点；这不是用户缺少目标信息。"
    ),
    "target_quote_ambiguous": (
        "quote在本轮原文出现多次，不能自动选择首处。请选可唯一定位的完整目标片段；无法唯一绑定时再请用户消除重复歧义。"
    ),
    "target_quote_overlap": "选择的目标原文片段互相重叠。请调整工具选择，勿重复选择同一部分；尚未登记目标。",
    "target_clause_unsupported": (
        "已定位原文，但所选字段profile无法按现有语法解析该片段。请先核对profile与原文字段；"
        "确认匹配仍不支持时才请用户明确字段名、比较符、值及该字段所需单位。不得自动替换profile、"
        "改写目标或补值。"
    ),
    "task_record_unavailable": (
        "保存的程序记录无法读取或校验，请重新读取相关依据后重新生成任务结果；不会使用缺失记录推断结论。"
    ),
    "task_record_limit": "程序记录或结果超过本次读取上限，请减少候选或约束后重试；尚不能给出完整任务结论。",
    "target_clarification": (
        "无法从用户原文直接确认目标，请用户明确字段、比较符、数值、单位和配方基准；不得替用户改写目标。"
    ),
    "not_ready": "研究服务尚未就绪，请查看模型和资料各自的状态。",
    "budget_denied": "这是旧版本保存的预算拒绝记录；当前版本不再使用金额预算，本轮历史状态保持不变。",
    "stale_context": "材料范围或引用已失效，请刷新来源并重新选择后新建研究会话。",
    "invalid_request": "研究请求格式无效，请检查选择与输入。",
    "query_limit": "检索问题超过词法或语义输入限制，请缩小问题；本次未截断问题或发送embedding请求。",
    "context_limit": "本轮上下文超过上限，请缩小问题或新建研究会话。",
    "timeout": "本轮研究查询超时；已保存的读取结果仍保留，本轮未自动重发模型请求。",
    "provider_failure": "模型请求未完成，执行结果可能未知；本轮未自动重发。",
    "provider_protocol": "模型返回不符合研究合同；本轮未自动重发。",
    "answer_validation_failed": "回答的数值或引用未通过校验；已保留读取结果，未自动修补。",
    "cancelled": "本轮已取消；已保存的读取结果仍保留。",
    "interrupted": "服务中断，本轮未自动重发；可以主动提交新消息。",
    "execution_failed": "研究执行失败；已保存读取结果，本轮未自动重发。",
    "tool_limit": "本轮读取次数已达上限，请缩小问题后继续。",
    "unsupported": "当前研究工具不支持此操作。",
}


class ResearchError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "execution_failed"
        super().__init__(ERRORS[self.code])

    def public(self):
        return {"code": self.code, "message": ERRORS[self.code]}
