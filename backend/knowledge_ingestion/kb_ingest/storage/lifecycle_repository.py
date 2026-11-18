"""Workspace-owned coordination rows used by lifecycle commands and recovery."""

from collections.abc import Collection

from sqlalchemy import Select, delete, select, update
from sqlalchemy.orm import Session

from .models import (
    Asset,
    Base,
    KnowledgePublication,
    PackageRevision,
    ReadingVersion,
    Source,
    SourceOperation,
    StudyUnit,
    StudyUnitRevision,
    TableDraft,
    Task,
)


def tasks(workspace_id: str) -> Select[Task]:
    return select(Task).where(Task.workspace_id == workspace_id)


def drafts(workspace_id: str) -> Select[TableDraft]:
    return select(TableDraft).where(TableDraft.workspace_id == workspace_id)


def operations(workspace_id: str) -> Select[SourceOperation]:
    return select(SourceOperation).where(SourceOperation.workspace_id == workspace_id)


def operation(session: Session, workspace_id: str, operation_id: str) -> SourceOperation:
    row = session.scalar(operations(workspace_id).where(SourceOperation.operation_id == operation_id))
    if row is None:
        message = "Operation not found"
        raise KeyError(message)
    return row


def reparses(session: Session, workspace_id: str) -> list[SourceOperation]:
    return list(session.scalars(operations(workspace_id).where(SourceOperation.kind == "reparse")))


def other_asset_ids(session: Session, workspace_id: str, excluded_sources: Collection[str]) -> set[str]:
    task_rows = session.scalars(tasks(workspace_id).where(Task.source_id.not_in(excluded_sources)))
    draft_rows = session.scalars(drafts(workspace_id).where(TableDraft.source_id.not_in(excluded_sources)))
    return {aid for row in task_rows for aid in row.asset_ids} | {aid for row in draft_rows for aid in row.asset_ids}


def purge_ingestion_rows(
    session: Session, workspace_id: str, source_ids: Collection[str], exclusive_assets: Collection[str]
) -> None:
    for task in session.scalars(tasks(workspace_id).where(Task.source_id.in_(source_ids))):
        task.asset_ids = []
        task.options = {"_source_deleted": True}
        task.receipt = None
        task.package_sha256 = None
        task.error = None
        task.progress = "来源已删除"
        task.state = "cancelled"
        task.confirmation_version_id = None
    session.flush()
    for model in (ReadingVersion, TableDraft, PackageRevision):
        session.execute(delete(model).where(model.workspace_id == workspace_id, model.source_id.in_(source_ids)))
    unreferenced = set(exclusive_assets) - other_asset_ids(session, workspace_id, ())
    session.execute(delete(Asset).where(Asset.workspace_id == workspace_id, Asset.asset_id.in_(unreferenced)))


def draft_source_ids(session: Session, workspace_id: str) -> frozenset[str]:
    return frozenset(session.scalars(select(TableDraft.source_id).where(TableDraft.workspace_id == workspace_id)))


def has_other_workspace_rows(session: Session, workspace_id: str) -> bool:
    for owned in Base.metadata.tables.values():
        if "workspace_id" in owned.c:
            owner = owned.c.workspace_id
            if session.scalar(select(owner).where(owner != workspace_id).limit(1)) is not None:
                return True
    return False


def by_request(session: Session, workspace_id: str, request_id: str) -> SourceOperation | None:
    return session.scalar(operations(workspace_id).where(SourceOperation.request_id == request_id))


def latest_task(session: Session, workspace_id: str, source_id: str) -> Task:
    return session.scalars(
        tasks(workspace_id).where(Task.source_id == source_id).order_by(Task.created_at.desc()).limit(1)
    ).one()


def is_stale_task(session: Session, workspace_id: str, task_id: str) -> bool:
    joined = select(Task.task_id).join(
        Source, (Source.workspace_id == Task.workspace_id) & (Source.source_id == Task.source_id)
    )
    return (
        session.scalar(
            joined.where(
                Task.workspace_id == workspace_id,
                Task.task_id == task_id,
                Task.generation < Source.visibility_generation,
            )
        )
        is not None
    )


def snapshot_task(session: Session, workspace_id: str, snapshot_id: str) -> str | None:
    return session.scalar(
        select(StudyUnitRevision.source_task_id)
        .join(StudyUnit)
        .where(StudyUnit.workspace_id == workspace_id, StudyUnitRevision.snapshot_id == snapshot_id)
    )


def retire_publications(session: Session, workspace_id: str, page_id: str) -> None:
    session.execute(
        update(KnowledgePublication)
        .where(
            KnowledgePublication.workspace_id == workspace_id,
            KnowledgePublication.page_id == page_id,
            KnowledgePublication.status == "published",
        )
        .values(status="historical")
    )
