# SPDX-License-Identifier: MPL-2.0

"""Opt-in capture of where a command attempt started.

Nothing here runs unless a caller asks for it: no platform query, no git
subprocess, and no file read happens for an ordinary run.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from ocura_oss import model
from ocura_oss.store import STATE_DIR_NAME, StoreError, size_and_sha256

_GIT_TIMEOUT = 10.0
_HEX_DIGITS = frozenset("0123456789abcdef")


def capture(
    root: Path, *, environment: bool, files: Iterable[os.PathLike[str] | str]
) -> model.RunContext | None:
    """Record the platform and git state, and digests of the named files.

    Returns ``None`` when nothing was requested. A named file that cannot be
    read raises StoreError, so a run never claims an input it did not see.
    """
    names = [os.fspath(item) for item in files]
    if not environment and not names:
        return None
    platform_facts: dict[str, str] | None = None
    revision: str | None = None
    dirty: bool | None = None
    if environment:
        import platform

        platform_facts = {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        }
        revision, dirty = _git_state(root)
    digests: dict[str, model.FileDigest] = {}
    for name in names:
        path = Path(name)
        try:
            size, digest = size_and_sha256(path if path.is_absolute() else root / path)
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            raise StoreError(f"cannot read context file {name}: {detail}") from exc
        digests[path.as_posix()] = model.FileDigest(bytes=size, sha256=digest)
    return model.RunContext(
        platform=platform_facts, git_revision=revision, git_dirty=dirty, files=digests
    )


def _git_state(root: Path) -> tuple[str | None, bool | None]:
    """Return the checked-out revision and whether the work tree differs from it."""
    head = _git(root, "rev-parse", "--verify", "HEAD")
    revision = None if head is None else head.strip().decode("ascii", "replace")
    if revision is None or len(revision) not in (40, 64) or set(revision) - _HEX_DIGITS:
        return None, None
    status = _git(
        root,
        "status",
        "--porcelain",
        # Stated outright, because a user or repository setting can hide either
        # kind of change and make unrecorded inputs look clean.
        "--untracked-files=normal",
        "--ignore-submodules=none",
        "--",
        ":/",
        # The ledger itself changes with every run and is not part of the workload.
        f":(exclude){STATE_DIR_NAME}",
    )
    if status is None:
        return None, None
    return revision, bool(status.strip())


def _git(root: Path, *arguments: str) -> bytes | None:
    """Run git and return its output undecoded, or ``None`` if it failed.

    Git prints paths in the repository's own encoding, which need not be one
    this platform's default codec can decode, so the output stays bytes.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            # Reading state must not write to the repository's index.
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
