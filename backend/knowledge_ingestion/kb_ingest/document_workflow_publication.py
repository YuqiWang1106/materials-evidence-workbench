"""Materialize explicitly reviewed Sol facts while preserving their source identities."""

import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import NotRequired, TypedDict

from .contract import validate_package
from .core import StateConflict
from .core.errors import raise_state_conflict as conflict
from .document_candidate_review import FACT, resolve
from .document_program_convert import materialize_program_output
from .document_source_binding import canonical_location_evidence
from .document_source_index import SourceIndex, load_source_index
from .document_workflow_candidates import CandidateGroup, CandidateReview, CandidateSnapshot
from .ingest.documents.document_quality_assess import package_content_hash
from .ingest.documents.document_quality_types import JsonObject, JsonValue
from .storage.task_files import checked_child


class _Assertion(TypedDict):
    assertion_id: str
    subject_ref: str
    value: JsonObject
    use_status: str
    review_state: str


class _Check(TypedDict, total=False):
    reasons: list[str]


class _Issue(TypedDict):
    issue_id: str
    target_refs: NotRequired[list[str]]
    source_context: NotRequired[JsonObject]
    program_check: NotRequired[_Check]


class PublicationPackage(TypedDict):
    header: JsonObject
    objects: list[JsonObject]
    evidence: list[JsonObject]
    assertions: list[_Assertion]
    relations: list[JsonObject]
    issues: list[_Issue]


class _GroupInfo(TypedDict):
    group_id: str
    target_block_ids: NotRequired[list[str]]


def _publication_failure_reason(reason: str) -> str:
    code = reason.partition(":")[0]
    labels = {
        "source_asset_unregistered": "证据引用的原文不属于当前资料",
        "source_hash_mismatch": "来源索引与已登记原文的哈希不一致",
        "source_page_unavailable": "证据页码不在已核验的原文页面中",
        "source_index_missing": "缺少可用的原文来源索引",
        "source_index_unreadable": "原文来源索引损坏或格式不正确",
        "source_reference_mismatch": "证据引用与来源索引中的原文或页码不一致",
        "source_reference_invalid": "证据引用格式不正确",
        "missing_evidence": "事实没有关联原文证据",
        "unknown_evidence_ref": "事实引用的证据不存在",
        "unresolved_source": "无法在已登记原文中定位证据",
        "missing_value": "事实缺少可保存的值",
        "missing_fact_ref": "事实缺少唯一引用编号",
        "fact_not_materialized": "事实未生成可保存记录",
    }
    return labels.get(code, "事实转换未完成")


def _publication_fact_label(pointer: str) -> str:
    parts = pointer.split("/")
    category = {
        "composition": "组成",
        "preparation": "制备",
        "conditions": "测试条件",
        "results": "测试结果",
        "details": "共享方法",
        "statements": "陈述",
    }.get(parts[-2], "内容")
    return f"{category}第 {int(parts[-1]) + 1} 项"


def publication_draft(
    candidate: PublicationPackage, view: CandidateSnapshot, candidate_sha256: str, review_sha256: str | None
) -> PublicationPackage:
    if view.get("status") != "available" or view.get("candidate_sha256") != candidate_sha256:
        conflict("Sol 候选版本已变化，请刷新后重新确认")
    if review_sha256 is not None and view.get("review_sha256") != review_sha256:
        conflict("review version changed; reload before publishing")
    if (
        candidate["assertions"]
        or candidate["relations"]
        or candidate["evidence"]
        or any(row.get("kind") != "source" for row in candidate["objects"])
    ):
        conflict("此任务在人工确认前已生成旧式记录，请重新提取后使用新核对流程")
    return deepcopy(candidate)


class _Omitted(Enum):
    FACT = "omitted"


@dataclass(frozen=True, slots=True)
class _ReviewedFact:
    candidate_id: str
    pointer: str
    model_ref: JsonValue
    evidence_refs: JsonValue

    def cites(self, ref: JsonValue) -> bool:
        return isinstance(ref, str) and isinstance(self.evidence_refs, (list, str, dict)) and ref in self.evidence_refs


@dataclass(slots=True)
class _GroupSelection:
    candidate_id: str
    decisions: dict[str, CandidateReview]
    records: list[_ReviewedFact] = field(default_factory=list)
    moves: list[tuple[str, str, JsonObject]] = field(default_factory=list)

    def prune(self, value: JsonValue, pointer: str) -> JsonValue | _Omitted:
        # Exclude malformed scalar/list facts by their original pointer too.
        if FACT.fullmatch(pointer) and pointer not in self.decisions:
            return _Omitted.FACT
        if isinstance(value, dict):
            return self.prune_object(value, pointer)
        if isinstance(value, list):
            return [
                selected
                for i, child in enumerate(value)
                if (selected := self.prune(child, f"{pointer}/{i}")) is not _Omitted.FACT
            ]
        return deepcopy(value)

    def prune_object(self, value: JsonObject, pointer: str) -> JsonObject | _Omitted:
        review = self.decisions.get(pointer)
        if review or FACT.fullmatch(pointer) or {"value", "evidence", "fact_type", "attribution"} <= set(value):
            if not review:
                return _Omitted.FACT
            corrected = deepcopy(value)
            corrected.update(deepcopy(review.get("correction", {})))
            self.records.append(
                _ReviewedFact(self.candidate_id, pointer, corrected.get("ref"), corrected.get("evidence", []))
            )
            owner = review.get("ownership_target_ref")
            if owner:
                self.moves.append((owner, pointer.split("/")[-2], corrected))
                return _Omitted.FACT
            return corrected
        children: JsonObject = {}
        for key, child in value.items():
            if key != "source":
                selected = self.prune(child, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"))
                if selected is not _Omitted.FACT:
                    children[key] = selected
        return children

    def extract(self, raw: JsonObject) -> JsonObject:
        extracted = self.prune_object(raw, "")
        for target_ref, category, fact in self.moves:
            resolve(extracted, target_ref).setdefault(category, []).append(fact)
        if extracted is _Omitted.FACT or len(self.records) != len(self.decisions):
            conflict("已确认内容的结构不完整，无法转换；候选和审核仍保留")
        return extracted


def _referenced_evidence(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        yield from (ref for ref in value.get("evidence", []) if isinstance(ref, str))
        for child in value.values():
            yield from _referenced_evidence(child)
    elif isinstance(value, list):
        for child in value:
            yield from _referenced_evidence(child)


def _source_evidence(raw: JsonObject) -> Iterator[JsonObject]:
    rows = raw.get("evidence", [])
    if isinstance(rows, list):
        yield from (row for row in rows if isinstance(row, dict))


@dataclass(frozen=True, slots=True)
class PublicationSources:
    index: SourceIndex
    assets: Mapping[str, JsonObject]
    groups: dict[str | None, _GroupInfo]

    @classmethod
    def load(cls, root: Path, assets: Mapping[str, JsonObject]) -> "PublicationSources":
        index = load_source_index(root, assets)
        summary_path = checked_child(root, "input-summary.json")
        try:
            groups = {row["group_id"]: row for row in json.loads(summary_path.read_text())}
        except (OSError, ValueError, TypeError, KeyError):
            message = "缺少候选对应的来源分组信息；候选和审核仍保留"
            raise StateConflict(message) from None
        return cls(index, assets, groups)

    def evidence(
        self, raw: JsonObject, referenced: set[str], metadata: _GroupInfo, records: list[_ReviewedFact]
    ) -> list[JsonValue]:
        selected: list[JsonValue] = []
        for row in _source_evidence(raw):
            ref = row.get("ref")
            if ref not in referenced:
                continue
            item = canonical_location_evidence(row, metadata, self.assets)
            reason = self.index.evidence_error(item, self.assets)
            if reason:
                related = next((r for r in records if r.cites(ref)), None)
                label = _publication_fact_label(related.pointer) if related else "已确认内容的上下文"
                conflict(f"{label}无法保存：{_publication_failure_reason(reason)}；候选和审核仍保留")
            selected.append(item)
        return selected


def _check_trace(published: PublicationPackage, refs: Mapping[JsonValue, str], records: list[_ReviewedFact]) -> None:
    assertions = {
        row["assertion_id"]
        for row in published["assertions"]
        if row["value"].get("raw_origin") != "model_generated_display_label"
    }
    issues = {row["issue_id"]: row for row in published["issues"]}
    for record in records:
        mapped = refs.get(record.model_ref)
        if mapped in assertions:
            continue
        issue = issues.get(mapped) if mapped is not None else None
        reasons = issue.get("program_check", {}).get("reasons", []) if issue else []
        reason = (
            "；".join(_publication_failure_reason(item) for item in reasons)
            if reasons
            else _publication_failure_reason("missing_fact_ref" if not record.model_ref else "fact_not_materialized")
        )
        conflict(f"{_publication_fact_label(record.pointer)}无法保存：{reason}；候选和审核仍保留")


def _materialize_group(
    group: CandidateGroup, sources: PublicationSources, published: PublicationPackage
) -> list[_ReviewedFact]:
    if group["raw_status"] != "available":
        return []
    reviews = {
        r["candidate_ref"]: r for r in group.get("reviews", []) if r.get("decision") in ("confirmed", "corrected")
    }
    if not reviews:
        return []
    group_id = group.get("group_id")
    if group_id not in sources.groups:
        conflict("已确认内容缺少对应的来源分组信息；候选和审核仍保留")
    selection = _GroupSelection(group["candidate_id"], reviews)
    extracted = selection.extract(group["raw"])
    metadata = sources.groups[group_id]
    extracted["evidence"] = sources.evidence(
        group["raw"], set(_referenced_evidence(extracted)), metadata, selection.records
    )
    try:
        trace = materialize_program_output(
            extracted,
            sources.index.registry,
            published,
            group_id=group_id,
            target_block_ids=metadata.get("target_block_ids", []),
        )
    except ValueError:
        message = "已确认内容的结构不完整或存在重复编号，无法转换；候选和审核仍保留"
        raise StateConflict(message) from None
    _check_trace(published, trace["fact_refs"], selection.records)
    return selection.records


def _mark_reviewed(published: PublicationPackage) -> None:
    selected: list[_Assertion] = []
    contextual_owners = {}
    for assertion in published["assertions"]:
        if assertion["value"].get("raw_origin") == "model_generated_display_label":
            assertion["use_status"] = "context_only"
            contextual_owners[assertion["assertion_id"]] = assertion["subject_ref"]
        else:
            assertion["use_status"] = "human_reviewed"
            assertion["review_state"] = "human_source_reviewed"
            selected.append(assertion)
    published["assertions"] = selected
    # A converter diagnostic targeting a display label belongs to its owner.
    for issue in published["issues"]:
        contextual_refs = [ref for ref in issue.get("target_refs", []) if ref in contextual_owners]
        if contextual_refs:
            issue["source_context"] = {
                **issue.get("source_context", {}),
                "contextual_assertion_refs": [*contextual_refs],
            }
            issue["target_refs"] = list(dict.fromkeys(contextual_owners.get(ref, ref) for ref in issue["target_refs"]))
    if not selected:
        conflict("已确认的内容没有生成可保存事实；候选仍保留在资料页")
    for relation in published["relations"]:
        relation["use_status"] = "human_reviewed"
        relation["review_state"] = "human_source_reviewed"


def materialize_publication(
    published: PublicationPackage, view: CandidateSnapshot, sources: PublicationSources, candidate_sha256: str
) -> PublicationPackage:
    reviewed = [record for group in view["groups"] for record in _materialize_group(group, sources, published)]
    if not reviewed or not published["assertions"]:
        conflict("没有可转换的已确认内容；候选仍保留在资料页")
    _mark_reviewed(published)
    header = published["header"]
    header["review_policy"] = "human_reviewed_candidate_v1"
    header["human_reviewed_candidates"] = [
        {"candidate_id": cid, "candidate_ref": ref}
        for cid, ref in sorted({(r.candidate_id, r.pointer) for r in reviewed})
    ]
    quality: JsonObject = {
        "version": "human_reviewed_candidate_v1",
        "outcome": "reviewed_subset",
        "reviewed_assertions": len(published["assertions"]),
        "candidate_sha256": candidate_sha256,
        "review_sha256": view.get("review_sha256"),
        "human_expert_review": False,
        "scientific_validation": False,
    }
    quality["package_content_sha256"] = package_content_hash(dict(published))
    header["document_quality"] = quality
    validate_package(dict(published))
    return published
