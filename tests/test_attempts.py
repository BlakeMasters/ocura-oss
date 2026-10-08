# SPDX-License-Identifier: Apache-2.0

"""Attempt journal, liveness locks, recovery, and concurrent recording."""

import contextlib
import io
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
from unittest import mock

import ocura_oss
from ocura_oss import cli, locking, model, runner
from ocura_oss.store import Store, StoreError

WAIT_SECONDS = 30.0

HOLD_LOCK = (
    "import sys, time; from pathlib import Path; from ocura_oss import locking\n"
    "with locking.hold(Path(sys.argv[1])):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)
REPORT_PID_AND_SLEEP = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"


def wait_until(condition, message):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting until {message}")


@contextlib.contextmanager
def reader_probe(path):
    """Hold the shared lock that a reader's liveness check takes for an instant."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        if not locking._try_lock(descriptor, shared=True):
            raise AssertionError("a recorder holds the lock")
        yield
        locking._unlock(descriptor)
    finally:
        os.close(descriptor)


def release_later(path, seconds):
    """Hold a reader's probe on *path* and let go of it shortly, as a real reader does."""
    probe = reader_probe(path)
    probe.__enter__()
    timer = threading.Timer(seconds, probe.__exit__, (None, None, None))
    timer.start()
    return timer


def stop(process):
    process.kill()
    process.wait(timeout=WAIT_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class LockTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "attempt.lock"

    def test_missing_lock_file_is_not_held(self):
        self.assertFalse(locking.is_held(self.path))

    def test_lock_is_seen_while_held_and_removed_after(self):
        with locking.hold(self.path):
            self.assertTrue(self.path.is_file())
            self.assertTrue(locking.is_held(self.path))
            self.assertTrue(locking.is_held(self.path), "probing must not release the lock")
        self.assertFalse(locking.is_held(self.path))
        self.assertFalse(self.path.exists())

    def test_lock_is_released_when_the_block_raises(self):
        with self.assertRaises(KeyboardInterrupt), locking.hold(self.path):
            raise KeyboardInterrupt
        self.assertFalse(locking.is_held(self.path))
        self.assertFalse(self.path.exists())

    def test_try_hold_refuses_a_held_lock_and_takes_a_free_one(self):
        with locking.hold(self.path):
            with locking.try_hold(self.path) as held:
                self.assertFalse(held)
            self.assertTrue(locking.is_held(self.path), "a refused attempt must not disturb it")
        with locking.try_hold(self.path) as held:
            self.assertTrue(held)
            self.assertTrue(locking.is_held(self.path))
        self.assertFalse(self.path.exists())

    def test_one_readers_probe_does_not_look_like_a_recorder_to_another(self):
        self.path.touch()
        with reader_probe(self.path):
            self.assertFalse(locking.is_held(self.path))
            with reader_probe(self.path):
                self.assertFalse(locking.is_held(self.path))
        self.assertTrue(self.path.exists(), "probing must not remove the lock file")

    def test_recorder_is_seen_by_every_reader(self):
        with locking.hold(self.path):
            with self.assertRaises(AssertionError), reader_probe(self.path):
                pass
            self.assertTrue(locking.is_held(self.path))
            self.assertTrue(locking.is_held(self.path))

    def test_try_hold_waits_out_a_passing_reader(self):
        self.path.touch()
        timer = release_later(self.path, 0.05)
        try:
            with locking.try_hold(self.path) as held:
                self.assertTrue(held)
        finally:
            timer.join()
        self.assertFalse(self.path.exists())

    def test_try_hold_gives_up_while_the_lock_stays_shared(self):
        self.path.touch()
        with reader_probe(self.path), locking.try_hold(self.path) as held:
            self.assertFalse(held)
        self.assertTrue(self.path.exists())
        self.assertFalse(locking.is_held(self.path))

    def test_operating_system_releases_the_lock_when_the_holder_is_killed(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLD_LOCK, str(self.path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), b"held")
            self.assertTrue(locking.is_held(self.path))
        finally:
            stop(holder)
        wait_until(lambda: not locking.is_held(self.path), "the killed holder's lock is released")
        self.assertTrue(self.path.is_file(), "a killed holder cannot remove its own lock file")
        with locking.try_hold(self.path) as held:
            self.assertTrue(held)
        self.assertFalse(self.path.exists())


class JournalTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = Store(Path(self._temporary.name) / "proj")
        _, self.pathway = self.store.initialize_state(name="journal tests")

    def run_command(self, argv=("unused",), **options):
        return runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=list(argv),
            declared_parameters={"label": "value"},
            **options,
        )

    def abandon(self, output=b"partial output\n", **options):
        """Record an attempt whose recorder stops before finalizing it."""

        def die(tokens, store, stdout_handle, stderr_handle, mirror):
            if stdout_handle is not None:
                stdout_handle.write(output)
            raise KeyboardInterrupt

        with mock.patch.object(runner, "_execute", die), self.assertRaises(KeyboardInterrupt):
            self.run_command(**options)
        ((attempt, state),) = self.store.list_attempts()
        self.assertIs(state, model.AttemptState.ABANDONED)
        return attempt

    def attempt_files(self):
        return sorted(item.name for item in self.store.attempts_dir.iterdir())


class RunningAttemptTests(JournalTestCase):
    def test_attempt_is_journaled_and_running_while_the_command_runs(self):
        seen = {}

        def observe(tokens, store, stdout_handle, stderr_handle, mirror):
            stdout_handle.write(b"so far")
            stdout_handle.flush()
            seen["attempts"] = store.list_attempts()
            seen["report"] = store.verify_state()
            seen["recovered"] = store.recover()
            seen["files"] = self.attempt_files()
            return 0, None, False

        with mock.patch.object(runner, "_execute", observe):
            execution = self.run_command(masked_arguments=[0])

        ((attempt, state),) = seen["attempts"]
        self.assertIs(state, model.AttemptState.RUNNING)
        self.assertEqual(attempt.id, execution.atom.id)
        self.assertEqual(attempt.chokepoint_id, execution.chokepoint.id)
        self.assertEqual(attempt.pathway_id, self.pathway.id)
        self.assertEqual(attempt.started_at, execution.atom.started_at)
        self.assertEqual(attempt.command, (model.MASKED_ARGUMENT,))
        self.assertEqual(attempt.masked_arguments, (0,))
        self.assertEqual(attempt.declared_parameters, {"label": "value"})
        self.assertEqual(attempt.stdout_log, execution.atom.stdout_log)

        report = seen["report"]
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(report.running_attempts, (execution.atom.id,))
        self.assertEqual(report.abandoned_attempts, ())
        self.assertEqual(report.atoms, 0)
        self.assertEqual(report.logs_checked, 0)

        self.assertEqual(seen["recovered"], (), "a running attempt must be left alone")
        self.assertEqual(seen["files"], [f"{execution.atom.id}.json", f"{execution.atom.id}.lock"])

    def test_finalized_run_leaves_no_journal_entry(self):
        execution = self.run_command([sys.executable, "-c", "print('done')"])
        self.assertEqual(self.attempt_files(), [])
        self.assertEqual(self.store.list_attempts(), [])
        report = self.store.verify_state()
        self.assertTrue(report.ok, report.problems)
        self.assertEqual((report.running_attempts, report.abandoned_attempts), ((), ()))
        self.assertEqual(self.store.recover(), ())
        self.assertEqual(self.store.load_atom(execution.atom.id), execution.atom)

    def test_log_creation_failure_leaves_no_attempt(self):
        self.store.logs_dir.rmdir()
        self.store.logs_dir.write_text("not a directory", "utf-8")
        with self.assertRaises(StoreError) as ctx:
            self.run_command([sys.executable, "-c", "pass"])
        self.assertIn("cannot create log files", str(ctx.exception))
        self.assertEqual(self.attempt_files(), [])
        self.assertEqual(self.store.list_atoms(), [])


class AbandonedAttemptTests(JournalTestCase):
    def test_abandoned_attempt_is_reported_until_recovered(self):
        attempt = self.abandon()
        self.assertEqual(self.attempt_files(), [f"{attempt.id}.json"])

        report = self.store.verify_state()
        self.assertFalse(report.ok)
        self.assertEqual(report.abandoned_attempts, (attempt.id,))
        self.assertEqual(report.running_attempts, ())
        self.assertEqual(
            report.problems,
            (
                (
                    f"attempts/{attempt.id}.json",
                    "attempt was not finalized; run `ocura-oss recover` to close it",
                ),
            ),
        )
        with self.assertRaises(StoreError) as ctx:
            ocura_oss.compare(root=self.store.root)
        self.assertIn("ocura-oss recover", str(ctx.exception))

    def killed_recorder_lock(self, attempt):
        """Leave the lock file a killed recorder cannot remove."""
        lock = self.store.attempts_dir / f"{attempt.id}.lock"
        lock.touch()
        return lock

    def test_another_readers_probe_cannot_hide_an_abandoned_attempt(self):
        attempt = self.abandon()
        with reader_probe(self.killed_recorder_lock(attempt)):
            ((_attempt, state),) = self.store.list_attempts()
            self.assertIs(state, model.AttemptState.ABANDONED)
            report = self.store.verify_state()
            self.assertFalse(report.ok)
            self.assertEqual(report.abandoned_attempts, (attempt.id,))
            self.assertEqual(report.running_attempts, ())

    def test_concurrent_verifiers_always_report_an_abandoned_attempt(self):
        attempt = self.abandon()
        self.killed_recorder_lock(attempt)
        seen = []

        def verify_repeatedly():
            for _ in range(100):
                report = self.store.verify_state()
                seen.append((report.ok, report.abandoned_attempts, report.running_attempts))

        verifiers = [threading.Thread(target=verify_repeatedly) for _ in range(4)]
        for verifier in verifiers:
            verifier.start()
        for verifier in verifiers:
            verifier.join()
        self.assertEqual(len(seen), 400)
        self.assertEqual(set(seen), {(False, (attempt.id,), ())})

    def test_recovery_waits_out_a_passing_reader(self):
        attempt = self.abandon()
        timer = release_later(self.killed_recorder_lock(attempt), 0.05)
        try:
            (recovered,) = self.store.recover()
        finally:
            timer.join()
        self.assertEqual(recovered.atom_id, attempt.id)
        self.assertIs(recovered.action, model.RecoveryAction.ABANDONED)
        self.assertEqual(self.attempt_files(), [])
        self.assertTrue(self.store.verify_state().ok)

    def test_lingering_reader_delays_recovery_without_hiding_the_attempt(self):
        attempt = self.abandon()
        with reader_probe(self.killed_recorder_lock(attempt)):
            self.assertEqual(self.store.recover(), ())
            self.assertEqual(self.store.verify_state().abandoned_attempts, (attempt.id,))
        (recovered,) = self.store.recover()
        self.assertEqual(recovered.atom_id, attempt.id)

    def test_recovery_records_an_abandoned_atom_without_inventing_an_outcome(self):
        attempt = self.abandon(output=b"partial output\n")
        (recovered,) = self.store.recover()
        self.assertIs(recovered.action, model.RecoveryAction.ABANDONED)
        self.assertEqual(recovered.atom_id, attempt.id)
        self.assertEqual(recovered.chokepoint_id, attempt.chokepoint_id)
        self.assertEqual(recovered.pathway_id, self.pathway.id)

        atom = self.store.load_atom(attempt.id)
        self.assertIs(atom.outcome, model.Outcome.ABANDONED)
        self.assertIsNone(atom.finished_at)
        self.assertIsNone(atom.duration_seconds)
        self.assertIsNone(atom.return_code)
        self.assertIsNone(atom.launch_error_category)
        self.assertEqual(atom.started_at, attempt.started_at)
        self.assertEqual(atom.command, attempt.command)
        self.assertEqual(atom.declared_parameters, {"label": "value"})
        self.assertEqual(self.store.read_verified_log(atom.id), b"partial output\n")
        self.assertEqual(self.store.read_verified_log(atom.id, stream="stderr"), b"")

        chokepoint = self.store.load_chokepoint(attempt.chokepoint_id)
        self.assertIs(chokepoint.outcome, model.Outcome.ABANDONED)
        self.assertTrue(chokepoint.branchable)

        self.assertEqual(self.attempt_files(), [])
        report = self.store.verify_state()
        self.assertTrue(report.ok, report.problems)
        self.assertEqual((report.atoms, report.chokepoints, report.logs_checked), (1, 1, 2))
        self.assertEqual(self.store.recover(), ())

    def test_recovery_creates_logs_the_recorder_never_opened(self):
        attempt = self.abandon()
        for relative in (attempt.stdout_log, attempt.stderr_log):
            (self.store.root / relative).unlink()
        self.store.recover()
        atom = self.store.load_atom(attempt.id)
        self.assertEqual((atom.stdout_bytes, atom.stderr_bytes), (0, 0))
        self.assertTrue(self.store.verify_state().ok)

    def test_recovery_of_an_uncaptured_attempt_records_no_logs(self):
        attempt = self.abandon(capture=False)
        self.assertIsNone(attempt.stdout_log)
        self.store.recover()
        atom = self.store.load_atom(attempt.id)
        self.assertIs(atom.outcome, model.Outcome.ABANDONED)
        self.assertIs(atom.output_capture, model.OutputCapture.NONE)
        self.assertIsNone(atom.stdout_sha256)
        self.assertEqual(list(self.store.logs_dir.iterdir()), [])
        self.assertTrue(self.store.verify_state().ok)

    def test_abandoned_run_can_be_branched_and_compared(self):
        baseline = self.run_command([sys.executable, "-c", "pass"])
        child = ocura_oss.branch(baseline.chokepoint.id, root=self.store.root, reason="try again")
        self.pathway = child
        self.abandon()
        self.store.recover()

        comparison = ocura_oss.compare(baseline.chokepoint.id, root=self.store.root)
        self.assertIs(comparison.state, model.ComparisonState.READY)
        child_run = comparison.children[0].child_run
        self.assertIs(child_run.outcome, model.Outcome.ABANDONED)
        self.assertIsNone(child_run.duration_seconds)
        self.assertIsNone(comparison.to_dict()["children"][0]["child_run"]["duration_seconds"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(
                ["compare", "--root", str(self.store.root), "--from", baseline.chokepoint.id]
            )
        self.assertEqual(code, 0)
        self.assertIn("child run: abandoned (duration unknown)", stdout.getvalue())

        retry = ocura_oss.branch(
            self.store.list_chokepoints()[0].id, root=self.store.root, reason="after abandonment"
        )
        self.assertEqual(retry.parent_pathway_id, child.id)

    def test_recorder_stopped_between_atom_and_chokepoint_is_completed(self):
        with (
            mock.patch.object(Store, "_save_chokepoint", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_command([sys.executable, "-c", "import sys; sys.exit(4)"])
        ((attempt, state),) = self.store.list_attempts()
        self.assertIs(state, model.AttemptState.ABANDONED)
        self.assertEqual(self.store.load_atom(attempt.id).return_code, 4)

        report = self.store.verify_state()
        self.assertEqual(
            [record for record, _problem in report.problems], [f"attempts/{attempt.id}.json"]
        )

        (recovered,) = self.store.recover()
        self.assertIs(recovered.action, model.RecoveryAction.COMPLETED)
        atom = self.store.load_atom(attempt.id)
        self.assertIs(atom.outcome, model.Outcome.FAILED, "the recorded outcome must be kept")
        chokepoint = self.store.load_chokepoint(attempt.chokepoint_id)
        self.assertIs(chokepoint.outcome, model.Outcome.FAILED)
        self.assertEqual(chokepoint.created_at, atom.finished_at)
        self.assertTrue(self.store.verify_state().ok)

    def test_recorder_stopped_after_the_chokepoint_is_cleared(self):
        with (
            mock.patch.object(Store, "_remove_attempt", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_command([sys.executable, "-c", "pass"])
        ((attempt, _state),) = self.store.list_attempts()
        before = self.store.load_chokepoint(attempt.chokepoint_id)

        (recovered,) = self.store.recover()
        self.assertIs(recovered.action, model.RecoveryAction.CLEARED)
        self.assertEqual(self.store.load_chokepoint(attempt.chokepoint_id), before)
        self.assertIs(self.store.load_atom(attempt.id).outcome, model.Outcome.PASSED)
        self.assertEqual(self.attempt_files(), [])
        self.assertTrue(self.store.verify_state().ok)

    def test_malformed_attempt_record_is_reported_and_stops_recovery(self):
        attempt = self.abandon()
        path = self.store.attempts_dir / f"{attempt.id}.json"
        path.write_text("{not json", "utf-8")
        report = self.store.verify_state()
        self.assertEqual(
            [record for record, _problem in report.problems],
            [
                f"attempts/{attempt.id}.json",
                f"logs/{attempt.id}.stderr.log",
                f"logs/{attempt.id}.stdout.log",
            ],
        )
        with self.assertRaises(StoreError):
            self.store.list_attempts()
        with self.assertRaises(StoreError):
            self.store.recover()
        self.assertTrue(path.is_file())

    def test_stray_lock_file_is_ignored_by_verification_and_removed_by_recovery(self):
        stray = self.store.attempts_dir / f"{model.make_id('atom')}.lock"
        stray.write_bytes(b"")
        self.assertTrue(self.store.verify_state().ok)
        self.assertEqual(self.store.list_attempts(), [])
        self.assertEqual(self.store.recover(), ())
        self.assertFalse(stray.exists())

    def test_recovery_requires_initialized_state(self):
        with self.assertRaises(StoreError):
            Store(Path(self._temporary.name) / "elsewhere").recover()


class ChokepointCompletenessTests(JournalTestCase):
    def test_atom_without_a_chokepoint_is_a_problem(self):
        execution = self.run_command([sys.executable, "-c", "pass"])
        (self.store.chokepoints_dir / f"{execution.chokepoint.id}.json").unlink()
        report = self.store.verify_state()
        self.assertEqual(
            report.problems, ((f"{execution.atom.id}.json", "atom has no terminal chokepoint"),)
        )

    def test_atom_with_two_chokepoints_is_a_problem(self):
        execution = self.run_command([sys.executable, "-c", "pass"])
        duplicate = model.Chokepoint(
            id=model.make_id("chokepoint"),
            pathway_id=execution.chokepoint.pathway_id,
            atom_id=execution.atom.id,
            created_at=execution.chokepoint.created_at,
            kind=model.TERMINAL_KIND,
            outcome=execution.chokepoint.outcome,
            branchable=True,
        )
        self.store._save_chokepoint(duplicate)
        report = self.store.verify_state()
        self.assertEqual(
            report.problems, ((f"{execution.atom.id}.json", "atom has 2 chokepoints"),)
        )


class ProcessTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "proj"
        self.store = Store(self.root)
        self.store.initialize_state(name="process tests")

    def spawn(self, *arguments):
        return subprocess.Popen(
            [sys.executable, "-m", "ocura_oss", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def invoke(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "ocura_oss", *arguments, "--root", str(self.root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=WAIT_SECONDS,
        )


class KilledRecorderTests(ProcessTestCase):
    def test_killed_recorder_leaves_an_attempt_that_recovery_closes(self):
        recorder = self.spawn(
            "run",
            "--root",
            str(self.root),
            "--json",
            "--",
            sys.executable,
            "-c",
            REPORT_PID_AND_SLEEP,
        )
        command_pid = None
        try:
            attempt, state = wait_until(
                lambda: next(iter(self.store.list_attempts()), None), "the attempt is journaled"
            )
            self.assertIs(state, model.AttemptState.RUNNING)
            log = self.root / attempt.stdout_log
            command_pid = int(
                wait_until(
                    lambda: log.is_file() and log.read_bytes().strip(),
                    "the command reports its process id",
                )
            )

            listed = json.loads(self.invoke("attempts", "--json").stdout)
            self.assertEqual(
                listed,
                {
                    "attempts": [
                        {
                            "id": attempt.id,
                            "pathway_id": attempt.pathway_id,
                            "started_at": attempt.started_at,
                            "state": "running",
                        }
                    ]
                },
            )
            running = self.invoke("verify", "--json")
            self.assertEqual(running.returncode, 0, running.stdout)
            self.assertEqual(
                json.loads(running.stdout)["attempts"], {"running": [attempt.id], "abandoned": []}
            )
            self.assertEqual(json.loads(self.invoke("recover", "--json").stdout)["recovered"], [])
        finally:
            stop(recorder)
            if command_pid is not None:
                # Where the command outlives its killed recorder, stop it before recovery.
                with contextlib.suppress(OSError):
                    os.kill(command_pid, signal.SIGTERM)

        wait_until(
            lambda: self.store.list_attempts()[0][1] is model.AttemptState.ABANDONED,
            "the killed recorder's lock is released",
        )
        abandoned = self.invoke("verify", "--json")
        self.assertEqual(abandoned.returncode, 2)
        report = json.loads(abandoned.stdout)
        self.assertEqual(report["attempts"], {"running": [], "abandoned": [attempt.id]})
        self.assertEqual(
            [item["record"] for item in report["problems"]], [f"attempts/{attempt.id}.json"]
        )
        self.assertIn("state=abandoned", self.invoke("attempts").stdout)

        def recover():
            # The stopped command may hold its log open for an instant longer.
            result = self.invoke("recover", "--json")
            return result if result.returncode == 0 else None

        recovered = json.loads(wait_until(recover, "recovery succeeds").stdout)
        self.assertEqual(
            recovered,
            {
                "recovered": [
                    {
                        "atom_id": attempt.id,
                        "chokepoint_id": attempt.chokepoint_id,
                        "pathway_id": attempt.pathway_id,
                        "action": "abandoned",
                    }
                ]
            },
        )
        atom = self.store.load_atom(attempt.id)
        self.assertIs(atom.outcome, model.Outcome.ABANDONED)
        self.assertEqual(int(self.store.read_verified_log(atom.id)), command_pid)
        self.assertEqual(self.invoke("verify", "--json").returncode, 0)
        self.assertEqual(self.invoke("recover").stdout, "recovered: 0\n")


class ConcurrentRecordingTests(ProcessTestCase):
    def test_runs_sharing_a_root_never_look_like_damage(self):
        command = "import time; print('x' * 4096); time.sleep(0.4)"
        recorders = [
            self.spawn(
                "run", "--root", str(self.root), "--json", "--", sys.executable, "-c", command
            )
            for _ in range(6)
        ]
        reports = []
        try:
            while any(recorder.poll() is None for recorder in recorders):
                reports.append(self.store.verify_state())
        finally:
            results = [recorder.communicate(timeout=WAIT_SECONDS) for recorder in recorders]

        for report in reports:
            self.assertTrue(report.ok, report.problems)
        self.assertTrue(any(report.running_attempts for report in reports))
        self.assertEqual([recorder.returncode for recorder in recorders], [0] * 6)
        atom_ids = {json.loads(stdout)["atom_id"] for stdout, _stderr in results}
        self.assertEqual(len(atom_ids), 6)

        final = self.store.verify_state()
        self.assertTrue(final.ok, final.problems)
        self.assertEqual(
            (final.atoms, final.chokepoints, final.logs_checked, final.running_attempts),
            (6, 6, 12, ()),
        )
        self.assertEqual(list(self.store.attempts_dir.iterdir()), [])

    def test_racing_initializers_create_exactly_one_state(self):
        root = Path(self._temporary.name) / "raced"
        initializers = [self.spawn("init", "--root", str(root), "--json") for _ in range(4)]
        results = [item.communicate(timeout=WAIT_SECONDS) for item in initializers]
        codes = sorted(item.returncode for item in initializers)
        self.assertEqual(codes, [0, 2, 2, 2], results)
        (winner,) = [json.loads(out) for out, _err in results if out.strip()]
        self.assertEqual(Store(root).load_den().id, winner["den_id"])
        self.assertTrue(Store(root).verify_state().ok)


if __name__ == "__main__":
    unittest.main()
