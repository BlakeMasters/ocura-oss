# SPDX-License-Identifier: Apache-2.0

"""Stop requests, a killed recorder, and what a finished command leaves running."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ocura_oss import supervise
from ocura_oss.store import Store

WAIT_SECONDS = 30.0

# Appends to a file for as long as it lives, so a test can tell whether it still does.
BEAT = (
    "import sys, time\n"
    "while True:\n"
    "    with open(sys.argv[1], 'ab') as stream:\n"
    "        stream.write(b'.')\n"
    "    time.sleep(0.05)\n"
)
# Starts a beat of its own and reports its process id; a third argument keeps it waiting.
START_BEAT = (
    "import subprocess, sys, time\n"
    f"beat = subprocess.Popen([sys.executable, '-c', {BEAT!r}, sys.argv[1]],\n"
    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "with open(sys.argv[2], 'w') as stream:\n"
    "    stream.write(str(beat.pid))\n"
    "if len(sys.argv) > 3:\n"
    "    time.sleep(120)\n"
)


def wait_until(condition, message):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting until {message}")


def size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def is_quiet(path):
    before = size(path)
    time.sleep(0.5)
    return size(path) == before


def use_default_handlers(test):
    """Start from the default handlers, whatever the test runner inherited."""
    for number in supervise._STOP_SIGNALS:
        test.addCleanup(signal.signal, number, signal.signal(number, signal.SIG_DFL))


class StopRequestHandlerTests(unittest.TestCase):
    def setUp(self):
        use_default_handlers(self)

    def handlers(self):
        return [signal.getsignal(number) for number in supervise._STOP_SIGNALS]

    def test_each_stop_request_is_delivered_as_a_keyboard_interrupt(self):
        for number in supervise._STOP_SIGNALS:
            with (
                self.subTest(signal=number.name),
                supervise.stop_requests_interrupt(),
                self.assertRaises(KeyboardInterrupt),
            ):
                signal.raise_signal(number)
                time.sleep(1)  # a handler runs between bytecodes

    def test_handlers_are_restored_afterwards(self):
        before = self.handlers()
        with supervise.stop_requests_interrupt():
            self.assertEqual(self.handlers(), [signal.default_int_handler] * len(before))
        self.assertEqual(self.handlers(), before)

    def test_an_ignored_or_handled_signal_is_left_alone(self):
        number = supervise._STOP_SIGNALS[-1]
        for handler in (signal.SIG_IGN, lambda _number, _frame: None):
            with self.subTest(handler=handler):
                signal.signal(number, handler)
                with supervise.stop_requests_interrupt():
                    self.assertIs(signal.getsignal(number), handler)
                self.assertIs(signal.getsignal(number), handler)

    def test_nothing_is_installed_outside_the_main_thread(self):
        seen = []

        def enter():
            with supervise.stop_requests_interrupt():
                seen.extend(self.handlers())

        thread = threading.Thread(target=enter)
        thread.start()
        thread.join()
        self.assertNotIn(signal.default_int_handler, seen)

    def test_a_launch_hook_is_used_only_on_linux_and_never_beside_other_threads(self):
        expected = ["preexec_fn"] if sys.platform == "linux" else []
        self.assertEqual(list(supervise.Tether().popen_options()), expected)
        beside = []
        thread = threading.Thread(target=lambda: beside.append(supervise.Tether().popen_options()))
        thread.start()
        thread.join()
        self.assertEqual(beside, [{}])


class RecorderTests(unittest.TestCase):
    def setUp(self):
        use_default_handlers(self)
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "proj"
        self.store = Store(self.root)
        self.store.initialize_state(name="supervision tests")
        self.beat = Path(self._temporary.name) / "beat"
        self.pid_file = Path(self._temporary.name) / "beat.pid"
        self.addCleanup(self.stop_started_beat)

    def record(self, script, *arguments, **options):
        """Start `ocura-oss run` on a script in its own process and return that recorder."""
        recorder = subprocess.Popen(
            [sys.executable, "-m", "ocura_oss", "run", "--root", str(self.root), "--json", "--"]
            + [sys.executable, "-c", script, *(str(argument) for argument in arguments)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
        self.addCleanup(self.reap, recorder)
        wait_until(lambda: size(self.beat) > 0, "the command starts")
        return recorder

    def reap(self, recorder):
        if recorder.poll() is None:
            recorder.kill()
        with contextlib.suppress(Exception):
            recorder.communicate(timeout=WAIT_SECONDS)

    def stop_started_beat(self):
        """End a beat that the command started, in case it outlived the test."""
        if self.pid_file.is_file():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(self.pid_file.read_text()), signal.SIGTERM)
            wait_until(lambda: is_quiet(self.beat), "the started beat stops")

    def assert_interrupted(self, recorder):
        stdout, stderr = recorder.communicate(timeout=WAIT_SECONDS)
        self.assertEqual(recorder.returncode, 1, stderr.decode(errors="replace"))
        result = json.loads(stdout)
        self.assertEqual(result["outcome"], "interrupted")
        self.assertEqual(result["launch_error_category"], "interrupted")
        self.assertTrue(is_quiet(self.beat), "the command is still running")
        self.assertEqual(self.store.list_attempts(), [])
        self.assertTrue(self.store.verify_state().ok)

    @unittest.skipUnless(os.name == "posix", "SIGTERM and SIGHUP are POSIX stop requests")
    def test_a_stop_signal_stops_the_command_and_records_the_run(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                self.beat.unlink(missing_ok=True)
                recorder = self.record(BEAT, self.beat)
                recorder.send_signal(number)
                self.assert_interrupted(recorder)

    @unittest.skipUnless(sys.platform == "win32", "Ctrl+Break is a Windows console event")
    def test_ctrl_break_stops_the_command_and_records_the_run(self):
        recorder = self.record(BEAT, self.beat, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            os.kill(recorder.pid, signal.CTRL_BREAK_EVENT)
        except OSError as exc:
            self.skipTest(f"no console to deliver Ctrl+Break through: {exc}")
        self.assert_interrupted(recorder)

    @unittest.skipUnless(
        sys.platform in ("win32", "linux"),
        "macOS cannot end a command when its recorder is killed",
    )
    def test_a_killed_recorder_takes_its_command_with_it(self):
        # tests/test_attempts.py covers the abandoned attempt this leaves and its recovery.
        if sys.platform == "win32":
            # A job also covers what the command started; Linux covers the command itself.
            recorder = self.record(START_BEAT, self.beat, self.pid_file, "stay")
        else:
            recorder = self.record(BEAT, self.beat)
        recorder.kill()
        recorder.communicate(timeout=WAIT_SECONDS)
        wait_until(lambda: is_quiet(self.beat), "the command stops with its recorder")

    def test_a_finished_command_leaves_what_it_started_running(self):
        recorder = self.record(START_BEAT, self.beat, self.pid_file)
        stdout, stderr = recorder.communicate(timeout=WAIT_SECONDS)
        self.assertEqual(recorder.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(json.loads(stdout)["outcome"], "passed")
        # The recorder has exited; the beat its command started must still be growing.
        before = size(self.beat)
        wait_until(lambda: size(self.beat) > before, "the started beat is still alive")


if __name__ == "__main__":
    unittest.main()
