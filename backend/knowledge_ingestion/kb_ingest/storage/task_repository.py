"""Workspace task ledger, version selection and fenced lifecycle transitions."""

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import raise_state_conflict as conflict
from .asset_repository import require_assets
from .models import PackageRevision, Source, Task
from .source_repository import create_source, get_source, require_registered

WORKING = ("reading", "extracting", "validating")


def get_task(session: Session, workspace_id: str, task_id: str) -> Task | None:
    return session.scalar(select(Task).where(Task.workspace_id == workspace_id, Task.task_id == task_id))


def require_task(session: Session, workspace_id: str, task_id: str) -> Task:
    task = get_task(session, workspace_id, task_id)
    if task is None:
        message = "Task not found"
        raise KeyError(message)
    return task


def by_key(session: Session, workspace_id: str, key: str) -> Task | None:
    return session.scalar(select(Task).where(Task.workspace_id == workspace_id, Task.idempotency_key == key))


def by_confirmation(session: Session, workspace_id: str, version_id: str) -> Task | None:
    return session.scalar(
        select(Task).where(Task.workspace_id == workspace_id, Task.confirmation_version_id == version_id)
    )


def list_tasks(session: Session, workspace_id: str, *, published: bool) -> Sequence[Task]:
    joined = (
        select(Task)
        .join(Source, (Task.source_id == Source.source_id) & (Task.workspace_id == Source.workspace_id))
        .where(Task.workspace_id == workspace_id)
    )
    if published:
        return session.scalars(joined.where(Source.published_task_id == Task.task_id)).all()
    return session.scalars(
        joined.where(Source.lifecycle == "active", Task.generation >= Source.visibility_generation).order_by(
            Task.created_at.desc()
        )
    ).all()


@dataclass
class Submission:
    source_id: str
    source_type: str
    assets: list[str]
    options: dict[str, Any]
    key: str
    expected_generation: int | None
    confirmation: str | None


def submit(session: Session, workspace_id: str, submission: Submission) -> Task:
    existing = by_key(session, workspace_id, submission.key)
    if existing:
        return existing
    require_assets(session, workspace_id, submission.assets)
    source = get_source(session, workspace_id, submission.source_id)
    current = source.generation if source else 0
    if submission.expected_generation is not None and current != submission.expected_generation:
        conflict("Source revision changed; reload before submitting")
    source = source or create_source(session, workspace_id, submission.source_id)
    if source.active_task_id:
        old = get_task(session, workspace_id, source.active_task_id)
        if old and old.source_id != source.source_id:
            conflict("Current task belongs to a different source")
        if old and old.state in ("publishing", "write_unknown"):
            conflict("Reconcile pending publication before revising source")
        if old and old.state not in ("saved", "cancelled", "failed", "superseded"):
            old.state, old.attempt_token = "superseded", None
    now = time.time()
    task = Task(
        workspace_id=workspace_id,
        task_id=uuid.uuid4().hex,
        idempotency_key=submission.key,
        source_id=source.source_id,
        generation=current + 1,
        source_type=submission.source_type,
        asset_ids=list(submission.assets),
        options=submission.options,
        confirmation_version_id=submission.confirmation,
        state="queued",
        progress="已登记原件，等待读取",
        attempt=0,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    source.generation, source.active_task_id = task.generation, task.task_id
    session.flush()
    return task


def review_revision(session: Session, workspace_id: str, parent_id: str, key: str) -> Task:
    parent = get_task(session, workspace_id, parent_id)
    if parent is None or parent.state != "saved":
        conflict("review parent must be saved")
    source = require_registered(session, workspace_id, parent.source_id)
    if source.active_task_id != parent_id or source.generation != parent.generation:
        conflict("source already has a newer revision; open it to continue")
    now = time.time()
    task = Task(
        workspace_id=workspace_id,
        task_id=uuid.uuid4().hex,
        idempotency_key=key,
        source_id=parent.source_id,
        generation=source.generation + 1,
        source_type=parent.source_type,
        asset_ids=list(parent.asset_ids),
        options={**parent.options, "_document_review_parent": parent_id},
        state="needs_review",
        progress="已复用原件和候选，继续人工核对",
        attempt=1,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    source.generation, source.active_task_id = task.generation, task.task_id
    return task


def claim(session: Session, workspace_id: str, task_id: str, lease_seconds: int) -> Task | None:
    row = require_task(session, workspace_id, task_id)
    source = require_registered(session, workspace_id, row.source_id)
    if source.lifecycle != "active" or source.active_task_id != task_id or source.generation != row.generation:
        return None
    expired = row.state in WORKING and (row.lease_until or 0) < time.time()
    if row.state != "queued" and not expired:
        return None
    row.state, row.attempt_token = "reading", uuid.uuid4().hex
    row.attempt += 1
    row.lease_until, row.updated_at = time.time() + lease_seconds, time.time()
    session.flush()
    return row


def working_attempt(session: Session, workspace_id: str, task_id: str, token: str, message: str) -> Task:
    row = get_task(session, workspace_id, task_id)
    if not token or row is None or row.attempt_token != token or row.state not in WORKING:
        conflict(message)
    return row


@dataclass
class Transition:
    token: str | None
    state: str
    progress: str
    error: str | None


def transition(session: Session, workspace_id: str, task_id: str, change: Transition) -> None:
    row = working_attempt(session, workspace_id, task_id, change.token or "", "Attempt is no longer current")
    if change.state not in (*WORKING, "needs_review", "failed"):
        conflict("Invalid extraction transition")
    row.state, row.progress, row.error = change.state, change.progress, change.error
    row.updated_at, row.lease_until = time.time(), time.time() + 900


def interrupt_expired(session: Session, workspace_id: str, task_id: str, change: Transition) -> None:
    """End a working attempt only if its lease is still expired once the write lock is held.

    A working task without an attempt token has no owner that could renew or finish it.
    """
    row = get_task(session, workspace_id, task_id)
    if row is None or row.attempt_token != (change.token or None) or row.state not in WORKING:
        conflict("Attempt is no longer current")
    if (row.lease_until or 0) >= time.time():
        conflict("Attempt lease was renewed")
    row.state, row.progress, row.error = change.state, change.progress, change.error
    row.updated_at, row.lease_until = time.time(), time.time() + 900


def heartbeat(session: Session, workspace_id: str, task_id: str, token: str) -> None:
    row = working_attempt(session, workspace_id, task_id, token, "Attempt is no longer current")
    row.lease_until = time.time() + 900


def cancel(session: Session, workspace_id: str, task_id: str) -> None:
    row = require_task(session, workspace_id, task_id)
    if row.state in ("saved", "publishing", "write_unknown"):
        conflict("Stored or possibly stored publication requires reconciliation, not cancellation")
    row.state, row.attempt_token, row.updated_at = "cancelled", None, time.time()


def retry(session: Session, workspace_id: str, task_id: str) -> None:
    row = require_task(session, workspace_id, task_id)
    if row.options.get("_document_review_parent"):
        conflict("A review revision cannot restart extraction")
    source = require_registered(session, workspace_id, row.source_id)
    if row.state not in ("failed", "cancelled", "needs_review") or source.active_task_id != task_id:
        conflict("Only current unpublished extraction can be retried")
    row.state, row.error, row.attempt_token, row.updated_at = "queued", None, None, time.time()


def record_study_unit_sync(
    session: Session, workspace_id: str, task_id: str, token: str, error_code: str | None
) -> None:
    if error_code not in (None, "STUDY_UNIT_SYNC_FAILED"):
        message = "invalid study sync status"
        raise ValueError(message)
    row = get_task(session, workspace_id, task_id)
    source = get_source(session, workspace_id, row.source_id) if row else None
    if (
        row is None
        or source is None
        or row.attempt_token != token
        or row.state != "needs_review"
        or source.active_task_id != task_id
        or source.generation != row.generation
    ):
        conflict("Stale attempt cannot change study-unit post-processing")
    if error_code:
        row.error, row.progress = error_code, "候选已保存，研究单元整理失败；可从已保存候选恢复"
    elif row.error in (None, "STUDY_UNIT_SYNC_FAILED"):
        row.error, row.progress = None, "研究单元已保存"
    else:
        return
    row.updated_at = time.time()


def begin_publish(session: Session, workspace_id: str, task_id: str) -> Task:
    row = require_task(session, workspace_id, task_id)
    source = require_registered(session, workspace_id, row.source_id)
    if source.active_task_id != task_id or source.generation != row.generation:
        conflict("Stale revision cannot become current")
    if row.state not in ("needs_review", "publishing", "write_unknown"):
        conflict("Task is not ready for storage")
    row.state, row.updated_at = "publishing", time.time()
    session.flush()
    return row


@dataclass
class Publication:
    receipt: dict[str, Any]
    make_current: bool
    progress: str | None


def finish_publish(session: Session, workspace_id: str, task_id: str, result: Publication) -> None:
    row = require_task(session, workspace_id, task_id)
    source = require_registered(session, workspace_id, row.source_id)
    if row.state != "publishing" or source.active_task_id != task_id or row.generation != source.generation:
        conflict("Publication no longer owns source revision")
    row.receipt, row.state, row.error, row.updated_at = result.receipt, "saved", None, time.time()
    if result.make_current:
        row.progress = result.progress or "持久归档 已保存并完成回读核对"
        source.published_task_id = task_id
    else:
        row.progress = "持久归档 已保存部分结果；保留原完整当前版本"


def publication_failed(session: Session, workspace_id: str, task_id: str, error: str, unknown: bool) -> None:
    row = get_task(session, workspace_id, task_id)
    if row is None or row.state != "publishing":
        conflict("No publication in progress")
    row.state = "write_unknown" if unknown else "needs_review"
    row.error, row.updated_at = error, time.time()


def review_candidate(session: Session, workspace_id: str, task: Task, revision: str, digest: str) -> None:
    if task.workspace_id != workspace_id:
        message = "Task not found"
        raise KeyError(message)
    session.add(
        PackageRevision(
            workspace_id=workspace_id, source_id=task.source_id, record_revision_id=revision, package_sha256=digest
        )
    )
    task.package_sha256 = digest
    session.flush()


def finish_candidate(workspace_id: str, task: Task, digest: str, failed: bool, message: str) -> None:
    if task.workspace_id != workspace_id:
        conflict("Late attempt cannot replace a candidate")
    task.package_sha256 = digest
    task.state, task.progress, task.updated_at = "failed" if failed else "needs_review", message, time.time()
