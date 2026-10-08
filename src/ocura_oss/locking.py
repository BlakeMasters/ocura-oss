# SPDX-License-Identifier: MPL-2.0

"""Operating-system file locks that show whether a recording process is alive.

A lock is tied to an open file, so the operating system releases it when the
holder exits for any reason, including a forced kill.

A recorder holds its lock exclusively for as long as it records. A reader asks
whether a recorder is alive by taking the same lock shared: that succeeds
unless an exclusive holder exists, and any number of readers can hold it at
once. One reader's probe therefore never looks like a recorder to another.

Locks are advisory and reliable on local filesystems only.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

_WAIT_STEP = 0.01
_WAIT_LIMIT = 10.0
_REMOVE_LIMIT = 2.0
_READER_GRACE = 0.25

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # msvcrt.locking can only lock exclusively, so the shared probe needs
    # LockFileEx. Both kinds go through it to keep one lock implementation.
    _FAIL_IMMEDIATELY = 0x1
    _EXCLUSIVE = 0x2

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _kernel32.UnlockFileEx.restype = wintypes.BOOL

    def _try_lock(descriptor: int, *, shared: bool = False) -> bool:
        flags = _FAIL_IMMEDIATELY | (0 if shared else _EXCLUSIVE)
        return bool(
            _kernel32.LockFileEx(
                msvcrt.get_osfhandle(descriptor), flags, 0, 1, 0, ctypes.byref(_Overlapped())
            )
        )

    def _unlock(descriptor: int) -> None:
        _kernel32.UnlockFileEx(
            msvcrt.get_osfhandle(descriptor), 0, 1, 0, ctypes.byref(_Overlapped())
        )

    def _is_current(descriptor: int, path: Path) -> bool:
        # Windows refuses to remove a file that is open, so the name still
        # refers to the file this descriptor locked.
        return True

    def _release(descriptor: int, path: Path) -> None:
        _unlock(descriptor)
        os.close(descriptor)
        # A reader probing this lock holds the file open for an instant, and
        # Windows will not remove it until that reader lets go.
        deadline = time.monotonic() + _REMOVE_LIMIT
        while True:
            try:
                os.unlink(path)
            except FileNotFoundError:
                return
            except OSError:
                if time.monotonic() > deadline:
                    return
                time.sleep(_WAIT_STEP)
            else:
                return

else:
    import fcntl

    def _try_lock(descriptor: int, *, shared: bool = False) -> bool:
        kind = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(descriptor, kind | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _is_current(descriptor: int, path: Path) -> bool:
        # Another process may have removed the file between our open and our
        # lock; a lock on a removed file is invisible to every later reader.
        try:
            return os.path.samestat(os.fstat(descriptor), os.stat(path))
        except OSError:
            return False

    def _release(descriptor: int, path: Path) -> None:
        # Removing the name while still holding the lock leaves no moment at
        # which the file exists unlocked.
        with contextlib.suppress(OSError):
            os.unlink(path)
        os.close(descriptor)


def is_held(path: Path) -> bool:
    """Return whether a live recorder holds the lock file at *path*.

    Other readers asking the same question at the same moment do not count.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        if not _try_lock(descriptor, shared=True):
            return True
        _unlock(descriptor)
        return False
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def hold(path: Path) -> Iterator[None]:
    """Create and hold the lock file at *path* until the block exits.

    The file is removed on exit. If the process dies first, the file remains
    but the operating system drops the lock, so :func:`is_held` reports false.
    """
    descriptor = _acquire(path, _WAIT_LIMIT)
    if descriptor is None:
        raise TimeoutError(f"could not lock {path.name}")
    try:
        yield
    finally:
        _release(descriptor, path)


@contextlib.contextmanager
def try_hold(path: Path) -> Iterator[bool]:
    """Hold the lock file at *path* if no live recorder does; yield whether held.

    A reader's probe shares the lock for an instant, while a recorder keeps it
    for as long as its command runs. Waiting briefly tells the two apart, so a
    passing reader does not make an abandoned lock look owned.
    """
    descriptor = _acquire(path, _READER_GRACE)
    if descriptor is None:
        yield False
        return
    try:
        yield True
    finally:
        _release(descriptor, path)


def _acquire(path: Path, limit: float) -> int | None:
    """Take the lock exclusively within *limit* seconds, or return ``None``."""
    deadline = time.monotonic() + limit
    while True:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            while not _try_lock(descriptor):
                if time.monotonic() > deadline:
                    os.close(descriptor)
                    return None
                time.sleep(_WAIT_STEP)
            if _is_current(descriptor, path):
                return descriptor
        except BaseException:
            os.close(descriptor)
            raise
        os.close(descriptor)
