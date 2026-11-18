"""Workspace-owned table drafts, confirmations and reusable recipes."""

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ReusableRecipe, Source, SourceConfirmation, TableDraft
from .source_repository import create_source, get_source


def get_draft(session: Session, workspace_id: str, source_id: str) -> TableDraft | None:
    return session.get(TableDraft, (workspace_id, source_id))


def list_drafts(session: Session, workspace_id: str) -> list[tuple[TableDraft, int]]:
    statement = (
        select(TableDraft, Source.generation)
        .join(Source)
        .where(TableDraft.workspace_id == workspace_id, Source.lifecycle == "active")
        .order_by(TableDraft.updated_at.desc())
    )
    return [(draft, generation) for draft, generation in session.execute(statement)]


@dataclass
class DraftData:
    title: str
    asset_ids: list[str]
    hashes: dict[str, str]
    recipe: dict[str, Any]
    options: dict[str, Any]


def refresh_draft_hash(workspace_id: str, draft: TableDraft, digest: Callable[[object], str]) -> None:
    if draft.workspace_id != workspace_id:
        message = "Table draft not found"
        raise KeyError(message)
    draft.recipe_sha256 = digest(draft.recipe)
    names = ("source_id", "revision", "title", "asset_ids", "asset_hashes", "recipe", "reader_options")
    draft.draft_sha256 = digest({name: getattr(draft, name) for name in names})


def new_draft(
    session: Session, workspace_id: str, source_id: str, data: DraftData, digest: Callable[[object], str]
) -> TableDraft:
    if get_source(session, workspace_id, source_id) is None:
        create_source(session, workspace_id, source_id)
    now = time.time()
    draft = TableDraft(
        workspace_id=workspace_id,
        source_id=source_id,
        title=data.title,
        asset_ids=data.asset_ids,
        asset_hashes=data.hashes,
        recipe=data.recipe,
        reader_options=data.options,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    refresh_draft_hash(workspace_id, draft, digest)
    session.add(draft)
    session.flush()
    return draft


@dataclass
class DraftChanges:
    title: str | None = None
    originals: tuple[list[str], dict[str, str]] | None = None
    recipe: dict[str, Any] | None = None
    options: dict[str, Any] | None = None


def update_draft(
    session: Session, workspace_id: str, draft: TableDraft, changes: DraftChanges, digest: Callable[[object], str]
) -> None:
    if draft.workspace_id != workspace_id:
        message = "Table draft not found"
        raise KeyError(message)
    if changes.title is not None:
        draft.title = changes.title
    if changes.originals is not None:
        draft.asset_ids, draft.asset_hashes = changes.originals
    if changes.recipe is not None:
        draft.recipe = changes.recipe
    if changes.options is not None:
        draft.reader_options = changes.options
    draft.revision += 1
    draft.updated_at = time.time()
    refresh_draft_hash(workspace_id, draft, digest)
    session.flush()


def get_confirmation(session: Session, workspace_id: str, version_id: str) -> SourceConfirmation | None:
    return session.scalar(
        select(SourceConfirmation).where(
            SourceConfirmation.workspace_id == workspace_id, SourceConfirmation.version_id == version_id
        )
    )


def draft_confirmation(session: Session, workspace_id: str, source_id: str, revision: int) -> SourceConfirmation | None:
    return session.scalar(
        select(SourceConfirmation).where(
            SourceConfirmation.workspace_id == workspace_id,
            SourceConfirmation.source_id == source_id,
            SourceConfirmation.draft_revision == revision,
        )
    )


@dataclass
class Confirmation:
    operator: str
    generation: int
    manifest: dict[str, Any]


def confirm(session: Session, workspace_id: str, draft: TableDraft, data: Confirmation) -> str:
    if draft.workspace_id != workspace_id:
        message = "Table draft not found"
        raise KeyError(message)
    version_id = "confirmation-" + uuid.uuid4().hex
    confirmation = SourceConfirmation(
        workspace_id=workspace_id,
        confirmation_version_id=version_id,
        version=draft.revision,
        structure=data.manifest,
        source_id=draft.source_id,
        draft_revision=draft.revision,
        expected_generation=data.generation,
        source_generation=data.generation + 1,
        operator=data.operator,
        created_at=time.time(),
        title=draft.title,
        asset_ids=draft.asset_ids,
        asset_hashes=draft.asset_hashes,
        recipe=draft.recipe,
        reader_options=draft.reader_options,
        draft_sha256=draft.draft_sha256,
        recipe_sha256=draft.recipe_sha256,
    )
    session.add(confirmation)
    session.flush()
    return version_id


def get_recipe(session: Session, workspace_id: str, version_id: str) -> ReusableRecipe | None:
    return session.scalar(
        select(ReusableRecipe).where(
            ReusableRecipe.workspace_id == workspace_id, ReusableRecipe.version_id == version_id
        )
    )


def list_recipes(session: Session, workspace_id: str) -> Sequence[ReusableRecipe]:
    return session.scalars(
        select(ReusableRecipe)
        .where(ReusableRecipe.workspace_id == workspace_id)
        .order_by(ReusableRecipe.name, ReusableRecipe.version.desc())
    ).all()


@dataclass
class RecipeData:
    name: str
    operator: str
    recipe: dict[str, Any]
    options: dict[str, Any]
    structure: dict[str, Any]
    digest: str


def create_recipe(session: Session, workspace_id: str, data: RecipeData) -> ReusableRecipe:
    previous = session.scalar(
        select(ReusableRecipe.version)
        .where(ReusableRecipe.workspace_id == workspace_id, ReusableRecipe.name == data.name)
        .order_by(ReusableRecipe.version.desc())
        .limit(1)
    )
    row = ReusableRecipe(
        workspace_id=workspace_id,
        recipe_version_id="recipe-" + uuid.uuid4().hex,
        name=data.name,
        version=(previous or 0) + 1,
        recipe=data.recipe,
        reader_options=data.options,
        structure=data.structure,
        recipe_sha256=data.digest,
        operator=data.operator,
        created_at=time.time(),
    )
    session.add(row)
    session.flush()
    return row
