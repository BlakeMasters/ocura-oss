# SPDX-License-Identifier: MPL-2.0
"""Deterministic Dream-RSI simulator and generated-code contract tests."""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "dream_rsi"
spec = importlib.util.spec_from_file_location("dream_rsi_core_tested", EXAMPLE / "rsi_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def node(identity, parent="root", score=1.0, depth=1, *, failed=False):
    return {
        "id": identity,
        "parent": parent,
        "score": None if failed else score,
        "status": "failed" if failed else "ok",
        "depth": depth,
        "candidate": {
            "schedule_source": "def learning_rate(step, total_steps):\n    return 0.001\n"
        },
        "diagnostic": "failed evaluation" if failed else "measured",
        "private_atom_id": f"secret-{identity}",
    }


def world(nodes=None, *, root_score=0.0):
    return {
        "schema": core.SCHEMA,
        "seed": 17,
        "root_score": root_score,
        "nodes": nodes
        if nodes is not None
        else [
            node("a", score=2),
            node("b", score=1),
            node("a2", "a", 3, 2),
            node("b2", "b", 8, 2),
        ],
        "private_future_summary": "b2 is the winner",
    }


def obs(nodes=None, *, workers=1, round_index=0):
    return core.observation(nodes or [], 0.0, workers, round_index, 8, 0.1, 0.2)


ROOT_ONLY = 'def choose(observation):\n    return ["root"]\n'
STOP = "def choose(observation):\n    return []\n"


class WorldValidationTests(unittest.TestCase):
    def test_valid_world_and_empty_world(self):
        core.validate_world(world())
        core.validate_world(world([]))

    def test_invalid_parent_order_fanout_depth_and_duplicate(self):
        invalids = [
            [node("a", "later", depth=2)],
            [node("a"), node("a")],
            [node("a"), node("b", "a", depth=2), node("c", "a", depth=2)],
            [node("a", depth=2)],
            [node("root")],
        ]
        for nodes in invalids:
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                core.validate_world(world(nodes))

    def test_scores_must_be_finite_and_failure_has_no_score(self):
        for value in (float("nan"), float("inf"), True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.validate_world(world(root_score=value))
        for value in (float("nan"), float("inf"), True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.validate_world(world([node("a", score=value)]))
        broken = node("a", failed=True)
        broken["score"] = 0.0
        with self.assertRaisesRegex(ValueError, "null score"):
            core.validate_world(world([broken]))

    def test_failed_parent_can_have_a_child(self):
        core.validate_world(world([node("a", failed=True), node("a2", "a", 4, 2)]))


class ObservationTests(unittest.TestCase):
    def test_only_public_fields_and_current_leaves_are_exposed(self):
        nodes = [node("a"), node("b", failed=True), node("a2", "a", 3, 2)]
        result = obs(nodes)
        self.assertEqual(result["legal"], ["root", "b", "a2"])
        self.assertEqual(
            set(result),
            {
                "nodes",
                "legal",
                "workers",
                "round",
                "max_rounds",
                "root_score",
                "beta_cost",
                "beta_parallel",
            },
        )
        self.assertTrue(all(set(item) == set(core.PUBLIC_FIELDS) for item in result["nodes"]))
        result["nodes"][0]["candidate"]["schedule_source"] = "mutated"
        self.assertNotEqual(nodes[0]["candidate"]["schedule_source"], "mutated")

    def test_valid_batches_and_stop(self):
        visible = obs([node("a")], workers=2)
        for batch in ([], ["root"], ["a", "root"]):
            core.validate_batch(batch, visible)

    def test_duplicate_unknown_nonleaf_and_oversized_batches_are_rejected(self):
        visible = obs([node("a"), node("a2", "a", depth=2)])
        for batch in (["a"], ["hidden"], ["root", "a2"], ["root", "root"], "root", [1], ("root",)):
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                core.validate_batch(batch, visible)

    def test_invalid_limits_and_coefficients(self):
        for field, value in (
            ("workers", 0),
            ("round_index", -1),
            ("max_rounds", True),
            ("beta_cost", -1),
            ("beta_parallel", float("nan")),
        ):
            args = dict(
                nodes=[],
                root_score=0,
                workers=1,
                round_index=0,
                max_rounds=8,
                beta_cost=0,
                beta_parallel=0,
            )
            args[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                core.observation(**args)


class ReplayTests(unittest.TestCase):
    def test_root_opens_earliest_unseen_branch(self):
        result = core.replay(world(), ROOT_ONLY, max_rounds=2)
        self.assertEqual([step["revealed"][0]["id"] for step in result["trace"]], ["a", "b"])
        self.assertEqual((result["attempts"], result["rounds"], result["best_score"]), (2, 2, 2))

    def test_failed_attempt_is_counted_and_can_be_refined(self):
        source = 'def choose(observation):\n    return [observation["legal"][-1]]\n'
        result = core.replay(
            world([node("a", failed=True), node("a2", "a", 4, 2)]), source, beta_cost=0.25
        )
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["score"], 3.5)
        self.assertEqual(result["stop_reason"], "exhausted")

    def test_empty_continuation_consumes_round_without_synthetic_observation(self):
        source = (
            'def choose(observation):\n    return ["root"] if not observation["nodes"] else ["a"]\n'
        )
        result = core.replay(
            world([node("a"), node("b", score=10)]),
            source,
            max_rounds=4,
            beta_cost=0.25,
            beta_parallel=1.0,
        )
        self.assertEqual((result["attempts"], result["rounds"], result["best_score"]), (1, 4, 1))
        self.assertEqual(result["score"], 1.0)
        self.assertTrue(all(step["revealed"] == [] for step in result["trace"][1:]))
        self.assertEqual(result["stop_reason"], "round_limit")

    def test_batching_cost_and_parallelism_follow_equation_one(self):
        source = """def choose(observation):
    if observation["round"] == 0:
        return ["root"]
    if observation["round"] == 1:
        return ["a", "root"]
    return ["b"]
"""
        result = core.replay(world(), source, workers=2, beta_cost=0.5, beta_parallel=3.0)
        self.assertEqual((result["attempts"], result["rounds"], result["best_score"]), (4, 3, 8))
        self.assertEqual(result["score"], 8 - 0.5 * 4 + 3 * 4 / 3)
        self.assertEqual(result["stop_reason"], "exhausted")

    def test_stop_costs_zero_rounds_and_root_is_a_meaningful_fallback(self):
        result = core.replay(world(root_score=2.75), STOP, beta_cost=1, beta_parallel=1)
        self.assertEqual((result["attempts"], result["rounds"], result["score"]), (0, 0, 2.75))
        self.assertEqual(result["stop_reason"], "policy_stop")
        failed = core.replay(
            world([node("a", failed=True)], root_score=2.75), ROOT_ONLY, beta_cost=0.25
        )
        self.assertEqual((failed["best_score"], failed["score"]), (2.75, 2.5))

    def test_stop_after_one_round_and_empty_world(self):
        source = (
            'def choose(observation):\n    return ["root"] if observation["round"] == 0 else []\n'
        )
        result = core.replay(world(), source)
        self.assertEqual((result["attempts"], result["rounds"]), (1, 1))
        self.assertEqual(result["stop_reason"], "policy_stop")
        result = core.replay(world([]), 'def choose(observation):\n    return ["illegal"]\n')
        self.assertEqual((result["attempts"], result["rounds"]), (0, 0))
        self.assertEqual(result["stop_reason"], "exhausted")

    def test_hidden_future_cannot_change_prefix_decisions(self):
        original = world()
        changed = copy.deepcopy(original)
        changed["nodes"][-1]["score"] = 1000
        changed["private_future_summary"] = "changed"
        first = core.replay(original, core.FIXED_POLICY, max_rounds=1)
        second = core.replay(changed, core.FIXED_POLICY, max_rounds=1)
        self.assertEqual(first, second)
        # Policy cannot inspect complete tree length, coverage, or artifact paths.
        checking = """def choose(observation):
    if len(observation) != 8:
        return ["bad-contract"]
    for node in observation["nodes"]:
        if len(node) != 7:
            return ["private-field-leak"]
    return ["root"]
"""
        self.assertEqual(core.replay(original, checking, max_rounds=2)["attempts"], 2)

    def test_all_fixed_policies_work_online_and_in_replay(self):
        for source in (core.FIXED_POLICY, core.GREEDY_POLICY, core.UNIFORM_POLICY):
            with self.subTest(source=source):
                core.validate_batch(core.choose_policy(source, obs()), obs())
                report = core.replay(world(), source, workers=2, max_rounds=8)
                self.assertGreater(report["attempts"], 0)
                self.assertLessEqual(report["attempts"], 4)

    def test_pool_average_and_world_state_reset(self):
        first, second = world([node("a", score=2)]), world([node("b", score=8)])
        second["seed"] = 18
        result = core.evaluate_policy([first, second], ROOT_ONLY, beta_cost=0.5)
        self.assertEqual(result["mean_score"], 4.5)
        self.assertEqual([report["attempts"] for report in result["worlds"]], [1, 1])
        with self.assertRaises(ValueError):
            core.evaluate_policy([], ROOT_ONLY)


class GeneratedCodeTests(unittest.TestCase):
    def test_schedule_and_local_computation(self):
        source = """def learning_rate(step, total_steps):
    rate = 0.001 * (1 - step / total_steps) ** 2
    return max(0.00001, rate)
"""
        self.assertAlmostEqual(core.invoke_source(source, "learning_rate", 5, 10), 0.00025)

    def test_no_import_attribute_private_or_global_access(self):
        invalid = [
            "def choose(observation):\n    import os\n    return []",
            'def choose(observation):\n    return observation.get("legal")',
            "def choose(observation):\n    return [].__class__",
            "def choose(observation):\n    return __builtins__",
            "def choose(observation):\n    global leaked\n    return []",
            "state = []\ndef choose(observation):\n    return state",
            "def choose(observation=[]):\n    return observation",
        ]
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(core.SourceError):
                core.validate_source(source, "choose")
        for name in ("open", "eval", "exec", "globals", "getattr"):
            source = f'def choose(observation):\n    return {name}("no")'
            with self.subTest(name=name), self.assertRaises(core.SourceError):
                core.invoke_source(source, "choose", obs())

    def test_operation_budget_catches_infinite_loop(self):
        source = "def choose(observation):\n    while True:\n        pass\n"
        with self.assertRaisesRegex(core.ExecutionLimitError, "operation budget"):
            core.choose_policy(source, obs())

    def test_subprocess_timeout_is_translated(self):
        with (
            patch.object(
                core.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 0.01)
            ),
            self.assertRaisesRegex(core.ExecutionLimitError, "subprocess timeout"),
        ):
            core.choose_policy(STOP, obs(), timeout=0.01)

    def test_arithmetic_range_and_sequence_growth_are_bounded(self):
        for expression in (
            "[0] * 1000000000",
            "2 ** 1000000000",
            "list(range(1000000000))",
            '"%1000000000s" % "x"',
        ):
            source = f"def choose(observation):\n    return {expression}\n"
            with self.subTest(expression=expression), self.assertRaises(core.SourceError):
                core.invoke_source(source, "choose", obs())

    def test_nonfinite_return_and_invalid_batch_are_rejected(self):
        with self.assertRaises(core.SourceError):
            core.invoke_source(
                'def learning_rate(step, total_steps):\n    return float("nan")\n',
                "learning_rate",
                1,
                2,
            )
        with self.assertRaisesRegex(ValueError, "illegal"):
            core.choose_policy('def choose(observation):\n    return ["hidden"]\n', obs())

    def test_compiled_calls_reset_budget_and_do_not_mutate_inputs(self):
        source = """def choose(observation):
    observation["nodes"] = []
    return observation["legal"][:1]
"""
        function = core.compile_function(source, "choose", max_operations=200)
        original = obs([node("a")])
        for _ in range(20):
            self.assertEqual(function(original), ["root"])
        self.assertEqual(len(original["nodes"]), 1)

    def test_augmented_assignment_obeys_bounds(self):
        source = """def learning_rate(step, total_steps):
    x = 1
    x += step
    return x / total_steps
"""
        self.assertEqual(core.compile_function(source, "learning_rate")(1, 4), 0.5)


if __name__ == "__main__":
    unittest.main()
