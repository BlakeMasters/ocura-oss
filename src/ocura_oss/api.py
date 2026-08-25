# SPDX-License-Identifier: MPL-2.0

"""Supported programmatic workflows for Ocura OSS."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ocura_oss import branching, runner
from ocura_oss.demo import DemoReport
from ocura_oss.demo import run_demo as _run_demo
from ocura_oss.model import ComparisonResult, Den, Pathway
from ocura_oss.runner import RunExecution
from ocura_oss.store import StateVerification, Store

_Root = os.PathLike[str] | str | None


@dataclass(frozen=True)
class Initialization:
    """State created by :func:`initialize`."""

    root: Path
    den: Den
    pathway: Pathway


def initialize(root: _Root = None, *, name: str = "ocura-oss") -> Initialization:
    """Create one den and its default pathway under a project root.

    The operation refuses to replace an existing ``.ocura-oss/`` directory.
    When *root* is omitted, the current working directory is used.
    """
    store = Store(root)
    den, pathway = store.initialize_state(name=name)
    return Initialization(root=store.root, den=den, pathway=pathway)


def run(
    command: Sequence[str],
    *,
    root: _Root = None,
    pathway_id: str | None = None,
    parameters: Mapping[str, str] | None = None,
    mirror: bool = False,
) -> RunExecution:
    """Run a trusted local command and record terminal evidence.

    The den's default pathway is used when *pathway_id* is omitted. A command
    failure is represented by the returned atom's outcome and does not raise
    an exception. Invalid state or input raises :class:`StoreError`.
    """
    store = _required_store(root)
    selected_pathway = pathway_id or store.load_den().default_pathway_id
    return runner.run_command(
        store,
        pathway_id=selected_pathway,
        argv=command,
        declared_parameters={} if parameters is None else parameters,
        mirror=mirror,
    )


def branch(
    source_chokepoint_id: str,
    *,
    reason: str,
    root: _Root = None,
    parameters: Mapping[str, str] | None = None,
) -> Pathway:
    """Create a metadata-only child pathway from verified terminal evidence."""
    store = _required_store(root)
    source_chokepoint, _source_atom, source_pathway = branching.load_verified_source(
        store, source_chokepoint_id
    )
    return branching.create_child_pathway(
        store,
        source_chokepoint=source_chokepoint,
        source_pathway=source_pathway,
        reason=reason,
        overrides={} if parameters is None else parameters,
    )


def compare(source_chokepoint_id: str | None = None, *, root: _Root = None) -> ComparisonResult:
    """Compare a verified source run with child pathway evidence.

    Omitting *source_chokepoint_id* selects the newest chokepoint referenced by
    a child pathway after the complete state passes verification. A state with
    no child pathways selects its newest terminal chokepoint.
    """
    store = _required_store(root)
    return branching.compare(store, source_chokepoint_id)


def verify(root: _Root = None) -> StateVerification:
    """Recheck every state record and every log referenced by a recorded run."""
    return _required_store(root).verify_state()


def run_demo(root: os.PathLike[str] | str) -> DemoReport:
    """Run the retained demonstration in a new directory."""
    return _run_demo(Path(root))


def _required_store(root: _Root) -> Store:
    store = Store(root)
    store.require()
    return store
