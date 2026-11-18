"""Workspace-owned original assets and access through their registered sources."""

import time
from collections.abc import Sequence

from sqlalchemy import select, union_all
from sqlalchemy.orm import Session

from ..core.errors import raise_state_conflict as conflict
from .models import Asset, Source, TableDraft, Task
from .sqlite import json_array_member


def get_asset(session: Session, workspace_id: str, asset_id: str) -> Asset | None:
    return session.get(Asset, (workspace_id, asset_id))


def register_asset(session: Session, workspace_id: str, digest: str, names: tuple[str, str], size: int) -> Asset:
    display, storage_name = names
    row = Asset(
        workspace_id=workspace_id,
        asset_id="asset-" + digest,
        sha256=digest,
        original_name=display,
        storage_name=storage_name,
        size_bytes=size,
        created_at=time.time(),
    )
    session.add(row)
    session.flush()
    return row


def require_asset_access(session: Session, workspace_id: str, asset_id: str) -> None:
    if get_asset(session, workspace_id, asset_id) is None:
        message = "Asset not found"
        raise KeyError(message)
    owners = union_all(
        select(Task.source_id).where(Task.workspace_id == workspace_id, json_array_member(Task.asset_ids, asset_id)),
        select(TableDraft.source_id).where(
            TableDraft.workspace_id == workspace_id, json_array_member(TableDraft.asset_ids, asset_id)
        ),
    ).subquery()
    states = (
        select(Source.lifecycle)
        .join(owners, Source.source_id == owners.c.source_id)
        .where(Source.workspace_id == workspace_id)
        .distinct()
    )
    lifecycles = set(session.scalars(states))
    if lifecycles and "active" not in lifecycles:
        conflict("来源已删除")


def require_assets(session: Session, workspace_id: str, asset_ids: Sequence[str]) -> dict[str, Asset]:
    rows = session.scalars(select(Asset).where(Asset.workspace_id == workspace_id, Asset.asset_id.in_(asset_ids)))
    assets = {row.asset_id: row for row in rows}
    if len(assets) != len(set(asset_ids)):
        message = "Unregistered asset"
        raise KeyError(message)
    return assets
