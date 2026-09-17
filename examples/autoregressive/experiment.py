# SPDX-License-Identifier: MPL-2.0

"""Record two autoregressive runs, or reconstruct their report from verified logs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from ocura_oss import ComparisonState, Store, StoreError, compare, verify


def cli(*arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "ocura_oss", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"ocura-oss {arguments[0]} exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return json.loads(completed.stdout)


def report(root: Path) -> dict:
    checked = verify(root)
    if not checked.ok:
        raise StoreError(f"verification failed: {checked.problems}")
    comparison = compare(root=root)
    if comparison.state is not ComparisonState.READY or len(comparison.children) != 1:
        raise ValueError("expected one baseline and one completed variant")
    child = comparison.children[0]
    if child.child_run is None:
        raise ValueError("variant has no recorded run")
    store = Store(root)
    metrics = []
    for summary in (comparison.source_run, child.child_run):
        if summary.outcome.value != "passed":
            raise ValueError(f"recorded run {summary.id} did not pass")
        atom = store.load_atom(summary.id)
        data = json.loads(store.read_verified_log(atom.id).decode("utf-8"))
        if data.get("example") != "autoregressive-character-v1":
            raise ValueError("the selected logs are not from this example")
        for key in ("backend", "steps", "learning_rate", "seed", "executor"):
            if str(data.get(key)) != atom.declared_parameters.get(key):
                raise ValueError(f"recorded parameter {key} disagrees with workload output")
        if not math.isfinite(data["validation_loss"]):
            raise ValueError("validation loss must be finite")
        metrics.append({**data, "atom_id": atom.id, "stdout_log": atom.stdout_log})
    baseline, variant = metrics
    return {
        "root": str(store.root),
        "source_chokepoint_id": comparison.source_chokepoint_id,
        "comparison": comparison.to_dict(),
        "verification": {"ok": True, "atoms": checked.atoms, "logs": checked.logs_checked},
        "baseline": baseline,
        "variant": variant,
        "validation_loss_delta": variant["validation_loss"] - baseline["validation_loss"],
        "metric_source": "example code reading verified workload stdout; lower loss is better",
    }


def run_experiment(args: argparse.Namespace) -> dict:
    if args.baseline_steps <= 0 or args.variant_steps <= 0:
        raise ValueError("step counts must be positive")
    if not math.isfinite(args.learning_rate) or args.learning_rate <= 0:
        raise ValueError("learning rate must be positive and finite")
    required = ["torch" if args.backend == "pytorch" else "jax"]
    if args.executor == "ray":
        required.append("ray")
    for name in required:
        if importlib.util.find_spec(name) is None:
            raise ValueError(
                f"install the optional {name} example dependency first (see README.md)"
            )
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    initial = cli("init", "--root", str(root), "--name", "autoregressive A/B", "--json")
    workload = (
        Path(__file__).resolve().with_name("ray_train.py" if args.executor == "ray" else "train.py")
    )

    def run_variant(steps: int, pathway: str) -> dict:
        parameters = {
            "backend": args.backend,
            "executor": args.executor,
            "steps": str(steps),
            "learning_rate": str(args.learning_rate),
            "seed": str(args.seed),
        }
        flags = [
            item for key, value in parameters.items() for item in ("--param", f"{key}={value}")
        ]
        return cli(
            "run",
            "--root",
            str(root),
            "--pathway",
            pathway,
            "--json",
            *flags,
            "--",
            sys.executable,
            str(workload),
            "--backend",
            args.backend,
            "--steps",
            str(steps),
            "--learning-rate",
            str(args.learning_rate),
            "--seed",
            str(args.seed),
        )

    baseline = run_variant(args.baseline_steps, initial["default_pathway_id"])
    child = cli(
        "branch",
        "--root",
        str(root),
        "--from",
        baseline["chokepoint_id"],
        "--reason",
        f"test {args.variant_steps} training steps against {args.baseline_steps}",
        "--param",
        f"steps={args.variant_steps}",
        "--json",
    )
    run_variant(args.variant_steps, child["id"])
    return report(root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run", help="create a new retained A/B experiment")
    run_parser.add_argument(
        "--root", type=Path, required=True, help="new directory; never overwritten"
    )
    run_parser.add_argument("--backend", choices=("pytorch", "jax"), required=True)
    run_parser.add_argument("--executor", choices=("local", "ray"), default="local")
    run_parser.add_argument("--baseline-steps", type=int, default=40)
    run_parser.add_argument("--variant-steps", type=int, default=120)
    run_parser.add_argument("--learning-rate", type=float, default=0.5)
    run_parser.add_argument("--seed", type=int, default=7)
    report_parser = subparsers.add_parser("report", help="read saved evidence without training")
    report_parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_experiment(args) if args.action == "run" else report(args.root)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, RuntimeError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
