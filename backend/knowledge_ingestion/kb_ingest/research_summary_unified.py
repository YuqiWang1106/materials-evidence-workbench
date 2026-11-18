"""Frozen unified summary windows; no model calls, extraction or new calculations."""

from copy import deepcopy

from .core.serialization import canonical, fingerprint
from .knowledge_support_resolver import region_from_raw
from .knowledge_table_wiki_policy import UNIFIED_INPUT_POLICY_VERSION as INPUT_POLICY
from .research_chat_evidence import EvidenceRegistry
from .research_chat_unified import used_supports

MAX_INPUT_BYTES = 256_000


def is_unified(manifest):
    return manifest.get("research_summary", {}).get("input_policy_version") == INPUT_POLICY


def summary_model_input(snapshot):
    """One shared projection and byte check for freeze and paid dispatch."""
    from .knowledge_summary_contracts import SECTIONS
    from .research_chat_projection import shared_contexts

    context = snapshot["manifest"]["research_summary"]
    registry = EvidenceRegistry(context["selection"], prior=context["registry"])
    reads = shared_contexts({"items": [registry.model_view(entry) for entry in registry.entries.values()]})
    projected = {
        "input_policy_version": INPUT_POLICY,
        "turns": deepcopy(context["turns"]),
        "turn_evidence": [
            {"turn_id": row["turn_id"], "used_ids": deepcopy(row["used_ids"])} for row in context["turn_contexts"]
        ],
        "evidence": reads,
        "scientific_validation": False,
    }
    payload = {"sections": SECTIONS, "context": projected}
    if len(canonical(payload).encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("SUMMARY_WINDOW_TOO_LARGE: 请减小 max_turns 后使用新的 request_id。")
    return payload


def reference_dependencies(wiki, supports):
    """Freeze publishing dependencies separately from the bounded model input."""
    deps = []
    seen = set()
    for support in supports:
        ref = support["ref"]
        kind = ref["kind"]
        deps.append(
            {
                "dependency_id": support["support_id"],
                "kind": "research_reference",
                "ref": deepcopy(ref),
                "scope": deepcopy(support.get("scope")),
                "research_entry_id": support.get("research_entry_id"),
            }
        )
        if kind not in {"table_snapshot", "table_analysis_artifact", "study_unit_snapshot"}:
            continue
        identity = fingerprint(ref)
        if identity in seen:
            continue
        seen.add(identity)
        dep = {
            "dependency_id": "material-" + identity,
            "kind": {"table_snapshot": "raw_table"}.get(kind, kind),
            "ref": deepcopy(ref),
        }
        if kind == "table_snapshot":
            raw = wiki.ingestion_service._raw_artifacts().load("raw", ref["table_version_id"])["payload"]
            source = wiki.ingestion_service.store.source_state(raw["source_id"]) or {}
            dep.update(
                region=region_from_raw(raw),
                reader_options=deepcopy(raw["reader_options"]),
                published_task_id=source.get("published_task_id"),
            )
        deps.append(dep)
    return deps


def source_ids(supports, *, wiki=None):
    sources = set()
    pending = list(supports)
    seen = set()
    while pending:
        ref = pending.pop()["ref"]
        source = ref.get("source_id") or ref.get("source_snapshot_ref", {}).get("source_id")
        if source:
            sources.add(source)
        if wiki is not None and ref["kind"] == "wiki_revision":
            identity = (ref["page_id"], ref["revision_id"])
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > 100:
                raise ValueError("WIKI_DEPENDENCY_LIMIT")
            revision = wiki.store.get_page(*identity)["revision"]
            if any(revision[k] != ref[k] for k in ("content_hash", "dependency_manifest_hash")):
                raise ValueError("WIKI_REFERENCE_CHANGED")
            pending.extend(revision["supports"])
    return sorted(sources)


def freeze_unified_window(research, wiki, cid, end_tid, max_turns):
    from .research_summary_supports import INPUT_V2, _merge_entries, _tools, _turn_entries, read_budget, turn_binding

    # Copy committed input under the owner lock; shared artifact reads acquire it too.
    with research.files.lock(cid):
        chat = research.files.read_chat(cid)
        if end_tid not in chat["turn_ids"]:
            raise ValueError("TURN_NOT_COMMITTED")
        end = chat["turn_ids"].index(end_tid) + 1
        tids = chat["turn_ids"][max(0, end - max_turns) : end]
        saved = [deepcopy(research.files.read_turn(cid, tid)) for tid in tids]
    entries = {}
    turns = []
    contexts = []
    deps = []
    for turn, snapshot in saved:
        if turn["status"] != "completed" or not turn["answer"]:
            raise ValueError("TURN_NOT_COMPLETED: 请调整窗口，所有轮次必须已完成。")
        selection = snapshot["selection"]
        if snapshot.get("input_version") != "research-chat-input-v3" or selection.get("scope_mode") != "unified":
            raise ValueError("RESEARCH_SCOPE_CHANGED")
        _merge_entries(entries, _turn_entries(selection, turn, INPUT_V2))
        turns.append({"turn_id": turn["turn_id"], "message": turn["message"]})
        contexts.append(
            {"turn_id": turn["turn_id"], "selection": selection, "used_ids": deepcopy(turn["answer"]["used_ids"])}
        )
        deps.append(
            {
                "dependency_id": turn["turn_id"],
                "kind": "research_turn",
                "chat_id": cid,
                "turn_id": turn["turn_id"],
                "binding_sha256": fingerprint(turn_binding(turn)),
            }
        )
    context = {
        "chat_id": cid,
        "turn_ids": tids,
        "turns": turns,
        "selection": contexts[-1]["selection"],
        "turn_contexts": contexts,
        "registry": entries,
        "input_policy_version": INPUT_POLICY,
    }
    context["input_hash"] = fingerprint(context)
    # Preserve every entry's original scope. The last selection is render-only.
    with read_budget():
        pages = {}
        _tools(research, context).validate(list(entries), wiki_pages=pages)
        supports = used_supports(
            EvidenceRegistry(context["selection"], prior=entries), list(entries), wiki=wiki, validated_wiki_pages=pages
        )
    corpus = wiki._corpus(source_ids(supports, wiki=wiki), kind="trusted-unified-research-conversation-corpus")
    manifest = {
        "version": "knowledge-dependencies-v1",
        "corpus_snapshot_id": corpus,
        "supports": supports,
        "dependencies": deps + reference_dependencies(wiki, supports),
        "research_summary": context,
    }
    snapshot = {"chat_id": cid, "turn_ids": tids, "manifest": manifest}
    summary_model_input(snapshot)
    return snapshot


def validate_unified(research, wiki, snapshot):
    from .research_summary_supports import INPUT_V2, _merge_entries, _tools, _turn_entries, read_budget, turn_binding

    manifest = snapshot["manifest"]
    context = manifest["research_summary"]
    cid = context["chat_id"]
    if context.get("input_hash") != fingerprint({k: v for k, v in context.items() if k != "input_hash"}):
        raise ValueError("RESEARCH_INPUT_CHANGED")
    wiki.registry.check_manifest(wiki.snapshot_store.read_corpus(manifest["corpus_snapshot_id"]))
    deps = [d for d in manifest["dependencies"] if d["kind"] == "research_turn"]
    if [d["turn_id"] for d in deps] != context["turn_ids"]:
        raise ValueError("RESEARCH_WINDOW_CHANGED")
    with research.files.lock(cid):
        chat = research.files.read_chat(cid)
        saved = []
        for dep in deps:
            if dep["chat_id"] != cid or dep["turn_id"] not in chat["turn_ids"]:
                raise ValueError("TURN_NOT_COMMITTED")
            saved.append(deepcopy(research.files.read_turn(cid, dep["turn_id"])))
    observed = {}
    contexts = []
    turns = []
    for dep, (turn, original) in zip(deps, saved):
        if turn["status"] != "completed" or fingerprint(turn_binding(turn)) != dep["binding_sha256"]:
            raise ValueError("TURN_CHANGED")
        selection = original["selection"]
        if original.get("input_version") != "research-chat-input-v3" or selection.get("scope_mode") != "unified":
            raise ValueError("RESEARCH_SCOPE_CHANGED")
        _merge_entries(observed, _turn_entries(selection, turn, INPUT_V2))
        contexts.append(
            {"turn_id": turn["turn_id"], "selection": selection, "used_ids": deepcopy(turn["answer"]["used_ids"])}
        )
        turns.append({"turn_id": turn["turn_id"], "message": turn["message"]})
    if (
        observed != context["registry"]
        or contexts != context["turn_contexts"]
        or turns != context["turns"]
        or contexts[-1]["selection"] != context["selection"]
    ):
        raise ValueError("RESEARCH_EVIDENCE_CHANGED")
    with read_budget():
        pages = {}
        _tools(research, context).validate(list(observed), wiki_pages=pages)
        supports = used_supports(
            EvidenceRegistry(context["selection"], prior=observed),
            list(observed),
            wiki=wiki,
            validated_wiki_pages=pages,
        )
        if (
            supports != manifest["supports"]
            or deps + reference_dependencies(wiki, supports) != manifest["dependencies"]
        ):
            raise ValueError("RESEARCH_EVIDENCE_CHANGED")
        physical = [
            d
            for d in manifest["dependencies"]
            if d["kind"] in {"raw_table", "study_unit_snapshot", "table_analysis_artifact"}
        ]
        if wiki.resolver.validate_dependencies({"dependencies": physical}):
            raise ValueError("RESEARCH_SUPPORT_INVALID")
