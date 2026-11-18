"""Transient model quote selections; persisted targets still use the original binder."""

from pydantic import Field

from .core.serialization import canonical
from .knowledge_contracts import Id, StrictModel
from .knowledge_domain import load_domain_profile
from .research_chat_contracts import ResearchError
from .research_tasks_contracts import TargetSelection, UserMessage


class TargetQuoteSelection(StrictModel):
    # Keep runtime validation separate from the advertised enum so an invalid
    # profile produces a useful binding error rather than a generic schema error.
    profile_id: Id
    quote: str = Field(min_length=1, max_length=512)


class TargetArgs(StrictModel):
    selections: list[TargetQuoteSelection] = Field(min_length=1, max_length=20)


def target_profiles():
    profiles = {}
    for mapping in load_domain_profile().mappings:
        profiles.setdefault(mapping.catalogue_profile_id, set()).add(mapping.source_label)
    return [{"profile_id": key, "source_labels": sorted(profiles[key])} for key in sorted(profiles)]


def target_schema():
    schema = TargetArgs.model_json_schema()
    profiles = target_profiles()
    prop = schema["$defs"]["TargetQuoteSelection"]["properties"]["profile_id"]
    prop["enum"] = [profile["profile_id"] for profile in profiles]
    prop["description"] = "字段profile，不是资料名称。以下仅为目标语法声明，不证明所选来源已有观测值：" + canonical(
        profiles
    )
    schema["$defs"]["TargetQuoteSelection"]["properties"]["quote"]["description"] = (
        "逐字复制本轮用户原文中唯一出现的完整目标片段；不改数字、单位、空白或标点，不提交offset。"
    )
    return schema


def quote_selections(message, selections):
    """Locate exact Unicode substrings; never normalize, guess or take the first duplicate."""
    message = UserMessage.model_validate(message)
    selections = TargetArgs.model_validate({"selections": selections}).selections
    allowed = {profile["profile_id"] for profile in target_profiles()}
    out = []
    for part in selections:
        if part.profile_id not in allowed:
            raise ResearchError("target_unknown_profile")
        start = message.text.find(part.quote)
        if start < 0:
            raise ResearchError("target_quote_not_found")
        if message.text.find(part.quote, start + 1) >= 0:
            raise ResearchError("target_quote_ambiguous")
        end = start + len(part.quote)
        if any(start < previous.end and previous.start < end for previous in out):
            raise ResearchError("target_quote_overlap")
        out.append(TargetSelection(profile_id=part.profile_id, start=start, end=end))
    return out
