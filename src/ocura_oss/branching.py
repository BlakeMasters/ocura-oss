# SPDX-License-Identifier: MPL-2.0

"""Source validation, metadata-only branching, and comparison states."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping

from ocura_oss import model
from ocura_oss.store import Store, StoreError


def load_verified_source(
    store: Store, chokepoint_id: str
) -> tuple[model.Chokepoint, model.Atom, model.Pathway]:
    """Load and verify a chokepoint, its atom, its pathway, and its evidence.

    The source is rejected before any write when the records are missing,
    malformed, checksum-mismatched, nonterminal, nonbranchable, or when the
    atom's recorded logs are missing or fail integrity verification.
    """
    chokepoint = store.load_chokepoint(chokepoint_id)
    if chokepoint.kind != model.TERMINAL_KIND:
        raise StoreError(f"source chokepoint {chokepoint_id} is not terminal")
    if not chokepoint.branchable:
        raise StoreError(f"source chokepoint {chokepoint_id} is not branchable")
    atom = store.load_atom(chokepoint.atom_id)
    pathway = store.load_pathway(chokepoint.pathway_id)
    store.verify_atom_evidence(atom)
    return chokepoint, atom, pathway


def create_child_pathway(
    store: Store,
    *,
    source_chokepoint: model.Chokepoint,
    source_pathway: model.Pathway,
    reason: str,
    overrides: Mapping[str, str],
    now: Callable[[], datetime.datetime] | None = None,
    new_id: Callable[[str], str] | None = None,
) -> model.Pathway:
    """Create one child pathway containing lineage metadata only.

    No workspace, process, memory image, checkpoint, artifact, or source atom
    is copied. Effective parameters are the parent's effective parameters plus
    the declared overrides.
    """
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise StoreError("branch reason must not be blank")
    override_parameters = model.validate_parameters(dict(overrides))
    clock = now or model.utc_now
    identifier = new_id or model.make_id
    den = store.load_den()
    effective = dict(source_pathway.parameters)
    effective.update(override_parameters)
    child = model.Pathway(
        id=identifier("pathway"),
        den_id=den.id,
        created_at=model.format_timestamp(clock()),
        parent_pathway_id=source_pathway.id,
        source_chokepoint_id=source_chokepoint.id,
        reason=text,
        parameters=effective,
    )
    store.save_pathway(child)
    return child


def select_source_chokepoint(
    store: Store,
) -> tuple[model.Chokepoint, model.Atom, model.Pathway]:
    """Choose the newest branchable chokepoint under a global fail-closed policy.

    The entire state is verified first: any malformed record, broken
    reference, unverified log, or orphaned file anywhere under the state
    directory prevents automatic selection instead of allowing a fallback to
    older evidence.
    """
    report = store.verify_state()
    if not report.ok:
        record, problem = report.problems[0]
        raise StoreError(
            f"automatic source selection requires a fully verified state ({record}: {problem})"
        )
    chokepoints = store.list_chokepoints()
    if not chokepoints:
        raise StoreError("no branchable chokepoint found")
    newest = chokepoints[0]
    return load_verified_source(store, newest.id)


def compare(store: Store, chokepoint_id: str | None = None) -> model.ComparisonResult:
    """Compare a source atom with every child pathway created from its chokepoint."""
    if chokepoint_id is None:
        source = select_source_chokepoint(store)
    else:
        source = load_verified_source(store, chokepoint_id)
    chokepoint, atom, pathway = source
    source_run = model.run_summary_from_atom(atom)

    children = []
    for candidate in store.list_pathways():
        if (
            candidate.parent_pathway_id == pathway.id
            and candidate.source_chokepoint_id == chokepoint.id
        ):
            children.append(store.load_pathway(candidate.id))
    children.sort(key=lambda item: (item.created_at, item.id))

    comparisons = []
    any_missing = False
    for child in children:
        child_atoms = [item for item in store.list_atoms() if item.pathway_id == child.id]
        child_run: model.RunSummary | None = None
        run_parameters: model.ParameterDelta | None = None
        if child_atoms:
            newest = max(child_atoms, key=lambda item: (item.started_at, item.id))
            loaded = store.load_atom(newest.id)
            store.verify_atom_evidence(loaded)
            child_run = model.run_summary_from_atom(loaded)
            run_parameters = model.parameter_delta(
                atom.declared_parameters, loaded.declared_parameters
            )
        else:
            any_missing = True
        delta = model.parameter_delta(pathway.parameters, child.parameters)
        comparisons.append(
            model.ChildComparison(
                pathway_id=child.id,
                reason=child.reason,
                source_chokepoint_id=chokepoint.id,
                parameters=delta,
                source_run=source_run,
                child_run=child_run,
                run_parameters=run_parameters,
                missing_evidence=child_run is None,
            )
        )

    if not children:
        state = model.ComparisonState.NO_BRANCH
    elif any_missing:
        state = model.ComparisonState.PARTIAL
    else:
        state = model.ComparisonState.READY
    return model.ComparisonResult(
        state=state,
        source_chokepoint_id=chokepoint.id,
        source_pathway_id=pathway.id,
        source_run=source_run,
        children=tuple(comparisons),
    )
