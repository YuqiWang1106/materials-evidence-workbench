"""Fail closed when human corrections no longer agree with their fixed source evidence.

This validates retained, edited claims without changing their text, locations or hashes. It is
not a scientific truth check: unsupported paraphrases require a source-supported formulation.
"""

import re
from collections import Counter

from .entity_check import BlockText, named_in
from .entity_evidence_values import normalized_evidence_text, value_evidence_issues


def _drops_negation(text, quote):
    """An excerpt cannot turn 'did not improve' into 'improve' by trimming its own clause."""

    def markers(value):
        # Recognized numeric comparisons (e.g. 不低于 -> >=) are already preserved in
        # quantity tokens. Negation outside those tokens must remain in the edited clause.
        normalized = re.sub(r"⟦[^⟧]*⟧", "", normalized_evidence_text(value))
        chinese = re.findall(r"未|不|无|没有|并非", normalized)
        english = re.findall(
            r"\b(?:not|no|never|without|neither|nor|cannot|unable|lack(?:s|ed|ing)?|failed|\w+n['’]t)\b",
            value,
            flags=re.I,
        )
        return Counter([*chinese, *(word.casefold() for word in english)])

    edited = normalized_evidence_text(text)
    clauses = [
        part for part in re.split(r"[。；;，,！？!?]|\.(?!\d)", quote) if edited in normalized_evidence_text(part)
    ]
    # A multi-clause edit is checked as a whole; unrelated negative clauses do not
    # prevent a positive, independently supported measurement from being selected.
    return all(bool(markers(clause) - markers(text)) for clause in (clauses or [quote]))


class EvidenceValidationError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        details = []
        for issue in issues[:5]:
            page = f"第 {issue['page']} 页" if issue.get("page") is not None else "页码未知"
            details.append(f"{issue['entity']}／{issue['section']}（{page}，{issue['block_id']}）：{issue['message']}")
        more = f"；另有 {len(issues) - 5} 项" if len(issues) > 5 else ""
        super().__init__("证据校验未通过，未发布；修改已保留。" + "；".join(details) + more)


def validate_review_evidence(draft, reviews, blocks):
    """Validate corrections before publication/model I/O; untouched and excluded rows stay untouched."""
    from .entity_review import attribute_key, effective_graph

    corrected = {key for key, review in reviews.items() if review.get("decision") == "corrected"}
    if not corrected:
        return
    originals = {attribute_key(row): row for row in draft.get("attributes", [])}
    original_entities = {row["local_id"]: row for row in draft.get("entities", [])}
    # Original names can explain chemical formula/standard digits. A new human alias must
    # never hide a newly introduced measurement from the numeric validator.
    names = [name for row in original_entities.values() for name in [row["name"], *row.get("aliases", [])]]
    effective = effective_graph(draft, reviews, with_review_keys=True)
    entities = {row["local_id"]: row for row in effective["entities"]}
    affected = []
    for row in effective["attributes"]:
        original = originals[row["_review_key"]]
        if (
            row["_review_key"] in corrected
            or original["local_id"] in corrected
            or row["local_id"] in corrected
            or row["local_id"] != original["local_id"]
            or corrected.intersection([*original.get("refs", []), *row.get("refs", [])])
        ):
            affected.append((row, original))
    if not affected:
        return
    registry = blocks if isinstance(blocks, dict) else {}
    source = BlockText([{**value, "block_id": key} for key, value in registry.items() if isinstance(value, dict)])
    issues = []

    def report(row, place, code, message):
        block = registry.get(place.get("block_id")) or {}
        issues.append(
            {
                "key": row["_review_key"],
                "entity": entities[row["local_id"]]["name"],
                "section": row["section"],
                "block_id": place.get("block_id", ""),
                "page": block.get("page"),
                "code": code,
                "message": message,
            }
        )

    for row, original in affected:
        edited_text = normalized_evidence_text(row["text"])
        text_edited = "text" in reviews.get(row["_review_key"], {}).get("correction", {})
        for place in [row, *row.get("citations", [])]:
            block_id, quote = place.get("block_id"), place.get("quote")
            covered = place.get("block_ids") or [block_id]
            if (
                not isinstance(covered, list)
                or not covered
                or block_id not in covered
                or any(
                    not isinstance(key, str)
                    or not isinstance(registry.get(key), dict)
                    or not isinstance(registry[key].get("text"), str)
                    for key in covered
                )
            ):
                report(row, place, "original_text_unavailable", "缺少可核对的原文内容，请补齐原文后再发布。")
                continue
            if not source.locate(block_id, quote, allowed=set(covered)):
                report(row, place, "quote_not_in_source", "引文与所标原文位置不一致，请核对原文。")
                continue
            value_issues = value_evidence_issues(row["text"], quote, names=names)
            for issue in value_issues:
                report(
                    row,
                    place,
                    issue["code"],
                    issue["message"] + (f"（{issue['value'][:80]}）" if issue.get("value") else ""),
                )
            if not value_issues and text_edited and edited_text not in normalized_evidence_text(quote):
                report(
                    row,
                    place,
                    "edited_text_not_supported",
                    "无法确认改写后的表述由该引文支持，请使用原文表述或排除该项。",
                )
            elif not value_issues and text_edited and _drops_negation(row["text"], quote):
                report(
                    row,
                    place,
                    "source_negation_removed",
                    "修改截去了原文同一子句的否定表述，请保留完整原文含义或排除该项。",
                )
            if row["local_id"] != original["local_id"]:
                # Use destination names from BEFORE the merge: inherited aliases cannot
                # turn S1's result into evidence for S2.
                target = original_entities.get(row["local_id"])
                peers = [entity for entity in original_entities.values() if target and entity["type"] == target["type"]]
                mentioned = named_in(quote, peers, None, include_formulations=True)
                if set(mentioned) != {row["local_id"]}:
                    report(
                        row,
                        place,
                        "changed_owner_not_supported",
                        "该引文不能确认唯一的新实体归属，不能把其他试样或材料的数据移入此实体。",
                    )
    if issues:
        raise EvidenceValidationError(issues)
