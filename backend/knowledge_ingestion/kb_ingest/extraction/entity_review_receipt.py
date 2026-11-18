"""The publication plan of a confirmed draft: its receipt, and the task line that says what was written."""

from collections.abc import Callable, Mapping
from typing import Protocol

from kb_ingest.storage.entity_store_confirm import TaskPublication
from kb_ingest.storage.entity_store_write import WriteCounts

type Summary = Mapping[str, object]


class _Store(Protocol):
    @property
    def workspace_id(self) -> str: ...


class _Service(Protocol):
    @property
    def store(self) -> _Store: ...


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def written_message(summary: Summary, written: WriteCounts) -> str:
    """The task's line after the write. It counts the rows written, not the lines that were planned: a line
    another paper already states in the same words joins that row as one more source."""
    entities = _mapping(_mapping(summary.get("resolved")).get("entities"))
    lines = f"属性 {written.attributes_written} 段"
    if written.attributes_cited:
        lines += f"，另有 {written.attributes_cited} 段与已有记录相同、已并入其来源"
    return (
        f"已写入知识库：配方 {_count(entities.get('formulation'))} 个，"
        f"实体共 {sum(_count(value) for value in entities.values())} 个，"
        f"{lines}；核对 {_count(summary.get('reviewed'))} 项"
    )


def publication_for(service: _Service, task_id: str, operator: str) -> Callable[[Summary], TaskPublication]:
    """Build a TaskPublication in the store identity, not the graph workspace."""

    def plan(summary: Summary) -> TaskPublication:
        return TaskPublication(
            workspace_id=service.store.workspace_id,
            task_id=task_id,
            receipt={"entity_graph": summary, "operator": operator.strip()},
            progress=None,
            describe=lambda written: written_message(summary, written),
        )

    return plan
