# SPDX-License-Identifier: MPL-2.0

"""Direct process launch, binary log capture, timing, and terminal evidence."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ocura_oss import model
from ocura_oss.store import STATE_DIR_NAME, Store, StoreError

_LOG_CHUNK = 1024 * 1024
_PUMP_CHUNK = 65536
_STOP_TIMEOUT = 5.0

_LAUNCH_CATEGORIES: tuple[tuple[type[BaseException], str], ...] = (
    (FileNotFoundError, "executable_not_found"),
    (PermissionError, "permission_denied"),
    (NotADirectoryError, "not_a_directory"),
    (ValueError, "invalid_argument"),
)


@dataclass(frozen=True)
class RunExecution:
    """Atom and terminal chokepoint produced by one command attempt."""

    atom: model.Atom
    chokepoint: model.Chokepoint


def categorize_launch_error(exc: BaseException) -> str:
    for exception_type, category in _LAUNCH_CATEGORIES:
        if isinstance(exc, exception_type):
            return category
    return "os_error"


def run_command(
    store: Store,
    *,
    pathway_id: str,
    argv: Sequence[str],
    declared_parameters: Mapping[str, str],
    mirror: bool = False,
    now: Callable[[], datetime.datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> RunExecution:
    """Run one command directly and persist its terminal evidence.

    The command runs with shell=False, cwd set to the project root, and the
    caller's inherited environment. No environment keys or values are ever
    serialized into records.

    When ``mirror`` is true the command's stdout/stderr are echoed to the
    terminal while they are recorded. A Ctrl+C interruption is recorded as an
    ``interrupted`` atom and chokepoint instead of leaving orphaned logs, so
    the state stays verifiable.
    """
    clock = now or model.utc_now
    counter = monotonic or time.monotonic
    if isinstance(argv, (str, bytes)):
        raise StoreError("command must be a sequence of argument tokens, not a string")
    tokens = tuple(argv)
    if not tokens or not all(isinstance(token, str) and token for token in tokens):
        raise StoreError("command must be a nonempty sequence of nonempty strings")
    parameters = model.validate_parameters(declared_parameters)
    pathway = store.load_pathway(pathway_id)

    atom_id = model.make_id("atom")
    chokepoint_id = model.make_id("chokepoint")
    stdout_name = f"{atom_id}.stdout.log"
    stderr_name = f"{atom_id}.stderr.log"
    stdout_path = store.logs_dir / stdout_name
    stderr_path = store.logs_dir / stderr_name

    started_at = clock()
    started_counter = counter()
    try:
        with (
            open(stdout_path, "wb") as stdout_handle,
            open(stderr_path, "wb") as stderr_handle,
        ):
            return_code, launch_category, interrupted = _execute(
                tokens,
                store,
                stdout_handle,
                stderr_handle,
                mirror,
            )
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        raise StoreError(f"cannot create log files under {STATE_DIR_NAME}/logs/: {detail}") from exc

    duration = max(counter() - started_counter, 0.0)
    finished_at = clock()
    stdout_size, stdout_digest = _file_digest(stdout_path)
    stderr_size, stderr_digest = _file_digest(stderr_path)

    if interrupted:
        outcome = model.Outcome.INTERRUPTED
        launch_category = "interrupted"
        return_code = None
    elif launch_category is not None:
        outcome = model.Outcome.LAUNCH_FAILED
    elif return_code == 0:
        outcome = model.Outcome.PASSED
    else:
        outcome = model.Outcome.FAILED

    atom = model.Atom(
        id=atom_id,
        pathway_id=pathway.id,
        started_at=model.format_timestamp(started_at),
        finished_at=model.format_timestamp(finished_at),
        duration_seconds=round(duration, 6),
        outcome=outcome,
        return_code=return_code,
        launch_error_category=launch_category,
        declared_parameters=parameters,
        command=tokens,
        stdout_log=f"{STATE_DIR_NAME}/logs/{stdout_name}",
        stderr_log=f"{STATE_DIR_NAME}/logs/{stderr_name}",
        stdout_bytes=stdout_size,
        stderr_bytes=stderr_size,
        stdout_sha256=stdout_digest,
        stderr_sha256=stderr_digest,
    )
    store._save_atom(atom)
    chokepoint = model.Chokepoint(
        id=chokepoint_id,
        pathway_id=pathway.id,
        atom_id=atom.id,
        created_at=model.format_timestamp(finished_at),
        kind=model.TERMINAL_KIND,
        outcome=outcome,
        branchable=True,
    )
    store._save_chokepoint(chokepoint)
    return RunExecution(atom=atom, chokepoint=chokepoint)


def _execute(
    tokens: tuple[str, ...],
    store: Store,
    stdout_handle: BinaryIO,
    stderr_handle: BinaryIO,
    mirror: bool,
) -> tuple[int | None, str | None, bool]:
    """Launch the process, drain its output, and normalize how it ended.

    Returns ``(return_code, launch_category, interrupted)``.
    """
    echo_out = getattr(sys.stdout, "buffer", None) if mirror else None
    echo_err = getattr(sys.stderr, "buffer", None) if mirror else None
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "flush"):
            stream.flush()

    try:
        process = subprocess.Popen(  # noqa: S603 - argv list, shell=False
            list(tokens),
            cwd=str(store.root),
            stdout=subprocess.PIPE if mirror else stdout_handle,
            stderr=subprocess.PIPE if mirror else stderr_handle,
            shell=False,
        )
    except (OSError, ValueError) as exc:
        return None, categorize_launch_error(exc), False

    interrupted = False
    return_code: int | None = None
    pumps: list[threading.Thread] = []
    if mirror:
        assert process.stdout is not None and process.stderr is not None
        pumps.append(
            threading.Thread(
                target=_pump, args=(process.stdout, stdout_handle, echo_out), daemon=True
            )
        )
        pumps.append(
            threading.Thread(
                target=_pump, args=(process.stderr, stderr_handle, echo_err), daemon=True
            )
        )
        for thread in pumps:
            thread.start()

    try:
        while True:
            try:
                return_code = process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
            except KeyboardInterrupt:
                interrupted = True
                _stop_process(process)
                return_code = None
                break
    finally:
        for thread in pumps:
            thread.join(timeout=_STOP_TIMEOUT)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

    return return_code, None, interrupted


def _stop_process(process: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=_STOP_TIMEOUT)
    except Exception:  # noqa: BLE001 - a stuck child must never block evidence
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=_STOP_TIMEOUT)


def _pump(source: BinaryIO, sink: BinaryIO, echo: BinaryIO | None) -> None:
    """Copy one output stream to its log file and, optionally, the console."""
    try:
        while chunk := source.read(_PUMP_CHUNK):
            try:
                sink.write(chunk)
            except (OSError, ValueError):
                break
            if echo is not None:
                with contextlib.suppress(OSError, ValueError):
                    echo.write(chunk)
                    echo.flush()
    finally:
        with contextlib.suppress(OSError, ValueError):
            source.close()


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(_LOG_CHUNK):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
