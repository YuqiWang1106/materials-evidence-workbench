"""Workspace-scoped staging, durable promotion and verification of immutable originals."""

import errno
import fcntl
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO

from ..core.errors import EmptyUploadError, StateConflict, UploadTooLargeError, raise_state_conflict
from .exclusive_rename import rename_noreplace
from .task_files import directory
from .upload_repository import PendingUpload

MAX_UPLOAD_BYTES = 128 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
_INTEGRITY = "Original asset integrity check failed"
_MISSING = "Registered original asset is unavailable"
_INVALID = "Invalid pending upload identity"


def validate_upload_size(size: int) -> None:
    if size > MAX_UPLOAD_BYTES:
        raise UploadTooLargeError
    if size == 0:
        raise EmptyUploadError


def _suffix(display: str) -> str:
    extension = Path(display).suffix.lower()
    return extension if re.fullmatch(r"\.[a-z0-9]{1,10}", extension) else ".bin"


def _check_pending(pending: PendingUpload) -> None:
    if (
        not re.fullmatch(r"[0-9a-f]{64}", pending.sha256)
        or not re.fullmatch(r"[0-9a-f]{32}", pending.upload_id)
        or pending.storage_name != pending.sha256 + _suffix(pending.original_name)
    ):
        raise_state_conflict(_INVALID)


def _same_device(left: int, right: int) -> bool:
    return os.fstat(left).st_dev == os.fstat(right).st_dev


def _suspect(err: OSError) -> bool:
    return err.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR)


def _stat(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _owned(info: os.stat_result | None, device: int, inode: int) -> bool:
    return bool(info and stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode))


def _publishable(info: os.stat_result | None, pending: PendingUpload) -> bool:
    return bool(
        info
        and _owned(info, pending.st_dev, pending.st_ino)
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o400
        and info.st_size == pending.size_bytes
    )


def _unlink_owned(parent: int, name: str, device: int, inode: int) -> None:
    if _owned(_stat(parent, name), device, inode):
        os.unlink(name, dir_fd=parent)
    os.fsync(parent)


def _lock_file(parent: int, stripe: str) -> int:
    # Simultaneous first-time O_CREAT at the same name can race on APFS.
    # Serialize name creation only; the actual upload lock is per stripe.
    fcntl.flock(parent, fcntl.LOCK_EX)
    try:
        return os.open(
            "upload-" + stripe + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
        )
    finally:
        fcntl.flock(parent, fcntl.LOCK_UN)


@contextmanager
def digest_locks(data_root: Path, workspace_root: Path, digests: Iterable[str]) -> Iterator[None]:
    stripes = set()
    for digest in digests:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise_state_conflict(_INVALID)
        stripes.add(digest[:2])
    with directory(data_root, workspace_root / "locks") as parent, ExitStack() as held:
        for stripe in sorted(stripes):
            fd = _lock_file(parent, stripe)
            held.callback(os.close, fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise_state_conflict(_INVALID)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield


def _copy(stream: BinaryIO, output: BinaryIO) -> tuple[str, int]:
    digest = sha256()
    size = 0
    while chunk := stream.read(CHUNK_SIZE):
        size += len(chunk)
        validate_upload_size(size)
        output.write(chunk)
        digest.update(chunk)
    validate_upload_size(size)
    return digest.hexdigest(), size


@dataclass
class UploadFiles:
    staging: int
    originals: int
    handles: ExitStack

    @classmethod
    @contextmanager
    def open(cls, data_root: Path, root: Path) -> Iterator["UploadFiles"]:
        with ExitStack() as handles:
            originals = handles.enter_context(directory(data_root, root / "originals"))
            if os.fstatvfs(originals).f_flag & os.ST_RDONLY:
                raise OSError(errno.EROFS, os.strerror(errno.EROFS))
            workspace = handles.enter_context(directory(data_root, root))
            staging = None
            if _same_device(workspace, originals):
                sibling = handles.enter_context(directory(data_root, root / "upload-staging"))
                if _same_device(sibling, originals):
                    staging = sibling
            if staging is None:
                staging = handles.enter_context(directory(data_root, root / "originals" / ".upload-staging"))
                if not _same_device(staging, originals):
                    raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
                os.fsync(originals)
            yield cls(staging, originals, handles)

    def stage(self, stream: BinaryIO, name: str) -> PendingUpload:
        display = Path(name.replace("\\", "/")).name
        if not display or "\x00" in display:
            message = "A valid original filename is required"
            raise ValueError(message)
        token = secrets.token_hex(16)
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.staging)
        info = os.fstat(fd)
        try:
            with os.fdopen(fd, "wb") as output:
                # Keep the inode allocated until all live failure compensation is finished.
                self.handles.callback(os.close, os.dup(fd))
                digest, size = _copy(stream, output)
                output.flush()
                os.fchmod(fd, 0o400)
                os.fsync(fd)
            os.fsync(self.staging)
        except BaseException:
            _unlink_owned(self.staging, token, info.st_dev, info.st_ino)
            raise
        return PendingUpload(digest, token, display, digest + _suffix(display), size, info.st_dev, info.st_ino)

    def _verify(self, name: str, digest: str, size: int, owner: PendingUpload | None) -> None:
        if not re.fullmatch(re.escape(digest) + r"\.[a-z0-9]{1,10}", name) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise_state_conflict(_INTEGRITY)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.originals)
        except FileNotFoundError as exc:
            raise StateConflict(_MISSING) from exc
        except OSError as exc:
            if _suspect(exc):
                raise StateConflict(_INTEGRITY) from exc
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise_state_conflict(_INTEGRITY)
            if owner and not _publishable(info, owner):
                raise_state_conflict(_INTEGRITY)
            hasher = sha256()
            with os.fdopen(fd, "rb", closefd=False) as original:
                while chunk := original.read(CHUNK_SIZE):
                    hasher.update(chunk)
            if hasher.hexdigest() != digest:
                raise_state_conflict(_INTEGRITY)
        finally:
            os.close(fd)

    def verify_ready(self, asset: dict[str, Any]) -> None:
        self._verify(asset["storage_name"], asset["sha256"], asset["size_bytes"], None)

    def promote(self, pending: PendingUpload) -> None:
        _check_pending(pending)
        final = _stat(self.originals, pending.storage_name)
        staged = _stat(self.staging, pending.upload_id)
        if final is not None and (staged is not None or not _publishable(final, pending)):
            raise_state_conflict(_INTEGRITY)
        if final is None:
            if not _publishable(staged, pending):
                raise_state_conflict(_INTEGRITY)
            try:
                rename_noreplace(self.staging, pending.upload_id, self.originals, pending.storage_name)
            except FileExistsError as exc:
                raise StateConflict(_INTEGRITY) from exc
        self._verify(pending.storage_name, pending.sha256, pending.size_bytes, pending)
        os.fsync(self.originals)
        os.fsync(self.staging)

    def owns_final(self, pending: PendingUpload) -> bool:
        _check_pending(pending)
        return _owned(_stat(self.originals, pending.storage_name), pending.st_dev, pending.st_ino)

    def remove_staged(self, pending: PendingUpload) -> None:
        _check_pending(pending)
        _unlink_owned(self.staging, pending.upload_id, pending.st_dev, pending.st_ino)

    def promoted(self, pending: PendingUpload) -> bool:
        return self.owns_final(pending) and _stat(self.staging, pending.upload_id) is None

    def sweep(self) -> None:
        # Exclusive flock on this staging directory excludes every live uploader.
        with os.scandir(self.staging) as entries:
            for entry in entries:
                if re.fullmatch("[0-9a-f]{32}", entry.name) and (
                    entry.is_file(follow_symlinks=False) or entry.is_symlink()
                ):
                    os.unlink(entry.name, dir_fd=self.staging)
        os.fsync(self.staging)
