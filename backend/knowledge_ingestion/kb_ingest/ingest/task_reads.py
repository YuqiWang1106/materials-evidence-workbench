"""List visible ingestion tasks and pending source operations for one workspace."""

from collections.abc import Callable
from typing import Protocol

from kb_ingest.ingest.service import IngestionService

from ..core.errors import StateConflict
from ..source_lifecycle import SourceLifecycle


class _SourceOperations(Protocol):
    def pending_rows(self) -> list[dict[str, object]]: ...


def list_task_rows(
    service: IngestionService, public_status: Callable[[str], dict[str, object]]
) -> list[dict[str, object]]:
    rows = []
    for task in service.store.list_tasks():
        if task["options"].get("_document_retired"):
            continue
        try:
            rows.append(public_status(task["task_id"]))
        except StateConflict:
            continue  # A source may be withdrawn during this read.
    lifecycle: Callable[[IngestionService], _SourceOperations] = SourceLifecycle
    return rows + lifecycle(service).pending_rows()
