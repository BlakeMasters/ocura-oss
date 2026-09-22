# SPDX-License-Identifier: MPL-2.0
"""Example-local artifact checks and bounded child processes, not a sandbox."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encoded(value)
    with path.open("xb") as handle:
        handle.write(data)
    return digest(data)


def checked_json(path, expected):
    raw = Path(path).read_bytes()
    if digest(raw) != expected:
        raise ValueError("artifact digest mismatch")
    return json.loads(raw)


def bounded_process(command, timeout, *, input_bytes=None, cwd=None):
    """Kill only this invocation's process tree on timeout or interruption."""
    env = os.environ.copy()
    for key in ("CODEX_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    env.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=os.name != "nt",
    )

    def stop():
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

    try:
        out, err = process.communicate(input_bytes, timeout=timeout)
        return process.returncode, out, err
    except subprocess.TimeoutExpired:
        stop()
        out, err = process.communicate(timeout=10)
        return 124, out, err + b"\nExample process deadline exceeded.\n"
    except BaseException:
        stop()
        process.communicate(timeout=10)
        raise


def main():
    """Execute a checked job request so Ocura retains stdout, stderr and exit code."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    request = checked_json(args.request, args.sha256)
    code, out, err = bounded_process(request["command"], request["timeout"])
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
