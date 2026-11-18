"""Selected website layers backed only by the existing KnowledgeService ports."""

from .knowledge_contracts import VERSION, CorpusRequest, PassageBrowseRequest
from .knowledge_snapshot import KnowledgeIntegrityError
from .research_chat_contracts import ResearchError, ResearchSelection


class ResearchSources:
    def __init__(self, knowledge):
        self.knowledge = knowledge

    def freeze(self, source_ids):
        if not source_ids:
            raise ResearchError("no_sources")
        try:
            inventory = self.knowledge.document_sources()
            available = {s["source_id"]: s for s in inventory["sources"] if s["source_type"] in ("paper", "patent")}
            if not set(source_ids) <= set(available):
                raise ResearchError("stale_context")
            scopes = {}
            for layer, kind, policy, status, ready in (
                ("record", "formal_records", "saved_extractions", "formal_status", "saved_receipt"),
                ("source_passage", "source_passages", "explore", "reading_status", "available"),
            ):
                selected = [
                    s
                    for s in source_ids
                    if available[s].get("record_status" if layer == "record" else status, available[s][status]) == ready
                ]
                if not selected:
                    continue
                frozen = self.knowledge.freeze_corpus(
                    CorpusRequest(
                        schema_version=VERSION,
                        request_id="research-freeze",
                        collection_id=inventory["collection_id"],
                        kind=kind,
                        source_ids=selected,
                        use_policy=policy,
                    )
                )
                scopes[layer] = {
                    k: getattr(frozen, k)
                    for k in ("workspace_id", "collection_id", "corpus_snapshot_id", "use_policy", "source_ids")
                }
                scopes[layer]["record_refs"] = [r.model_dump(mode="json") for r in frozen.record_refs]
            if not scopes:
                raise ResearchError("sources_not_ready")
            if self.knowledge.wiki_adapter is not None:
                wiki = self.knowledge.wiki_adapter.freeze_selected(source_ids)
                if wiki:
                    scopes["wiki"] = wiki
            return ResearchSelection(source_ids=source_ids, scopes=scopes).model_dump(mode="json")
        except KnowledgeIntegrityError as error:
            raise ResearchError("index_not_ready" if error.code == "INDEX_NOT_READY" else "stale_context") from None

    def passages(self, scope, args):
        return self.knowledge.passages(
            PassageBrowseRequest(
                schema_version=VERSION,
                request_id="research-passages",
                corpus_snapshot_id=scope["corpus_snapshot_id"],
                use_policy=scope["use_policy"],
                source_id=args["source_id"],
                physical_page=args.get("physical_page"),
                segment_id=args.get("segment_id"),
                page={"size": args["page_size"], "cursor": args.get("cursor")},
            )
        )
