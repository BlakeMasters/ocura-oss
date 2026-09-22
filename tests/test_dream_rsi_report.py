# SPDX-License-Identifier: MPL-2.0
"""Report semantic checks over a retained synthetic controller ledger.

The mutation helpers replace already-verified inputs to exercise cross-record
consistency independently of the Store's separate byte-integrity checks.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_dream_rsi_controller import FixtureController, arguments, controller_module

# isort: split

import rsi_report
from rsi_io import encoded


class ReportIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / "experiment"
        with patch.object(
            controller_module, "capacity_snapshot", return_value={"cpu_percent": 0, "free_mb": 8192}
        ):
            cls.controller = FixtureController(arguments(cls.root, rounds=2))
            cls.baseline = cls.controller.execute()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def changed_record(self, kind, change):
        original = rsi_report.Store.read_verified_log

        def read(store, identity, *args, **kwargs):
            raw = original(store, identity, *args, **kwargs)
            if kwargs.get("stream", "stdout") == "stdout" and (
                store.load_atom(identity).declared_parameters.get("kind") == kind
            ):
                value = json.loads(raw)
                change(value)
                return encoded(value)
            return raw

        with patch.object(rsi_report.Store, "read_verified_log", read):
            return rsi_report.build_report(self.root)

    def changed_request(self, predicate, change):
        original = rsi_report.checked_json

        def checked(path, expected):
            result = original(path, expected)
            if predicate(result):
                change(result)
            return result

        with patch.object(rsi_report, "checked_json", checked):
            return rsi_report.build_report(self.root)

    def test_complete_fixture_has_reconstructed_replay_and_measured_costs(self):
        self.assertEqual(self.baseline["status"], "complete")
        self.assertTrue(self.baseline["verification"]["replay_reconstructed"])
        self.assertTrue(self.baseline["costs"]["measured_training_complete"])
        self.assertEqual(self.baseline["costs"]["training_evaluations_without_metrics"], 0)

    def test_frozen_agent_request_and_result_are_checked(self):
        with self.assertRaisesRegex(ValueError, "agent request.*frozen"):
            self.changed_request(
                lambda value: "role" in value,
                lambda value: value.update(model="different-model"),
            )
        with self.assertRaisesRegex(ValueError, "agent result.*frozen"):
            self.changed_record("agent", lambda value: value.update(effort="high"))

    def test_frozen_evaluator_request_and_fingerprint_are_checked(self):
        with self.assertRaisesRegex(ValueError, "evaluator request.*frozen"):
            self.changed_request(
                lambda value: "operation" in value,
                lambda value: value.update(profile="different-profile"),
            )
        with self.assertRaisesRegex(ValueError, "evaluator fingerprint"):
            self.changed_record("evaluation", lambda value: value.update(evaluator_sha256="bad"))

    def test_exact_prefix_and_recomputed_decision_are_checked(self):
        def add_future(value):
            value["observation"]["future_best_score"] = 1000

        with self.assertRaisesRegex(ValueError, "exact revealed prefix"):
            self.changed_record("decision", add_future)
        with self.assertRaisesRegex(ValueError, "recomputed decision"):
            self.changed_record("decision", lambda value: value.update(batch=[]))

    def test_world_aggregates_are_recomputed(self):
        with self.assertRaisesRegex(ValueError, "world aggregates"):
            self.changed_record("world", lambda value: value.update(best_score=999))
        with self.assertRaisesRegex(ValueError, "world aggregates"):
            self.changed_record("world", lambda value: value.update(objective=999))

    def test_selected_policy_must_be_redeployed(self):
        def equivalent_unselected_source(selection):
            for candidate in selection["candidates"]:
                if candidate["name"] == selection["selected"]:
                    candidate["source"] += "\n"
                    selection["source"] = candidate["source"]

        with self.assertRaisesRegex(ValueError, "redeploy the selected policy"):
            self.changed_record("selection", equivalent_unselected_source)

    def test_controls_are_recomputed(self):
        def alter_control(selection):
            selection["controls"]["greedy"]["mean_score"] += 1

        with self.assertRaisesRegex(ValueError, "replay control"):
            self.changed_record("selection", alter_control)

    def test_complete_requires_all_held_out_tests(self):
        with self.assertRaisesRegex(ValueError, "missing.*held-out test"):
            self.changed_record("completion", lambda value: value.update(tests=[]))

    def test_missing_training_measurement_is_explicit(self):
        # Simulate a verified evaluator result that omitted optional timing metrics.
        # Both redundant copies must agree so this test reaches cost accounting.
        original = rsi_report.Store.read_verified_log

        def remove_metrics(value):
            if isinstance(value, dict):
                value.pop("training_seconds", None)
                for item in value.values():
                    remove_metrics(item)
            elif isinstance(value, list):
                for item in value:
                    remove_metrics(item)

        def read(store, identity, *args, **kwargs):
            raw = original(store, identity, *args, **kwargs)
            if kwargs.get("stream", "stdout") != "stdout":
                return raw
            value = copy.deepcopy(json.loads(raw))
            remove_metrics(value)
            return encoded(value)

        with patch.object(rsi_report.Store, "read_verified_log", read):
            report = rsi_report.build_report(self.root)
        self.assertFalse(report["costs"]["measured_training_complete"])
        self.assertEqual(report["costs"]["training_evaluations_without_metrics"], 5)


class PartialWorldReportTests(unittest.TestCase):
    def test_host_deferral_retains_nodes_and_checks_every_successful_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "partial"
            controller = FixtureController(arguments(root, rounds=2, attempts=3))

            def capacity():
                # The third proposal is retained, then the genuine host guard
                # defers before its evaluator can run. No training is performed.
                return {
                    "cpu_percent": 60 if controller.discovery_calls >= 3 else 0,
                    "free_mb": 8192,
                }

            with patch.object(controller_module, "capacity_snapshot", side_effect=capacity):
                report = controller.execute()
            self.assertEqual(report["status"], "deferred")
            self.assertIn("host busy", report["reason"])
            self.assertEqual(report["worlds"], [])
            self.assertEqual(report["selections"], [])
            self.assertEqual(report["tests"], [])
            self.assertEqual(report["verification"]["checkpoints_checked"], 3)
            self.assertEqual(controller.discovery_calls, 3)
            retained_nodes = [
                json.loads(controller.store.read_verified_log(atom.id))
                for atom in controller.store.list_atoms()
                if atom.declared_parameters.get("kind") == "node"
            ]
            self.assertEqual(len(retained_nodes), 2)
            self.assertTrue(all(item["node"]["status"] == "ok" for item in retained_nodes))
            checkpoint = Path(retained_nodes[-1]["node"]["result"]["checkpoint"])
            checkpoint.write_bytes(checkpoint.read_bytes() + b"tampered checkpoint")
            # The Ocura records are intact; the separately bound output is not.
            self.assertTrue(controller.store.verify_state().ok)
            with self.assertRaisesRegex(ValueError, "checkpoint digest mismatch"):
                rsi_report.build_report(root)


if __name__ == "__main__":
    unittest.main()
