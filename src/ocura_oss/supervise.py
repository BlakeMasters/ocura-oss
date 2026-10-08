# SPDX-License-Identifier: Apache-2.0

"""Keep a recorded command from outliving the process that records it.

Three mechanisms, each as far as the platform allows:

- A request to stop the recorder becomes ``KeyboardInterrupt``, so the recorder
  stops the command and records the attempt as interrupted, as for Ctrl+C.
- On Windows the command is placed in a job object. When the recorder exits
  without releasing it, the operating system ends the command and every
  process the command started.
- On Linux the kernel is asked to kill the command when the recorder exits.

On macOS nothing stops the command when its recorder is killed outright.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any

_STOP_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP", "SIGBREAK") if hasattr(signal, name)
)


@contextlib.contextmanager
def stop_requests_interrupt() -> Iterator[None]:
    """Raise ``KeyboardInterrupt`` in this thread for SIGTERM, SIGHUP, and Ctrl+Break.

    A signal the caller ignores or already handles is left alone, so a run
    started under ``nohup`` still survives its terminal. Handlers can only be
    set from the main thread; elsewhere this does nothing.
    """
    replaced = {}
    if threading.current_thread() is threading.main_thread():
        for number in _STOP_SIGNALS:
            if signal.getsignal(number) is signal.SIG_DFL:
                replaced[number] = signal.signal(number, signal.default_int_handler)
    try:
        yield
    finally:
        for number, previous in replaced.items():
            signal.signal(number, previous)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _EXTENDED_LIMIT_INFORMATION = 9
    _BREAKAWAY_OK = 0x0800
    _KILL_ON_JOB_CLOSE = 0x2000

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _set_limits(job: int, flags: int) -> bool:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = flags
        return bool(
            _kernel32.SetInformationJobObject(
                job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
            )
        )

    def _job_for(process: subprocess.Popen) -> int | None:
        """Put *process* in a new job that ends with its last handle, which is ours."""
        handle = getattr(process, "_handle", None)
        if handle is None:
            return None
        # A handle created this way is not inherited, so only this process holds the job.
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        # A process that asks to leave its job may: it did so freely before it had one.
        if _set_limits(job, _KILL_ON_JOB_CLOSE | _BREAKAWAY_OK) and (
            _kernel32.AssignProcessToJobObject(job, int(handle))
        ):
            return int(job)
        _kernel32.CloseHandle(job)
        return None

    def _close_job(job: int, *, keep_processes: bool) -> None:
        if keep_processes:
            _set_limits(job, _BREAKAWAY_OK)
        _kernel32.CloseHandle(job)


if sys.platform == "linux":
    _PR_SET_PDEATHSIG = 1

    def _die_with_recorder() -> Callable[[], None] | None:
        """Return a pre-exec hook that has the Linux kernel kill the command with this process."""
        if threading.active_count() != 1:
            # Python code between fork and exec can deadlock beside other threads.
            return None
        try:
            import ctypes

            prctl = ctypes.CDLL(None, use_errno=True).prctl
        except (ImportError, OSError, AttributeError):
            return None
        recorder = os.getpid()

        def hook() -> None:
            try:
                prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
            except Exception:  # noqa: BLE001 - a failed request must not fail the launch
                return
            if os.getppid() != recorder:
                os._exit(1)  # the recorder exited before the request took effect

        return hook


class Tether:
    """Ties one launched command to this process, where the platform can."""

    def __init__(self) -> None:
        self._job: int | None = None

    def popen_options(self) -> dict[str, Any]:
        """Return extra ``subprocess.Popen`` arguments for the launch."""
        if sys.platform == "linux":
            hook = _die_with_recorder()
            if hook is not None:
                return {"preexec_fn": hook}
        return {}

    def attach(self, process: subprocess.Popen) -> None:
        """Take hold of a command that has just been launched."""
        if sys.platform == "win32":
            self._job = _job_for(process)

    def release(self, *, command_finished: bool) -> None:
        """Let go of the command.

        When it finished on its own, anything it left running is left alone.
        Otherwise, on Windows, every process it started is ended now.
        """
        if sys.platform == "win32" and self._job is not None:
            _close_job(self._job, keep_processes=command_finished)
            self._job = None
