# SPDX-License-Identifier: MPL-2.0
"""Reconstruct a pilot from checked Ocura bytes without importing Torch or agents."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from rsi_core import choose_policy, evaluate_policy, observation, validate_world
from rsi_io import checked_json, digest

from ocura_oss import Store


def contained(root, path):
    result = (root / path).resolve()
    if not result.is_relative_to(root):
        raise ValueError("artifact path escapes the experiment root")
    return result


def build_report(root):
    root = Path(root).resolve()
    store = Store(root)
    verification = store.verify_state()
    if not verification.ok:
        raise ValueError(f"Ocura verification failed: {verification.problems}")
    atoms = {a.id: a for a in store.list_atoms()}
    records, requests = {}, {}
    for identity, atom in atoms.items():
        labels = atom.declared_parameters
        for prefix in ("job", "artifact"):
            if f"{prefix}_file" in labels:
                value = checked_json(
                    contained(root, labels[f"{prefix}_file"]), labels[f"{prefix}_sha256"]
                )
                if prefix == "artifact":
                    requests[identity] = value
        raw = store.read_verified_log(identity)
        try:
            records[identity] = json.loads(raw) if raw.strip() else None
        except ValueError:
            records[identity] = None
        if atom.return_code == 0 and records[identity] is None:
            raise ValueError("successful recorded command has no JSON output")

    def tagged(kind):
        return [
            (a, records[a.id]) for a in atoms.values() if a.declared_parameters.get("kind") == kind
        ]

    manifests = tagged("manifest")
    if len(manifests) != 1:
        raise ValueError("expected exactly one manifest")
    manifest = manifests[0][1]
    worlds = [record for _, record in tagged("world")]
    completions = tagged("completion")
    completion = completions[-1][1] if completions else {"status": "incomplete", "tests": []}
    if len(completions) > 1:
        raise ValueError("ambiguous completion records")
    checked_checkpoints = {}
    expected_worlds = {"bootstrap": ("bootstrap", manifest["seed"])}
    for phase in range(1, manifest["rounds"]):
        for arm in ("adaptive", "fixed"):
            expected_worlds[f"{arm}-{phase}"] = (arm, manifest["seed"] + phase)
    by_name = {world["name"]: world for world in worlds}
    if len(by_name) != len(worlds) or set(by_name) - set(expected_worlds):
        raise ValueError("unexpected or duplicate discovery world")
    for atom, result in tagged("agent"):
        request = requests[atom.id]
        if any(request.get(key) != manifest[key] for key in ("model", "effort")):
            raise ValueError("agent request differs from the frozen model/effort")
        if result is not None and any(
            result.get(key) != manifest[key] for key in ("model", "effort")
        ):
            raise ValueError("agent result differs from the frozen model/effort")
    contracts = {}
    for atom, result in tagged("evaluation"):
        request = requests[atom.id]
        labels = atom.declared_parameters
        operation = labels["operation"]
        name = labels["world"]
        if name not in expected_worlds:
            raise ValueError("evaluator refers to an unexpected world")
        expected = {
            "operation": operation,
            "profile": manifest["profile"],
            "seed": expected_worlds[name][1],
            "updates": manifest["updates"] if operation == "train" else 0,
            "total_steps": manifest["updates"] * manifest["attempts"],
        }
        if any(request.get(key) != value for key, value in expected.items()) or (
            Path(request["prepared"]).resolve() != Path(manifest["prepared"]).resolve()
        ):
            raise ValueError("evaluator request differs from the frozen configuration")
        if result is None:
            continue
        if result.get("evaluator_sha256") != manifest["source_sha256"]["rsi_training.py"]:
            raise ValueError("evaluator fingerprint differs from the manifest")
        support = {
            "rsi_core.py": manifest["source_sha256"]["rsi_core.py"],
            "dream_replay/common.py": manifest["shared_inputs_sha256"],
        }
        if not manifest.get("synthetic") and result.get("support_sha256") != support:
            raise ValueError("evaluator support fingerprint differs from the manifest")
        if any(result.get(key) != expected[key] for key in ("profile", "seed", "total_steps")):
            raise ValueError("evaluator output differs from its frozen request")
        if result.get("schedule_sha256") != digest(request["schedule_source"].encode()):
            raise ValueError("evaluator output differs from its requested schedule")
        if result.get("input_checkpoint_sha256") != request["parent_checkpoint_sha256"]:
            raise ValueError("evaluator output differs from its requested checkpoint")
        contract = result["contract_sha256"]
        if name in contracts and contracts[name] != contract:
            raise ValueError("evaluator contract changed within a world")
        contracts[name] = contract

    def checkpoint(result):
        path = contained(root, result["checkpoint"])
        if path not in checked_checkpoints:
            checked_checkpoints[path] = digest(path.read_bytes())
        if checked_checkpoints[path] != result["checkpoint_sha256"]:
            raise ValueError("checkpoint digest mismatch")

    # Deferral can leave successful evaluator outputs without a completed world.
    # Their immutable checkpoint bytes still belong to the retained evidence.
    for atom, result in tagged("evaluation"):
        if atom.return_code == 0:
            checkpoint(result)

    def source_parent(identity):
        pathway = store.load_pathway(atoms[identity].pathway_id)
        return (
            store.load_chokepoint(pathway.source_chokepoint_id).atom_id
            if pathway.source_chokepoint_id
            else None
        )

    world_reports = []
    for world in worlds:
        validate_world(world)
        if (world["arm"], world["seed"]) != expected_worlds[world["name"]]:
            raise ValueError("world arm/seed differs from the matched comparison")
        if world["arm"] in {"bootstrap", "fixed"} and (
            world["policy_source"] != manifest["initial_policy"]
        ):
            raise ValueError("fixed world did not deploy the frozen initial policy")
        initial = records[world["root_atom"]]
        if initial != world["root_result"] or initial["score"] != world["root_score"]:
            raise ValueError("world root differs from verified evaluator output")
        checkpoint(initial)
        retained = {"root": initial}
        terminals = {"root": world["root_atom"]}
        decisions = [
            (atom, decision)
            for atom, decision in tagged("decision")
            if decision["world"] == world["name"]
        ]
        expected_decisions = len(world["nodes"]) + (world["stop_reason"] == "policy_stop")
        if len(decisions) != expected_decisions:
            raise ValueError("world has missing or extra policy decisions")
        expected_trace = []
        for index, (atom, decision) in enumerate(decisions):
            prefix = observation(
                world["nodes"][:index],
                world["root_score"],
                manifest["workers"],
                index,
                manifest["attempts"],
                manifest["beta_cost"],
                manifest["beta_parallel"],
            )
            if decision["observation"] != prefix:
                raise ValueError("policy observation differs from the exact revealed prefix")
            if decision["policy_sha256"] != digest(world["policy_source"].encode()):
                raise ValueError("decision policy fingerprint differs from deployed source")
            batch = choose_policy(world["policy_source"], prefix)
            if decision["batch"] != batch:
                raise ValueError("recorded policy batch differs from recomputed decision")
            if index < len(world["nodes"]):
                node = world["nodes"][index]
                if node["decision_atom"] != atom.id or batch != [node["parent"]]:
                    raise ValueError("discovery node differs from its policy batch")
                expected_trace.append(
                    {"round": index + 1, "batch": batch, "revealed": [node["id"]]}
                )
            elif batch:
                raise ValueError("policy stop must be a recorded empty batch")
        if world["trace"] != expected_trace:
            raise ValueError("world trace differs from recorded decisions")
        if world["stop_reason"] not in {"policy_stop", "round_limit"} or (
            world["stop_reason"] == "round_limit" and len(world["nodes"]) != manifest["attempts"]
        ):
            raise ValueError("world termination differs from its declared decision budget")
        for node in world["nodes"]:
            if source_parent(node["agent_atom"]) != terminals[node["parent"]]:
                raise ValueError("agent ancestry differs from recorded discovery parent")
            decision = records[node["decision_atom"]]
            if decision["world"] != world["name"] or node["parent"] not in decision["batch"]:
                raise ValueError("node does not follow its recorded policy decision")
            parent_checkpoint = retained[node["parent"]]
            if node["input_checkpoint_sha256"] != parent_checkpoint["checkpoint_sha256"]:
                raise ValueError("restoration differs from the last successful ancestor")
            if node["status"] == "ok":
                if (
                    atoms[node["agent_atom"]].return_code != 0
                    or atoms[node["terminal_atom"]].return_code != 0
                ):
                    raise ValueError("successful node references a failed execution")
                actual = records[node["terminal_atom"]]
                if source_parent(node["terminal_atom"]) != node["agent_atom"]:
                    raise ValueError("evaluation does not descend from its proposal")
                if node["result"] != actual or node["score"] != actual["score"]:
                    raise ValueError("node score differs from verified evaluator output")
                if actual["input_checkpoint_sha256"] != parent_checkpoint["checkpoint_sha256"]:
                    raise ValueError("evaluator restored the wrong checkpoint")
                if (
                    actual["start"] != parent_checkpoint["step"]
                    or actual["end"] - actual["start"] != manifest["updates"]
                ):
                    raise ValueError("training update boundary differs from the frozen budget")
                if actual["schedule_sha256"] != digest(
                    node["candidate"]["schedule_source"].encode()
                ):
                    raise ValueError("evaluated schedule differs from generated candidate")
                checkpoint(actual)
                retained[node["id"]] = actual
            else:
                retained[node["id"]] = parent_checkpoint
            terminals[node["id"]] = node["terminal_atom"]
        best_score = max(
            [world["root_score"]] + [n["score"] for n in world["nodes"] if n["status"] == "ok"]
        )
        attempts = len(world["nodes"])
        objective = (
            best_score
            - manifest["beta_cost"] * attempts
            + manifest["beta_parallel"] * attempts / max(1, len(expected_trace))
        )
        if world["best_score"] != best_score or world["objective"] != objective:
            raise ValueError("world aggregates differ from verified node scores and costs")
        world_reports.append(
            {
                "name": world["name"],
                "arm": world["arm"],
                "seed": world["seed"],
                "attempts": len(world["nodes"]),
                "failed_attempts": sum(n["status"] == "failed" for n in world["nodes"]),
                "best_validation_loss": -best_score,
                "objective": objective,
                "stop_reason": world["stop_reason"],
            }
        )

    selections = []
    previous_source = manifest["initial_policy"]
    for _, selection in tagged("selection"):
        phase = len(selections) + 1
        if selection["phase"] != phase or selection["world_names"] != (
            ["bootstrap"] + [f"adaptive-{index}" for index in range(1, phase)]
        ):
            raise ValueError("policy selection did not use the frozen adaptive history")
        if not selection["candidates"] or selection["candidates"][0]["source"] != previous_source:
            raise ValueError("policy selection did not include the deployed incumbent first")
        history = [
            next(w for w in worlds if w["name"] == name) for name in selection["world_names"]
        ]
        eligible = []
        for candidate in selection["candidates"]:
            if candidate["accepted"]:
                recomputed = evaluate_policy(
                    history,
                    candidate["source"],
                    workers=manifest["workers"],
                    max_rounds=manifest["attempts"],
                    beta_cost=manifest["beta_cost"],
                    beta_parallel=manifest["beta_parallel"],
                )
                if recomputed != candidate["evaluation"]:
                    raise ValueError("replay evaluation differs from recorded result")
                eligible.append(candidate)
        best = max(eligible, key=lambda c: c["evaluation"]["mean_score"])
        if selection["selected"] != best["name"] or selection["source"] != best["source"]:
            raise ValueError("selection differs from best replay score/incumbent tie rule")
        deployed = by_name.get(f"adaptive-{phase}")
        if deployed is not None and deployed["policy_source"] != best["source"]:
            raise ValueError("adaptive world did not redeploy the selected policy")
        previous_source = best["source"]
        if set(selection["controls"]) != set(manifest["controls"]):
            raise ValueError("replay controls differ from the frozen comparison")
        for name, source in manifest["controls"].items():
            recomputed = evaluate_policy(
                history,
                source,
                workers=manifest["workers"],
                max_rounds=manifest["attempts"],
                beta_cost=manifest["beta_cost"],
                beta_parallel=manifest["beta_parallel"],
            )
            if selection["controls"][name] != recomputed:
                raise ValueError("replay control differs from reconstructed evaluation")
        selections.append(
            {
                "phase": selection["phase"],
                "history_worlds": len(history),
                "selected": best["name"],
                "mean_replay_score": best["evaluation"]["mean_score"],
                "incumbent_score": selection["candidates"][0]["evaluation"]["mean_score"],
                "invalid_revisions": sum(not c["accepted"] for c in selection["candidates"]),
            }
        )

    agent_atoms = tagged("agent")
    token_usage = {
        key: 0
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
    }
    unknown_usage = 0
    for _, result in agent_atoms:
        if not result or not result.get("usage"):
            unknown_usage += 1
        else:
            for usage in result["usage"]:
                for key in token_usage:
                    token_usage[key] += usage.get(key, 0)
    tests = []
    for test in completion["tests"]:
        actual = records[test["atom_id"]]
        if actual != test["result"] or atoms[test["atom_id"]].return_code != 0:
            raise ValueError("final test differs from successful verified evidence")
        if any(a.finished_at > atoms[test["atom_id"]].started_at for a, _ in agent_atoms):
            raise ValueError("an agent ran after held-out test results became available")
        world = next(w for w in worlds if w["name"] == test["world"])
        choices = [(world["root_score"], "root", world["root_result"])]
        choices.extend(
            (n["score"], n["id"], n["result"]) for n in world["nodes"] if n["status"] == "ok"
        )
        winner = max(choices, key=lambda item: item[0])
        if (
            winner[1] != test["selected_node"]
            or winner[2]["checkpoint_sha256"] != actual["checkpoint_sha256"]
        ):
            raise ValueError("test checkpoint was not selected by validation only")
        checkpoint(actual)
        tests.append(
            {
                "world": test["world"],
                "selected_node": test["selected_node"],
                "test_loss": actual["test_loss"],
            }
        )
    evaluations = tagged("evaluation")
    missing_training = sum(
        requests[atom.id]["operation"] == "train"
        and (not result or "training_tokens" not in result or "training_seconds" not in result)
        for atom, result in evaluations
    )
    if completion["status"] == "complete" and (
        set(by_name) != set(expected_worlds)
        or len(selections) != manifest["rounds"] - 1
        or {test["world"] for test in tests} != set(expected_worlds) - {"bootstrap"}
        or len(tests) != 2 * (manifest["rounds"] - 1)
    ):
        raise ValueError("complete experiment is missing a world, policy phase, or held-out test")
    return {
        "status": completion["status"],
        "reason": completion.get("reason"),
        "root": str(root),
        "model": manifest["model"],
        "profile": manifest["profile"],
        "worlds": world_reports,
        "selections": selections,
        "tests": tests,
        "costs": {
            "agent_calls": len(agent_atoms),
            "tokens": token_usage,
            "agent_calls_without_usage": unknown_usage,
            "agent_command_seconds": sum(a.duration_seconds for a, _ in agent_atoms),
            "evaluator_command_seconds": sum(a.duration_seconds for a, _ in evaluations),
            "training_seconds": sum((r or {}).get("training_seconds", 0) for _, r in evaluations),
            "training_tokens": sum((r or {}).get("training_tokens", 0) for _, r in evaluations),
            "measured_training_complete": missing_training == 0,
            "training_evaluations_without_metrics": missing_training,
            "controller_wall_seconds": completion.get("wall_seconds"),
        },
        "verification": {
            **dataclasses.asdict(verification),
            "checkpoints_checked": len(checked_checkpoints),
            "replay_reconstructed": True,
        },
        "limitations": [
            "small local training adaptation, not author-code or paper-scale reproduction",
            "fixed depth-two online control; stronger live greedy/always-continue "
            "control required before a benefit claim",
            "single CPU worker; no measured parallel speedup",
            "all discovery and policy-development costs are real; "
            "replay-skipped work is not already saved compute",
        ],
    }
