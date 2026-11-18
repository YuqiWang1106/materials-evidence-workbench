"""Document-v2 model-call identity and interrupted-result preservation."""

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from kb_ingest.core import fingerprint
from kb_ingest.core.interrupted import save_interrupted
from kb_ingest.llm import ModelCallError
from kb_ingest.llm.client import ModelClient
from kb_ingest.llm.errors import ModelOutcomeUnknownError, ModelRequestBindingError
from kb_ingest.llm.logical_request import BudgetOpenEvidence, LogicalRequestBinding, LogicalRequestFamily
from kb_ingest.stage_cache import stage_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentStageInput:
    name: str
    instructions: str
    payload: object
    schema: dict[str, object]
    effort: str
    images: tuple[str, ...]
    source_revision: str
    pipeline_version: str
    stage_version: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class DocumentInterruptionState:
    """Existing document state made explicit at the interruption boundary."""

    package: dict[str, object]
    unresolved: list[dict[str, object]] | None = None
    gaps: list[dict[str, object]] | None = None

    def snapshot(self, reason: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "interrupted",
            "stage": "document_v2",
            "reason": reason,
            "package": self.package,
        }
        if self.unresolved is not None:
            payload["unresolved"] = self.unresolved
        if self.gaps is not None:
            payload["gaps"] = self.gaps
        return payload


def stage_binding(value: DocumentStageInput) -> tuple[dict[str, object], str, int | None]:
    request = {
        "instructions": value.instructions,
        "payload": value.payload,
        "schema": value.schema,
        "effort": value.effort,
        "images_sha256": [sha256(image.encode()).hexdigest() for image in value.images],
    }
    context: dict[str, object] = dict(stage_context(value.stage_version, value.prompt_version, value.instructions))
    context.update(
        source_revision_id=value.source_revision, pipeline=value.pipeline_version, stage_scope=value.source_revision
    )
    stage = value.name + "-" + fingerprint({"context": context, "request": request})
    ceiling = (
        len(json.dumps(request, ensure_ascii=False).encode()) + 65536 * len(value.images) if value.images else None
    )
    return context, stage, ceiling


class DocumentJournal(Protocol):
    def get(self, name: str) -> dict[str, object] | None: ...

    def put(self, name: str, result: dict[str, object]) -> dict[str, object]: ...


def require_open_evidence(value: object) -> BudgetOpenEvidence:
    if not isinstance(value, BudgetOpenEvidence):
        raise ModelRequestBindingError()
    return value


def check_cancelled_before_call(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check and cancel_check():
        message = "cancelled"
        raise ModelCallError(message)


def run_stage(
    client: ModelClient,
    journal: DocumentJournal,
    value: DocumentStageInput,
    cancel_check: Callable[[], bool] | None,
    bypass_cache: bool,
) -> dict[str, object]:
    context, stage, ceiling = stage_binding(value)
    text = json.dumps(value.payload, ensure_ascii=False)
    call_options = {
        "phase": value.name,
        "context": context,
        "images": list(value.images),
        "reasoning_effort": value.effort,
        "bypass_cache": bypass_cache,
        "input_token_ceiling": ceiling,
    }
    if isinstance(client, ModelClient):
        binding = LogicalRequestBinding(
            client.workspace_id,
            value.source_revision,
            LogicalRequestFamily.DOCUMENT_V2,
            fingerprint([value.source_revision, value.pipeline_version]),
            "document",
        )
        with client.call_scope(
            value.instructions,
            text,
            value.schema,
            phase=value.name,
            context=context,
            images=list(value.images),
            reasoning_effort=value.effort,
            bypass_cache=bypass_cache,
            input_token_ceiling=ceiling,
            logical_binding=binding,
        ) as scope:
            previous = scope.cached(lambda: journal.get(stage))
            if previous is not None:
                return previous
            result = scope.call()
            if cancel_check and cancel_check():
                message = "cancelled after billed response"
                raise ModelCallError(message)
            return journal.put(stage, result)
    previous = journal.get(stage)
    if previous is not None:
        return previous
    result = client.call(value.instructions, text, value.schema, **call_options)
    if cancel_check and cancel_check():
        message = "cancelled after billed response"
        raise ModelCallError(message)
    return journal.put(stage, result)


@contextmanager
def preserve_unknown(work: Path, saved_state: Callable[[], DocumentInterruptionState]) -> Iterator[None]:
    try:
        yield
    except (ModelCallError, ModelOutcomeUnknownError, ModelRequestBindingError) as error:
        if isinstance(error, ModelCallError) and not error.outcome_unknown:
            raise
        reason = "model_outcome_unknown" if isinstance(error, ModelCallError) else error.code
        try:
            save_interrupted(
                work,
                saved_state().snapshot(reason),
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
        ) as storage_error:
            logger.warning("Interrupted document preservation failed: %s", type(storage_error).__name__)
        if isinstance(error, ModelCallError):
            raise ModelOutcomeUnknownError() from error
        raise
