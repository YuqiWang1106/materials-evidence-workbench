"""Freeze committed research reads; deterministic claims retain their original scope."""

import time
from contextlib import contextmanager
from copy import deepcopy

from .core.serialization import canonical, fingerprint
from .knowledge_claim_contracts import KnowledgeSupport
from .knowledge_summary_contracts import SECTIONS, SummaryContent
from .research_chat_evidence import EditorialBlock, EvidenceRegistry
from .research_chat_tools import ResearchTools
from .research_summary_contracts import SummaryDraft, UserRequirement

MAX_INPUT_BYTES = 180_000
INPUT_V1 = "research-summary-input-v1"
INPUT_V2 = "research-summary-input-v2"


@contextmanager
def read_budget():
    from .knowledge_service import READ_DEADLINE

    previous = READ_DEADLINE.get()
    deadline = time.monotonic() + 30
    token = READ_DEADLINE.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        READ_DEADLINE.reset(token)


def turn_binding(turn):
    return {key: deepcopy(turn[key]) for key in ("chat_id", "turn_id", "input_hash", "status", "message", "answer")}


def _tools(research, context):
    selection = deepcopy(context["selection"])
    if not any(e["layer"] == "wiki" for e in context["registry"].values()):
        selection["scopes"].pop("wiki", None)
    return ResearchTools(
        research.knowledge,
        selection,
        prior=context["registry"],
        passage_reader=research.passage_reader,
        message_reader=lambda mid: research.user_message(context["chat_id"], mid),
    )


def _turn_entries(selection, turn, policy):
    if policy not in (INPUT_V1, INPUT_V2):
        raise ValueError("UNSUPPORTED_RESEARCH_INPUT_POLICY")
    # Validate the whole saved ledger first, then resolve this turn's own proof.
    # A later turn must never supply a missing earlier dependency.
    registry = EvidenceRegistry(selection, prior=turn["answer"]["registry"])
    if policy == INPUT_V1:
        return registry.entries
    used = turn["answer"].get("used_ids")
    if type(used) is not list or any(type(key) is not str for key in used):
        raise ValueError("RESEARCH_USED_IDS_INVALID")
    return {key: registry.entries[key] for key in registry.closure(used)}


def _merge_entries(entries, selected):
    for key, entry in selected.items():
        if key in entries and entries[key] != entry:
            raise ValueError("RESEARCH_EVIDENCE_CHANGED")
        entries[key] = entry


def freeze_window(research, wiki, chat_id, end_turn_id, *, max_turns=20):
    if type(max_turns) is not int or not 1 <= max_turns <= 20:
        raise ValueError("INVALID_SUMMARY_WINDOW")
    if research.files.read_chat(chat_id)["selection"].get("scope_mode") == "unified":
        from .research_summary_unified import freeze_unified_window

        return freeze_unified_window(research, wiki, chat_id, end_turn_id, max_turns)
    with research.files.lock(chat_id):
        chat = research.files.read_chat(chat_id)
        if end_turn_id not in chat["turn_ids"]:
            raise ValueError("TURN_NOT_COMMITTED")
        end = chat["turn_ids"].index(end_turn_id) + 1
        tids = chat["turn_ids"][max(0, end - max_turns) : end]
        turns = []
        entries = {}
        dependencies = []
        for tid in tids:
            turn, snapshot = research.files.read_turn(chat_id, tid)
            if turn["status"] != "completed" or not turn["answer"]:
                raise ValueError("TURN_NOT_COMPLETED")
            if snapshot["selection"] != chat["selection"]:
                raise ValueError("RESEARCH_SCOPE_CHANGED")
            _merge_entries(entries, _turn_entries(chat["selection"], turn, INPUT_V2))
            turns.append({"turn_id": tid, "message": turn["message"]})
            dependencies.append(
                {
                    "dependency_id": tid,
                    "kind": "research_turn",
                    "chat_id": chat_id,
                    "turn_id": tid,
                    "binding_sha256": fingerprint(turn_binding(turn)),
                }
            )
    context = {
        "chat_id": chat_id,
        "turn_ids": tids,
        "turns": turns,
        "selection": chat["selection"],
        "registry": entries,
    }
    context["input_policy_version"] = INPUT_V2
    context["input_hash"] = fingerprint(context)
    # The same saved reads are fresh-validated before a paid request or a Wiki write.
    with read_budget():
        _tools(research, context).prepare(list(entries))
    supports = []
    registry = EvidenceRegistry(chat["selection"], prior=entries)
    for key, entry in entries.items():
        for ref in registry.references([key]).get(entry["layer"], []):
            sid = "support-" + fingerprint([key, ref])
            support = {
                "support_id": sid,
                "ref": ref,
                "research_entry_id": key,
                "scope": entry["scope"],
                "support_type": "calculation"
                if entry["kind"] == "task_result"
                else "source_passage"
                if entry["layer"] == "source_passage"
                else "reported_evidence",
            }
            supports.append(KnowledgeSupport.model_validate(support).model_dump(mode="json"))
    corpus = wiki._corpus(chat["selection"]["source_ids"], kind="trusted-research-conversation-corpus")
    dependencies.extend(
        {
            "dependency_id": s["support_id"],
            "kind": "research_reference",
            "ref": s["ref"],
            "scope": s["scope"],
            "research_entry_id": s["research_entry_id"],
        }
        for s in supports
    )
    manifest = {
        "version": "knowledge-dependencies-v1",
        "corpus_snapshot_id": corpus,
        "supports": supports,
        "dependencies": dependencies,
        "research_summary": context,
    }
    snapshot = {"chat_id": chat_id, "turn_ids": tids, "manifest": manifest}
    if len(canonical(snapshot).encode()) > MAX_INPUT_BYTES:
        raise ValueError("SUMMARY_WINDOW_TOO_LARGE: 请减小 max_turns 后使用新的 request_id。")
    return snapshot


def validate_frozen(research, wiki, snapshot):
    manifest = snapshot["manifest"]
    context = manifest["research_summary"]
    cid = context["chat_id"]
    from .research_summary_unified import is_unified, validate_unified

    if is_unified(manifest):
        return validate_unified(research, wiki, snapshot)
    policy = context.get("input_policy_version")
    if policy not in (INPUT_V1, INPUT_V2):
        raise ValueError("UNSUPPORTED_RESEARCH_INPUT_POLICY")
    if context.get("input_hash") != fingerprint({k: v for k, v in context.items() if k != "input_hash"}):
        raise ValueError("RESEARCH_INPUT_CHANGED")
    wiki.registry.check_manifest(wiki.snapshot_store.read_corpus(manifest["corpus_snapshot_id"]))
    with research.files.lock(cid):
        chat = research.files.read_chat(cid)
        if chat["selection"] != context["selection"]:
            raise ValueError("RESEARCH_SCOPE_CHANGED")
        observed = {}
        deps = [d for d in manifest["dependencies"] if d["kind"] == "research_turn"]
        if [d["turn_id"] for d in deps] != context["turn_ids"]:
            raise ValueError("RESEARCH_WINDOW_CHANGED")
        for dep in deps:
            if dep["kind"] != "research_turn" or dep["chat_id"] != cid or dep["turn_id"] not in chat["turn_ids"]:
                raise ValueError("TURN_NOT_COMMITTED")
            turn, _ = research.files.read_turn(cid, dep["turn_id"])
            if turn["status"] != "completed" or fingerprint(turn_binding(turn)) != dep["binding_sha256"]:
                raise ValueError("TURN_CHANGED")
            _merge_entries(observed, _turn_entries(context["selection"], turn, policy))
        if observed != context["registry"]:
            raise ValueError("RESEARCH_EVIDENCE_CHANGED")
        if context["turns"] != [
            {"turn_id": d["turn_id"], "message": research.files.read_turn(cid, d["turn_id"])[0]["message"]}
            for d in deps
        ]:
            raise ValueError("TURN_CHANGED")
    expected = []
    registry = EvidenceRegistry(context["selection"], prior=observed)
    for key, entry in observed.items():
        for ref in registry.references([key]).get(entry["layer"], []):
            expected.append(
                {
                    "dependency_id": "support-" + fingerprint([key, ref]),
                    "kind": "research_reference",
                    "ref": ref,
                    "scope": entry["scope"],
                    "research_entry_id": key,
                }
            )
    if [d for d in manifest["dependencies"] if d["kind"] != "research_turn"] != expected:
        raise ValueError("RESEARCH_EVIDENCE_CHANGED")
    for support, dep in zip(manifest["supports"], expected):
        if support["support_id"] != dep["dependency_id"] or any(
            support.get(k) != dep[k] for k in ("ref", "scope", "research_entry_id")
        ):
            raise ValueError("RESEARCH_EVIDENCE_CHANGED")
    if len(manifest["supports"]) != len(expected):
        raise ValueError("RESEARCH_EVIDENCE_CHANGED")
    with read_budget():
        _tools(research, context).prepare(list(observed))
        from .knowledge_contracts import VERSION, GetRequest

        for entry in observed.values():
            if entry["kind"] != "quote":
                continue
            result = research.knowledge.get(
                GetRequest(
                    schema_version=VERSION,
                    request_id="summary-quote-validation",
                    target=entry["ref"],
                    use_policy=entry["scope"]["use_policy"],
                    view="location",
                )
            )
            row = result.results[0]
            evidence = row if row.kind == "source_passage" else row.evidence_data
            actual = evidence.content_kind if row.kind == "source_passage" else evidence.effective_content_kind
            provenance = evidence.provenance.model_dump(mode="json") if evidence.provenance else None
            if (evidence.quote, actual, provenance) != (entry["quote"], entry["content_kind"], entry["provenance"]):
                raise ValueError("RESEARCH_QUOTE_CHANGED")


def _claim(block, context, supports):
    registry = EvidenceRegistry(context["selection"], prior=context["registry"])
    if block["kind"] == "user_requirement":
        tid = UserRequirement.model_validate(block).turn_id
        message = next((t["message"] for t in context["turns"] if t["turn_id"] == tid), None)
        if message is None:
            raise ValueError("REQUIREMENT_OUTSIDE_WINDOW")
        text = "用户目标/要求（非实测；原始输入）：" + message
        return text, "editorial_synthesis", []
    result = registry.render({"blocks": [block]})
    used = result["used_ids"]
    kind = block["kind"]
    claim_type = "editorial_synthesis"
    if kind in ("fact", "count", "comparison", "quote", "guide"):
        entry = registry.item(block["id"], kind)
        claim_type = (
            "reported_measurement"
            if kind == "fact" and entry["layer"] == "record" and entry["value"]["field_id"] == "observation.value"
            else "source_description"
        )
    ids = [s["support_id"] for s in supports if s.get("research_entry_id") in used]
    return result["text"], claim_type, ids


def render_summary(draft, snapshot):
    draft = SummaryDraft.model_validate(draft)
    manifest = snapshot["manifest"]
    context = manifest["research_summary"]
    EvidenceRegistry(context["selection"], prior=context["registry"])._prose(
        EditorialBlock(kind="editorial", text=draft.title)
    )
    sections = []
    for section, (_, heading) in zip(draft.sections, SECTIONS):
        claims = []
        for index, block in enumerate(section.blocks):
            raw = block.model_dump(mode="json")
            text, kind, ids = _claim(raw, context, manifest["supports"])
            claims.append(
                {
                    "claim_id": "claim-"
                    + fingerprint([context["chat_id"], context["turn_ids"], section.section_id, index]),
                    "claim_type": kind,
                    "text": text,
                    "support_ids": ids,
                    "scientific_validation": False,
                    "research_provenance": {"block": raw},
                }
            )
        sections.append({"section_id": section.section_id, "heading": heading, "claims": claims})
    return SummaryContent.model_validate(
        {
            "template_version": "experimental-analysis-v1",
            "title": draft.title,
            "sections": sections,
            "research_question": "",
            "conditions": [],
            "uncertainties": [],
        }
    ).model_dump(mode="json", exclude_none=True)


def validate_content(manifest, content):
    content = SummaryContent.model_validate(content).model_dump(mode="json", exclude_none=True)
    context = manifest["research_summary"]
    registry = EvidenceRegistry(context["selection"], prior=context["registry"])
    for text in [content["title"], content["research_question"], *content["conditions"], *content["uncertainties"]]:
        if text:
            registry._prose(EditorialBlock(kind="editorial", text=text))
    for section in content["sections"]:
        for claim in section["claims"]:
            proof = claim.get("research_provenance")
            if not proof or set(proof) != {"block"}:
                raise ValueError("RESEARCH_CLAIM_PROOF_REQUIRED")
            text, kind, ids = _claim(proof["block"], context, manifest["supports"])
            if (claim["text"], claim["claim_type"], claim["support_ids"]) != (text, kind, ids):
                raise ValueError("RESEARCH_CLAIM_CHANGED")
    return content


def normalize_edit(manifest, content):
    """Editable interpretation stays labelled; source facts retain exact proof."""
    from .research_chat_evidence import LABELS

    content = deepcopy(content)
    for section in content["sections"]:
        for claim in section["claims"]:
            block = (claim.get("research_provenance") or {}).get("block", {})
            if block.get("kind") not in LABELS:
                continue
            expected, _, _ = _claim(block, manifest["research_summary"], manifest["supports"])
            if claim["text"] == expected:
                continue
            label = LABELS[block["kind"]] + "："
            block["text"] = claim["text"][len(label) :] if claim["text"].startswith(label) else claim["text"]
            claim["text"], claim["claim_type"], claim["support_ids"] = _claim(
                block, manifest["research_summary"], manifest["supports"]
            )
    return validate_content(manifest, content)


def no_self_reference(wiki, context, target):
    if target is None:
        return
    pending = [e["ref"] for e in context["registry"].values() if e.get("ref", {}).get("kind") == "wiki_revision"]
    for entry in context["registry"].values():
        binding = entry.get("wiki_binding")
        if binding:
            pending.append(binding)
    seen = set()
    while pending:
        ref = pending.pop()
        key = (ref["page_id"], ref["revision_id"])
        if ref["page_id"] == target:
            raise ValueError("WIKI_SELF_REFERENCE")
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > 100:
            raise ValueError("WIKI_DEPENDENCY_LIMIT")
        page = wiki.store.get_page(ref["page_id"], ref["revision_id"])
        pending.extend(s["ref"] for s in page["revision"]["supports"] if s["ref"]["kind"] == "wiki_revision")
