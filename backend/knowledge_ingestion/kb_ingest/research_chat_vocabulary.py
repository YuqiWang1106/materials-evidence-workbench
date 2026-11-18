"""Observed query navigation for trusted selected record roots, never global caches."""

import time
from copy import deepcopy

from pydantic import Field

from .contract import FIELDS, PROFILES
from .core.serialization import canonical, fingerprint
from .knowledge_contracts import VERSION, ContextValidateRequest, Id, RecordCatalogueRequest, StrictModel
from .knowledge_service import READ_DEADLINE
from .knowledge_snapshot import KnowledgeIntegrityError
from .research_chat_contracts import ResearchError, ResearchSelection


class FieldsArgs(StrictModel):
    source_id: Id | None = Field(
        default=None, description="已知来源时限于该已选来源；只缩小词汇目录，不改变query的冻结范围。"
    )
    field_id: str | None = Field(
        default=None, description="已知所需field_id时用它缩小目录；找到所需selector即可query，不必读完所有字段。"
    )
    page_size: int = Field(default=20, ge=1, le=20)
    cursor: Id | None = None


class VocabularyLimit(ResearchError):
    def __init__(self, reason):
        super().__init__("context_limit")
        self.reason = reason

    def public(self):
        return {**super().public(), "reason": self.reason}


class SelectedSourceVocabulary:
    def __init__(self, knowledge, selection):
        self.knowledge = knowledge
        self.selection = ResearchSelection.model_validate(selection).model_dump(mode="json")

    def _validate(self, scope):
        roots = scope["record_refs"]
        if not roots or {r["source_id"] for r in roots} != set(scope["source_ids"]):
            raise ResearchError("not_ready")
        for start in range(0, len(roots), 40):
            result = self.knowledge.validate_context(
                ContextValidateRequest(
                    schema_version=VERSION,
                    request_id="research-fields-check",
                    use_policy=scope["use_policy"],
                    refs=roots[start : start + 40],
                )
            )
            if not result.valid:
                raise KnowledgeIntegrityError("STALE_CONTEXT")

    def _entries(self, scope, args):
        unique = {}
        size = 0
        for root in scope["record_refs"]:
            if args.source_id is not None and root["source_id"] != args.source_id:
                continue
            cursor = None
            while True:
                self.knowledge._deadline(float("inf"))
                page = self.knowledge.catalogue(
                    RecordCatalogueRequest(
                        schema_version=VERSION,
                        request_id="research-fields-catalogue",
                        target=root,
                        use_policy=scope["use_policy"],
                        kind="assertions",
                        page={"size": 200, "cursor": cursor},
                    )
                )
                if not page.completeness.matching_complete or not page.completeness.exhaustive_within_scope:
                    raise ResearchError("stale_context")
                for row in page.entries:
                    if args.field_id is not None and row.field_id != args.field_id:
                        continue
                    allowed = {k for p in PROFILES.values() if p["field_id"] == row.field_id for k in p["qualifiers"]}
                    filtering = {k: v for k, v in row.qualifiers.items() if k in allowed}
                    metadata = {k: v for k, v in row.qualifiers.items() if k not in allowed}
                    entry = {
                        "source_id": root["source_id"],
                        "field_id": row.field_id,
                        "observed_qualifiers": dict(row.qualifiers),
                        "non_filtering_metadata": metadata,
                        "distinguishes_observed_tuple": False,
                        "query_template": {
                            "filters": [{"field_id": row.field_id, "op": "exists", "qualifiers": filtering}]
                        },
                    }
                    key = canonical(entry)
                    if key not in unique:
                        unique[key] = entry
                        size += len(key.encode())
                        if size > 262144:
                            raise VocabularyLimit("vocabulary_scope_too_large")
                if not page.completeness.has_more:
                    break
                cursor = page.completeness.next_cursor
        return [unique[key] for key in sorted(unique)]

    def read(self, args):
        args = FieldsArgs.model_validate(args)
        scope = self.selection["scopes"].get("record")
        if scope is None:
            raise ResearchError("not_ready")
        if args.source_id is not None and args.source_id not in scope["source_ids"]:
            raise ResearchError("invalid_request")
        if args.field_id is not None and args.field_id not in FIELDS:
            raise ResearchError("invalid_request")
        deadline = time.monotonic() + self.knowledge.timeout_seconds
        inherited = READ_DEADLINE.get()
        token = READ_DEADLINE.set(min(deadline, inherited) if inherited is not None else deadline)
        try:
            self.knowledge._deadline(deadline)
            self._validate(scope)
            semantic = {
                "version": "research-observed-fields-v1",
                "selection": self.selection,
                "arguments": args.model_dump(mode="json", exclude={"cursor"}),
            }
            digest = fingerprint(semantic)
            if args.cursor:
                resultset, offset, payload = self.knowledge.results.read(args.cursor, digest)
            else:
                payload = {"fingerprint": digest, "entries": self._entries(scope, args)}
                resultset = self.knowledge.results.put(payload)
                offset = 0
            view = {
                "scope": {k: scope[k] for k in ("workspace_id", "collection_id", "corpus_snapshot_id", "use_policy")},
                "navigation_only": True,
                "template_scope": "selected_record_scope",
                "navigation_guidance": (
                    "找到所需query_template即可query；has_more仅表示词汇目录尚有其它条目，"
                    "不表示样本匹配覆盖。样本范围与计数由实际query及其分页回执确认。"
                ),
                "matching_semantics": (
                    "同一断言须匹配模板列出的键，但可有额外限定符；non_filtering_metadata不参与过滤。"
                    "模板查询整个选定record范围，source_id只标明词汇的观测来源。"
                ),
                "items": [],
                "has_more": False,
                "next_cursor": None,
            }
            end = offset
            # Reserve the actual cursor token's maximum wire length while fitting
            # complete entries. The cursor advances only over visible entries.
            for entry in payload["entries"][offset : offset + args.page_size]:
                trial = {**view, "items": view["items"] + [entry], "has_more": True, "next_cursor": "x" * 43}
                if len(canonical(trial).encode()) > 12000:
                    if not view["items"]:
                        raise VocabularyLimit("vocabulary_item_too_large")
                    break
                view["items"].append(deepcopy(entry))
                end += 1
            self._validate(scope)
            self.knowledge._deadline(deadline)
            if end < len(payload["entries"]):
                view.update(has_more=True, next_cursor=self.knowledge.results.cursor(resultset, end))
            return view
        finally:
            READ_DEADLINE.reset(token)
