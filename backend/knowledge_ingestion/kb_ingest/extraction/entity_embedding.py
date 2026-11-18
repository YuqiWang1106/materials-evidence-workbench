"""Entity vectors with durable request ownership and service-isolated caches."""

from __future__ import annotations

import json
import math
import struct
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from kb_ingest.core.endpoints import normalize_base_url
from kb_ingest.core.workspace import DEFAULT_WORKSPACE
from kb_ingest.llm import ModelBudget, ModelCallError, http, result_hash
from kb_ingest.observation import hooks as _observe

EMBEDDING_MODEL = "text-embedding-3-small"

BATCH = 256
DIMENSIONS = 1536
# Upper bound for the reservation: a UTF-8 byte is never less than a token.
MAX_INPUT_BYTES = 4000


def _embedding_history(path, workspace_id):
    if path.stat().st_size > 16 * 1024 * 1024:
        message = "legacy request history too large"
        raise ValueError(message)
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or state.get("workspace_id", DEFAULT_WORKSPACE) != workspace_id:
        message = "legacy workspace history invalid"
        raise ValueError(message)
    rows = []
    for section in ("pending", "settled"):
        if not isinstance(state.get(section), dict):
            raise TypeError
        for call_id, row in state[section].items():
            if not isinstance(row, dict):
                raise TypeError
            if row.get("request_kind") == "embedding":
                rows.append((call_id, row))
    return rows


def _embedding_summary(root, texts, missing, result):
    """One timeline line for all texts of a call: how many have a vector, and how many came from the cache."""
    done = sum(vector is not None for vector in result.values())
    counts = {"total": len(texts), "completed": done, "failed": len(texts) - done, "cached": len(texts) - len(missing)}
    lacking = len(texts) > done
    code = "embedding_unavailable" if lacking else None
    _observe.note(root, "embedding_summary", "partial" if lacking else "completed", error_code=code, **counts)


@dataclass(kw_only=True)
class _EmbeddingOptions:
    model: str = EMBEDDING_MODEL
    dimensions: int = DIMENSIONS
    ledger: object = None
    stage: str | None = None
    run_id: str | None = None
    transport: httpx.BaseTransport | None = None
    timeout: float = 60
    legacy_cost_path: Path | None = None
    legacy_state_required: bool = False
    legacy_run_id: str | None = None
    model_workspace: dict[str, Any] = field(default_factory=dict)
    observation_root: Path | None = None


class EntityEmbedder:
    def __init__(self, *, api_key, base_url, cache_root, **options):
        cfg = _EmbeddingOptions(**options)
        model, dimensions = cfg.model, cfg.dimensions
        self.api_key, self.base_url = api_key, normalize_base_url(base_url)
        if not api_key or not model or type(dimensions) is not int or not 1 <= dimensions <= 65536:
            raise ValueError("invalid embedding configuration")
        self.model, self.dimensions = model, dimensions
        self.service_identity = result_hash(
            {"base_url": self.base_url, "endpoint": "/embeddings", "model": model, "dimensions": dimensions}
        )
        budget_path = Path(cache_root) / self.service_identity / "requests.json"
        self.requests = ModelBudget(budget_state_path=budget_path, **cfg.model_workspace)
        owned_file = self.requests.budget_state_path
        if not isinstance(owned_file, Path):
            raise TypeError
        self.cache_root = owned_file.parent
        self.transport, self.timeout = cfg.transport, cfg.timeout
        self.sent = 0
        self.observation_root = cfg.observation_root
        self.legacy_run_id = cfg.legacy_run_id or cfg.run_id
        self._load_history(
            cfg.legacy_cost_path or getattr(cfg.ledger, "state_path", None),
            cfg.legacy_state_required,
        )

    def _load_history(self, legacy_path, legacy_state_required):
        self._legacy_rows = []
        self._legacy_unavailable = False
        if legacy_path is not None:
            try:
                self._legacy_rows = _embedding_history(Path(legacy_path), self.requests.workspace_id)
            except FileNotFoundError:
                self._legacy_unavailable = legacy_state_required
            except (OSError, ValueError, TypeError):
                self._legacy_unavailable = True
        elif legacy_state_required:
            self._legacy_unavailable = True

    def _path(self, text):
        digest = sha256(text.encode("utf-8")).hexdigest()
        path = self.cache_root / digest[:2] / (digest + ".f32")
        self.requests.scope.guard(path, self.requests.workspace_root)
        self.requests.scope.guard(path.with_suffix(".json"), self.requests.workspace_root)
        return path

    def cached(self, text):
        try:
            path = self._path(text)
            blob = path.read_bytes()
            metadata = json.loads(path.with_suffix(".json").read_text())
        except (OSError, ValueError):
            return None
        if (
            not isinstance(metadata, dict)
            or metadata.get("service_identity") != self.service_identity
            or metadata.get("input_sha256") != sha256(text.encode("utf-8")).hexdigest()
            or metadata.get("result_sha256") != sha256(blob).hexdigest()
            or not blob
            or len(blob) != self.dimensions * 4
        ):
            return None
        self.requests.scope.verify(metadata)
        values = struct.unpack("<%df" % (len(blob) // 4), blob)
        if any(not math.isfinite(value) for value in values):
            return None
        norm = math.sqrt(math.fsum(value * value for value in values))
        return [value / norm for value in values] if norm > 0 else None

    def _store(self, text, values):
        from kb_ingest.core import atomic_json

        path = self._path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        blob = struct.pack("<%df" % len(values), *values)
        temporary.write_bytes(blob)
        temporary.replace(path)
        atomic_json(
            path.with_suffix(".json"),
            {
                "workspace_id": self.requests.workspace_id,
                "service_identity": self.service_identity,
                "input_sha256": sha256(text.encode("utf-8")).hexdigest(),
                "result_sha256": sha256(blob).hexdigest(),
            },
        )

    def vectors(self, texts):
        """{text: unit vector}. Missing vectors are fetched in batches; a failed batch leaves its texts out."""
        texts = [text[:MAX_INPUT_BYTES] for text in dict.fromkeys(texts) if text and text.strip()]
        result = {text: vector for text in texts if (vector := self.cached(text)) is not None}
        missing = [text for text in texts if text not in result]
        shown = {"requested_model": self.model, "protocol": "embeddings"}
        for start in range(0, len(missing), BATCH):
            batch = missing[start : start + BATCH]
            name, sent = f"embedding-{start // BATCH + 1}", self.sent
            step = _observe.start(self.observation_root, "embedding_batch", name, batch_size=len(batch), **shown)
            try:
                values = self._request(batch)
            except ModelCallError:
                # A batch that was sent may have been answered: its outcome is unknown, not failed.
                step.ended("unknown" if self.sent > sent else "failed", error_code="embedding_unavailable")
                continue
            for text, vector in zip(batch, values):
                self._store(text, vector)
                result[text] = self.cached(text)
            step.ended()
        _embedding_summary(self.observation_root, texts, missing, result)
        return result

    def _request(self, batch):
        request = {"model": self.model, "input": batch, "encoding_format": "float", "dimensions": self.dimensions}
        request_hash = sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.requests.request_lease(request_hash):
            return self._request_owned(batch, request, request_hash)

    def _post_embedding(self, request, attempt_id):
        return http.post_sync(
            self.base_url + "/embeddings",
            request,
            api_key=self.api_key,
            seconds=self.timeout,
            transport=self.transport,
            idempotency_key=self.requests.scope.provider_key(attempt_id),
        )

    def _request_owned(self, batch, request, request_hash):
        # Legacy embedding attempts lived only in cost.json. Read their request
        # identity/state without touching amounts, validating prices, or rewriting it.
        legacy = next(
            (
                (call_id, row)
                for call_id, row in self._legacy_rows
                if row.get("requested_model") == self.model
                and (
                    row.get("request_hash") == request_hash
                    or self.legacy_run_id is not None
                    and row.get("run_id") == self.legacy_run_id
                )
            ),
            None,
        )
        if self._legacy_unavailable or legacy:
            with self.requests._transaction() as state:
                prior = state.setdefault("requests", {}).get(request_hash)
                if prior is None:
                    state["requests"][request_hash] = {
                        "call_id": legacy[0] if legacy else "legacy-" + request_hash,
                        "request_hash": request_hash,
                        "model": self.model,
                        "service_identity": "legacy:unbound",
                        "state": "unknown",
                        "legacy": True,
                        "dispatched": True,
                        "result_sha256": None,
                        "error_code": "legacy_outcome_unknown",
                    }
            if prior is None or prior.get("state") != "completed":
                raise ModelCallError("legacy_outcome_unknown")
        attempt_id, saved = self.requests.claim_request(
            request_hash, model=self.model, service_identity=self.service_identity, recover_pending=True
        )
        if saved is not None:
            return saved["vectors"]
        self.requests.update_request(request_hash, state="sent", dispatched=True)
        self.sent += 1
        try:
            response = self._post_embedding(request, attempt_id)
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise ValueError("embedding response")
            data = body["data"]
            if any(not isinstance(row, dict) or type(row.get("index")) is not int for row in data):
                raise ValueError("embedding response")
            data = sorted(data, key=lambda row: row["index"])
            if (
                body.get("model") not in (None, self.model)
                or len(data) != len(batch)
                or [row.get("index") for row in data] != list(range(len(batch)))
                or any(
                    not isinstance(row.get("embedding"), list) or len(row["embedding"]) != self.dimensions
                    for row in data
                )
                or any(
                    type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 3.4028234663852886e38
                    for row in data
                    for value in row["embedding"]
                )
                or any(not any(row["embedding"]) for row in data)
            ):
                raise ValueError("embedding response")
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            self.requests.update_request(request_hash, state="unknown", error_code="model_outcome_unknown")
            raise ModelCallError("embedding request failed") from exc
        values = [row["embedding"] for row in data]
        result = {"vectors": values}
        self.requests.update_request(request_hash, state="completed", result=result, result_sha256=result_hash(result))
        return values


def entity_text(entity_type, name, aliases, description=None):
    """The same text as the search index's entity unit, so both share one cached vector."""
    from kb_ingest.extraction.entity_index_units import TYPE_LABELS

    parts = [TYPE_LABELS.get(entity_type, entity_type), name]
    if aliases:
        parts.append("别名 " + "、".join(aliases))
    if description:
        parts.append(description)
    return " · ".join(parts)


def similar_entities(embedder, incoming, stored, *, limit=3, threshold=0.55):
    """For each incoming (key, type, text), the stored (entity_id, type, text) of the same type with the closest
    meaning: up to `limit` above `threshold`. Returns {key: [entity_id, ...]}."""
    vectors = embedder.vectors([text for _, _, text in incoming] + [text for _, _, text in stored])
    by_type = {}
    for entity_id, entity_type, text in stored:
        if text in vectors:
            by_type.setdefault(entity_type, []).append((entity_id, vectors[text]))
    result = {}
    for key, entity_type, text in incoming:
        mine = vectors.get(text)
        if mine is None:
            continue
        scored = sorted(
            (
                (math.fsum(a * b for a, b in zip(mine, vector)), entity_id)
                for entity_id, vector in by_type.get(entity_type, [])
            ),
            reverse=True,
        )
        result[key] = [entity_id for score, entity_id in scored[:limit] if score >= threshold]
    return result
