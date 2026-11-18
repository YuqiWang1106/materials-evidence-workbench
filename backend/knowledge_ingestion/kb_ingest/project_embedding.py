"""Bounded embeddings with website-owned execution records and isolated vectors."""

import asyncio
import math
import struct
import time
from array import array
from hashlib import sha256

from .core import fingerprint
from .core.endpoints import normalize_base_url
from .llm import http, result_hash
from .llm.model_requests import ModelRequestError, ModelRequestService, identity
from .llm.project_model_dispatch import _strict_provider_json
from .llm.provider_protocol import provider_error_code

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
INPUT_VERSION = "wiki-embedding-input-v1"
BASE_URL = "https://api.openai.com/v1"
MAX_INPUT_BYTES = 4096


class EmbeddingUnavailable(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def check_time(deadline, cancel=None):
    if cancel is not None and cancel.is_set():
        raise EmbeddingUnavailable("cancelled")
    if time.monotonic() >= deadline:
        raise EmbeddingUnavailable("embedding_deadline")


def input_identity(project_id, value, *, model=MODEL, dimensions=DIMENSIONS, base_url=BASE_URL):
    if type(value) is not str or not value.strip() or len(value.encode()) > MAX_INPUT_BYTES:
        raise EmbeddingUnavailable("embedding_input_limit")
    base_url = normalize_base_url(base_url)
    digest = sha256(value.encode()).hexdigest()
    # Preserve the established official cache identity on upgrade. Other service,
    # model or dimension choices use a separate namespace, never mixed vectors.
    version = (
        INPUT_VERSION
        if (base_url, model, dimensions) == (BASE_URL, MODEL, DIMENSIONS)
        else "wiki-embedding-input-v2-" + result_hash([base_url, model, dimensions])
    )
    key = fingerprint([project_id, model, dimensions, version, digest])
    request = {"model": model, "dimensions": dimensions, "encoding_format": "float", "input": [value]}
    return {
        "cache_key": key,
        "project_id": project_id,
        "model": model,
        "dimensions": dimensions,
        "input_version": version,
        "input_sha256": digest,
        "call_id": "emb-" + key,
        "request_sha256": result_hash(request),
    }, request


def decode_vector(blob: object, digest: object, dimensions: int = DIMENSIONS) -> array[float]:
    if type(blob) is not bytes or len(blob) != dimensions * 4 or sha256(blob).hexdigest() != digest:
        raise EmbeddingUnavailable("embedding_cache_invalid")
    values = struct.unpack("<" + str(dimensions) + "f", blob)
    norm = math.sqrt(math.fsum(v * v for v in values))
    if not math.isfinite(norm) or norm <= 0:
        raise EmbeddingUnavailable("embedding_cache_invalid")
    return array("f", (v / norm for v in values))


class ProjectEmbeddingDispatcher:
    def __init__(
        self,
        requests,
        project_id,
        *,
        api_key,
        base_url=BASE_URL,
        model=MODEL,
        dimensions=DIMENSIONS,
        provider="compatible",
        transport=None,
    ):
        if not isinstance(requests, ModelRequestService):
            raise ValueError("trusted_request_service_required")
        identity(project_id)
        identity(model)
        identity(provider)
        if type(api_key) is not str or not api_key or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
            raise ValueError("provider_credential_required")
        if type(dimensions) is not int or not 1 <= dimensions <= 65536:
            raise ValueError("invalid_embedding_dimensions")
        self.requests, self.project_id = requests, project_id
        self.base_url = normalize_base_url(base_url)
        self.model, self.dimensions, self.provider = model, dimensions, provider
        self.service_identity = result_hash([provider, self.base_url, "embeddings", "/embeddings"])
        self._api_key, self._transport = api_key, transport
        self.input_version = self.input_identity("identity")[0]["input_version"]

    @property
    def workspace_id(self) -> str:
        return self.requests.workspace_id

    def input_identity(self, value):
        return input_identity(
            self.project_id, value, model=self.model, dimensions=self.dimensions, base_url=self.base_url
        )

    def _result(self, body, headers):
        result = _strict_provider_json(body)
        if (
            type(result) is not dict
            or result.get("model") not in (None, self.model)
            or result.get("object") not in (None, "list")
        ):
            raise EmbeddingUnavailable("embedding_protocol")
        data = result.get("data")
        if (
            type(data) is not list
            or len(data) != 1
            or type(data[0]) is not dict
            or type(data[0].get("index")) is not int
            or data[0]["index"] != 0
        ):
            raise EmbeddingUnavailable("embedding_protocol")
        values = data[0].get("embedding")
        if (
            type(values) is not list
            or len(values) != self.dimensions
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
        ):
            raise EmbeddingUnavailable("embedding_vector")
        try:
            blob = struct.pack("<" + str(self.dimensions) + "f", *values)
        except (OverflowError, struct.error):
            raise EmbeddingUnavailable("embedding_vector") from None
        digest = sha256(blob).hexdigest()
        decode_vector(blob, digest, self.dimensions)
        # Usage/request-id are optional metadata, not prerequisites for saving.
        receipt = {"result_sha256": digest}
        if headers.get("x-request-id"):
            receipt["receipt_id"] = headers["x-request-id"]
        return blob, digest, receipt

    async def _http(self, request, deadline, cancel):
        check_time(deadline, cancel)
        remaining = deadline - time.monotonic()

        async def fetch():
            try:
                status, headers, body = await http.post_bounded(
                    self.base_url + "/embeddings",
                    request,
                    api_key=self._api_key,
                    seconds=remaining,
                    max_bytes=max(262144, self.dimensions * 32),
                    transport=self._transport,
                    before_chunk=lambda: check_time(deadline, cancel),
                )
            except http.ResponseTooLargeError:
                raise EmbeddingUnavailable("embedding_response_limit") from None
            if status != 200:
                raise EmbeddingUnavailable(provider_error_code(status))
            return self._result(body, headers)

        task = asyncio.create_task(fetch())
        try:
            async with asyncio.timeout(remaining):
                while not task.done():
                    await asyncio.wait({task}, timeout=0.02)
                    check_time(deadline, cancel)
                return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def complete(self, binding, receipt):
        if type(receipt) is not dict or type(receipt.get("result_sha256")) is not str:
            raise EmbeddingUnavailable("embedding_receipt")
        prior = self.requests.read_call(self.project_id, binding["call_id"])
        if prior is None or any(
            prior[key] != value
            for key, value in {"model": self.model, "request_sha256": binding["request_sha256"]}.items()
        ):
            raise EmbeddingUnavailable("embedding_receipt_conflict")
        if prior["service_identity"] != self.service_identity and not (
            prior["service_identity"] == "legacy:openai"
            and self.base_url == BASE_URL
            and self.model == MODEL
            and self.dimensions == DIMENSIONS
        ):
            raise EmbeddingUnavailable("embedding_receipt_conflict")
        if (
            prior["state"] == "completed"
            and prior["result_sha256"] is None
            and prior["service_identity"].startswith("legacy:")
        ):
            # Existing received bytes are checked by cache._decode and their
            # original request identity before repairing this legacy cache row.
            return prior
        return self.requests.complete_call(
            self.project_id,
            binding["call_id"],
            result_sha256=receipt["result_sha256"],
            receipt_id=receipt.get("receipt_id"),
        )

    def dispatch(self, value, *, deadline, cancel=None, received):
        binding, request = self.input_identity(value)
        check_time(deadline, cancel)
        try:
            prior = self.requests.prepare_call(
                project_id=self.project_id,
                call_id=binding["call_id"],
                provider=self.provider,
                model=self.model,
                service_identity=self.service_identity,
                request_sha256=binding["request_sha256"],
            )
            if prior["state"] != "pending":
                raise EmbeddingUnavailable("embedding_outcome_unknown")
            check_time(deadline, cancel)
            self.requests.mark_sent(self.project_id, binding["call_id"])
        except ModelRequestError as error:
            raise EmbeddingUnavailable(
                "embedding_deadline" if error.code == "request_state_timeout" else "embedding_outcome_unknown"
            ) from None
        saved = False
        try:
            result = asyncio.run(self._http(request, deadline, cancel))
            received(*result)
            saved = True
            self.complete(binding, result[2])
            return decode_vector(result[0], result[1], self.dimensions)
        except BaseException as error:
            if not saved:
                try:
                    self.requests.mark_call_unknown(self.project_id, binding["call_id"], "transport_failure")
                except ModelRequestError:
                    pass
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, EmbeddingUnavailable):
                raise
            raise EmbeddingUnavailable(
                "embedding_completion_pending" if saved else "embedding_provider_failure"
            ) from None
