"""Exact table artifacts routed to their real table or research turn owner."""

from .core import fingerprint
from .knowledge_contracts import AnalysisResult, TableAnalysisArtifactRef
from .knowledge_snapshot import KnowledgeIntegrityError


def _answer_uses_artifact(answer, ref):
    """Only the answer's used evidence closure authorizes an external read."""
    registry = answer.get("registry", {})
    pending = list(answer.get("used_ids", []))
    seen = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        entry = registry[key]
        candidates = [
            entry.get("ref"),
            (entry.get("support") or {}).get("ref"),
            (entry.get("program") or {}).get("artifact_ref"),
            (entry.get("provenance") or {}).get("research_artifact_ref"),
        ]
        if ref in candidates:
            return True
        pending.extend(entry.get("dependency_ids", []))
    return False


def _research_artifact(service, chat_id, turn_id, artifact_id, *, allow_running=False):
    from .research_chat_store import ResearchChatStore

    files = ResearchChatStore(service.store)
    with files.lock(chat_id):
        chat = files.read_chat(chat_id)
        turn, snapshot = files.read_turn(chat_id, turn_id)
        payload = files.read_artifact(chat_id, turn_id, artifact_id)
        running = allow_running and turn["status"] == "running" and chat["active_turn_id"] == turn_id
        if turn["status"] != "completed" and not running:
            raise KnowledgeIntegrityError("TURN_NOT_COMPLETED")
        selection = snapshot["selection"]
        artifact = payload["artifact"]
        used = (
            [artifact["table_version_id"]]
            if "table_version_id" in artifact
            else [t["table_version_id"] for t in artifact.get("tables", [])]
        )
        tables = {r["table_version_id"]: r for r in selection["table_refs"]}
        refs = [tables[tid] for tid in used]
        workspace = selection["wiki_scope"]["workspace_id"]
        corpus = refs[0]["corpus_snapshot_id"]
        for ref in refs:
            if ref["workspace_id"] != workspace or ref["corpus_snapshot_id"] != corpus:
                raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
            raw = service._raw_artifacts().load("raw", ref["table_version_id"])["payload"]
            if fingerprint(raw) != ref["snapshot_sha256"]:
                raise KnowledgeIntegrityError()
        expected = TableAnalysisArtifactRef(
            kind="table_analysis_artifact",
            chat_kind="research",
            workspace_id=workspace,
            corpus_snapshot_id=corpus,
            chat_id=chat_id,
            turn_id=turn_id,
            artifact_id=artifact_id,
            table_version_ids=used,
            artifact_sha256=fingerprint(payload),
        ).model_dump(mode="json")
        if not running and not _answer_uses_artifact(turn.get("answer") or {}, expected):
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        return payload, {
            "table_version_ids": used,
            "source_ids": sorted({r["source_id"] for r in refs}),
            "workspace_id": workspace,
            "corpus_snapshot_id": corpus,
            "status": turn["status"],
        }


def read_table_artifact(service, ref, *, allow_running=False):
    """Read an exact reference; running access is for its current tool context only."""
    try:
        ref = TableAnalysisArtifactRef.model_validate(ref)
        if ref.chat_kind == "research":
            payload, turn = _research_artifact(
                service, ref.chat_id, ref.turn_id, ref.artifact_id, allow_running=allow_running
            )
            if turn["workspace_id"] != ref.workspace_id:
                raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
            if (
                turn["corpus_snapshot_id"] != ref.corpus_snapshot_id
                or turn["table_version_ids"] != ref.table_version_ids
            ):
                raise KnowledgeIntegrityError()
        else:
            payload = service.get_table_chat_artifact(ref.chat_id, ref.turn_id, ref.artifact_id)
        if any(
            payload.get(k) != v
            for k, v in [("chat_id", ref.chat_id), ("turn_id", ref.turn_id), ("artifact_id", ref.artifact_id)]
        ):
            raise KnowledgeIntegrityError()
        if fingerprint(payload) != ref.artifact_sha256:
            raise KnowledgeIntegrityError()
        return payload
    except KnowledgeIntegrityError:
        raise
    except Exception:
        raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND") from None


class KnowledgeTableArtifactAdapter:
    def __init__(self, service, registry, workspace_id, collection_id, turn_scope, corpus_manifest):
        self.service = service
        self.registry = registry
        self.workspace_id = workspace_id
        self.collection_id = collection_id
        # Trusted server adapters; no client-provided turn metadata or corpus.
        self.turn_scope = turn_scope
        self.corpus_manifest = corpus_manifest

    def _read(self, chat_id, turn_id, artifact_id, corpus_snapshot_id, *, chat_kind=None):
        try:
            manifest = self.corpus_manifest(corpus_snapshot_id)
            scope = self.registry.check_manifest(manifest)
            if scope.workspace_id != self.workspace_id or scope.collection_id != self.collection_id:
                raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
            if chat_kind == "research":
                payload, turn = _research_artifact(self.service, chat_id, turn_id, artifact_id)
                if turn["workspace_id"] != self.workspace_id or turn["corpus_snapshot_id"] != corpus_snapshot_id:
                    raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
            else:
                turn = self.turn_scope(chat_id, turn_id)
                payload = self.service.get_table_chat_artifact(chat_id, turn_id, artifact_id)
            if not turn["table_version_ids"] or not set(turn["source_ids"]) <= set(manifest["sources"]):
                raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
            if any(
                payload.get(k) != v
                for k, v in [("chat_id", chat_id), ("turn_id", turn_id), ("artifact_id", artifact_id)]
            ):
                raise KnowledgeIntegrityError()
            if (
                payload["artifact"].get("table_version_id")
                and payload["artifact"]["table_version_id"] not in turn["table_version_ids"]
            ):
                raise KnowledgeIntegrityError()
            return payload, turn
        except KnowledgeIntegrityError:
            raise
        except Exception:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND") from None

    def make_ref(self, chat_id, turn_id, artifact_id, corpus_snapshot_id, *, chat_kind=None):
        payload, turn = self._read(chat_id, turn_id, artifact_id, corpus_snapshot_id, chat_kind=chat_kind)
        return TableAnalysisArtifactRef(
            kind="table_analysis_artifact",
            chat_kind=chat_kind,
            workspace_id=self.workspace_id,
            corpus_snapshot_id=corpus_snapshot_id,
            chat_id=chat_id,
            turn_id=turn_id,
            artifact_id=artifact_id,
            table_version_ids=turn["table_version_ids"],
            artifact_sha256=fingerprint(payload),
        )

    def resolve(self, ref, *, use_policy="explore"):
        if ref.workspace_id != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        if use_policy != "explore":
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        payload, turn = self._read(
            ref.chat_id, ref.turn_id, ref.artifact_id, ref.corpus_snapshot_id, chat_kind=ref.chat_kind
        )
        if fingerprint(payload) != ref.artifact_sha256 or turn["table_version_ids"] != ref.table_version_ids:
            raise KnowledgeIntegrityError()
        return AnalysisResult(
            kind="table_analysis_artifact",
            result_id=ref.artifact_id,
            ref=ref,
            config=payload["artifact"].get("config", turn.get("config", {})),
            coverage=payload["artifact"].get("coverage", {}),
            computation=payload["artifact"],
        )
