# SPDX-License-Identifier: MPL-2.0

"""Operating-system file locks that show whether a recording process is alive.

A lock is tied to an open file, so the operating system releases it when the
holder exits for any reason, including a forced kill. A reader that can take
the lock therefore knows the holder is gone. Locks are advisory and reliable
on local filesystems only.
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

if sys.platform == "win32":
    import msvcrt

    def _try_lock(descriptor: int) -> bool:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

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

    def _try_lock(descriptor: int) -> bool:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    """Return whether a live process holds the lock file at *path*."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        if not _try_lock(descriptor):
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
    descriptor = _acquire(path)
    try:
        yield
    finally:
        _release(descriptor, path)


@contextlib.contextmanager
def try_hold(path: Path) -> Iterator[bool]:
    """Hold the lock file at *path* if no live process does; yield whether held."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    if not (_try_lock(descriptor) and _is_current(descriptor, path)):
        os.close(descriptor)
        yield False
        return
    try:
        yield True
    finally:
        _release(descriptor, path)


def _acquire(path: Path) -> int:
    deadline = time.monotonic() + _WAIT_LIMIT
    while True:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            # Only a reader probing this file can be in the way, and only briefly.
            while not _try_lock(descriptor):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {path.name}")
                time.sleep(_WAIT_STEP)
            if _is_current(descriptor, path):
                return descriptor
        except BaseException:
            os.close(descriptor)
            raise
        os.close(descriptor)
