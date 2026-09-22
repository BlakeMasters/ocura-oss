# SPDX-License-Identifier: MPL-2.0
"""Synthetic controller integration: real ledger, zero agents and zero training."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "dream_rsi"
sys.path.insert(0, str(EXAMPLE))
spec = importlib.util.spec_from_file_location(
    "_dream_rsi_controller_tests", EXAMPLE / "experiment.py"
)
controller_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = controller_module
spec.loader.exec_module(controller_module)

import rsi_agent  # noqa: E402
from rsi_core import FIXED_POLICY  # noqa: E402
from rsi_io import digest, encoded, write_json  # noqa: E402

STOP_AFTER_GAIN = """def choose(observation):
    if observation["nodes"]:
        scores = [node["score"] for node in observation["nodes"] if node["status"] == "ok"]
        best = max([observation["root_score"]] + scores)
        if best > observation["root_score"]:
            return []
    return ["root"]
"""


def arguments(root: Path, **changes):
    values = dict(
        root=root,
        prepared=root.parent / "prepared",
        model="synthetic-test-fixture",
        effort="low",
        profile="smoke",
        seed=41,
        rounds=3,
        attempts=2,
        updates=2,
        policy_revisions=1,
        beta_cost=0.01,
        beta_parallel=0.0,
        max_seconds=180,
        max_agent_calls=30,
        agent_timeout=20,
        eval_timeout=20,
        max_host_load=50,
        min_free_mb=3072,
    )
    values.update(changes)
    return SimpleNamespace(**values)


class FixtureController(controller_module.Controller):
    """Only the external agent/evaluator are fake; orchestration and evidence are real."""

    def __init__(self, args, first_failure=None):
        self.first_failure = first_failure
        self.calls = []
        self.prompts = []
        self.discovery_calls = 0
        super().__init__(args)

    def record_value(self, kind, value, parent=None):
        if kind == "manifest":
            value["synthetic"] = True
            value["claim"] = "Synthetic integration fixture; no agent or training evidence."
        return super().record_value(kind, value, parent)

    def emit_fixture(self, kind, request, result, parent, *, return_code=0, **labels):
        prefix = f"{self.sequence + 1:04d}-{kind}"
        request_path = self.root / "requests" / f"{prefix}-fixture-request.json"
        result_path = self.root / "requests" / f"{prefix}-fixture-result.json"
        request_sha = write_json(request_path, request)
        result_sha = write_json(result_path, result)
        if return_code:
            # This deliberately emits valid JSON before failing: stdout alone is not success.
            command = [
                sys.executable,
                "-c",
                "import pathlib,sys;"
                "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes());"
                "sys.exit(int(sys.argv[2]))",
                str(result_path),
                str(return_code),
            ]
        else:
            command = [
                sys.executable,
                str(EXAMPLE / "experiment.py"),
                "emit",
                "--path",
                str(result_path),
                "--sha256",
                result_sha,
            ]
        return self.record_command(
            kind,
            command,
            parent,
            timeout=15,
            artifact_file=str(request_path.relative_to(self.root)),
            artifact_sha256=request_sha,
            fixture_result_file=str(result_path.relative_to(self.root)),
            fixture_result_sha256=result_sha,
            synthetic="true",
            **labels,
        )

    def agent(self, role, prompt, parent):
        self.guard()
        self.agent_calls += 1
        self.calls.append(("agent", role))
        context_marker = "Development context:\n" if role == "policy" else "Context:\n"
        context = json.loads(prompt.split(context_marker, 1)[1])
        self.prompts.append((role, context))
        field = "schedule_source" if role == "discovery" else "policy_source"
        source = controller_module.INITIAL_SCHEDULE if role == "discovery" else STOP_AFTER_GAIN
        failure = None
        if role == "discovery":
            self.discovery_calls += 1
            if self.discovery_calls == 1:
                failure = self.first_failure
        if failure == "source":
            source = "import os\ndef learning_rate(step, total_steps):\n    return 0.002\n"
        response = {field: source, "rationale": "Synthetic fixture only"}
        result = {
            "ok": failure != "parse",
            "error": "malformed agent JSON" if failure == "parse" else None,
            "response": response,
            "usage": [],
            "seconds": 0.001,
            "model": self.args.model,
            "effort": self.args.effort,
            "events": [],
            "exit_code": 7 if failure == "exit" else 0,
            "synthetic": True,
        }
        request = {
            "role": role,
            "model": self.args.model,
            "effort": self.args.effort,
            "field": field,
            "timeout": 15,
            "prompt": prompt,
        }
        return self.emit_fixture(
            "agent", request, result, parent, role=role, return_code=7 if failure == "exit" else 0
        )

    def evaluate(self, world_name, seed, operation, candidate, checkpoint, parent, node_id):
        self.guard()
        self.calls.append(("evaluation", operation, world_name, node_id))
        start = checkpoint["step"] if checkpoint else 0
        end = start + (self.args.updates if operation == "train" else 0)
        path = self.root / "checkpoints" / f"{world_name}-{node_id}.pt"
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
            "output_checkpoint": None if operation == "test" else str(path),
            "max_seconds": 15,
        }
        result = {
            "schema": "dream-rsi-training-v1",
            "profile": self.args.profile,
            "seed": seed,
            "evaluator_sha256": self.manifest["source_sha256"]["rsi_training.py"],
            "contract_sha256": digest(encoded({"synthetic": True, "seed": seed})),
            "start": start,
            "end": end,
            "step": end,
            "total_steps": request["total_steps"],
            "input_checkpoint_sha256": request["parent_checkpoint_sha256"],
            "schedule_sha256": digest(candidate["schedule_source"].encode()),
            "evaluation_tokens": 64,
            "training_tokens": (end - start) * 32,
            "wall_seconds": 0.001,
            "evaluation_seconds": 0.001,
            "training_seconds": 0.0,
            "runtime": {"device": "synthetic", "threads": 1},
            "synthetic": True,
        }
        if operation == "test":
            result.update(
                kind="final-test",
                test_loss=1.75 + 0.001 * seed,
                checkpoint=checkpoint["checkpoint"],
                checkpoint_sha256=checkpoint["checkpoint_sha256"],
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = encoded({"synthetic": True, "seed": seed, "step": end})
            with path.open("xb") as handle:
                handle.write(payload)
            # The second continuation is worse. Replay should learn to stop after the first gain.
            loss = 4.0 if operation == "initialize" else 2.0 if start == 0 else 3.0
            result.update(
                kind="initialization" if operation == "initialize" else "training-attempt",
                validation_loss=loss,
                score=-loss,
                checkpoint=str(path),
                checkpoint_sha256=digest(payload),
            )
        return self.emit_fixture(
            "evaluation",
            request,
            result,
            parent,
            operation=operation,
            world=world_name,
            node=node_id,
            host_cpu=0,
            host_free_mb=8192,
        )


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "experiment"
        self.capacity = patch.object(
            controller_module, "capacity_snapshot", return_value={"cpu_percent": 0, "free_mb": 8192}
        )
        self.capacity.start()
        self.addCleanup(self.capacity.stop)
        self.addCleanup(self.temporary.cleanup)

    def test_iterative_replay_redeployment_control_and_frozen_test(self):
        controller = FixtureController(arguments(self.root))
        report = controller.execute()
        self.assertEqual(report["status"], "complete")
        self.assertTrue(controller.manifest["synthetic"])
        self.assertEqual(controller.store.verify_state().problems, ())
        worlds = {world["name"]: world for world in controller.worlds}
        self.assertEqual(
            set(worlds), {"bootstrap", "adaptive-1", "fixed-1", "adaptive-2", "fixed-2"}
        )
        self.assertEqual(
            [item["world_names"] for item in controller.selections],
            [
                ["bootstrap"],
                ["bootstrap", "adaptive-1"],
            ],
        )
        self.assertEqual(controller.selections[0]["source"], STOP_AFTER_GAIN)
        for phase in (1, 2):
            adaptive, fixed = worlds[f"adaptive-{phase}"], worlds[f"fixed-{phase}"]
            self.assertEqual(adaptive["seed"], fixed["seed"])
            self.assertEqual(adaptive["policy_source"], STOP_AFTER_GAIN)
            self.assertEqual(fixed["policy_source"], FIXED_POLICY)
            self.assertEqual(len(adaptive["nodes"]), 1)
            self.assertEqual(len(fixed["nodes"]), 2)
            self.assertEqual(adaptive["stop_reason"], "policy_stop")
            self.assertEqual(fixed["nodes"][1]["parent"], fixed["nodes"][0]["id"])
            self.assertEqual(
                fixed["nodes"][1]["result"]["input_checkpoint_sha256"],
                fixed["nodes"][0]["result"]["checkpoint_sha256"],
            )
        first_test = next(
            i for i, call in enumerate(controller.calls) if call[:2] == ("evaluation", "test")
        )
        self.assertFalse(any(call[0] == "agent" for call in controller.calls[first_test:]))
        self.assertEqual(len(controller.tests), 4)
        for item in controller.tests:
            self.assertEqual(item["selected_node"], "n001")
            self.assertEqual(item["selection_metric"], "validation_loss")
            expected = worlds[item["world"]]["nodes"][0]["result"]["checkpoint_sha256"]
            self.assertEqual(item["result"]["checkpoint_sha256"], expected)
            self.assertNotIn("score", item["result"])
        for _, context in controller.prompts:
            text = json.dumps(context)
            self.assertNotIn('"test_loss"', text)
            self.assertNotIn('"checkpoint"', text)
        policy_contexts = [context for role, context in controller.prompts if role == "policy"]
        self.assertEqual([len(context["history"]) for context in policy_contexts], [1, 2])
        discovery_contexts = [
            context for role, context in controller.prompts if role == "discovery"
        ]
        self.assertEqual(
            [len(context["completed_history"]) for context in discovery_contexts],
            [0, 0, 1, 1, 1, 2, 2, 2],
        )

    def test_busy_host_defers_before_any_agent_or_evaluator(self):
        controller = FixtureController(arguments(self.root))
        with patch.object(
            controller_module,
            "capacity_snapshot",
            return_value={
                "cpu_percent": 90,
                "free_mb": 1024,
            },
        ):
            report = controller.execute()
        self.assertEqual(report["status"], "deferred")
        self.assertEqual(controller.calls, [])
        self.assertEqual(controller.agent_calls, 0)
        self.assertEqual(controller.store.verify_state().problems, ())

    def test_invalid_schedule_is_retained_and_repaired_from_last_checkpoint(self):
        controller = FixtureController(arguments(self.root), first_failure="source")
        world = controller.collect("invalid-source", 41, FIXED_POLICY, [], "bootstrap")
        self.assertEqual([node["status"] for node in world["nodes"]], ["failed", "ok"])
        failed, repaired = world["nodes"]
        self.assertEqual(repaired["parent"], failed["id"])
        self.assertEqual(repaired["result"]["start"], 0)
        self.assertEqual(
            repaired["input_checkpoint_sha256"], world["root_result"]["checkpoint_sha256"]
        )
        self.assertEqual(
            len([call for call in controller.calls if call[:2] == ("evaluation", "train")]), 1
        )
        self.assertEqual(controller.store.verify_state().problems, ())

    def test_invalid_agent_json_is_not_consumed(self):
        controller = FixtureController(arguments(self.root), first_failure="parse")
        world = controller.collect("invalid-agent", 41, FIXED_POLICY, [], "bootstrap")
        self.assertEqual(world["nodes"][0]["status"], "failed")
        self.assertIn("malformed agent JSON", world["nodes"][0]["diagnostic"])
        self.assertEqual(world["nodes"][1]["status"], "ok")
        self.assertEqual(
            len([call for call in controller.calls if call[:2] == ("evaluation", "train")]), 1
        )

    def test_nonzero_agent_exit_cannot_authorize_valid_stdout_candidate(self):
        controller = FixtureController(arguments(self.root), first_failure="exit")
        world = controller.collect("failed-agent", 41, FIXED_POLICY, [], "bootstrap")
        self.assertEqual(world["nodes"][0]["status"], "failed")
        self.assertEqual(world["nodes"][1]["status"], "ok")
        self.assertEqual(
            len([call for call in controller.calls if call[:2] == ("evaluation", "train")]), 1
        )


class AgentParsingTests(unittest.TestCase):
    def call_adapter(self, response_text, *, tool=False, return_code=0):
        items = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": response_text}}
        ]
        if tool:
            items.append({"type": "item.completed", "item": {"type": "command_execution"}})
        items.append({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
        raw = b"\n".join(encoded(item) for item in items)
        return self.call_raw(raw, return_code=return_code)

    def call_raw(self, raw, *, return_code=0):
        request = {
            "model": "synthetic-test-fixture",
            "effort": "low",
            "timeout": 1,
            "prompt": "Synthetic fixture only",
            "field": "schedule_source",
        }
        with (
            patch.object(rsi_agent.shutil, "which", return_value="codex-fixture"),
            patch.object(rsi_agent, "bounded_process", return_value=(return_code, raw, b"")),
            patch.object(rsi_agent.sys, "stderr", SimpleNamespace(buffer=io.BytesIO())),
        ):
            return rsi_agent.execute(request, Path("synthetic.schema.json"))

    def test_response_must_match_schema_and_successful_command(self):
        valid = json.dumps(
            {"schedule_source": controller_module.INITIAL_SCHEDULE, "rationale": "fixture"}
        )
        self.assertTrue(self.call_adapter(valid)["ok"])
        for response in (
            "not json",
            "null",
            '{"schedule_source":17,"rationale":"fixture"}',
            '{"policy_source":"wrong field","rationale":"fixture"}',
        ):
            with self.subTest(response=response):
                self.assertFalse(self.call_adapter(response)["ok"])
        self.assertFalse(self.call_adapter(valid, tool=True)["ok"])
        self.assertFalse(self.call_adapter(valid, return_code=7)["ok"])

    def test_malformed_event_shapes_are_retained_without_losing_valid_usage(self):
        valid_message = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(
                    {
                        "schedule_source": controller_module.INITIAL_SCHEDULE,
                        "rationale": "fixture",
                    }
                ),
            },
        }
        valid_usage = {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2}
        malformed = [
            None,
            [],
            7,
            "scalar",
            {},
            {"type": None},
            {"type": "item.completed"},
            {"type": "item.completed", "item": []},
            {"type": "item.completed", "item": {"type": 7}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": None}},
            {"type": "turn.completed"},
            {"type": "turn.completed", "usage": []},
            {"type": "turn.completed", "usage": {}},
            {"type": "turn.completed", "usage": {"input_tokens": True, "output_tokens": 2}},
            {"type": "turn.completed", "usage": {"input_tokens": -1, "output_tokens": 2}},
            {"type": "turn.completed", "usage": {"input_tokens": "10", "output_tokens": 2}},
            {"type": "turn.completed", "usage": {"input_tokens": 1.5, "output_tokens": 2}},
            {"type": "turn.failed"},
        ]
        for bad_event in malformed:
            with self.subTest(event=bad_event):
                events = [
                    valid_message,
                    bad_event,
                    {"type": "turn.completed", "usage": valid_usage},
                ]
                result = self.call_raw(b"\n".join(encoded(event) for event in events))
                self.assertFalse(result["ok"])
                self.assertIsNone(result["response"])
                self.assertEqual(result["events"], events)
                self.assertEqual(result["usage"], [valid_usage])
                self.assertTrue(result["event_errors"])
                json.dumps(result, allow_nan=False)

    def test_bad_json_lines_are_retained_and_do_not_poison_finite_result(self):
        raw = (
            b'not-json\n{"type":"turn.completed","usage":NaN}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n'
        )
        result = self.call_raw(raw)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["response"])
        self.assertEqual(
            result["unparsed_stdout"],
            [
                "not-json",
                '{"type":"turn.completed","usage":NaN}',
            ],
        )
        self.assertEqual(result["usage"], [{"input_tokens": 12, "output_tokens": 3}])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
