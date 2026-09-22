# SPDX-License-Identifier: MPL-2.0
"""Local agent-driven Dream-RSI Section 3 training adaptation; never publishes."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from rsi_core import (
    FIXED_POLICY,
    GREEDY_POLICY,
    PUBLIC_FIELDS,
    SCHEMA,
    UNIFORM_POLICY,
    choose_policy,
    evaluate_policy,
    observation,
    validate_source,
    validate_world,
)
from rsi_io import checked_json, digest, write_json

from ocura_oss import Store, branch, initialize, run

HERE = Path(__file__).resolve().parent
INITIAL_SCHEDULE = "def learning_rate(step, total_steps):\n    return 0.002\n"
SOURCE_RULES = (
    "Return exactly one plain Python function, without annotations, imports, attributes, "
    "decorators, global variables, tools, commands, or file access. Allowed builtins: "
    "abs,min,max,sum,len,range,round,int,float,bool,list,tuple,dict,sorted,enumerate,zip,all,any. "
    "Use indexing, arithmetic, comparisons, if/for, lists and comprehensions. "
    "No .append or .get; use list concatenation and indexing. Code has an operation budget. "
    "Do not inspect any local files or use tools. Return only the JSON schema. "
)


class Deferred(RuntimeError):
    """Host capacity or the declared experiment allowance prevents more work."""


def capacity_snapshot():
    if os.name == "nt":
        command = (
            "$c=Get-CimInstance Win32_Processor; $o=Get-CimInstance Win32_OperatingSystem; "
            "@{cpu_percent=($c | Measure-Object LoadPercentage -Average).Average; "
            "free_mb=[math]::Floor($o.FreePhysicalMemory/1024)} | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )
        return json.loads(result.stdout)
    if Path("/proc/meminfo").exists():
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return {
            "cpu_percent": 100 * os.getloadavg()[0] / (os.cpu_count() or 1),
            "free_mb": int(memory["MemAvailable"].split()[0]) // 1024,
        }
    raise Deferred("Automatic host capacity check is unavailable on this OS")


def public_world(world):
    return {
        "schema": SCHEMA,
        "seed": world["seed"],
        "root_score": world["root_score"],
        "nodes": [{key: node[key] for key in PUBLIC_FIELDS} for node in world["nodes"]],
    }


def policy_prompt(history, candidates, options):
    """Present compact development feedback without changing policy observations."""
    if not history:
        raise ValueError("policy development requires at least one replay world")
    compact_history = [
        {
            "root_score": world["root_score"],
            "nodes": [
                {key: node[key] for key in ("id", "parent", "depth", "status", "score")}
                for node in world["nodes"]
            ],
        }
        for world in history
    ]
    versions = []
    for candidate in candidates:
        version = {
            key: candidate[key]
            for key in ("name", "source", "accepted", "error")
            if key in candidate
        }
        if "evaluation" in candidate:
            evaluation = candidate["evaluation"]
            worlds = []
            for report in evaluation["worlds"]:
                feedback = {
                    key: report[key]
                    for key in ("score", "best_score", "attempts", "rounds", "stop_reason")
                }
                feedback["trace"] = [
                    {
                        "round": step["round"],
                        "batch": step["batch"],
                        "revealed": [node["id"] for node in step["revealed"]],
                    }
                    for step in report["trace"]
                ]
                worlds.append(feedback)
            version["evaluation"] = {"mean_score": evaluation["mean_score"], "worlds": worlds}
        versions.append(version)
    context = {
        "objective": "best_score - beta_cost*N + beta_parallel*N/max(1,rounds)",
        "options": options,
        "history": compact_history,
        "earlier_versions": versions,
        "observation_example": observation(
            [],
            history[0]["root_score"],
            options["workers"],
            0,
            options["max_rounds"],
            options["beta_cost"],
            options["beta_parallel"],
        ),
    }
    return (
        SOURCE_RULES + "You are the fixed exploration-policy development agent. "
        "Make one small valid revision to the incumbent policy, in at most 20 lines. "
        "Write def choose(observation), returning unique IDs from observation['legal'], "
        "at most observation['workers']; [] stops the rollout. Use a simple general rule "
        "based on observed scores, depth and costs. Seek an improvement in the supplied "
        "mean replay objective; do not attempt an exhaustive optimization. "
        "Policies receive only the observed prefix, not this development history. "
        "The root opens a new branch; a leaf continues it, including failed leaves. "
        "Replay reveals the next recorded child, or nothing if none is available; "
        "every nonempty batch consumes a round. Do not hardcode node IDs, seeds, "
        "future outcomes or a particular tree. Return policy_source and a brief rationale. "
        "Development context:\n" + json.dumps(context, separators=(",", ":"))
    )


class Controller:
    def __init__(self, args):
        self.args = args
        self.root = args.root.resolve()
        self.started = time.monotonic()
        self.sequence = 0
        self.agent_calls = 0
        self.worlds = []
        self.selections = []
        self.tests = []
        self.root.mkdir(parents=True, exist_ok=False)
        initialize(self.root, name="Dream-RSI agent training pilot")
        self.store = Store(self.root)
        self.manifest = {
            "schema": SCHEMA,
            "task": "nanoGPT bounded LR-program search",
            "model": args.model,
            "effort": args.effort,
            "profile": args.profile,
            "seed": args.seed,
            "rounds": args.rounds,
            "attempts": args.attempts,
            "policy_revisions": args.policy_revisions,
            "updates": args.updates,
            "workers": 1,
            "beta_cost": args.beta_cost,
            "beta_parallel": args.beta_parallel,
            "max_seconds": args.max_seconds,
            "max_agent_calls": args.max_agent_calls,
            "agent_timeout": args.agent_timeout,
            "eval_timeout": args.eval_timeout,
            "max_host_load": args.max_host_load,
            "min_free_mb": args.min_free_mb,
            "capacity_wait": getattr(args, "capacity_wait", 0),
            "prepared": str(args.prepared.resolve()),
            "source_sha256": {p.name: digest(p.read_bytes()) for p in sorted(HERE.glob("*.py"))},
            "shared_inputs_sha256": digest(
                (HERE.parent / "dream_replay" / "common.py").read_bytes()
            ),
            "agent_workspace": "empty temporary directory, explicit prompt only; no user config",
            "initial_schedule": INITIAL_SCHEDULE,
            "initial_policy": FIXED_POLICY,
            "controls": {"fixed": FIXED_POLICY, "greedy": GREEDY_POLICY, "uniform": UNIFORM_POLICY},
            "comparison": "matched fresh seeds; separate histories after shared bootstrap",
            "test_policy": "all final tests deferred until every discovery/policy call is finished",
            "claim": "bounded method pilot; no assumed efficacy or paper-scale reproduction",
        }
        self.anchor, _ = self.record_value("manifest", self.manifest)

    def guard(self):
        wait_started = time.monotonic()
        wait_deadline = wait_started + getattr(self.args, "capacity_wait", 0)
        experiment_deadline = self.started + self.args.max_seconds
        busy_seen = False
        snapshot = None
        while True:
            now = time.monotonic()
            if now >= experiment_deadline:
                raise Deferred("experiment wall-time allowance exhausted")
            if busy_seen and now >= wait_deadline:
                raise Deferred(f"host busy; capacity-wait allowance exhausted: {snapshot}")
            try:
                snapshot = capacity_snapshot()
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise Deferred(f"host capacity could not be checked: {exc}") from exc
            now = time.monotonic()
            if now >= experiment_deadline:
                raise Deferred("experiment wall-time allowance exhausted")
            if busy_seen and now >= wait_deadline:
                raise Deferred(f"host busy; capacity-wait allowance exhausted: {snapshot}")
            if (
                snapshot["cpu_percent"] <= self.args.max_host_load
                and snapshot["free_mb"] >= self.args.min_free_mb
            ):
                if busy_seen:
                    print(
                        f"Host capacity available after {now - wait_started:.1f}s: {snapshot}",
                        file=sys.stderr,
                        flush=True,
                    )
                return snapshot
            remaining = min(wait_deadline, experiment_deadline) - now
            if not busy_seen:
                print(
                    f"Host busy: {snapshot}; waiting up to {max(0, remaining):.1f}s for capacity",
                    file=sys.stderr,
                    flush=True,
                )
                busy_seen = True
            if remaining <= 0:
                raise Deferred(f"host busy; capacity-wait allowance exhausted: {snapshot}")
            time.sleep(min(10, remaining))

    def remaining(self, limit):
        remaining = self.args.max_seconds - (time.monotonic() - self.started)
        if remaining < 2:
            raise Deferred("experiment wall-time allowance exhausted")
        return min(limit, remaining)

    def record_command(self, kind, command, parent=None, timeout=90, **labels):
        self.sequence += 1
        job_file = self.root / "requests" / f"{self.sequence:04d}-{kind}-job.json"
        job_hash = write_json(job_file, {"command": command, "timeout": timeout})
        labels = {
            "kind": kind,
            "job_file": str(job_file.relative_to(self.root)),
            "job_sha256": job_hash,
            **{key: str(value) for key, value in labels.items()},
        }
        pathway = None
        if parent is not None:
            pathway = branch(
                parent.chokepoint.id, root=self.root, reason=kind, parameters=labels
            ).id
        execution = run(
            [
                sys.executable,
                str(HERE / "rsi_io.py"),
                "--request",
                str(job_file),
                "--sha256",
                job_hash,
            ],
            root=self.root,
            pathway_id=pathway,
            parameters=labels,
        )
        raw = self.store.read_verified_log(execution.atom.id)
        self.store.read_verified_log(execution.atom.id, stream="stderr")
        result = None
        if raw.strip():
            with contextlib.suppress(ValueError):
                result = json.loads(raw)
        return execution, result

    def record_value(self, kind, value, parent=None):
        path = self.root / "requests" / f"{self.sequence + 1:04d}-{kind}.json"
        sha = write_json(path, value)
        execution, recorded = self.record_command(
            kind,
            [
                sys.executable,
                str(HERE / "experiment.py"),
                "emit",
                "--path",
                str(path),
                "--sha256",
                sha,
            ],
            parent,
            timeout=15,
            artifact_file=str(path.relative_to(self.root)),
            artifact_sha256=sha,
        )
        if execution.atom.return_code != 0 or recorded != value:
            raise ValueError(f"could not retain {kind} evidence")
        return execution, recorded

    def agent(self, role, prompt, parent):
        self.guard()
        if self.agent_calls >= self.args.max_agent_calls:
            raise Deferred("agent invocation allowance exhausted")
        self.agent_calls += 1
        timeout = self.remaining(self.args.agent_timeout)
        field = "schedule_source" if role == "discovery" else "policy_source"
        request = {
            "role": role,
            "model": self.args.model,
            "effort": self.args.effort,
            "field": field,
            "timeout": max(1, timeout - 3),
            "prompt": prompt,
        }
        path = self.root / "requests" / f"{self.sequence + 1:04d}-{role}.json"
        sha = write_json(path, request)
        return self.record_command(
            "agent",
            [sys.executable, str(HERE / "rsi_agent.py"), "--request", str(path), "--sha256", sha],
            parent,
            timeout=timeout,
            role=role,
            artifact_file=str(path.relative_to(self.root)),
            artifact_sha256=sha,
        )

    def evaluate(self, world_name, seed, operation, candidate, checkpoint, parent, node_id):
        host = self.guard()
        timeout = self.remaining(self.args.eval_timeout)
        request = {
            "operation": operation,
            "profile": self.args.profile,
            "prepared": str(self.args.prepared.resolve()),
            "seed": seed,
            "updates": self.args.updates if operation == "train" else 0,
            "total_steps": self.args.updates * self.args.attempts,
            "schedule_source": candidate["schedule_source"],
            "parent_checkpoint": checkpoint["checkpoint"] if checkpoint else None,
            "parent_checkpoint_sha256": checkpoint["checkpoint_sha256"] if checkpoint else None,
            "output_checkpoint": None
            if operation == "test"
            else str(self.root / "checkpoints" / f"{world_name}-{node_id}.pt"),
            "max_seconds": max(1, timeout - 3),
        }
        path = self.root / "requests" / f"{self.sequence + 1:04d}-{operation}.json"
        sha = write_json(path, request)
        execution, result = self.record_command(
            "evaluation",
            [
                sys.executable,
                str(HERE / "rsi_training.py"),
                "--request",
                str(path),
                "--sha256",
                sha,
            ],
            parent,
            timeout=timeout,
            operation=operation,
            world=world_name,
            node=node_id,
            artifact_file=str(path.relative_to(self.root)),
            artifact_sha256=sha,
            host_cpu=host["cpu_percent"],
            host_free_mb=host["free_mb"],
        )
        return execution, result

    def collect(self, name, seed, policy, history, arm):
        print(
            f"Starting {name}: at most {self.args.attempts} discovery attempts",
            file=sys.stderr,
            flush=True,
        )
        candidate = {"schedule_source": INITIAL_SCHEDULE, "rationale": "frozen initial schedule"}
        initial_execution, initial = self.evaluate(
            name, seed, "initialize", candidate, None, self.anchor, "root"
        )
        if initial_execution.atom.return_code != 0 or not initial:
            raise RuntimeError("initial evaluator failed; inspect retained stderr")
        world = {
            "schema": SCHEMA,
            "name": name,
            "arm": arm,
            "seed": seed,
            "root_score": initial["score"],
            "root_result": initial,
            "root_atom": initial_execution.atom.id,
            "policy_source": policy,
            "nodes": [],
            "trace": [],
            "stop_reason": "round_limit",
        }
        evidence = {"root": initial_execution}
        checkpoints = {"root": initial}
        candidates = {"root": candidate}
        for step in range(self.args.attempts):
            obs = observation(
                world["nodes"],
                world["root_score"],
                1,
                step,
                self.args.attempts,
                self.args.beta_cost,
                self.args.beta_parallel,
            )
            batch = choose_policy(policy, obs, timeout=5)
            action_execution, _ = self.record_value(
                "decision",
                {
                    "world": name,
                    "observation": obs,
                    "batch": batch,
                    "policy_sha256": digest(policy.encode()),
                },
                initial_execution,
            )
            if not batch:
                world["stop_reason"] = "policy_stop"
                break
            parent_id = batch[0]
            parent_node = next((n for n in world["nodes"] if n["id"] == parent_id), None)
            node_id = f"n{step + 1:03d}"
            context = {
                "task": "minimize fixed validation loss while training a tiny GPT",
                "parent_candidate": candidates[parent_id],
                "parent_feedback": parent_node
                if parent_node is None
                else {k: parent_node[k] for k in PUBLIC_FIELDS},
                "restored_step": checkpoints[parent_id]["step"],
                "next_updates": self.args.updates,
                "total_steps": self.args.updates * self.args.attempts,
                "current_observations": obs,
                "completed_history": [public_world(w) for w in history],
            }
            prompt = (
                SOURCE_RULES + "You are the fixed discovery agent. Propose a learning-rate "
                "program def learning_rate(step, total_steps) for this candidate, improving "
                "its parent's program using the observed validation feedback. Every returned "
                "rate must be finite and between 0.00001 and 0.02 inclusive for every global "
                "step. Model, optimizer, data, evaluation, dropout and token budget are fixed. "
                "The parent checkpoint and optimizer/RNG state are restored; step is global. "
                "Opening root restores the initial checkpoint. No test scores are available. "
                "Return schedule_source and a short rationale. Context:\n" + json.dumps(context)
            )
            agent_execution, proposal = self.agent("discovery", prompt, evidence[parent_id])
            result, terminal = None, agent_execution
            new_candidate = candidates[parent_id]
            diagnostic = "agent invocation failed"
            try:
                if agent_execution.atom.return_code != 0 or not proposal or not proposal["ok"]:
                    raise ValueError(proposal.get("error", diagnostic) if proposal else diagnostic)
                new_candidate = proposal["response"]
                validate_source(new_candidate["schedule_source"], "learning_rate")
                terminal, result = self.evaluate(
                    name,
                    seed,
                    "train",
                    new_candidate,
                    checkpoints[parent_id],
                    agent_execution,
                    node_id,
                )
                if terminal.atom.return_code != 0 or result is None:
                    error = self.store.read_verified_log(terminal.atom.id, stream="stderr").decode(
                        errors="replace"
                    )
                    raise ValueError("evaluator failed: " + error[-1200:])
                diagnostic = (
                    f"validation_loss={result['validation_loss']:.8f}; step={result['step']}"
                )
            except (ValueError, KeyError) as exc:
                diagnostic = str(exc)[:1600]
                result = None
            node = {
                "id": node_id,
                "parent": parent_id,
                "depth": (parent_node["depth"] if parent_node else 0) + 1,
                "score": result["score"] if result else None,
                "status": "ok" if result else "failed",
                "candidate": new_candidate,
                "diagnostic": diagnostic,
                "agent_atom": agent_execution.atom.id,
                "terminal_atom": terminal.atom.id,
                "decision_atom": action_execution.atom.id,
                "result": result,
                "input_checkpoint_sha256": checkpoints[parent_id]["checkpoint_sha256"],
            }
            world["nodes"].append(node)
            world["trace"].append({"round": step + 1, "batch": batch, "revealed": [node_id]})
            evidence[node_id] = terminal
            checkpoints[node_id] = result or checkpoints[parent_id]
            candidates[node_id] = new_candidate
            # Every completed node survives interruption independently of a final world snapshot.
            self.record_value("node", {"world": name, "node": node}, terminal)
            print(f"  {name}/{node_id}: {diagnostic}", file=sys.stderr, flush=True)
        validate_world(world)
        world["best_score"] = max(
            [world["root_score"]] + [n["score"] for n in world["nodes"] if n["status"] == "ok"]
        )
        world["objective"] = (
            world["best_score"]
            - self.args.beta_cost * len(world["nodes"])
            + self.args.beta_parallel * len(world["nodes"]) / max(1, len(world["trace"]))
        )
        self.record_value("world", world, initial_execution)
        self.worlds.append(world)
        return world

    def improve(self, history, incumbent, phase):
        options = dict(
            workers=1,
            max_rounds=self.args.attempts,
            beta_cost=self.args.beta_cost,
            beta_parallel=self.args.beta_parallel,
        )
        frozen = [public_world(w) for w in history]
        candidates = [
            {
                "name": "incumbent",
                "source": incumbent,
                "evaluation": evaluate_policy(frozen, incumbent, **options),
                "accepted": True,
            }
        ]
        for revision in range(self.args.policy_revisions):
            prompt = policy_prompt(frozen, candidates, options)
            execution, proposal = self.agent("policy", prompt, self.anchor)
            entry = {
                "name": f"phase-{phase}-revision-{revision + 1}",
                "agent_atom": execution.atom.id,
                "accepted": False,
            }
            try:
                if execution.atom.return_code != 0 or not proposal or not proposal["ok"]:
                    raise ValueError("policy agent failed")
                entry["source"] = proposal["response"]["policy_source"]
                entry["rationale"] = proposal["response"]["rationale"]
                entry["evaluation"] = evaluate_policy(frozen, entry["source"], **options)
                entry["accepted"] = True
            except (ValueError, KeyError) as exc:
                entry["error"] = str(exc)
            candidates.append(entry)
        eligible = [c for c in candidates if c["accepted"]]
        selected = max(eligible, key=lambda c: c["evaluation"]["mean_score"])
        controls = {
            name: evaluate_policy(frozen, source, **options)
            for name, source in self.manifest["controls"].items()
        }
        selection = {
            "phase": phase,
            "world_names": [w["name"] for w in history],
            "candidates": candidates,
            "selected": selected["name"],
            "source": selected["source"],
            "controls": controls,
            "replay_training_calls": 0,
            "replay_agent_calls": 0,
        }
        self.record_value("selection", selection, self.anchor)
        self.selections.append(selection)
        return selected["source"]

    def final_tests(self):
        for world in self.worlds:
            if world["arm"] == "bootstrap":
                continue
            choices = [
                (
                    world["root_score"],
                    "root",
                    world["root_result"],
                    world["root_atom"],
                    {"schedule_source": INITIAL_SCHEDULE},
                )
            ]
            choices.extend(
                (n["score"], n["id"], n["result"], n["terminal_atom"], n["candidate"])
                for n in world["nodes"]
                if n["status"] == "ok"
            )
            _, node_id, checkpoint, atom_id, candidate = max(choices, key=lambda x: x[0])
            atom = self.store.load_atom(atom_id)
            chokepoint = next(c for c in self.store.list_chokepoints() if c.atom_id == atom_id)
            from ocura_oss import RunExecution

            execution, result = self.evaluate(
                world["name"],
                world["seed"],
                "test",
                candidate,
                checkpoint,
                RunExecution(atom, chokepoint),
                node_id,
            )
            if execution.atom.return_code != 0 or not result:
                raise RuntimeError("final test failed; inspect retained evidence")
            self.tests.append(
                {
                    "world": world["name"],
                    "selected_node": node_id,
                    "selection_metric": "validation_loss",
                    "result": result,
                    "atom_id": execution.atom.id,
                }
            )

    def execute(self):
        status, reason = "complete", None
        try:
            self.guard()
            first = self.collect("bootstrap", self.args.seed, FIXED_POLICY, [], "bootstrap")
            adaptive, controls, policy = [first], [first], FIXED_POLICY
            for phase in range(1, self.args.rounds):
                policy = self.improve(adaptive, policy, phase)
                seed = self.args.seed + phase
                adaptive.append(
                    self.collect(f"adaptive-{phase}", seed, policy, adaptive, "adaptive")
                )
                controls.append(
                    self.collect(f"fixed-{phase}", seed, FIXED_POLICY, controls, "fixed")
                )
            self.final_tests()
        except Deferred as exc:
            status, reason = "deferred", str(exc)
        except KeyboardInterrupt:
            status, reason = "interrupted", "user interrupted the experiment"
        except Exception as exc:
            status, reason = "failed", str(exc)
        finally:
            self.record_value(
                "completion",
                {
                    "status": status,
                    "reason": reason,
                    "wall_seconds": time.monotonic() - self.started,
                    "agent_calls": self.agent_calls,
                    "tests": self.tests,
                },
                self.anchor,
            )
        from rsi_report import build_report

        report = build_report(self.root)
        write_json(self.root / "report.json", report)
        return report


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    live = subs.add_parser("run")
    live.add_argument("--root", required=True, type=Path)
    live.add_argument("--prepared", required=True, type=Path)
    live.add_argument("--model", required=True)
    live.add_argument(
        "--effort", choices=("low", "medium", "high", "xhigh", "max"), default="medium"
    )
    live.add_argument("--profile", choices=("smoke", "cpu"), default="smoke")
    live.add_argument("--seed", type=int, default=1101)
    live.add_argument("--rounds", type=int, default=3)
    live.add_argument("--attempts", type=int, default=3)
    live.add_argument("--updates", type=int, default=16)
    live.add_argument("--policy-revisions", type=int, default=2)
    live.add_argument("--max-agent-calls", type=int, default=20)
    live.add_argument("--max-seconds", type=float, default=900)
    live.add_argument("--agent-timeout", type=float, default=90)
    live.add_argument("--eval-timeout", type=float, default=60)
    live.add_argument("--max-host-load", type=float, default=50)
    live.add_argument("--min-free-mb", type=float, default=3072)
    live.add_argument("--capacity-wait", type=float, default=60)
    live.add_argument("--beta-cost", type=float, default=0.01)
    live.add_argument("--beta-parallel", type=float, default=0)
    report = subs.add_parser("report")
    report.add_argument("--root", required=True, type=Path)
    emit = subs.add_parser("emit")
    emit.add_argument("--path", type=Path, required=True)
    emit.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.action == "run":
        if (
            args.rounds < 2
            or args.attempts < 1
            or args.policy_revisions < 1
            or not 1 <= args.updates <= 256
            or args.max_agent_calls < 1
        ):
            parser.error(
                "use at least two rounds, positive attempts/revisions/agent calls, "
                "and 1 to 256 updates per attempt"
            )
        if args.updates * args.attempts > 4096:
            parser.error("the training horizon may not exceed 4096 updates")
        if args.seed < 0 or args.seed + args.rounds >= 2**32:
            parser.error("seeds must fit unsigned 32-bit integers")
        for name in (
            "max_seconds",
            "agent_timeout",
            "eval_timeout",
            "max_host_load",
            "min_free_mb",
        ):
            if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
                parser.error(f"{name} must be positive and finite")
        for name in ("beta_cost", "beta_parallel", "capacity_wait"):
            if not math.isfinite(getattr(args, name)) or getattr(args, name) < 0:
                parser.error(f"{name} must be nonnegative and finite")
    return args


def main():
    args = arguments()
    if args.action == "emit":
        print(json.dumps(checked_json(args.path, args.sha256), allow_nan=False))
        return 0
    if args.action == "report":
        from rsi_report import build_report

        result = build_report(args.root)
    else:
        result = Controller(args).execute()
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result.get("status") == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
