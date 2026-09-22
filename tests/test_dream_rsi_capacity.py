# SPDX-License-Identifier: MPL-2.0
"""Capacity-wait scheduling with a fake clock: no sleep, agents or training."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_dream_rsi_controller import controller_module

BUSY = {"cpu_percent": 91, "free_mb": 6104}
LOW_MEMORY = {"cpu_percent": 0, "free_mb": 2048}
IDLE = {"cpu_percent": 19, "free_mb": 6104}


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class CapacityWaitTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.controller = controller_module.Controller.__new__(controller_module.Controller)
        self.controller.started = 0.0
        self.controller.args = SimpleNamespace(
            max_seconds=480,
            capacity_wait=60,
            max_host_load=50,
            min_free_mb=3072,
        )
        self.output = io.StringIO()
        for context in (
            patch.object(controller_module.time, "monotonic", self.clock.monotonic),
            patch.object(controller_module.time, "sleep", self.clock.sleep),
            patch.object(controller_module.sys, "stderr", self.output),
        ):
            context.start()
            self.addCleanup(context.stop)

    def test_busy_then_idle_waits_before_returning_capacity(self):
        with patch.object(
            controller_module, "capacity_snapshot", side_effect=[BUSY, BUSY, IDLE]
        ) as read:
            self.assertEqual(self.controller.guard(), IDLE)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(self.clock.sleeps, [10, 10])
        self.assertEqual(self.output.getvalue().count("Host busy:"), 1)
        self.assertEqual(self.output.getvalue().count("Host capacity available"), 1)

    def test_capacity_wait_deadline_is_bounded(self):
        self.controller.args.capacity_wait = 25
        with (
            patch.object(controller_module, "capacity_snapshot", return_value=LOW_MEMORY) as read,
            self.assertRaisesRegex(controller_module.Deferred, "capacity-wait allowance exhausted"),
        ):
            self.controller.guard()
        self.assertEqual(self.clock.sleeps, [10, 10, 5])
        self.assertEqual(self.clock.now, 25)
        self.assertEqual(read.call_count, 3)

    def test_global_deadline_takes_precedence(self):
        self.controller.args.max_seconds = 15
        with (
            patch.object(controller_module, "capacity_snapshot", return_value=BUSY),
            self.assertRaisesRegex(controller_module.Deferred, "wall-time allowance exhausted"),
        ):
            self.controller.guard()
        self.assertEqual(self.clock.sleeps, [10, 5])
        self.assertEqual(self.clock.now, 15)

    def test_capacity_read_cannot_authorize_work_after_global_deadline(self):
        self.controller.args.max_seconds = 15

        def delayed_read():
            self.clock.now = 16
            return IDLE

        with (
            patch.object(controller_module, "capacity_snapshot", side_effect=delayed_read),
            self.assertRaisesRegex(controller_module.Deferred, "wall-time allowance exhausted"),
        ):
            self.controller.guard()
        self.assertEqual(self.clock.sleeps, [])

    def test_zero_wait_keeps_immediate_deferral_and_idle_success(self):
        self.controller.args.capacity_wait = 0
        with (
            patch.object(controller_module, "capacity_snapshot", return_value=BUSY),
            self.assertRaisesRegex(controller_module.Deferred, "host busy"),
        ):
            self.controller.guard()
        with patch.object(controller_module, "capacity_snapshot", return_value=IDLE):
            self.assertEqual(self.controller.guard(), IDLE)
        self.assertEqual(self.clock.sleeps, [])

    def test_legacy_synthetic_args_default_to_no_wait(self):
        del self.controller.args.capacity_wait
        with (
            patch.object(controller_module, "capacity_snapshot", return_value=BUSY),
            self.assertRaises(controller_module.Deferred),
        ):
            self.controller.guard()
        self.assertEqual(self.clock.sleeps, [])

    def test_cli_default_and_nonnegative_finite_validation(self):
        base = [
            "experiment.py",
            "run",
            "--root",
            "unused",
            "--prepared",
            "unused",
            "--model",
            "fixture",
        ]
        with patch.object(controller_module.sys, "argv", base):
            self.assertEqual(controller_module.arguments().capacity_wait, 60)
        with patch.object(controller_module.sys, "argv", base + ["--capacity-wait", "0"]):
            self.assertEqual(controller_module.arguments().capacity_wait, 0)
        for value in ("-1", "nan", "inf"):
            with (
                self.subTest(value=value),
                patch.object(controller_module.sys, "argv", base + ["--capacity-wait", value]),
                self.assertRaises(SystemExit),
            ):
                controller_module.arguments()


if __name__ == "__main__":
    unittest.main()
