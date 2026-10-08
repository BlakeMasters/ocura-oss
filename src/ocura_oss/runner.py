# SPDX-License-Identifier: Apache-2.0

"""Direct process launch, binary log capture, timing, and terminal evidence."""

from __future__ import annotations

import contextlib
import datetime
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO

from ocura_oss import model
from ocura_oss.context import capture as capture_context
from ocura_oss.store import STATE_DIR_NAME, Store, StoreError, size_and_sha256

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
    capture: bool = True,
    masked_arguments: Iterable[int] = (),
    substitute: bool = False,
    context: bool = False,
    context_files: Iterable[os.PathLike[str] | str] = (),
    now: Callable[[], datetime.datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> RunExecution:
    """Run one command directly and persist its terminal evidence.

    The command runs with shell=False, cwd set to the project root, and the
    caller's inherited environment. No environment keys or values are ever
    serialized into records.

    When ``mirror`` is true the command's stdout/stderr are echoed to the
    terminal. When ``capture`` is false no output is retained and the atom
    records that it has no logs. ``masked_arguments`` names command token
    positions whose values are replaced by a placeholder in every record; the
    command itself still receives them.

    When ``substitute`` is true, ``{KEY}`` in a command token is replaced by
    that parameter's value, taken from the declared parameters and then the
    pathway's, and every parameter used is recorded as a label of the run.
    ``context`` records the platform and git state at launch, and
    ``context_files`` records the size and digest of each named file.

    The attempt is journaled before the command launches. A Ctrl+C
    interruption is recorded as an ``interrupted`` atom and chokepoint. If
    this process stops before it can finalize, the journal entry remains and
    :meth:`Store.recover` closes the attempt as ``abandoned``.
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
    try:
        if substitute:
            tokens, used = model.substitute_parameters(tokens, {**pathway.parameters, **parameters})
            parameters = {**parameters, **used}
        recorded_command, masked = model.mask_command(tokens, masked_arguments)
    except model.ValidationError as exc:
        raise StoreError(str(exc)) from exc
    run_context = capture_context(store.root, environment=context, files=context_files)

    atom_id = model.make_id("atom")
    chokepoint_id = model.make_id("chokepoint")
    stdout_path = store.logs_dir / f"{atom_id}.stdout.log"
    stderr_path = store.logs_dir / f"{atom_id}.stderr.log"
    stdout_log = f"{STATE_DIR_NAME}/logs/{stdout_path.name}" if capture else None
    stderr_log = f"{STATE_DIR_NAME}/logs/{stderr_path.name}" if capture else None
    output_capture = model.OutputCapture.FULL if capture else model.OutputCapture.NONE

    started_at = clock()
    attempt = model.Attempt(
        id=atom_id,
        pathway_id=pathway.id,
        chokepoint_id=chokepoint_id,
        started_at=model.format_timestamp(started_at),
        declared_parameters=parameters,
        command=recorded_command,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        output_capture=output_capture,
        masked_arguments=masked,
        context=run_context,
    )
    with store._recording(attempt), contextlib.ExitStack() as logs:
        stdout_handle: BinaryIO | None = None
        stderr_handle: BinaryIO | None = None
        if capture:
            try:
                stdout_handle = logs.enter_context(open(stdout_path, "wb"))
                stderr_handle = logs.enter_context(open(stderr_path, "wb"))
            except OSError as exc:
                # Nothing was launched, so there is no attempt to leave behind.
                logs.close()
                for path in (stdout_path, stderr_path):
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                store._remove_attempt(attempt.id)
                detail = exc.strerror or exc.__class__.__name__
                raise StoreError(
                    f"cannot create log files under {STATE_DIR_NAME}/logs/: {detail}"
                ) from exc
        started_counter = counter()
        return_code, launch_category, interrupted = _execute(
            tokens, store, stdout_handle, stderr_handle, mirror
        )
        logs.close()
        duration = max(counter() - started_counter, 0.0)
        finished_at = clock()

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

        stdout_size, stdout_digest = size_and_sha256(stdout_path) if capture else (None, None)
        stderr_size, stderr_digest = size_and_sha256(stderr_path) if capture else (None, None)
        atom = model.Atom(
            id=atom_id,
            pathway_id=pathway.id,
            started_at=attempt.started_at,
            finished_at=model.format_timestamp(finished_at),
            duration_seconds=round(duration, 6),
            outcome=outcome,
            return_code=return_code,
            launch_error_category=launch_category,
            declared_parameters=parameters,
            command=recorded_command,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            stdout_bytes=stdout_size,
            stderr_bytes=stderr_size,
            stdout_sha256=stdout_digest,
            stderr_sha256=stderr_digest,
            output_capture=output_capture,
            masked_arguments=masked,
            context=run_context,
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
        # Last, and while still holding the attempt's lock: a reader then sees
        # either a live attempt or a complete atom and chokepoint.
        store._remove_attempt(attempt.id)
    return RunExecution(atom=atom, chokepoint=chokepoint)


def _execute(
    tokens: tuple[str, ...],
    store: Store,
    stdout_handle: BinaryIO | None,
    stderr_handle: BinaryIO | None,
    mirror: bool,
) -> tuple[int | None, str | None, bool]:
    """Launch the process, drain its output, and normalize how it ended.

    A missing log handle means that stream is not retained. Returns
    ``(return_code, launch_category, interrupted)``.
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
            stdout=subprocess.PIPE if mirror else (stdout_handle or subprocess.DEVNULL),
            stderr=subprocess.PIPE if mirror else (stderr_handle or subprocess.DEVNULL),
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


def _pump(source: BinaryIO, sink: BinaryIO | None, echo: BinaryIO | None) -> None:
    """Copy one output stream to its log file and, optionally, the console."""
    try:
        while chunk := source.read(_PUMP_CHUNK):
            if sink is not None:
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
