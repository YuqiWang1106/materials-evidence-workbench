"""Bounded structured model calls. All claims precede dispatch."""

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Unpack

from .attempts import send_attempts
from .counting import TokenCounting
from .errors import ModelCallError, ModelRequestBindingError
from .identity import Json, request_hash, result_hash
from .inputs import _validated_image_inputs, _validated_input_files
from .logical_request import LogicalOwnership
from .model_limits import MODEL_INPUT_LIMITS, MODEL_MAX_INPUT_TOKENS
from .provider_protocol import encode_request
from .request import Call, CallOptions, Request


class LogicalCallScope:
    """One live unit lease covers journal cache, model cache and the original dispatch."""

    def __init__(self, client: "ModelClient", request: Request, ownership: LogicalOwnership) -> None:
        self._client, self._request, self._ownership = client, request, ownership
        self._called = False

    def cached(self, reader: Callable[[], Json | None]) -> Json | None:
        budget, req = self._client.budget, self._request
        with budget._request_snapshot() as state:
            pending = budget.check_request_snapshot(
                state, req.request_hash, req.options.logical_binding, self._ownership
            )
            return None if pending else reader()

    def call(self) -> Json:
        if self._called:
            raise ModelRequestBindingError()
        self._called = True
        client, req = self._client, self._request
        with client.budget._request_snapshot() as state:
            pending = client.budget.check_request_snapshot(
                state, req.request_hash, req.options.logical_binding, self._ownership
            )
            cached = None if pending else client._cached(req, state.get("requests", {}).get(req.request_hash))
        if cached is not None:
            client._record_cache_hit(req)
            return cached
        encode_request(req.payload, client.protocol)
        model_limit = MODEL_INPUT_LIMITS.get(client.model, MODEL_MAX_INPUT_TOKENS)
        if (req.file_count or req.image_count) and not client.count_input_tokens:
            req.input_bound, req.input_source = model_limit, "model_input_limit"
        if not client.count_input_tokens and req.input_bound > model_limit:
            message = "input reservation exceeds pinned model input limit"
            raise ModelCallError(message)
        with client._semaphore, client._shared_limiter, client._cross_process_limiter:
            return client._send_owned(req, model_limit, self._ownership)


class ModelClient(TokenCounting):
    def call(self, instructions: str, text: str, schema: Json, **options: Unpack[CallOptions]) -> Json:
        with self.call_scope(instructions, text, schema, **options) as scope:
            return scope.call()

    @contextmanager
    def call_scope(
        self, instructions: str, text: str, schema: Json, **options: Unpack[CallOptions]
    ) -> Iterator[LogicalCallScope]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            message = "synchronous model calls require a worker thread"
            raise ModelCallError(message)
        self._check_cancelled()
        req = self._prepare(instructions, text, schema, Call(**options))
        with self.budget.logical_lease(
            req.request_hash, req.options.logical_binding, req.stage_input_id or req.request_hash
        ) as ownership:
            yield LogicalCallScope(self, req, ownership)

    def _prepare(self, instructions: str, text: str, schema: Json, options: Call) -> Request:
        reasoning_effort, files, images = options.reasoning_effort, options.files, options.images
        if reasoning_effort is not None and reasoning_effort not in {"low", "medium", "high"}:
            message = "invalid reasoning effort"
            raise ValueError(message)
        input_file_parts, file_bytes, has_pdf = _validated_input_files(files)
        images = _validated_image_inputs(images, file_bytes=file_bytes)
        content = [{"type": "input_text", "text": text}]
        content += [{"type": "input_image", "image_url": image, "detail": "high"} for image in images or []]
        content += input_file_parts
        request: Json = {
            "model": self.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "store": False,
            "text": {"format": {"type": "json_schema", "name": "source_extraction", "strict": True, "schema": schema}},
        }
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        chosen_effort = reasoning_effort if reasoning_effort is not None else self.reasoning_effort
        if chosen_effort:
            request["reasoning"] = {"effort": chosen_effort}

        current, legacy = self._hashes(request, options)
        stage = None
        if options.context and "stage_scope" in options.context:
            stage = result_hash([options.context["stage_scope"], options.phase, content, self.cache_namespace])
        input_bound = len((instructions + text + json.dumps(schema)).encode("utf-8")) + len(images) * 16384
        cache = self.cache_dir / f"{current}.json" if self.cache_dir else None
        return Request(
            options, request, current, legacy, stage, cache, input_bound, len(images), len(input_file_parts), has_pdf
        )

    def _hashes(self, request: Json, options: Call) -> tuple[str, str]:
        # Cached candidates can contain source-local object keys and quotations.
        # Bind them to the exact source/revision context and provider as well as
        # the model, prompt, schema and image/text payload. Never hash credentials.
        cache_identity: Json = {
            "version": 2,
            "endpoint": self.base_url,
            "phase": options.phase,
            "source_context": options.context or {},
            "request": request,
        }
        if self.cache_namespace:
            cache_identity["run_namespace"] = self.cache_namespace
        if self.protocol != "responses":
            cache_identity["protocol"] = self.protocol
        legacy_identity = {
            **cache_identity,
            "request": {**request, "service_tier": "default", "truncation": "disabled"},
        }
        return request_hash(cache_identity), request_hash(legacy_identity)

    def _cached(self, req: Request, execution: Json | None) -> Json | None:
        cache = req.cache
        if not cache or req.options.bypass_cache:
            return None
        self.budget.scope.guard(cache, self.budget.workspace_root)
        if not cache.exists():
            return None
        cached = json.loads(cache.read_bytes())
        self.budget.scope.verify(cached)
        if cached.get("request_hash") != req.request_hash or not isinstance(cached.get("result"), dict):
            return None
        if (
            execution is None
            or execution.get("state") != "completed"
            or cached.get("source_context") != (req.options.context or {})
            or result_hash(cached["result"]) != execution.get("result_sha256")
            or result_hash(execution.get("result")) != execution.get("result_sha256")
        ):
            message = "model_cache_result_binding_invalid"
            raise ModelCallError(message)
        result: Json = execution["result"]
        return result

    def _record_cache_hit(self, req: Request) -> None:
        if req.cache is not None:
            req.cache.chmod(0o600)
        self._record(
            {
                "status": "cache_hit",
                "phase": req.options.phase,
                "request_hash": req.request_hash,
                "context": req.options.context or {},
                "model": self.model,
                "requested_model": self.model,
                "returned_model": None,
                "usage": None,
            }
        )

    def _send_owned(self, req: Request, model_limit: int, ownership: LogicalOwnership) -> Json:
        service_identity = result_hash({"base_url": self.base_url, "endpoint": "/responses", "protocol": self.protocol})
        call_id, saved = self.budget.claim_request(
            req.request_hash,
            model=self.model,
            service_identity=service_identity,
            legacy_hashes=(req.legacy_hash,),
            recover_pending=True,
            stage_input_id=req.stage_input_id,
            logical_binding=req.options.logical_binding,
            ownership=ownership,
        )
        if saved is not None:
            return saved
        ownership.claim_parent()
        try:
            self._measure(req, model_limit, ownership)
        except BaseException:
            self.budget.update_request(req.request_hash, state="failed", dispatched=False)
            raise
        return send_attempts(self, req, call_id)

    def _measure(self, req: Request, model_limit: int, ownership: LogicalOwnership) -> None:
        if self.count_input_tokens:
            counted, count_unknown = self._count_input_tokens(
                req.payload,
                req.request_hash,
                req.options.phase,
                req.stage_input_id,
                logical_binding=req.options.logical_binding,
                ownership=ownership,
            )
            if count_unknown:
                # An uncertain count is still a sent request, including for plain text.
                raise ModelCallError("model_outcome_unknown", outcome_unknown=True)
            if counted is not None:
                if counted > model_limit:
                    message = "input token count exceeds pinned model input limit"
                    raise ModelCallError(message)
                req.input_bound, req.input_source = counted, "count_api"
            elif req.has_pdf:
                message = "PDF input token count unavailable"
                raise ModelCallError(message, outcome_unknown=count_unknown)
            elif req.image_count:
                message = "visual input token count unavailable"
                raise ModelCallError(message, outcome_unknown=count_unknown)
        if req.input_bound > model_limit:
            message = "input reservation exceeds pinned model input limit"
            raise ModelCallError(message)
