# SPDX-License-Identifier: MPL-2.0

"""Core-only example checks plus explicitly selected real backend integration tests."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "autoregressive"


def load_example(name):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutoregressiveContractTests(unittest.TestCase):
    def test_next_character_inputs_never_include_target_or_future(self):
        model = load_example("train")
        x, y = model.examples(["red\n"])
        changed, _ = model.examples(["rex\n"])
        self.assertEqual(x[:3], changed[:3])
        self.assertNotEqual(x[3], changed[3])
        self.assertEqual(y, [model.VOCAB.index(char) for char in "red\n"])
        self.assertEqual(x[0], model.encode_context(""))
        self.assertEqual(x[1], model.encode_context("r"))
        self.assertFalse(set(model.TRAIN_LINES) & set(model.VALIDATION_LINES))

    def test_generation_uses_previously_generated_characters(self):
        model = load_example("train")
        contexts = []
        tokens = iter("fox\n")

        def predict(context):
            contexts.append(context)
            token = next(tokens)
            return [float(char == token) for char in model.VOCAB]

        self.assertEqual(model.generate(predict), "red fox\n")
        self.assertEqual(contexts[0], model.encode_context("red "))
        self.assertEqual(contexts[1], model.encode_context("red f"))
        self.assertEqual(contexts[-1], model.encode_context("red fox"))

    def test_invalid_training_configuration_rejected_before_backend_import(self):
        model = load_example("train")
        for steps, rate in ((0, 0.5), (-1, 0.5), (1, float("nan")), (1, -0.5)):
            with self.subTest(steps=steps, rate=rate), self.assertRaises(ValueError):
                model.train("pytorch", steps, rate, 7)


class AutoregressiveIntegrationTests(unittest.TestCase):
    def exercise(self, executor):
        with tempfile.TemporaryDirectory() as temporary:
            reports = {}
            for backend in ("pytorch", "jax"):
                root = Path(temporary) / backend
                command = [sys.executable, str(EXAMPLE_DIR / "experiment.py")]
                completed = subprocess.run(
                    [
                        *command,
                        "run",
                        "--root",
                        str(root),
                        "--backend",
                        backend,
                        "--executor",
                        executor,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=240,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                reports[backend] = result
                self.assertEqual(result["verification"], {"ok": True, "atoms": 2, "logs": 4})
                for key, steps in (("baseline", 40), ("variant", 120)):
                    metrics = result[key]
                    self.assertEqual(metrics["backend"], backend)
                    self.assertEqual(metrics["executor"], executor)
                    self.assertEqual(metrics["steps"], steps)
                    self.assertLess(metrics["train_loss"], metrics["initial_train_loss"])
                    self.assertTrue(metrics["generated_text"].startswith("red "))
                self.assertLess(result["validation_loss_delta"], 0)
                paths = list((root / ".ocura-oss").rglob("*"))
                before = {path: path.read_bytes() for path in paths if path.is_file()}
                restored = subprocess.run(
                    [*command, "report", "--root", str(root)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=30,
                )
                self.assertEqual(restored.returncode, 0, restored.stderr)
                self.assertEqual(json.loads(restored.stdout), result)
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                rejected = subprocess.run(
                    [*command, "run", "--root", str(root), "--backend", backend],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                log = root / result["variant"]["stdout_log"]
                log.write_bytes(log.read_bytes() + b"changed")
                rejected = subprocess.run(
                    [*command, "report", "--root", str(root)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(rejected.stdout, "")
                self.assertIn("verification failed", rejected.stderr)
            # Shared data, initialization, and updates should agree within float32 tolerance.
            self.assertAlmostEqual(
                reports["pytorch"]["variant"]["validation_loss"],
                reports["jax"]["variant"]["validation_loss"],
                places=4,
            )

    @unittest.skipUnless(
        os.environ.get("OCURA_OSS_EXAMPLE_TESTS") in ("local", "all"),
        "set OCURA_OSS_EXAMPLE_TESTS=local or all; needs PyTorch and JAX",
    )
    def test_local_backends(self):
        self.exercise("local")

    @unittest.skipUnless(
        os.environ.get("OCURA_OSS_EXAMPLE_TESTS") in ("ray", "all"),
        "set OCURA_OSS_EXAMPLE_TESTS=ray or all; needs Ray, PyTorch, and JAX",
    )
    def test_ray_backends(self):
        self.exercise("ray")
