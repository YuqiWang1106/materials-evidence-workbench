"""Resolve the packaged Alembic scripts independently of the current directory."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from ..core.workspace import Workspace, primary_workspace_id, workspace_files


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", "kb_ingest:migrations")
    return config


def initialize_workspace(data_root: Path, workspace_id: str | None, connection: Connection) -> tuple[str, Path]:
    """Bind the file owner and initialize the schema while both initialization locks are held."""
    owner = Workspace.local(data_root).workspace_id if workspace_id is None else workspace_id
    root = workspace_files(data_root, owner)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for folder in ("originals", "pending", "locks", "cache"):
        (root / folder).mkdir(exist_ok=True, mode=0o700)
    primary_workspace_id(data_root)
    config = migration_config()
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    return owner, root
