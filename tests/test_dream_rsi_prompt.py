# SPDX-License-Identifier: MPL-2.0
"""Pure policy-prompt projection tests; no subprocesses, agents or training."""

from __future__ import annotations

import copy
import json
import unittest

from test_dream_rsi_controller import controller_module


class PolicyPromptTests(unittest.TestCase):
    def setUp(self):
        self.node = {
            "id": "n001",
            "parent": "root",
            "depth": 1,
            "status": "ok",
            "score": -2.0,
            "candidate": {"schedule_source": "PRIVATE SCHEDULE " * 200, "rationale": "omit me"},
            "diagnostic": "private diagnostic",
            "checkpoint": "private checkpoint",
        }
        self.history = [{"root_score": -4.0, "seed": 1001, "nodes": [self.node]}]
        report = {
            "seed": 1001,
            "score": -2.01,
            "best_score": -2.0,
            "attempts": 1,
            "rounds": 1,
            "stop_reason": "exhausted",
            "trace": [{"round": 1, "batch": ["root"], "revealed": [self.node]}],
        }
        self.candidates = [
            {
                "name": "incumbent",
                "source": controller_module.FIXED_POLICY,
                "accepted": True,
                "evaluation": {"mean_score": -2.01, "worlds": [report]},
            },
            {
                "name": "rejected",
                "source": "def choose(observation):\n    return [1]\n",
                "accepted": False,
                "error": "invalid batch",
                "rationale": "omit this too",
            },
        ]
        self.options = {"workers": 1, "max_rounds": 3, "beta_cost": 0.01, "beta_parallel": 0.0}

    def prompt_and_context(self):
        prompt = controller_module.policy_prompt(self.history, self.candidates, self.options)
        return prompt, json.loads(prompt.split("Development context:\n", 1)[1])

    def test_compact_feedback_preserves_scores_structure_sources_and_errors(self):
        prompt, context = self.prompt_and_context()
        self.assertEqual(
            context["history"],
            [
                {
                    "root_score": -4.0,
                    "nodes": [
                        {
                            "id": "n001",
                            "parent": "root",
                            "depth": 1,
                            "status": "ok",
                            "score": -2.0,
                        }
                    ],
                }
            ],
        )
        incumbent, rejected = context["earlier_versions"]
        self.assertEqual(incumbent["source"], controller_module.FIXED_POLICY)
        feedback = incumbent["evaluation"]["worlds"][0]
        self.assertEqual(feedback["trace"], [{"round": 1, "batch": ["root"], "revealed": ["n001"]}])
        self.assertEqual(feedback["best_score"], -2.0)
        self.assertEqual(rejected["source"], self.candidates[1]["source"])
        self.assertEqual(rejected["error"], "invalid batch")
        for removed in ("PRIVATE SCHEDULE", "omit me", "private diagnostic", "private checkpoint"):
            self.assertNotIn(removed, prompt)
        self.assertLess(len(prompt), 4000)

    def test_observation_contract_and_inputs_remain_unchanged(self):
        before = copy.deepcopy((self.history, self.candidates, self.options))
        prompt, context = self.prompt_and_context()
        self.assertEqual(before, (self.history, self.candidates, self.options))
        self.assertEqual(
            context["observation_example"],
            controller_module.observation(
                [],
                -4.0,
                1,
                0,
                3,
                0.01,
                0.0,
            ),
        )
        self.assertIn("at most 20 lines", prompt)
        self.assertIn("only the observed prefix", prompt)
        self.assertIn("Do not hardcode", prompt)

    def test_failures_and_empty_reveals_remain_available_feedback(self):
        self.node.update(status="failed", score=None)
        self.candidates[0]["evaluation"]["worlds"][0]["trace"].append(
            {"round": 2, "batch": ["n001"], "revealed": []}
        )
        _, context = self.prompt_and_context()
        self.assertIsNone(context["history"][0]["nodes"][0]["score"])
        trace = context["earlier_versions"][0]["evaluation"]["worlds"][0]["trace"]
        self.assertEqual(trace[-1], {"round": 2, "batch": ["n001"], "revealed": []})

    def test_empty_history_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one replay world"):
            controller_module.policy_prompt([], self.candidates, self.options)


if __name__ == "__main__":
    unittest.main()
