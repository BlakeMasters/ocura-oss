# SPDX-License-Identifier: MPL-2.0

"""Orchestration of the retained one-command demonstration."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from ocura_oss import branching, model, runner
from ocura_oss.store import Store


class DemoError(Exception):
    """A demo step failed. The destination directory is retained."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


@dataclass(frozen=True)
class DemoStep:
    step: str
    detail: str


@dataclass(frozen=True)
class DemoReport:
    root: Path
    steps: tuple[DemoStep, ...]
    comparison: model.ComparisonResult

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "steps": [{"step": item.step, "detail": item.detail} for item in self.steps],
            "comparison": self.comparison.to_dict(),
        }


def run_demo(root: Path) -> DemoReport:
    """Perform the whole loop in a new retained directory.

    The root must not exist. It is created and retained even when a step fails,
    and the failing step is reported instead of a completed demo. Both
    demonstration runs must pass and all records and logs must verify before
    the demo reports completion.
    """
    destination = Path(root).expanduser().resolve()
    if destination.exists():
        raise DemoError("prepare", f"destination already exists: {destination}")
    try:
        destination.mkdir(parents=True)
    except OSError as exc:
        raise DemoError("prepare", f"cannot create destination: {exc}") from exc

    steps: list[DemoStep] = []
    stage = "initialize"
    try:
        store = Store(destination)
        den, pathway = store.initialize_state(name="demo")
        steps.append(
            DemoStep(
                "initialize",
                f"den {den.id}; baseline pathway {pathway.id}",
            )
        )

        stage = "baseline-run"
        baseline = runner.run_command(
            store,
            pathway_id=pathway.id,
            argv=[sys.executable, "-c", "print('batch=1 run complete')"],
            declared_parameters={"batch": "1"},
        )
        if baseline.atom.outcome is not model.Outcome.PASSED:
            raise DemoError(
                "baseline-run",
                f"baseline ended in outcome {baseline.atom.outcome.value}",
            )
        steps.append(
            DemoStep(
                "baseline-run",
                f"atom {baseline.atom.id} passed with declared batch=1",
            )
        )

        stage = "chokepoint"
        chokepoint, _atom, source_pathway = branching.load_verified_source(
            store, baseline.chokepoint.id
        )
        steps.append(
            DemoStep(
                "chokepoint",
                f"terminal evidence boundary {chokepoint.id} (outcome passed)",
            )
        )

        stage = "branch"
        child = branching.create_child_pathway(
            store,
            source_chokepoint=chokepoint,
            source_pathway=source_pathway,
            reason="hypothesis: batch=4 changes the result",
            overrides={"batch": "4"},
        )
        steps.append(
            DemoStep(
                "branch",
                f"pathway {child.id}; reason recorded; effective batch=4",
            )
        )

        stage = "rerun"
        rerun = runner.run_command(
            store,
            pathway_id=child.id,
            argv=[sys.executable, "-c", "print('batch=4 run complete')"],
            declared_parameters={"batch": "4"},
        )
        if rerun.atom.outcome is not model.Outcome.PASSED:
            raise DemoError(
                "rerun",
                f"rerun ended in outcome {rerun.atom.outcome.value}",
            )
        steps.append(
            DemoStep(
                "rerun",
                f"atom {rerun.atom.id} passed on pathway {child.id} with batch=4",
            )
        )

        stage = "verify"
        verification = store.verify_state()
        if not verification.ok:
            record, problem = verification.problems[0]
            raise DemoError("verify", f"{record}: {problem}")
        steps.append(
            DemoStep(
                "verify",
                f"verified {verification.atoms} atoms; every record matches its recorded checksum",
            )
        )

        stage = "compare"
        result = branching.compare(store, baseline.chokepoint.id)
        if result.state is not model.ComparisonState.READY:
            raise DemoError("compare", f"comparison ended in state {result.state.value}")
        run_delta = result.children[0].run_parameters
        steps.append(
            DemoStep(
                "compare",
                "state ready; declared runs: "
                + (run_delta.phrase() if run_delta else "(no run parameters)"),
            )
        )
    except DemoError:
        raise
    except Exception as exc:
        raise DemoError(stage, str(exc)) from exc

    return DemoReport(
        root=destination,
        steps=tuple(steps),
        comparison=result,
    )
