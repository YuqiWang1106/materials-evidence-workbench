"""Atomically publish a file only into a vacant name in the destination directory."""

import ctypes
import errno
import os
import sys


def rename_noreplace(src_dir_fd: int, source: str, dst_dir_fd: int, target: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        syscall = getattr(library, "renameatx_np", None)
        flag = 4  # <sys/stdio.h> RENAME_EXCL
    elif sys.platform == "linux":
        syscall = getattr(library, "renameat2", None)
        flag = 1  # <linux/fs.h> RENAME_NOREPLACE
    else:
        syscall = None
        flag = 0
    if syscall is None:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
    syscall.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    syscall.restype = ctypes.c_int
    if syscall(src_dir_fd, os.fsencode(source), dst_dir_fd, os.fsencode(target), flag):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
