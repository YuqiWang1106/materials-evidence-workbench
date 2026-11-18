"""Workspace-specific source identities and scientific revision reservations."""

from sqlalchemy.orm import Session

from ..core.errors import raise_state_conflict as conflict
from .models import PackageRevision, Source


def get_source(session: Session, workspace_id: str, source_id: str) -> Source | None:
    return session.get(Source, (workspace_id, source_id))


def require_registered(session: Session, workspace_id: str, source_id: str) -> Source:
    source = get_source(session, workspace_id, source_id)
    if source is None:
        message = "Source not found"
        raise KeyError(message)
    return source


def create_source(session: Session, workspace_id: str, source_id: str) -> Source:
    source = Source(workspace_id=workspace_id, source_id=source_id, generation=0)
    session.add(source)
    session.flush()
    return source


def reserve_revision(session: Session, workspace_id: str, identity: tuple[str, str], digest: str, message: str) -> None:
    source_id, revision = identity
    existing = session.get(PackageRevision, (workspace_id, source_id, revision))
    if existing:
        if existing.package_sha256 != digest:
            conflict(message)
        return
    session.add(
        PackageRevision(
            workspace_id=workspace_id, source_id=source_id, record_revision_id=revision, package_sha256=digest
        )
    )
