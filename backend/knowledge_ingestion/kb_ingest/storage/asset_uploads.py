"""Original uploads become visible only after durable promotion to their workspace."""

import errno
import fcntl
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ..core.errors import StateConflict
from . import upload_repository as repo
from .asset_repository import get_asset
from .models import Asset
from .task_files import directory
from .upload_files import UploadFiles, digest_locks
from .upload_repository import PendingUpload


def _receipt(asset: Asset) -> dict[str, Any]:
    return {col.name: getattr(asset, col.name) for col in asset.__table__.columns if col.name != "workspace_id"}


@dataclass
class Uploads:
    data_root: Path
    workspace_root: Path
    workspace_id: str
    engine: Engine
    write: Callable[[], AbstractContextManager[Session]]

    def _ready(self, digest: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = get_asset(session, self.workspace_id, "asset-" + digest)
            return _receipt(row) if row else None

    def _pending(self, digest: str) -> PendingUpload | None:
        with Session(self.engine) as session:
            return repo.get_pending(session, self.workspace_id, digest)

    def _discard(self, files: UploadFiles, pending: PendingUpload) -> None:
        # The staging namespace is private. Never unlink or move a published pathname.
        files.remove_staged(pending)
        with self.write() as session:
            repo.discard_pending(session, self.workspace_id, pending)

    def _finish(self, pending: PendingUpload) -> dict[str, Any]:
        with self.write() as session:
            return _receipt(repo.publish_pending(session, self.workspace_id, pending))

    def _resume(self, files: UploadFiles, pending: PendingUpload) -> None:
        if self._ready(pending.sha256) is not None:
            self._discard(files, pending)
            return
        try:
            files.promote(pending)
        except StateConflict:
            # No live handle can prove a conflicting final inode was not recycled.
            # Drop the registration and its private staging name, never the occupant.
            self._discard(files, pending)
        except OSError as exc:
            if exc.errno not in (errno.ENOSYS, errno.ENOTSUP, errno.EXDEV) or files.promoted(pending):
                raise
            # A volume without exclusive rename cannot ever promote this staged file.
            self._discard(files, pending)
        else:
            self._publish(files, pending)

    def recover(self) -> None:
        """Skip active staging; a duplicate upload also reconciles its own interrupted digest."""
        try:
            with UploadFiles.open(self.data_root, self.workspace_root) as files:
                self._recover_workspace(files)
        except OSError as exc:
            if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
                raise
            # Defer this workspace's pending files until its volume permits writes.
            # Existing registered originals still have a fully usable read path.

    def _recover_workspace(self, files: UploadFiles) -> None:
        try:
            fcntl.flock(files.staging, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Persist the new staging/originals entries and any new workspace directory chain.
        path = self.workspace_root
        while True:
            with directory(self.data_root, path) as fd:
                os.fsync(fd)
            if path == self.data_root:
                break
            path = path.parent
        with Session(self.engine) as session:
            pending = repo.list_pending(session, self.workspace_id)
        for registration in pending:
            with digest_locks(self.data_root, self.workspace_root, [registration.sha256]):
                self._resume(files, registration)
        files.sweep()

    def _publish(self, files: UploadFiles, pending: PendingUpload) -> dict[str, Any]:
        try:
            return self._finish(pending)
        except Exception:
            # commit() can report failure after SQLite made this exact receipt durable.
            ready = self._ready(pending.sha256)
            if (
                ready is None
                or ready["original_name"] != pending.original_name
                or ready["storage_name"] != pending.storage_name
                or ready["size_bytes"] != pending.size_bytes
                or self._pending(pending.sha256) is not None
                or not files.promoted(pending)
            ):
                raise
            files.verify_ready(ready)
            return ready

    def _register(self, files: UploadFiles, pending: PendingUpload) -> dict[str, Any]:
        previous = self._pending(pending.sha256)
        if previous:
            self._resume(files, previous)
        ready = self._ready(pending.sha256)
        if ready is not None:
            files.verify_ready(ready)
            return ready
        try:
            with self.write() as session:
                repo.add_pending(session, self.workspace_id, pending)
            files.promote(pending)
            return self._publish(files, pending)
        except BaseException:
            # A completed rename is one-way: the files and registration can be
            # verified again on reopen, even if the final fsync/commit failed.
            if self._pending(pending.sha256) == pending and not files.promoted(pending):
                self._discard(files, pending)
            raise

    def register_stream(self, stream: BinaryIO, original_name: str) -> dict[str, Any]:
        with UploadFiles.open(self.data_root, self.workspace_root) as files:
            fcntl.flock(files.staging, fcntl.LOCK_SH)
            pending = files.stage(stream, original_name)
            try:
                with digest_locks(self.data_root, self.workspace_root, [pending.sha256]):
                    return self._register(files, pending)
            finally:
                # Preserve staged bytes when a registered operation still awaits recovery.
                if self._pending(pending.sha256) != pending:
                    files.remove_staged(pending)
