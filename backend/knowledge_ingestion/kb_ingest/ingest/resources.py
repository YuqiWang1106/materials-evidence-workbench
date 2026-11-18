"""Application access to registered ingestion resources in a trusted workspace."""

from copy import deepcopy
from pathlib import Path
from typing import BinaryIO, TypedDict

from ..core.workspace import Workspace
from ..storage.upload_files import validate_upload_size
from ..store import Store


class ModelWorkspace(TypedDict):
    workspace_id: str
    workspace_root: Path


def model_workspace(store: Store) -> ModelWorkspace:
    return {"workspace_id": store.workspace_id, "workspace_root": store.root}


class IngestionResources:
    def __init__(self, location: Path | Workspace) -> None:
        if isinstance(location, Workspace):
            self.store = Store(location.data_root, workspace_id=location.workspace_id)
        else:
            self.store = Store(location)

    @property
    def model_workspace(self) -> ModelWorkspace:
        return model_workspace(self.store)

    def model_options(self, options: dict) -> dict:
        return deepcopy(options) | {"_model_workspace": self.model_workspace}

    @staticmethod
    def check_upload_size(size: int) -> None:
        validate_upload_size(size)

    def upload(self, stream: BinaryIO, name: str) -> dict:
        return self.store.register_stream(stream, name)

    def task(self, task_id: str) -> dict:
        return self.store.get_task(task_id)

    def require_source(self, source_id: str) -> None:
        if self.store.source_state(source_id) is None:
            raise KeyError(source_id)
        self.store.require_source(source_id)

    def require_asset(self, asset_id: str) -> None:
        self.store.require_asset_access(asset_id)

    def download(self, asset_id: str) -> tuple[dict, bytes]:
        self.require_asset(asset_id)
        return self.store.get_asset(asset_id), self.store.asset_bytes(asset_id)

    def cancel(self, task_id: str) -> dict:
        self.store.cancel(task_id)
        return self.task(task_id)
