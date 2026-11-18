"""Entity model adapter: one owned request scope covers both local caches."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kb_ingest import pipeline_versions
from kb_ingest.core import fingerprint
from kb_ingest.ingest.documents.document_pipeline import StageJournal, journal_workspace_id
from kb_ingest.llm import ModelCallError, ModelClient
from kb_ingest.llm.errors import ModelOutcomeUnknownError, ModelRequestBindingError
from kb_ingest.llm.logical_request import (
    BudgetOpenEvidence,
    BudgetOpenMode,
    LogicalRequestBinding,
    LogicalRequestFamily,
)
from kb_ingest.observation import model_calls as _calls
from kb_ingest.stage_cache import model_cache_identity, stage_context

from .entity_request_identity import EntityRequestIdentity

_SINGLETONS = {
    family.value: family
    for family in (
        LogicalRequestFamily.SYNONYMS,
        LogicalRequestFamily.IDENTITY,
        LogicalRequestFamily.RESOLVE,
        LogicalRequestFamily.CLAIMS,
    )
}


@dataclass(frozen=True, slots=True)
class EntityModelCallContext:
    """Source and execution context needed by the entity model adapter."""

    identity: EntityRequestIdentity | None
    source_revision_id: str
    source_type: str
    work_dir: str | Path
    workspace_id: str
    load_asset_ids: Callable[[], tuple[str, ...]]
    reparse_run: str | None
    observation_root: Path | None
    cancel_check: Callable[[], bool]


class _Client(Protocol):
    def call(
        self,
        instructions: str,
        text: str,
        schema: dict[str, object],
        *,
        phase: str,
        context: dict[str, object],
        images: list[str],
    ) -> dict[str, object]: ...


def request_identity(options: dict, client: object, package: dict) -> EntityRequestIdentity | None:
    if not isinstance(client, ModelClient):
        return None
    identity = options.get("_entity_request_identity")
    if identity is None:
        identity = EntityRequestIdentity.from_package(client.workspace_id, package)
    if not isinstance(identity, EntityRequestIdentity) or identity.workspace_id != client.workspace_id:
        raise ModelRequestBindingError()
    return identity


def client_call(
    call_context: EntityModelCallContext, client: _Client, *, stage_version: str, prompt_version: str
) -> Callable[..., dict[str, object]]:
    """Reuse the journalled result when the same stage has already completed."""
    # The source revision includes every original asset digest. Fresh reparses have their own checkpoints.
    identity = call_context.identity
    source_context: dict[str, object] = {
        "source_revision_id": call_context.source_revision_id,
        "stage_scope": call_context.source_revision_id,
        "source_type": call_context.source_type,
        "pipeline": pipeline_versions.ENTITY_PIPELINE_VERSION,
    }
    if call_context.reparse_run:
        source_context["reparse_run"] = call_context.reparse_run
    binding = {**source_context, "model_settings": model_cache_identity(client)}
    binding["workspace_id"] = journal_workspace_id({"workspace_id": call_context.workspace_id})
    journal = StageJournal(Path(call_context.work_dir) / "stages", binding)
    observed = _calls.ModelCalls(call_context.observation_root, client, list(call_context.load_asset_ids()))

    def call(name, instructions, payload, schema, *, images=None, logical_binding=None):
        # Counts requests actually sent; journalled answers cost nothing and are not counted.
        if call_context.cancel_check():
            raise ModelCallError("cancelled")
        context = {**source_context, **stage_context(stage_version, prompt_version, instructions)}
        text = json.dumps(payload, ensure_ascii=False)
        stage = name + "-" + fingerprint({"context": context, "text": text, "schema": schema, "images": images or []})
        logical = _bound(identity, name, logical_binding)
        request = _Invocation(name, instructions, text, schema, context, images or [], logical)

        def dispatching():
            with lock:
                counted.sent += 1
            _calls.dispatching()

        def save_result(value):
            if call_context.cancel_check():
                message = "cancelled after billed response"
                raise ModelCallError(message)
            return journal.put(stage, value)

        try:
            return _invoke(client, request, lambda: journal.get(stage), dispatching, save_result)
        except ModelCallError as exc:
            if exc.outcome_unknown:
                # Unknown outcomes must not become a partial draft or a dependent call.
                raise ModelOutcomeUnknownError() from exc
            raise

    lock = threading.Lock()  # Tables are mapped in parallel; the count must not lose a request.
    counted = _CountedCall(call)
    return observed.wrap(counted)


class _CountedCall:
    def __init__(self, call: Callable[..., dict[str, object]]) -> None:
        self._call = call
        self.sent = 0

    def __call__(
        self,
        name: str,
        instructions: str,
        payload: object,
        schema: dict[str, object],
        *,
        images: list[str] | None = None,
        logical_binding: LogicalRequestBinding | None = None,
    ) -> dict[str, object]:
        return self._call(name, instructions, payload, schema, images=images, logical_binding=logical_binding)


@dataclass(frozen=True, slots=True)
class _Invocation:
    phase: str
    instructions: str
    text: str
    schema: dict[str, object]
    context: dict[str, object]
    images: list[str]
    logical_binding: LogicalRequestBinding | None


def _bound(
    identity: EntityRequestIdentity | None, name: str, binding: LogicalRequestBinding | None
) -> LogicalRequestBinding | None:
    if identity is None:
        return None
    if binding is None and name in _SINGLETONS:
        binding = identity.singleton(_SINGLETONS[name])
    if not isinstance(binding, LogicalRequestBinding):
        raise ModelRequestBindingError()
    if (binding.workspace_id, binding.source_revision_id) != (identity.workspace_id, identity.source_revision_id):
        raise ModelRequestBindingError()
    return binding


def _invoke(
    client: _Client,
    request: _Invocation,
    read_cache: Callable[[], dict | None],
    dispatching: Callable[[], None],
    save_result: Callable[[dict], dict],
) -> dict:
    if isinstance(client, ModelClient):
        with client.call_scope(
            request.instructions,
            request.text,
            request.schema,
            phase=request.phase,
            context=request.context,
            images=request.images,
            logical_binding=request.logical_binding,
        ) as scope:
            previous = scope.cached(read_cache)
            if previous is not None:
                _calls.cache_hit()
                return previous
            dispatching()
            return save_result(scope.call())
    previous = read_cache()
    if previous is not None:
        _calls.cache_hit()
        return previous
    dispatching()
    return save_result(
        client.call(
            request.instructions,
            request.text,
            request.schema,
            phase=request.phase,
            context=request.context,
            images=request.images,
        )
    )


def confirm_options(service, task, task_id, workspace, model_client):
    options = {
        **deepcopy(task["options"]),
        **dict(service.document_options, _model_workspace=service.resources.model_workspace),
        "source_type": task["source_type"],
        "work_dir": str(service._document_workspace(task_id)),
        "workspace_id": workspace,
        "cost_run_id": task_id,
        "_model_budget_evidence": BudgetOpenEvidence(
            BudgetOpenMode.OPEN_EXISTING,
            service._document_workspace(task_id, "model-budget.json"),
            service.store.workspace_id,
        ),
    }
    if options.get("model_config"):
        options["model_config"] = dict(options["model_config"])
        options["model_config"].setdefault("cache_dir", service.store.root / "cache" / "model")
        options["model_config"]["budget"] = {
            **options["model_config"].get("budget", {}),
            "budget_state_path": service.store.pending_dir(task_id) / "model-budget.json",
        }
    if model_client is not None:
        options["model_client"] = model_client
    return options
