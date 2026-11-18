"""Record and publish original uploads inside caller-owned short transactions."""

from dataclasses import dataclass
from typing import cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from ..core.errors import raise_state_conflict
from .asset_repository import register_asset
from .models import Asset, AssetUpload


@dataclass(frozen=True)
class PendingUpload:
    sha256: str
    upload_id: str
    original_name: str
    storage_name: str
    size_bytes: int
    st_dev: int
    st_ino: int

    @classmethod
    def from_row(cls, row: AssetUpload) -> "PendingUpload":
        return cls(
            row.sha256,
            row.upload_id,
            row.original_name,
            row.storage_name,
            row.size_bytes,
            int(row.st_dev),
            int(row.st_ino),
        )


def get_pending(session: Session, workspace_id: str, sha256: str) -> PendingUpload | None:
    row = session.get(AssetUpload, (workspace_id, sha256))
    return PendingUpload.from_row(row) if row else None


def list_pending(session: Session, workspace_id: str) -> list[PendingUpload]:
    rows = session.scalars(select(AssetUpload).where(AssetUpload.workspace_id == workspace_id))
    return [PendingUpload.from_row(row) for row in rows]


def add_pending(session: Session, workspace_id: str, pending: PendingUpload) -> None:
    row = AssetUpload(
        workspace_id=workspace_id,
        sha256=pending.sha256,
        upload_id=pending.upload_id,
        original_name=pending.original_name,
        storage_name=pending.storage_name,
        size_bytes=pending.size_bytes,
        st_dev=str(pending.st_dev),
        st_ino=str(pending.st_ino),
    )
    session.add(row)
    session.flush()


def discard_pending(session: Session, workspace_id: str, pending: PendingUpload) -> bool:
    # A delayed cleanup may run after a retry registered the same digest with a new token.
    statement = delete(AssetUpload).where(
        AssetUpload.workspace_id == workspace_id,
        AssetUpload.sha256 == pending.sha256,
        AssetUpload.upload_id == pending.upload_id,
        AssetUpload.st_dev == str(pending.st_dev),
        AssetUpload.st_ino == str(pending.st_ino),
        AssetUpload.storage_name == pending.storage_name,
        AssetUpload.original_name == pending.original_name,
        AssetUpload.size_bytes == pending.size_bytes,
    )
    result = session.execute(statement, execution_options={"synchronize_session": "evaluate"})
    return cast("CursorResult[tuple[()]]", result).rowcount == 1


def publish_pending(session: Session, workspace_id: str, pending: PendingUpload) -> Asset:
    if not discard_pending(session, workspace_id, pending):
        raise_state_conflict("Pending upload no longer belongs to this operation")
    return register_asset(
        session, workspace_id, pending.sha256, (pending.original_name, pending.storage_name), pending.size_bytes
    )
