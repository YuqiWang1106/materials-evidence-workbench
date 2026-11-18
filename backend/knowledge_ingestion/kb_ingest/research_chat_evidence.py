"""Model-visible evidence ledger and server-rendered factual selections."""

import re
from copy import deepcopy
from typing import Annotated, Literal, Union

from pydantic import Field

from .core.serialization import canonical, fingerprint
from .knowledge_contracts import Id, KnowledgeResult, RecordCatalogueResult, StrictModel
from .research_chat_contracts import ResearchError, ResearchSelection
from .research_chat_projection import ReadProjectionLimit, bounded, item_view, shared_contexts


class SelectedClaim(StrictModel):
    kind: Literal[
        "fact", "count", "comparison", "quote", "guide", "user_target", "task_result", "wiki_content", "wiki_evidence"
    ]
    id: Id


class EditorialBlock(StrictModel):
    kind: Literal["editorial", "hypothesis", "question", "limitation"]
    text: str = Field(min_length=1, max_length=3000)
    related_ids: list[Id] = Field(default_factory=list, max_length=20)


class AnswerDraft(StrictModel):
    blocks: list[Annotated[Union[SelectedClaim, EditorialBlock], Field(discriminator="kind")]] = Field(
        min_length=1, max_length=40
    )
    focus_ids: list[Id] = Field(default_factory=list, max_length=20)


OWNER = (
    "workspace_id",
    "corpus_snapshot_id",
    "task_id",
    "source_id",
    "source_revision_id",
    "record_revision_id",
    "record_snapshot_id",
    "package_sha256",
)
LABELS = {"editorial": "解释（未验证）", "hypothesis": "假设（未验证）", "question": "待澄清", "limitation": "限制"}
# Only scientific quantities, not generic digits, numbered prose or material names.
QUANTITY = re.compile(
    r"(?<![A-Za-z0-9_.-])[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*(?:wt\s*%|vol\s*%|%|℃|°\s*C|[mkMG]?Pa\b|[μumnc]?m\b|kg\b|mg\b|g\b|K\b|mol\b)"
)
TOKEN = re.compile(r"\{\{(fact|label):([^{}]+)\}\}")


def fail():
    raise ResearchError("answer_validation_failed")


class EvidenceRegistry:
    def __init__(self, selection, *, prior=None):
        self.selection = ResearchSelection.model_validate(selection).model_dump(mode="json")
        self.entries = {}
        self.query_receipt_navigation = None
        # Derived model navigation never changes persisted entry fingerprint IDs.
        self.wiki_support_navigation = {}
        for key, entry in (prior or {}).items():
            if type(entry) is not dict or entry.get("id") != key:
                fail()
            if self.selection.get("scope_mode") == "unified":
                if entry["scope"].get("workspace_id") != self.selection["wiki_scope"]["workspace_id"]:
                    raise ResearchError("stale_context")
            else:
                self._scope(entry["layer"], entry["scope"])
            if self._id(entry) != key:
                fail()
            self.entries[key] = deepcopy(entry)

    @staticmethod
    def _id(entry):
        return entry["kind"] + "-" + fingerprint({k: v for k, v in entry.items() if k != "id"})

    def _scope(self, layer, scope):
        if self.selection.get("scope_mode") in ("all_wiki", "unified") and layer == "wiki":
            if layer != "wiki" or scope != self.selection["wiki_scope"]:
                raise ResearchError("stale_context")
            return self.selection["wiki_scope"]
        bound = self.selection["scopes"].get(layer)
        if bound is None or any(
            scope.get(k) != bound[k] for k in ("workspace_id", "collection_id", "corpus_snapshot_id", "use_policy")
        ):
            raise ResearchError("stale_context")
        return bound

    def _owner(self, ref, scope):
        if any(ref.get(k) != scope[k] for k in ("workspace_id", "corpus_snapshot_id")):
            raise ResearchError("stale_context")
        if ref.get("kind") == "wiki_revision":
            if ref not in scope.get("wiki_refs", []):
                raise ResearchError("stale_context")
            return
        sid = ref.get("source_id") or ref.get("source_snapshot_ref", {}).get("source_id")
        if sid not in scope["source_ids"]:
            raise ResearchError("stale_context")

    @staticmethod
    def operations(entry):
        kind = entry["kind"]
        operations = []
        if kind in {
            "fact",
            "count",
            "comparison",
            "quote",
            "guide",
            "user_target",
            "task_result",
            "wiki_content",
            "wiki_evidence",
        }:
            operations.append("answer")
        if kind == "wiki_navigation":
            operations.append("wiki_read")
        if kind == "wiki_support":
            operations.append("wiki_evidence")
        if kind in {"navigation", "context", "fact", "quote", "guide"} and (
            entry.get("ref") or entry.get("context_ref")
        ):
            operations.append("get")
        operations.append("validate")
        return operations

    def model_view(self, entry):
        view = item_view(
            entry,
            self.operations(entry),
            comparison_text=self._comparison_text(entry) if entry["kind"] == "comparison" else None,
        )
        if self.selection.get("scope_mode") == "unified":
            ref = entry.get("ref") or entry.get("support", {}).get("ref", {})
            selected = (
                ref.get("source_id") in self.selection["source_ids"]
                or ref.get("table_version_id") in self.selection["source_focus"]["table_version_ids"]
                or bool(
                    set(ref.get("table_version_ids", [])) & set(self.selection["source_focus"]["table_version_ids"])
                )
            )
            view["source_role"] = "focus" if selected else "background"
        navigation = self.wiki_support_navigation.get(entry["id"])
        if entry["kind"] == "wiki_support" and navigation is not None:
            view["navigation"] = deepcopy(navigation)
        if entry["kind"] == "query_receipt" and self.query_receipt_navigation is not None:
            view["query_navigation"] = self.query_receipt_navigation(entry["id"])
        return view

    def project(self, entries, view, *, max_bytes=12000):
        view = shared_contexts({**view, "items": [self.model_view(entry) for entry in entries]})
        bounded(view, max_bytes)
        # Atomic visibility: no hidden item becomes an accepted claim or focus ID.
        for entry in entries:
            self.entries[entry["id"]] = deepcopy(entry)
        return view

    @staticmethod
    def entry_scope(entry):
        """Read authority only for an already registered immutable source owner."""
        scope = deepcopy(entry["scope"])
        sources = []
        for field in ("ref", "context_ref"):
            ref = entry.get(field) or {}
            sid = ref.get("source_id") or ref.get("source_snapshot_ref", {}).get("source_id")
            if sid and sid not in sources:
                sources.append(sid)
        scope["source_ids"] = sources
        return scope

    def observe(self, result, layer, *, query=None, max_bytes=12000, context_complete=False, parent_id=None):
        result = KnowledgeResult.model_validate(result.model_dump() if isinstance(result, KnowledgeResult) else result)
        actual = {
            "workspace_id": result.workspace_id,
            "collection_id": result.scope.collection_id,
            "corpus_snapshot_id": result.versions.corpus_snapshot_id,
            "use_policy": result.scope.use_policy,
        }
        if self.selection.get("scope_mode") == "unified" and parent_id is not None:
            parent = self.item(parent_id)
            scope = self.entry_scope(parent)
            if parent["layer"] != layer or any(actual[k] != scope[k] for k in actual):
                raise ResearchError("stale_context")
        else:
            scope = deepcopy(self._scope(layer, actual))
        candidates = []

        def add(kind, data):
            entry = {
                "kind": kind,
                "layer": layer,
                "scope": {k: v for k, v in scope.items() if k not in ("source_ids", "record_refs", "wiki_refs")},
                **data,
            }
            entry["id"] = self._id(entry)
            candidates.append(entry)

        for row in result.results:
            ref = row.ref.model_dump(mode="json")
            self._owner(ref, scope)
            status = (
                row.status.model_dump(mode="json")
                if hasattr(row, "status")
                else {"source_review": getattr(row, "source_review", "unknown"), "scientific_validation": False}
            )
            base = {
                "ref": ref,
                "status": status,
                "storage": row.storage,
                "integrity": getattr(row, "integrity", "unverified"),
            }
            if row.kind == "wiki_revision":
                add(
                    "guide",
                    {
                        **base,
                        "title": row.title,
                        "body": row.body[:6000],
                        "body_clipped": len(row.body) > 6000,
                        "dependency_refs": [r.model_dump(mode="json") for r in row.dependency_refs],
                    },
                )
            quote = (
                row.quote
                if row.kind == "source_passage"
                else (row.evidence_data.quote if getattr(row, "evidence_data", None) else None)
            )
            if quote is not None:
                kind = (
                    getattr(row, "content_kind", "unknown")
                    if row.kind == "source_passage"
                    else getattr(row.evidence_data, "effective_content_kind", "unknown")
                )
                provenance = (
                    getattr(row, "provenance", None)
                    if row.kind == "source_passage"
                    else getattr(row.evidence_data, "provenance", None)
                )
                add(
                    "quote",
                    {
                        **base,
                        "quote": quote,
                        "content_kind": kind,
                        "provenance": provenance.model_dump(mode="json") if provenance else None,
                        "quote_sha256": __import__("hashlib").sha256(quote.encode()).hexdigest(),
                    },
                )
            if row.kind in {"record_revision", "sample_test_context", "assertion"}:
                context = {
                    **base,
                    "label": row.label,
                    "evidence_refs": [e.model_dump(mode="json") for e in row.evidence_refs[:20]],
                    "evidence_refs_complete": len(row.evidence_refs) <= 20,
                }
                add("context", context)
                test_context = None
                if ref["kind"] == "sample_test_context":
                    test = ref.get("test_ref")
                    conditions = [
                        v.model_dump(mode="json")
                        for v in row.values
                        if test is not None
                        and v.subject_ref == test
                        and v.field_id in ("condition.parameter", "method.standard")
                    ]
                    test_label = next(
                        (
                            v.subject_label
                            for v in row.values
                            if test is not None and v.subject_ref == test and v.subject_label
                        ),
                        None,
                    )
                    test_context = {
                        "sample_ref": ref["sample_ref"],
                        "test_ref": test,
                        "test_label": test_label,
                        "conditions": conditions,
                        "coverage": "saved_context" if context_complete else "read_projection",
                        "condition_status": "reported" if conditions else "unconfirmed",
                    }
                context_ref = ref
                label = row.label
                if parent_id is not None:
                    parent = self.item(parent_id)
                    if ref["kind"] == "assertion" and parent.get("ref") == ref:
                        context_ref = deepcopy(parent.get("context_ref", ref))
                        label = parent.get("label") or label
                        test_context = deepcopy(parent.get("test_context"))
                for value in row.values:
                    aref = {"kind": "assertion", **{k: ref[k] for k in OWNER}, "assertion_id": value.assertion_id}
                    add(
                        "fact",
                        {
                            **base,
                            "ref": aref,
                            "context_ref": context_ref,
                            "label": label,
                            "value": value.model_dump(mode="json"),
                            "test_context": test_context,
                        },
                    )
        for pair in result.comparisons:
            raw = pair.model_dump(mode="json")
            self._owner(raw["left_ref"], scope)
            self._owner(raw["right_ref"], scope)
            # The immutable pair includes exact permitted operations, eligibility,
            # conditions, unknowns and server-computed cells. No model arithmetic.
            add("comparison", {"pair": raw, "ref": raw["left_ref"], "other_ref": raw["right_ref"]})
        comp = result.completeness
        if (
            result.operation == "query"
            and query is not None
            and result.status == "ok"
            and comp.matching_complete
            and comp.exhaustive_within_scope
            and comp.total_matches is not None
        ):
            if any(query.get(k) != scope[k] for k in ("collection_id", "use_policy")):
                raise ResearchError("stale_context")
            if query.get("corpus_snapshot_id") not in (None, scope["corpus_snapshot_id"]):
                raise ResearchError("stale_context")
            add(
                "count",
                {
                    "count": comp.total_matches,
                    "query": {**deepcopy(query), "corpus_snapshot_id": scope["corpus_snapshot_id"]},
                    "result_set_id": comp.result_set_id,
                    "completeness": comp.model_dump(mode="json"),
                },
            )
        view = {
            "items": [],
            "clipped": False,
            "result_status": result.status,
            "has_more": comp.has_more,
            "next_cursor": comp.next_cursor,
            "errors": [e.model_dump(mode="json") for e in result.errors],
        }
        # Exact totals and selected text cannot be displaced by optional values.
        candidates.sort(
            key=lambda e: {"count": 0, "quote": 1, "comparison": 2, "guide": 2, "context": 3, "fact": 4}[e["kind"]]
        )
        # Only a matching immutable record may provide navigation back to a selected root.
        for row in result.results:
            if row.kind != "wiki_revision":
                continue
            record = self.selection["scopes"].get("record")
            if record is None:
                continue
            for dependency in row.dependency_refs:
                ref = dependency.model_dump(mode="json")
                roots = [
                    root
                    for root in record["record_refs"]
                    if all(root.get(k) == ref.get(k) for k in OWNER if k != "corpus_snapshot_id")
                ]
                if not roots:
                    continue
                ref["corpus_snapshot_id"] = record["corpus_snapshot_id"]
                self._owner(ref, record)
                entry = {
                    "kind": "navigation",
                    "layer": "record",
                    "scope": {k: v for k, v in record.items() if k not in ("source_ids", "record_refs", "wiki_refs")},
                    "ref": ref,
                    "label": "Wiki关联原始依据；须读取后引用",
                    "requires_authoritative_read": True,
                }
                entry["id"] = self._id(entry)
                candidates.append(entry)
        if (
            result.operation == "get"
            and result.results
            and all(row.kind in {"evidence", "source_passage"} for row in result.results)
        ):
            view["associated_facts_read"] = False
        try:
            return self.project(candidates, view, max_bytes=max_bytes)
        except ReadProjectionLimit:
            if result.operation != "query":
                raise
            # Query remains useful for exact counts and complete context navigation.
            # Values omitted from this response never enter the read registry.
            candidates = [entry for entry in candidates if entry["kind"] != "fact"]
            return self.project(
                candidates,
                {
                    **view,
                    "projection": "context_navigation",
                    "facts_included": False,
                    "read_hint": "本页仅含完整计数和上下文导航；用context ID调用get读取科学值。",
                },
                max_bytes=max_bytes,
            )

    def catalogue(self, result, max_bytes=12000):
        result = RecordCatalogueResult.model_validate(result)
        scope = self._scope(
            "record",
            {
                "workspace_id": result.workspace_id,
                "collection_id": result.scope.collection_id,
                "corpus_snapshot_id": result.corpus_snapshot_id,
                "use_policy": result.scope.use_policy,
            },
        )
        view = {
            "items": [],
            "clipped": False,
            "has_more": result.completeness.has_more,
            "next_cursor": result.completeness.next_cursor,
            "completeness": result.completeness.model_dump(mode="json"),
        }
        candidates = []
        for row in result.entries:
            data = row.model_dump(mode="json")
            self._owner(data["ref"], scope)
            entry = {
                "kind": "navigation",
                "layer": "record",
                "scope": {k: v for k, v in scope.items() if k not in ("source_ids", "record_refs", "wiki_refs")},
                **data,
            }
            entry["id"] = self._id(entry)
            candidates.append(entry)
        return self.project(candidates, view, max_bytes=max_bytes)

    def item(self, key, kind=None):
        entry = self.entries.get(key)
        if entry is None or kind is not None and entry["kind"] != kind:
            fail()
        return entry

    def closure(self, ids):
        seen = set()
        active = set()
        ordered = []

        def visit(key, depth):
            if key in active or depth > 8 or len(seen) > 1000:
                fail()
            if key in seen:
                return
            active.add(key)
            entry = self.item(key)
            for dependency in entry.get("dependency_ids", []):
                visit(dependency, depth + 1)
            active.remove(key)
            seen.add(key)
            ordered.append(key)

        for key in ids:
            visit(key, 0)
        return ordered

    def references(self, ids):
        groups = {}
        for key in self.closure(ids):
            entry = self.item(key)
            layer = entry["layer"]
            refs = groups.setdefault(layer, [])
            for name in ("ref", "other_ref", "context_ref"):
                ref = entry.get(name)
                if ref is not None and ref not in refs:
                    refs.append(ref)
        return groups

    def _label(self, ref, fallback="所选样品"):
        for entry in self.entries.values():
            context = entry.get("context_ref", entry.get("ref", {}))
            if entry.get("label") and all(
                context.get(k) == ref.get(k) for k in ("source_id", "sample_ref", "test_ref")
            ):
                return entry["label"]
        return fallback

    @staticmethod
    def _field(value):
        fields = {
            "material.description": "材料描述",
            "material.specification": "材料规格",
            "composition.amount": "配方用量",
            "condition.parameter": "测试条件",
            "method.standard": "测试方法",
            "observation.value": "测试结果",
            "process.parameter": "制备参数",
            "process.description": "制备过程",
            "sample.description": "样品描述",
        }
        known = {
            "specimen_thickness": "试样厚度",
            "temperature": "温度",
            "pressure": "压力",
            "duration": "时长",
            "limiting_oxygen_index": "氧指数",
            "ul94_rating": "燃烧等级",
            "matrix": "基体",
            "test_method": "方法标准",
        }
        q = value.get("qualifiers", {})
        name = next((q[k] for k in ("metric_id", "parameter_id", "component_id") if q.get(k)), None)
        if name:
            name = str(name).split(":")[-1]
            # Opaque identifiers are retained in the registry, not the prose.
            if not re.fullmatch(r"[0-9a-f-]{20,}", name):
                return known.get(name, name)
        return fields.get(value.get("field_id"), "来源记录")

    @staticmethod
    def _value_text(value):
        raw = value.get("raw_value")
        raw = "未提供" if raw is None else raw if isinstance(raw, str) else canonical(raw)
        return (
            raw
            + ((" " + str(value["unit"])) if value.get("unit") else "")
            + (("（基准：" + str(value["basis"]) + "）") if value.get("basis") else "")
        )

    def _fact_text(self, entry):
        value = entry["value"]
        ref = entry.get("context_ref", entry["ref"])
        owner = entry.get("label") or self._label(ref, "所选记录")
        context = entry.get("test_context")
        if context and context["test_ref"] is None:
            owner += "（样品层；未推断测试）"
        text = owner + "：" + self._field(value) + "为 " + self._value_text(value) + "。"
        if value["field_id"] == "observation.value" and context is None:
            text += " 测试归属和条件尚未确认；请读取对应样品/测试上下文。"
        if context and context["test_ref"] is not None:
            label = context.get("test_label") or "所选测试（标识见依据）"
            text += " 所属测试：" + label + "。"
            conditions = context["conditions"]
            if conditions:
                text += (
                    " 已读条件："
                    + "；".join(self._field(v) + "为 " + self._value_text(v) for v in conditions)
                    + "。其他条件未确认。"
                )
            else:
                text += " 该测试的条件未确认。"
            if not any(v["field_id"] == "method.standard" for v in conditions):
                text += " 方法标准未确认。"
        return text

    def _comparison_text(self, entry):
        pair = entry["pair"]
        left = self._label(pair["left_ref"], "左侧样品")
        right = self._label(pair["right_ref"], "右侧样品")
        status = {
            "comparable": "已核对条件一致",
            "not_comparable": "存在不同条件，不宜直接比较性能",
            "insufficient_conditions": "条件不足，不能确认可比性",
        }[pair["status"]]
        rows = [left + " 与 " + right + "：" + status + "。"]
        verdicts = {
            "same": "相同",
            "allowed_difference": "允许的差异",
            "different": "不同",
            "missing": "缺失",
            "unknown": "未知",
        }
        seen = set()
        for condition in pair["checked_conditions"] + pair["differences"]:
            digest = fingerprint(condition)
            if digest in seen:
                continue
            seen.add(digest)
            rows.append(
                self._field(condition["condition"])
                + "："
                + self._value_text(condition["left"] or {})
                + " / "
                + self._value_text(condition["right"] or {})
                + "（"
                + verdicts[condition["verdict"]]
                + "）。"
            )
        for missing in pair["missing_conditions"]:
            rows.append(
                self._field(missing["condition"])
                + "："
                + {"left": "左侧", "right": "右侧", "both": "两侧"}[missing["side"]]
                + "未提供。"
            )
        rows.append("这里只并列已保存条件；未计算性能差值、排名或推断实验结论。")
        return "\n".join(rows)

    def _prose(self, block):
        text = block.text
        for key in block.related_ids:
            self.item(key)

        def token(match):
            kind, key = match.groups()
            entry = self.item(key, "fact")
            if key not in block.related_ids:
                fail()
            return (
                self._fact_text(entry)
                if kind == "fact"
                else entry.get("label") or entry.get("context_ref", {}).get("sample_ref") or entry["ref"]["source_id"]
            )

        plain = TOKEN.sub("", text)
        if "{{" in plain or "}}" in plain or QUANTITY.search(plain):
            fail()
        # No trained prediction exists in this MVP. Unitless model-generated
        # predictions/accuracy/confidence intervals are not an escape hatch.
        if re.search(
            r"(?:预测|准确率|置信区间|prediction|accuracy|confidence interval)[^。！？\n]{0,60}[+-]?\d", plain, re.I
        ):
            fail()
        # Explicit scientific property assignment also requires server data; the
        # same number in a list index or a source-derived label remains legal.
        names = set()
        categories = {"HB", "V-0", "V-1", "V-2", "V0", "V1", "V2"}
        aliases = {
            "limiting_oxygen_index": {"氧指数", "极限氧指数", "LOI"},
            "LOI": {"氧指数", "极限氧指数", "LOI"},
            "ul94_rating": {"燃烧等级", "原表燃烧等级"},
        }
        for entry in self.entries.values():
            if entry["kind"] == "fact":
                value = entry["value"]
                names.add(self._field(value))
                for name in value.get("qualifiers", {}).values():
                    name = str(name).split(":")[-1]
                    names.add(name)
                    names.update(aliases.get(name, set()))
                raw = value.get("raw_value")
                if isinstance(raw, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9._ -]{0,49}", raw):
                    categories.add(raw)
        scalar = r"(?:[+-]?\d|" + "|".join(re.escape(c) for c in sorted(categories, key=len, reverse=True)) + r")"
        for name in names:
            if len(name) >= 2 and re.search(
                re.escape(name) + r"\s*(?:为|是|达到|等于|=|:|：|is)\s*" + scalar, plain, re.I
            ):
                fail()
        return TOKEN.sub(token, text)

    def render(self, draft):
        draft = AnswerDraft.model_validate(draft)
        claims = []
        blocks = []
        used = []
        for block in draft.blocks:
            if isinstance(block, SelectedClaim):
                entry = self.item(block.id, block.kind)
                used.append(block.id)
                if block.kind in ("user_target", "task_result"):
                    text = entry["text"]
                elif block.kind == "fact":
                    text = self._fact_text(entry)
                elif block.kind == "count":
                    text = "在已固定完整查询范围内，共 " + str(entry["count"]) + " 项匹配。"
                elif block.kind in ("wiki_content", "wiki_evidence"):
                    from .wiki_query_service import evidence_text

                    text = evidence_text(entry)
                elif block.kind == "guide":
                    text = (
                        "已发布 Wiki 导读（非原始实测；须另读关联依据）："
                        + entry["body"]
                        + ("\n[导读已截取，不能视为全文。]" if entry["body_clipped"] else "")
                    )
                elif block.kind == "quote":
                    text = (
                        "["
                        + {
                            "verbatim": "来源逐字内容",
                            "parsed": "来源解析文本；待原页核对",
                            "model_transcription": "模型转写；待原页核对",
                            "unknown": "内容类型未知；待核对",
                        }.get(entry.get("content_kind"), "待核对")
                        + "] "
                        + entry["quote"]
                    )
                else:
                    text = self._comparison_text(entry)
                claim = {
                    "claim_id": "claim-" + fingerprint([block.kind, block.id]),
                    "kind": block.kind,
                    "support_id": block.id,
                    "text": text,
                    "scientific_validation": False,
                }
                if block.kind == "fact":
                    claim["test_context"] = deepcopy(entry.get("test_context"))
                claims.append(claim)
                blocks.append(claim)
            else:
                used.extend(block.related_ids)
                blocks.append(
                    {
                        "kind": block.kind,
                        "text": LABELS[block.kind] + "：" + self._prose(block),
                        "scientific_validation": False,
                    }
                )
        for key in draft.focus_ids:
            self.item(key)
        focus = list(draft.focus_ids)
        if not focus:
            facts = [self.entries[key] for key in used if self.entries[key]["kind"] in ("fact", "context")]
            owners = {fingerprint(f.get("context_ref", f.get("ref"))) for f in facts}
            if len(owners) == 1 and facts:
                focus = [facts[0]["id"]]
        return {
            "version": "research-answer-v1",
            "blocks": blocks,
            "claims": claims,
            "text": "\n\n".join(b["text"] for b in blocks),
            "focus_ids": focus,
            "used_ids": list(dict.fromkeys(used + focus)),
            "registry": deepcopy(self.entries),
            "scientific_validation": False,
        }
