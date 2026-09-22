# SPDX-License-Identifier: MPL-2.0
"""Cheap evidence/policy tests; real CPU continuation is explicitly opt-in."""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ocura_oss import Store, branch, run

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "dream_replay"
sys.path.insert(0, str(EXAMPLE))
common = importlib.import_module("common")
policies = importlib.import_module("policies")
replay = importlib.import_module("replay")
experiment = importlib.import_module("experiment")


def fixture_manifest():
    config = common.configuration("smoke")
    return {
        "schema": common.SCHEMA,
        "kind": "manifest",
        "purpose": "collection",
        "config": config,
        "seed": 11,
        "order": [r["name"] for r in config["recipes"]],
    }


def fixture_history():
    manifest = fixture_manifest()
    nodes = {}
    curves = ([4, 3, 2, 1], [3, 2.5, 2.3, 2.2], [5, 4, 3, 2], [4.5, 4, 3.5, 3])
    for name, curve in zip(manifest["order"], curves, strict=True):
        for q, loss in enumerate(curve, 1):
            nodes[(name, q)] = {
                "atom_id": f"{name}-{q}",
                "recipe": name,
                "quantum": q,
                "result": {"validation_loss": loss},
                "seconds": 1.0,
            }
    return {
        "manifest": manifest,
        "nodes": nodes,
        "total_command_seconds": 16.0,
        "reserved_training_tokens": 16 * common.quantum_tokens(manifest),
    }


class ReplayPolicyTests(unittest.TestCase):
    def test_controls_obey_prefix_and_budget(self):
        history = fixture_history()
        for policy in policies.candidates(list(history["manifest"]["order"])):
            result = replay.simulate(history, policy, 8)
            levels = {}
            for node in result["trace"]:
                levels[node["recipe"]] = levels.get(node["recipe"], 0) + 1
                self.assertEqual(node["quantum"], levels[node["recipe"]])
            self.assertEqual(result["consumed_quanta"], 8)
            self.assertEqual(result["reserved_training_tokens"], 8 * 64)

    def test_policy_receives_no_hidden_nodes_and_future_changes_do_not_leak(self):
        history = fixture_history()
        changed = copy.deepcopy(history)
        for (_, q), node in changed["nodes"].items():
            if q > 1:
                node["result"]["validation_loss"] = 1000
        policy = {"name": "greedy", "mode": "greedy", "explore": 4}
        self.assertEqual(replay.simulate(history, policy, 4), replay.simulate(changed, policy, 4))
        real_choose = replay.choose
        calls = []

        def observe(policy, order, horizon, seen, legal):
            self.assertIsInstance(seen, tuple)
            self.assertTrue(all(isinstance(o, policies.Observation) for o in seen))
            calls.append(len(seen))
            return real_choose(policy, order, horizon, seen, legal)

        with patch.object(replay, "choose", observe), patch("subprocess.Popen") as launch:
            replay.simulate(history, policy, 8)
        launch.assert_not_called()
        self.assertEqual(calls, list(range(8)))

    def test_missing_future_is_coverage_error_not_a_fabricated_loss(self):
        history = fixture_history()
        del history["nodes"][("constant", 2)]
        with self.assertRaises(replay.CoverageError):
            replay.simulate(history, {"mode": "uniform"}, 8)
        with self.assertRaises(replay.CoverageError):
            replay.select(history, 8)

    def test_failed_attempt_consumes_budget_and_closes_branch(self):
        history = fixture_history()
        history["nodes"][("constant", 1)]["result"] = None
        result = replay.simulate(history, {"mode": "uniform"}, 8)
        self.assertEqual(result["consumed_quanta"], 8)
        self.assertEqual(result["trace"][0]["loss"], None)
        self.assertEqual(sum(n["recipe"] == "constant" for n in result["trace"]), 1)

    def test_illegal_action_and_invalid_budget_rejected(self):
        with (
            patch.object(replay, "choose", return_value="not-a-recipe"),
            self.assertRaisesRegex(ValueError, "illegal"),
        ):
            replay.simulate(fixture_history(), {"mode": "uniform"}, 8)
        with self.assertRaisesRegex(ValueError, "positive"):
            replay.simulate(fixture_history(), {"mode": "uniform"}, 0)

    def test_selection_keeps_incumbent_and_ties_are_deterministic(self):
        history = fixture_history()
        for node in history["nodes"].values():
            node["result"]["validation_loss"] = 1.0
        selection = replay.select(history, 8)
        self.assertEqual(selection["selected_policy"]["name"], "uniform")
        self.assertEqual(selection["training_invocations"], 0)
        self.assertEqual(selection["task_evaluations"], 0)
        history["manifest"]["purpose"] = "evaluation"
        with self.assertRaisesRegex(ValueError, "development"):
            replay.select(history, 8)


class ReplayEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "history"
        self.manifest = fixture_manifest()
        self.parent, self.manifest_hash = experiment.start_world(self.root, self.manifest)

    def segment(self, *, parent=None, level=1, input_digest=None, failure=False, **overrides):
        labels = {
            "kind": "segment",
            "recipe": "constant",
            "start": str((level - 1) * 2),
            "end": str(level * 2),
            "manifest_sha256": self.manifest_hash,
        }
        result = {
            "kind": "segment",
            "manifest_sha256": self.manifest_hash,
            "recipe": "constant",
            "start": (level - 1) * 2,
            "end": level * 2,
            "training_tokens": 64,
            "validation_loss": 3.0,
            "runtime": {"fixture": True},
            "checkpoint_sha256": "a" * 64,
            "batch_trace_sha256": "b" * 64,
            "checkpoint": "checkpoints/constant.pt",
            "input_sha256": input_digest,
            **overrides,
        }
        child = branch(
            (parent or self.parent).chokepoint.id,
            root=self.root,
            reason="fixture segment",
            parameters=labels,
        )
        execution = run(
            [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1]); sys.exit(int(sys.argv[2]))",
                json.dumps(result),
                "1" if failure else "0",
            ],
            root=self.root,
            pathway_id=child.id,
            parameters=labels,
        )
        return execution

    def test_report_and_replay_do_not_need_artifacts_or_torch(self):
        self.segment()
        # Fail immediately if a framework import or external command is attempted by report.
        code = (
            "import builtins, sys; original = builtins.__import__; "
            'exec("def guarded(name, *a, **kw):\\n'
            " if name.split('.')[0] in ('torch', 'numpy'): raise AssertionError(name)\\n"
            ' return original(name, *a, **kw)"); '
            "builtins.__import__ = guarded; "
            f"sys.path.insert(0, {str(EXAMPLE)!r}); import experiment; "
            f"raise SystemExit(experiment.main(['report', '--root', {str(self.root)!r}]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["segments"], 1)
        self.assertFalse((self.root / "checkpoints").exists())
        history = replay.load_history(self.root)
        self.assertEqual(replay.simulate(history, {"mode": "uniform"}, 1)["consumed_quanta"], 1)

    def test_changed_log_fails_before_policy_execution(self):
        execution = self.segment()
        path = Store(self.root).resolve_log_path(execution.atom, "stdout")
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(Exception, "verification failed"):
            replay.load_history(self.root)

    def test_resume_requires_parent_digest_and_step_lineage(self):
        parent = self.segment()
        self.segment(parent=parent, level=2, input_digest="c" * 64)
        with self.assertRaisesRegex(ValueError, "input digest"):
            replay.load_history(self.root)

    def test_later_segment_cannot_branch_directly_from_manifest(self):
        self.segment()
        self.segment(level=2, input_digest="a" * 64)
        with self.assertRaisesRegex(ValueError, "parent/step"):
            replay.load_history(self.root)

    def test_nonfinite_loss_rejected(self):
        self.segment(validation_loss=float("nan"))
        with self.assertRaisesRegex(ValueError, "finite"):
            replay.load_history(self.root)

    def test_duplicate_segment_rejected(self):
        self.segment()
        self.segment()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            replay.load_history(self.root)

    def test_failed_command_is_retained_and_costed(self):
        self.segment(failure=True)
        history = replay.load_history(self.root)
        self.assertIsNone(history["nodes"][("constant", 1)]["result"])
        self.assertEqual(history["reserved_training_tokens"], 64)

    def test_existing_destinations_and_escaping_artifacts_rejected(self):
        before = (self.root / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            experiment.start_world(self.root, self.manifest)
        self.assertEqual(before, (self.root / "manifest.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "inside"):
            common.artifact_path(self.root, "../checkpoint.pt")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            common.checked_bytes(self.root / "manifest.json", "0" * 64)


class OrchestrationTests(unittest.TestCase):
    def test_frozen_policy_fresh_execution_and_final_scoring_with_synthetic_worker(self):
        """Exercise actual Ocura commands/lineage without launching a tensor workload."""

        def make_manifest(prepared, profile, seed, purpose, **extra):
            return {
                **fixture_manifest(),
                "seed": seed,
                "purpose": purpose,
                "prepared": str(prepared),
                "profile": profile,
                "dataset": {"fixture": True},
                "adapter_sha256": "fixture",
                **extra,
            }

        def worker(
            root,
            manifest,
            manifest_hash,
            parent,
            recipe,
            level,
            allowance,
            previous=None,
            test_only=False,
        ):
            start, end = (level - 1) * 2, level * 2
            if test_only:
                start = end
            kind = "final-test" if test_only else "segment"
            labels = {
                "kind": kind,
                "recipe": recipe,
                "start": str(start),
                "end": str(end),
                "manifest_sha256": manifest_hash,
            }
            result = {
                "kind": kind,
                "recipe": recipe,
                "start": start,
                "end": end,
                "manifest_sha256": manifest_hash,
                "training_tokens": 64,
                "runtime": {"synthetic": True},
                "validation_loss": 5 - level + manifest["order"].index(recipe) / 10,
                "test_loss": 4.2,
                "checkpoint": f"checkpoints/{recipe}-{level}.pt",
                "checkpoint_sha256": common.digest(f"{recipe}-{level}".encode()),
                "batch_trace_sha256": "b" * 64,
                "input_sha256": previous["checkpoint_sha256"] if previous else None,
            }
            child = branch(parent.chokepoint.id, root=root, reason=kind, parameters=labels)
            execution = run(
                [sys.executable, "-c", "import sys; print(sys.argv[1])", json.dumps(result)],
                root=root,
                pathway_id=child.id,
                parameters=labels,
            )
            return execution, result

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(experiment, "run_segment", worker),
            patch.object(experiment, "make_manifest", make_manifest),
            patch.dict(sys.modules, {"torch": None}),
        ):
            root = Path(temporary)
            history_root = root / "history"
            manifest = make_manifest(root, "smoke", 11, "collection")
            experiment.execute_world(history_root, manifest, {"mode": "uniform"}, 16, 30)
            args = SimpleNamespace(
                history=history_root,
                seed=11,
                budget_quanta=8,
                prepared=None,
                root=root / "fresh",
                max_seconds=30,
            )
            with self.assertRaisesRegex(ValueError, "differ"):
                experiment.evaluate(args)
            self.assertFalse(args.root.exists())
            args.seed = 201
            fresh = experiment.evaluate(args)
            restored = experiment.report(Path(fresh["arms"]["selected"]["root"]))
            self.assertEqual(restored, fresh["arms"]["selected"])
            self.assertEqual(restored["manifest"]["policy"], fresh["selection"]["selected_policy"])
            self.assertEqual(restored["segments"], 8)
            self.assertEqual(restored["final_tests"][0]["result"]["test_loss"], 4.2)
            recorded = replay.load_history(Path(restored["root"]))
            played = replay.simulate(recorded, restored["manifest"]["policy"], 8)
            self.assertEqual(played["best_validation_loss"], restored["best_validation_loss"])
            self.assertEqual(fresh["arms"]["baseline"]["same_policy_as"], "selected")


@unittest.skipUnless(
    os.environ.get("OCURA_DREAM_TESTS") == "cpu",
    "set OCURA_DREAM_TESTS=cpu and OCURA_DREAM_PREPARED; serial CPU checks",
)
class DreamCPUIntegrationTests(unittest.TestCase):
    def test_exact_continuation_interleaving_and_corrupt_checkpoint(self):
        import torch

        torch.set_num_threads(1)
        prepared = Path(os.environ["OCURA_DREAM_PREPARED"]).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = experiment.make_manifest(prepared, "smoke", 17, "collection")
            # Tiny shape, full 128-update continuation test, nonzero dropout and cosine LR.
            manifest["config"]["quantum"] = 32
            common.write_json(root / "manifest.json", manifest)
            manifest_hash = common.digest(common.encoded(manifest))

            def segment(start, end, name, previous=None, seed_recipe="dropout", fail=False):
                command = [
                    sys.executable,
                    str(EXAMPLE / "train_segment.py"),
                    "--manifest",
                    str(root / "manifest.json"),
                    "--manifest-sha256",
                    manifest_hash,
                    "--recipe",
                    seed_recipe,
                    "--start",
                    str(start),
                    "--end",
                    str(end),
                    "--output",
                    f"checkpoints/{name}.pt",
                    "--max-seconds",
                    "30",
                ]
                if previous:
                    command += [
                        "--checkpoint",
                        previous["checkpoint"],
                        "--checkpoint-sha256",
                        previous["checkpoint_sha256"],
                    ]
                completed = subprocess.run(command, capture_output=True, text=True, timeout=40)
                self.assertEqual(completed.returncode, 2 if fail else 0, completed.stderr)
                return completed if fail else json.loads(completed.stdout)

            uninterrupted = segment(0, 128, "continuous")
            previous = None
            resumed_parts = []
            for start in range(0, 128, 32):
                previous = segment(start, start + 32, f"part-{start}", previous)
                resumed_parts.append(previous)
                if start == 32:
                    segment(0, 32, "unrelated", seed_recipe="constant")

            def state(result):
                return torch.load(root / result["checkpoint"], weights_only=True)

            def equal(a, b):
                if isinstance(a, torch.Tensor):
                    self.assertTrue(torch.equal(a, b))
                elif isinstance(a, dict):
                    self.assertEqual(a.keys(), b.keys())
                    for key in a:
                        equal(a[key], b[key])
                elif isinstance(a, (list, tuple)):
                    self.assertEqual(len(a), len(b))
                    for x, y in zip(a, b, strict=True):
                        equal(x, y)
                else:
                    self.assertEqual(a, b)

            equal(state(uninterrupted), state(previous))
            self.assertEqual(uninterrupted["validation_loss"], previous["validation_loss"])
            # Repeating a prefix reproduces the sampled batch order as well as state.
            repeated = segment(0, 32, "repeated")
            self.assertEqual(repeated["batch_trace_sha256"], resumed_parts[0]["batch_trace_sha256"])
            parent = root / resumed_parts[0]["checkpoint"]
            parent.write_bytes(parent.read_bytes() + b"changed")
            failed = segment(32, 64, "corrupt", resumed_parts[0], fail=True)
            self.assertIn("digest mismatch", failed.stderr)
            self.assertFalse((root / "checkpoints/corrupt.pt").exists())

    def test_full_smoke_collection_replay_and_fresh_execution(self):
        prepared = Path(os.environ["OCURA_DREAM_PREPARED"]).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def command(*args):
                completed = subprocess.run(
                    [sys.executable, str(EXAMPLE / "experiment.py"), *map(str, args)],
                    capture_output=True,
                    text=True,
                    timeout=150,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)

            history = root / "history"
            collected = command(
                "collect",
                "--root",
                history,
                "--prepared",
                prepared,
                "--profile",
                "smoke",
                "--max-seconds",
                120,
            )
            self.assertEqual(collected["segments"], 16)
            self.assertEqual(collected["failed_segments"], 0)
            selected = command("replay", "--root", history)
            self.assertEqual(selected["training_invocations"], 0)
            fresh = command(
                "evaluate",
                "--history",
                history,
                "--root",
                root / "fresh",
                "--seed",
                201,
                "--max-seconds",
                120,
            )
            selected_root = Path(fresh["arms"]["selected"]["root"])
            restored = command("report", "--root", selected_root)
            self.assertEqual(restored, fresh["arms"]["selected"])
            self.assertEqual(restored["segments"], 8)
            self.assertEqual(len(restored["final_tests"]), 1)
            frozen_policy = restored["manifest"]["policy"]
            played = replay.simulate(replay.load_history(selected_root), frozen_policy, 8)
            self.assertEqual(played["best_validation_loss"], restored["best_validation_loss"])
