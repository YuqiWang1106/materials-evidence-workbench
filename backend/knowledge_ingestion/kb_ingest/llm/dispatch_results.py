"""Private gateway result preservation and explicit local recovery."""

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import ValidationError

from ..core import clock
from ..storage.model_result_types import (
    ModelResultBinding,
    ModelResultProtocol,
    NormalizedResult,
    SavedModelResult,
    StoredModelResult,
)
from .model_requests import ModelRequestContext, ModelRequestError

logger = logging.getLogger(__name__)


def _report_cancelled_save(task: asyncio.Task[StoredModelResult]) -> None:
    if task.cancelled():
        logger.warning("Cancelled result preservation was not confirmed")
    elif (error := task.exception()) is not None:
        # Provider content and database paths must never be included in this log.
        logger.warning("Cancelled result preservation failed: %s", type(error).__name__)


async def _wait_cancelled_save(task: asyncio.Task[StoredModelResult], deadline: float) -> None:
    task.add_done_callback(_report_cancelled_save)
    while not task.done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("Cancelled result preservation exceeded its deadline")
            return
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            # Repeated cancellation must not cancel a queued local save.
            continue


@contextmanager
def dispatch_deadline(timeout_seconds: float) -> Iterator[float]:
    deadline = time.monotonic() + timeout_seconds
    parent = clock.IO_DEADLINE.get()
    if parent is not None:
        deadline = min(deadline, parent)
    token = clock.IO_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        clock.IO_DEADLINE.reset(token)


class DispatchResults:
    def __init__(self, context: ModelRequestContext) -> None:
        self.context = context

    def binding(self, call_id: str, request_sha256: str) -> ModelResultBinding:
        context = self.context
        return ModelResultBinding(
            workspace_id=context.service.workspace_id,
            project_id=context.project_id,
            call_id=call_id,
            provider=context.policy.provider,
            service_identity=context.policy.service_identity,
            model=context.policy.model,
            request_sha256=request_sha256,
        )

    async def read(self, call_id: str, request_sha256: str) -> SavedModelResult | None:
        return await asyncio.to_thread(self.context.service.read_saved_result, self.binding(call_id, request_sha256))

    async def reconcile(self, call_id: str, request_sha256: str, deadline: float) -> SavedModelResult:
        return await asyncio.to_thread(
            self.context.service.reconcile_saved_result,
            self.binding(call_id, request_sha256),
            deadline=deadline,
        )

    async def preserve(self, response: object, call_id: str, request_sha256: str, deadline: float) -> None:
        try:
            normalized = NormalizedResult.model_validate(response)
        except ValidationError:
            # Never propagate validation details containing a private response.
            raise ModelRequestError("model_result_invalid") from None
        # Once a response arrives, cancellation must not discard a queued local save.
        save = asyncio.create_task(
            asyncio.to_thread(
                self.context.service.save_received_result,
                self.binding(call_id, request_sha256),
                response=normalized,
                protocol=ModelResultProtocol(self.context.policy.protocol),
                deadline=deadline,
            )
        )
        try:
            await asyncio.shield(save)
        except asyncio.CancelledError:
            await _wait_cancelled_save(save, deadline)
            raise
        # Saving the response and confirming completion are separate commits.
        try:
            await self.reconcile(call_id, request_sha256, deadline)
        except ModelRequestError as error:
            if error.code in {"request_state_timeout", "request_state_unavailable"}:
                raise ModelRequestError("model_completion_pending") from None
            raise
