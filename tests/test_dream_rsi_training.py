# SPDX-License-Identifier: MPL-2.0
"""Evaluator contract checks; real CPU continuation is explicitly opt-in."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "dream_rsi"
sys.path.insert(0, str(EXAMPLE))
training = importlib.import_module("rsi_training")
CONSTANT = "def learning_rate(step, total_steps):\n    return 0.001\n"


def request(root: Path, **updates) -> dict:
    result = {
        "operation": "initialize",
        "profile": "smoke",
        "prepared": str(root.resolve()),
        "seed": 17,
        "updates": 0,
        "total_steps": 8,
        "schedule_source": CONSTANT,
        "parent_checkpoint": None,
        "parent_checkpoint_sha256": None,
        "output_checkpoint": str((root / "initial.pt").resolve()),
        "max_seconds": 60,
    }
    result.update(updates)
    return result


class TrainingContractTests(unittest.TestCase):
    def test_validation_does_not_import_torch(self):
        with patch.dict(sys.modules, {"torch": None}):
            config = training.validate_request(request(Path.cwd()))
            self.assertEqual(config["dropout"], 0.1)
            self.assertEqual(training.checked_schedule(CONSTANT, 8), [0.001] * 8)

    def test_rejects_nonfixed_and_malformed_requests(self):
        cases = [
            {"profile": "gpu"},
            {"profile": []},
            {"operation": "other"},
            {"prepared": "relative"},
            {"output_checkpoint": "relative.pt"},
            {"seed": True},
            {"seed": -1},
            {"seed": 2**32},
            {"total_steps": False},
            {"total_steps": 4097},
            {"updates": 1},
            {"updates": False},
            {"updates": -1},
            {"max_seconds": 0},
            {"max_seconds": float("nan")},
            {"model_width": 256},
            {"schedule_source": None},
            {"parent_checkpoint_sha256": "f" * 64},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                training.validate_request(request(Path.cwd(), **change))

    def test_train_requires_verified_parent_and_positive_updates(self):
        root = Path.cwd()
        training_request = request(
            root,
            operation="train",
            updates=2,
            parent_checkpoint=str(root / "parent.pt"),
            parent_checkpoint_sha256="a" * 64,
        )
        training.validate_request(training_request)
        for change in (
            {"updates": 0},
            {"updates": 9},
            {"parent_checkpoint": None},
            {"parent_checkpoint_sha256": None},
            {"parent_checkpoint_sha256": "xyz"},
            {"output_checkpoint": str(root / "parent.pt")},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                training.validate_request({**training_request, **change})

    def test_test_operation_cannot_train_or_write_checkpoint(self):
        root = Path.cwd()
        frozen = request(
            root,
            operation="test",
            updates=0,
            output_checkpoint=None,
            parent_checkpoint=str(root / "chosen.pt"),
            parent_checkpoint_sha256="a" * 64,
        )
        training.validate_request(frozen)
        for change in ({"updates": 1}, {"output_checkpoint": str(root / "new.pt")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                training.validate_request({**frozen, **change})

    def test_entire_schedule_is_bounded_including_future_updates(self):
        for expression in ("True", "0", "0.02001", "1e999", "'0.001'"):
            source = f"def learning_rate(step, total_steps):\n    return {expression}\n"
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                training.checked_schedule(source, 8)
        future_bad = (
            "def learning_rate(step, total_steps):\n    return 0.001 if step < 7 else 0.03\n"
        )
        with self.assertRaisesRegex(ValueError, "step 7"):
            training.checked_schedule(future_bad, 8)
        for endpoint in (training.MIN_LR, training.MAX_LR):
            source = f"def learning_rate(step, total_steps):\n    return {endpoint}\n"
            self.assertEqual(training.checked_schedule(source, 1), [endpoint])

    def test_schedule_has_global_step_and_fixed_horizon(self):
        source = (
            "def learning_rate(step, total_steps):\n    return 0.001 + 0.001 * step / total_steps\n"
        )
        self.assertEqual(training.checked_schedule(source, 4), [0.001, 0.00125, 0.0015, 0.00175])

    def test_invalid_code_is_rejected_before_torch(self):
        for source in (
            "import os\ndef learning_rate(step, total_steps):\n    return 0.001\n",
            "def learning_rate(step, total_steps):\n    return step.__class__\n",
        ):
            with patch.dict(sys.modules, {"torch": None}), self.assertRaises(ValueError):
                training.execute(request(Path.cwd(), schedule_source=source))

    def test_request_digest_and_nonstandard_json_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            data = json.dumps(request(Path(directory)), allow_nan=False).encode()
            path.write_bytes(data)
            expected = hashlib.sha256(data).hexdigest()
            self.assertEqual(training.read_request(path, expected), json.loads(data))
            path.write_bytes(data + b" ")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                training.read_request(path, expected)
            data = b'{"max_seconds":NaN}'
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                training.read_request(path, hashlib.sha256(data).hexdigest())

    def test_configuration_is_not_mutated_by_callers(self):
        original = copy.deepcopy(training.PROFILES)
        training.configuration("smoke")["width"] = 999
        self.assertEqual(training.PROFILES, original)


@unittest.skipUnless(
    os.environ.get("OCURA_DREAM_RSI_TRAINING_TEST") == "1",
    "set OCURA_DREAM_RSI_TRAINING_TEST=1 and OCURA_DREAM_RSI_PREPARED for CPU integration",
)
class TrainingContinuationTests(unittest.TestCase):
    def test_changed_schedule_preserves_model_optimizer_and_both_rngs(self):
        import torch

        prepared = Path(os.environ["OCURA_DREAM_RSI_PREPARED"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = request(root, prepared=str(prepared), total_steps=4)
            initial = training.execute(base)
            self.assertEqual(initial["step"], 0)
            self.assertEqual(initial["training_tokens"], 0)
            self.assertEqual(initial["score"], -initial["validation_loss"])
            train_base = {
                **base,
                "operation": "train",
                "updates": 2,
                "parent_checkpoint": initial["checkpoint"],
                "parent_checkpoint_sha256": initial["checkpoint_sha256"],
            }
            first = training.execute({**train_base, "output_checkpoint": str(root / "first.pt")})
            second_source = "def learning_rate(step, total_steps):\n    return 0.002\n"
            second = training.execute(
                {
                    **train_base,
                    "parent_checkpoint": first["checkpoint"],
                    "parent_checkpoint_sha256": first["checkpoint_sha256"],
                    "schedule_source": second_source,
                    "output_checkpoint": str(root / "second.pt"),
                }
            )
            combined_source = (
                "def learning_rate(step, total_steps):\n    return 0.001 if step < 2 else 0.002\n"
            )
            combined = training.execute(
                {
                    **train_base,
                    "updates": 4,
                    "schedule_source": combined_source,
                    "output_checkpoint": str(root / "combined.pt"),
                }
            )
            self.assertEqual(second["start"], 2)
            self.assertEqual(second["end"], 4)
            self.assertEqual(second["training_tokens"], 64)
            self.assertEqual(second["validation_loss"], combined["validation_loss"])
            self.assertNotEqual(first["schedule_sha256"], second["schedule_sha256"])
            split_state = torch.load(second["checkpoint"], weights_only=True)
            full_state = torch.load(combined["checkpoint"], weights_only=True)

            def assert_same(left, right):
                if isinstance(left, torch.Tensor):
                    self.assertTrue(torch.equal(left, right))
                elif isinstance(left, dict):
                    self.assertEqual(left.keys(), right.keys())
                    for key in left:
                        assert_same(left[key], right[key])
                elif isinstance(left, (list, tuple)):
                    self.assertEqual(len(left), len(right))
                    for a, b in zip(left, right, strict=True):
                        assert_same(a, b)
                else:
                    self.assertEqual(left, right)

            for key in ("model", "optimizer", "torch_rng", "batch_rng", "step", "contract"):
                with self.subTest(checkpoint_part=key):
                    assert_same(split_state[key], full_state[key])
            frozen_bytes = Path(second["checkpoint"]).read_bytes()
            final = training.execute(
                {
                    **base,
                    "operation": "test",
                    "updates": 0,
                    "parent_checkpoint": second["checkpoint"],
                    "parent_checkpoint_sha256": second["checkpoint_sha256"],
                    "output_checkpoint": None,
                    "schedule_source": second_source,
                }
            )
            self.assertNotIn("validation_loss", final)
            self.assertNotIn("score", final)
            self.assertEqual(final["training_tokens"], 0)
            self.assertEqual(final["checkpoint_sha256"], second["checkpoint_sha256"])
            self.assertEqual(Path(second["checkpoint"]).read_bytes(), frozen_bytes)
            corrupt = root / "corrupt.pt"
            corrupt.write_bytes(frozen_bytes + b"corruption")
            with (
                patch.dict(sys.modules, {"torch": None}),
                self.assertRaisesRegex(ValueError, "digest mismatch"),
            ):
                training.execute(
                    {
                        **train_base,
                        "parent_checkpoint": str(corrupt),
                        "parent_checkpoint_sha256": second["checkpoint_sha256"],
                        "output_checkpoint": str(root / "should-not-exist.pt"),
                    }
                )
            self.assertFalse((root / "should-not-exist.pt").exists())


if __name__ == "__main__":
    unittest.main()
