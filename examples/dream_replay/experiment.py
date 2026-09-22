# SPDX-License-Identifier: MPL-2.0
"""Collect CPU training history, replay policies, and execute a frozen fresh comparison."""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.request
from pathlib import Path

from common import (
    HERE,
    SCHEMA,
    SOURCES,
    adapter_digest,
    checked_bytes,
    configuration,
    dataset_info,
    digest,
    encoded,
    validate_manifest,
    write_json,
)
from policies import Observation, choose, legal_actions
from replay import load_history, select

from ocura_oss import RunExecution, Store, StoreError, branch, initialize, run


def prepare(root: Path, download: bool, upstream: Path | None, data: Path | None) -> dict:
    if not download and (upstream is None or data is None):
        raise ValueError("use --download or provide both --upstream and --data")
    if download and (upstream is not None or data is not None):
        raise ValueError("choose downloads or existing local files")
    root.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    for name, (url, expected) in SOURCES.items():
        if download:
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read()
        else:
            source = (
                data
                if name == "input.txt"
                else upstream / ("LICENSE" if name == "LICENSE.nanoGPT" else name)
            )
            content = source.read_bytes()
        if digest(content) != expected:
            raise ValueError(f"pinned source digest mismatch: {name}")
        (root / name).write_bytes(content)
    result = {
        "sources": {name: {"url": url, "sha256": sha} for name, (url, sha) in SOURCES.items()},
        "seconds": time.monotonic() - started,
    }
    write_json(root / "preparation.json", result)
    return result


def make_manifest(prepared: Path, profile: str, seed: int, purpose: str, **extra) -> dict:
    prepared = prepared.resolve()
    data = checked_bytes(prepared / "input.txt", SOURCES["input.txt"][1])
    for name in ("model.py", "LICENSE.nanoGPT"):
        checked_bytes(prepared / name, SOURCES[name][1])
    config = configuration(profile)
    order = [r["name"] for r in config["recipes"]]
    random.Random(seed).shuffle(order)
    manifest = {
        "schema": SCHEMA,
        "kind": "manifest",
        "purpose": purpose,
        "prepared": str(prepared),
        "profile": profile,
        "seed": seed,
        "dataset": dataset_info(data),
        "config": config,
        "order": order,
        "adapter_sha256": adapter_digest(),
        "sources": {name: {"url": url, "sha256": sha} for name, (url, sha) in SOURCES.items()},
        **extra,
    }
    validate_manifest(manifest)
    return manifest


def start_world(root: Path, manifest: dict):
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "manifest.json", manifest)
    initialize(root, name="Dream-RSI-inspired training replay")
    manifest_hash = digest(encoded(manifest))
    execution = run(
        [
            sys.executable,
            str(HERE / "experiment.py"),
            "emit",
            "--path",
            str(root / "manifest.json"),
            "--sha256",
            manifest_hash,
        ],
        root=root,
        parameters={"kind": "manifest"},
    )
    if execution.atom.outcome.value != "passed":
        raise ValueError("manifest command failed; see retained stderr")
    return execution, manifest_hash


def run_segment(
    root, manifest, manifest_hash, parent, recipe, level, allowance, previous=None, test_only=False
):
    quantum = manifest["config"]["quantum"]
    start, end = (level - 1) * quantum, level * quantum
    if test_only:
        start = end
    labels = {
        "kind": "final-test" if test_only else "segment",
        "recipe": recipe,
        "start": str(start),
        "end": str(end),
        "manifest_sha256": manifest_hash,
    }
    child = branch(parent.chokepoint.id, root=root, reason=labels["kind"], parameters=labels)
    command = [
        sys.executable,
        str(HERE / "train_segment.py"),
        "--manifest",
        str(root / "manifest.json"),
        "--manifest-sha256",
        manifest_hash,
        "--recipe",
        recipe,
        "--start",
        str(start),
        "--end",
        str(end),
        "--output",
        f"checkpoints/{recipe}-{level}.pt",
        "--max-seconds",
        str(allowance),
    ]
    if previous is not None:
        command += [
            "--checkpoint",
            previous["checkpoint"],
            "--checkpoint-sha256",
            previous["checkpoint_sha256"],
        ]
    if test_only:
        command += ["--test-only"]
    execution = run(command, root=root, pathway_id=child.id, parameters=labels)
    result = None
    if execution.atom.outcome.value == "passed":
        result = json.loads(Store(root).read_verified_log(execution.atom.id))
    return execution, result


def execute_world(
    root: Path, manifest: dict, policy: dict, budget: int, max_seconds: float, final_test=False
) -> dict:
    started = time.monotonic()
    root_execution, manifest_hash = start_world(root, manifest)
    order, horizon = tuple(manifest["order"]), manifest["config"]["horizon"]
    seen, results, executions = (), {}, {}
    for _ in range(budget):
        legal = legal_actions(order, horizon, seen)
        action = choose(policy, order, horizon, seen, legal)
        if action is None:
            break
        if action not in legal:
            raise ValueError("policy requested an illegal action")
        remaining = max_seconds - (time.monotonic() - started)
        if remaining < 1:
            raise TimeoutError("experiment allowance exhausted; incomplete history retained")
        level = sum(o.recipe == action for o in seen) + 1
        previous = results.get(action)
        parent = executions.get(action, root_execution)
        execution, result = run_segment(
            root,
            manifest,
            manifest_hash,
            parent,
            action,
            level,
            min(60, remaining),
            previous,
        )
        executions[action], results[action] = execution, result
        loss = result["validation_loss"] if result else None
        seen += (Observation(action, level, loss),)
        print(
            f"{root.name}: {action}/{level} "
            + (f"loss={loss:.4f}" if loss is not None else "failed"),
            file=sys.stderr,
        )
    history = load_history(root)
    successful = [n for n in history["nodes"].values() if n["result"]]
    if final_test and successful:
        # Freeze the best revealed validation checkpoint before reading any test score.
        winner = min(
            successful,
            key=lambda n: (n["result"]["validation_loss"], order.index(n["recipe"]), n["quantum"]),
        )
        remaining = max_seconds - (time.monotonic() - started)
        if remaining < 1:
            raise TimeoutError("allowance exhausted before final test; training history retained")
        store = Store(root)
        # Recover the exact selected atom's terminal boundary, including earlier segments.
        point = next(c for c in store.list_chokepoints() if c.atom_id == winner["atom_id"])
        parent = RunExecution(store.load_atom(winner["atom_id"]), point)
        run_segment(
            root,
            manifest,
            manifest_hash,
            parent,
            winner["recipe"],
            winner["quantum"],
            min(60, remaining),
            winner["result"],
            test_only=True,
        )
    return report(root)


def report(root: Path) -> dict:
    history = load_history(root)
    nodes = sorted(history["nodes"].values(), key=lambda n: (n["recipe"], n["quantum"]))
    successes = [n for n in nodes if n["result"]]
    return {
        "root": str(root),
        "verification": "passed",
        "manifest": history["manifest"],
        "segments": len(nodes),
        "failed_segments": len(nodes) - len(successes),
        "best_validation_loss": min(
            (n["result"]["validation_loss"] for n in successes), default=None
        ),
        "reserved_training_tokens": history["reserved_training_tokens"],
        "total_command_seconds": history["total_command_seconds"],
        "final_tests": history["final_tests"],
        "nodes": nodes,
    }


def evaluate(args) -> dict:
    history = load_history(args.history)
    development = history["manifest"]
    if args.seed == development["seed"]:
        raise ValueError("fresh evaluation seed must differ from the development seed")
    started = time.monotonic()
    selection = select(history, args.budget_quanta)
    selection["replay_seconds"] = time.monotonic() - started
    controls = [
        r
        for r in selection["comparisons"]
        if r["policy"]["name"]
        in ("uniform", "fixed-development-ranking", "successive-halving", "greedy-explore-4")
        and r["best_validation_loss"] is not None
    ]
    baseline = min(controls, key=lambda r: r["best_validation_loss"])["policy"]
    prepared = args.prepared or Path(development["prepared"])
    frozen = {
        "development_manifest_sha256": history["manifest_sha256"],
        "selection": selection,
        "baseline": baseline,
        "budget_quanta": args.budget_quanta,
    }
    manifests = {}
    for arm, policy in (("selected", selection["selected_policy"]), ("baseline", baseline)):
        manifest = make_manifest(
            prepared,
            development["profile"],
            args.seed,
            "evaluation",
            frozen=frozen,
            policy=policy,
            arm=arm,
        )
        if any(manifest[k] != development[k] for k in ("config", "dataset", "adapter_sha256")):
            raise ValueError("workload or adapter changed after development collection")
        manifests[arm] = manifest
    args.root.mkdir(parents=True, exist_ok=False)
    reports = {}
    for arm, manifest in manifests.items():
        if arm == "baseline" and baseline == selection["selected_policy"]:
            reports[arm] = {"same_policy_as": "selected", "root": reports["selected"]["root"]}
            continue
        remaining = args.max_seconds - (time.monotonic() - started)
        if remaining < 1:
            raise TimeoutError("evaluation allowance exhausted; completed arms retained")
        reports[arm] = execute_world(
            args.root / arm,
            manifest,
            manifest["policy"],
            args.budget_quanta,
            remaining,
            final_test=True,
        )
    return {
        "selection": selection,
        "baseline_policy": baseline,
        "arms": reports,
        "claim": "Single-seed mechanism check; no savings or generalization claim.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    prep = subs.add_parser("prepare")
    prep.add_argument("--root", type=Path, required=True)
    prep.add_argument("--download", action="store_true")
    prep.add_argument("--upstream", type=Path)
    prep.add_argument("--data", type=Path)
    collect = subs.add_parser("collect")
    collect.add_argument("--root", type=Path, required=True)
    collect.add_argument("--prepared", type=Path, required=True)
    collect.add_argument("--profile", choices=("smoke", "cpu"), default="smoke")
    collect.add_argument("--seed", type=int, default=101)
    collect.add_argument("--max-seconds", type=float, default=300)
    replay = subs.add_parser("replay")
    replay.add_argument("--root", type=Path, required=True)
    replay.add_argument("--budget-quanta", type=int, default=8)
    fresh = subs.add_parser("evaluate")
    fresh.add_argument("--history", type=Path, required=True)
    fresh.add_argument("--root", type=Path, required=True)
    fresh.add_argument("--prepared", type=Path)
    fresh.add_argument("--seed", type=int, default=201)
    fresh.add_argument("--budget-quanta", type=int, default=8)
    fresh.add_argument("--max-seconds", type=float, default=300)
    saved = subs.add_parser("report")
    saved.add_argument("--root", type=Path, required=True)
    emit = subs.add_parser("emit", help="emit a checked manifest (used internally)")
    emit.add_argument("--path", type=Path, required=True)
    emit.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "root"):
            args.root = args.root.expanduser().resolve()
        if hasattr(args, "max_seconds") and (
            not math.isfinite(args.max_seconds) or args.max_seconds <= 0
        ):
            raise ValueError("max-seconds must be positive and finite")
        if args.action == "prepare":
            result = prepare(args.root, args.download, args.upstream, args.data)
        elif args.action == "collect":
            manifest = make_manifest(args.prepared, args.profile, args.seed, "collection")
            result = execute_world(
                args.root,
                manifest,
                {"name": "uniform", "mode": "uniform"},
                len(manifest["order"]) * manifest["config"]["horizon"],
                args.max_seconds,
            )
        elif args.action == "replay":
            started = time.monotonic()
            result = select(load_history(args.root), args.budget_quanta)
            result["replay_seconds"] = time.monotonic() - started
        elif args.action == "evaluate":
            result = evaluate(args)
        elif args.action == "report":
            result = report(args.root)
        else:
            sys.stdout.buffer.write(checked_bytes(args.path, args.sha256))
            return 0
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, StoreError, KeyError, TypeError, StopIteration) as exc:
        print(f"dream replay: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
