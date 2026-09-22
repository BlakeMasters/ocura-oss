# SPDX-License-Identifier: MPL-2.0
"""Verify recorded evidence and reveal only the next requested observation."""

from __future__ import annotations

import json
import math
from pathlib import Path

from common import digest, encoded, quantum_tokens, validate_manifest
from policies import Observation, candidates, choose, legal_actions

from ocura_oss import Store, StoreError


class CoverageError(ValueError):
    """A requested historical continuation was never recorded."""


def load_history(root: Path) -> dict:
    store = Store(root)
    checked = store.verify_state()
    if not checked.ok:
        raise StoreError(f"verification failed: {checked.problems}")
    atoms = store.list_atoms()
    roots = [a for a in atoms if a.declared_parameters.get("kind") == "manifest"]
    if len(roots) != 1:
        raise ValueError("expected exactly one experiment manifest")
    root_atom = roots[0]
    if root_atom.outcome.value != "passed":
        raise ValueError("manifest command failed")
    manifest = json.loads(store.read_verified_log(root_atom.id))
    validate_manifest(manifest)
    manifest_hash = digest(encoded(manifest))
    quantum = manifest["config"]["quantum"]
    horizon = manifest["config"]["horizon"]
    nodes = {}
    by_atom = {}
    runtime = None
    final_tests = []
    for atom in atoms:
        if atom.id == root_atom.id:
            continue
        labels = atom.declared_parameters
        if labels.get("kind") == "final-test":
            final_tests.append(atom)
            continue
        if labels.get("kind") != "segment" or labels.get("manifest_sha256") != manifest_hash:
            raise ValueError("unexpected command or manifest in history")
        recipe, step = labels["recipe"], int(labels["end"])
        start = int(labels["start"])
        if recipe not in manifest["order"] or not 0 <= start < step <= quantum * horizon:
            raise ValueError("invalid segment identity")
        if start % quantum or step - start != quantum:
            raise ValueError("expected exactly one quantum per recorded segment")
        key = (recipe, step // quantum)
        if key in nodes:
            raise ValueError("duplicate recipe/quantum")
        pathway = store.load_pathway(atom.pathway_id)
        if not pathway.source_chokepoint_id:
            raise ValueError("segment has no explicit parent")
        parent = store.load_chokepoint(pathway.source_chokepoint_id).atom_id
        result = None
        if atom.outcome.value == "passed":
            result = json.loads(store.read_verified_log(atom.id))
            expected = {
                "kind": "segment",
                "manifest_sha256": manifest_hash,
                "recipe": recipe,
                "start": start,
                "end": step,
                "training_tokens": quantum_tokens(manifest),
            }
            if any(result.get(k) != v for k, v in expected.items()):
                raise ValueError("segment output disagrees with recorded configuration")
            if not math.isfinite(result["validation_loss"]) or result["validation_loss"] < 0:
                raise ValueError("validation loss must be finite and nonnegative")
            if runtime is not None and runtime != result["runtime"]:
                raise ValueError("mixed runtimes in one history")
            runtime = result["runtime"]
            for key_name in ("checkpoint_sha256", "batch_trace_sha256"):
                value = result[key_name]
                if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise ValueError("invalid result digest")
        node = {
            "atom_id": atom.id,
            "parent_atom": parent,
            "recipe": recipe,
            "quantum": step // quantum,
            "result": result,
            "seconds": atom.duration_seconds,
        }
        nodes[key] = by_atom[atom.id] = node
    for node in nodes.values():
        previous = nodes.get((node["recipe"], node["quantum"] - 1))
        expected_parent = (
            root_atom.id
            if node["quantum"] == 1
            else (previous["atom_id"] if previous and previous["result"] else None)
        )
        if node["parent_atom"] != expected_parent:
            raise ValueError("broken segment parent/step continuity")
        if node["result"]:
            expected_digest = previous["result"]["checkpoint_sha256"] if previous else None
            if node["result"]["input_sha256"] != expected_digest:
                raise ValueError("continuation input digest disagrees with parent result")
    if len(final_tests) > 1 or (final_tests and manifest["purpose"] != "evaluation"):
        raise ValueError("expected at most one final test, after fresh policy execution")
    successes = [n for n in nodes.values() if n["result"]]
    winner = (
        min(
            successes,
            key=lambda n: (
                n["result"]["validation_loss"],
                manifest["order"].index(n["recipe"]),
                n["quantum"],
            ),
        )
        if successes
        else None
    )
    tests = []
    for atom in final_tests:
        pathway = store.load_pathway(atom.pathway_id)
        parent = store.load_chokepoint(pathway.source_chokepoint_id).atom_id
        if parent not in by_atom or not by_atom[parent]["result"]:
            raise ValueError("final test has no successful training parent")
        if parent != winner["atom_id"]:
            raise ValueError("final test must use the best revealed validation checkpoint")
        labels = atom.declared_parameters
        if (
            labels.get("manifest_sha256") != manifest_hash
            or labels.get("recipe") != winner["recipe"]
            or labels.get("start") != str(winner["quantum"] * quantum)
            or labels.get("end") != labels.get("start")
        ):
            raise ValueError("final test labels disagree with selected checkpoint")
        result = None
        if atom.outcome.value == "passed":
            result = json.loads(store.read_verified_log(atom.id))
            if (
                result.get("kind") != "final-test"
                or result.get("manifest_sha256") != manifest_hash
                or result.get("input_sha256") != by_atom[parent]["result"]["checkpoint_sha256"]
                or result.get("recipe") != winner["recipe"]
                or result.get("runtime") != runtime
                or not math.isfinite(result["test_loss"])
                or result["test_loss"] < 0
            ):
                raise ValueError("invalid final test result")
        tests.append({"atom_id": atom.id, "result": result, "seconds": atom.duration_seconds})
    return {
        "manifest": manifest,
        "manifest_sha256": manifest_hash,
        "root_atom": root_atom.id,
        "nodes": nodes,
        "final_tests": tests,
        "total_command_seconds": sum(a.duration_seconds for a in atoms),
        "reserved_training_tokens": len(nodes) * quantum_tokens(manifest),
    }


def simulate(history: dict, policy: dict, budget: int) -> dict:
    if budget <= 0:
        raise ValueError("budget must be positive")
    manifest = history["manifest"]
    order, horizon = tuple(manifest["order"]), manifest["config"]["horizon"]
    seen = ()
    trace = []
    for _ in range(budget):
        legal = legal_actions(order, horizon, seen)
        action = choose(policy, order, horizon, seen, legal)
        if action is None:
            break
        if action not in legal:
            raise ValueError("policy selected an illegal continuation")
        level = sum(o.recipe == action for o in seen) + 1
        node = history["nodes"].get((action, level))
        if node is None:
            raise CoverageError(f"unrecorded continuation: {action}/{level}")
        loss = node["result"]["validation_loss"] if node["result"] else None
        seen += (Observation(action, level, loss),)
        trace.append(
            {
                "recipe": action,
                "quantum": level,
                "loss": loss,
                "atom_id": node["atom_id"],
                "seconds": node["seconds"],
            }
        )
    losses = [o.loss for o in seen if o.loss is not None]
    return {
        "policy": policy,
        "budget_quanta": budget,
        "consumed_quanta": len(trace),
        "reserved_training_tokens": len(trace) * quantum_tokens(manifest),
        "best_validation_loss": min(losses) if losses else None,
        "historical_seconds": sum(n["seconds"] for n in trace),
        "trace": trace,
    }


def select(history: dict, budget: int) -> dict:
    if history["manifest"]["purpose"] != "collection":
        raise ValueError("select policies only from a development collection")
    manifest = history["manifest"]
    horizon = manifest["config"]["horizon"]
    if any(
        (r, q) not in history["nodes"] for r in manifest["order"] for q in range(1, horizon + 1)
    ):
        raise CoverageError("policy selection requires a complete development collection")

    def best(recipe):
        values = [
            n["result"]["validation_loss"]
            for (r, _), n in history["nodes"].items()
            if r == recipe and n["result"]
        ]
        return min(values, default=math.inf)

    ranking = sorted(manifest["order"], key=lambda r: (best(r), r))
    comparisons = [simulate(history, p, budget) for p in candidates(ranking)]
    valid = [r for r in comparisons if r["best_validation_loss"] is not None]
    if not valid:
        raise ValueError("no policy revealed a successful candidate")
    # Declared ties prefer candidate order: the uniform incumbent comes first.
    winner = min(valid, key=lambda r: r["best_validation_loss"])
    return {
        "selected_policy": winner["policy"],
        "comparisons": comparisons,
        "budget_quanta": budget,
        "training_invocations": 0,
        "task_evaluations": 0,
        "collection_command_seconds": history["total_command_seconds"],
        "collection_reserved_training_tokens": history["reserved_training_tokens"],
        "claim": "One development world; demonstrates the mechanism, not savings.",
    }
