# SPDX-License-Identifier: MPL-2.0

import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ocura_oss import model, runner
from ocura_oss.store import Store, StoreError


@unittest.skipUnless(sys.executable, "requires a Python interpreter")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name) / "proj"
        self.store = Store(root)
        _, self.pathway = self.store.initialize_state(name="runner tests")

    def test_passing_run_records_evidence_and_separate_logs(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "print('hello'); import sys; sys.stderr.write('oops')"],
            declared_parameters={},
        )
        atom = execution.atom
        self.assertEqual(atom.outcome, model.Outcome.PASSED)
        self.assertEqual(atom.return_code, 0)
        self.assertIsNone(atom.launch_error_category)
        self.assertGreaterEqual(atom.duration_seconds, 0.0)
        model.parse_timestamp(atom.started_at, "started_at")
        model.parse_timestamp(atom.finished_at, "finished_at")

        stdout_path = self.store.root / atom.stdout_log
        stderr_path = self.store.root / atom.stderr_log
        self.assertNotEqual(atom.stdout_log, atom.stderr_log)
        self.assertIn(b"hello", stdout_path.read_bytes())
        self.assertIn(b"oops", stderr_path.read_bytes())
        self.assertEqual(atom.stdout_bytes, stdout_path.stat().st_size)
        self.assertEqual(atom.stderr_bytes, stderr_path.stat().st_size)

        chokepoint = self.store.load_chokepoint(execution.chokepoint.id)
        self.assertEqual(chokepoint.kind, "terminal")
        self.assertTrue(chokepoint.branchable)
        self.assertEqual(chokepoint.outcome, model.Outcome.PASSED)

    def test_nonzero_run_retains_exact_return_code(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "import sys; sys.exit(7)"],
            declared_parameters={},
        )
        self.assertEqual(execution.atom.outcome, model.Outcome.FAILED)
        self.assertEqual(execution.atom.return_code, 7)
        self.assertEqual(execution.chokepoint.outcome, model.Outcome.FAILED)

    def test_launch_failure_still_creates_terminal_evidence(self):
        missing = "definitely-not-a-real-executable-ocura-oss"
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[missing],
            declared_parameters={},
        )
        atom = execution.atom
        self.assertEqual(atom.outcome, model.Outcome.LAUNCH_FAILED)
        self.assertIsNone(atom.return_code)
        self.assertEqual(atom.launch_error_category, "executable_not_found")
        stdout_path = self.store.root / atom.stdout_log
        stderr_path = self.store.root / atom.stderr_log
        self.assertTrue(stdout_path.is_file())
        self.assertTrue(stderr_path.is_file())
        self.assertEqual(stdout_path.read_bytes(), b"")
        self.assertEqual(stderr_path.read_bytes(), b"")
        chokepoint = self.store.load_chokepoint(execution.chokepoint.id)
        self.assertEqual(chokepoint.kind, "terminal")
        self.assertTrue(chokepoint.branchable)

    def test_every_attempt_yields_one_atom_and_one_terminal_chokepoint(self):
        cases = [
            [sys.executable, "-c", "print('ok')"],
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            ["definitely-not-a-real-executable-ocura-oss"],
        ]
        for argv in cases:
            runner.run_command(
                self.store,
                pathway_id=self.pathway.id,
                argv=argv,
                declared_parameters={},
            )
        atoms = self.store.list_atoms()
        chokepoints = self.store.list_chokepoints()
        self.assertEqual(len(atoms), 3)
        self.assertEqual(len(chokepoints), 3)
        for chokepoint in chokepoints:
            self.assertEqual(chokepoint.kind, "terminal")
            self.assertTrue(chokepoint.branchable)
        self.assertEqual({item.atom_id for item in chokepoints}, {item.id for item in atoms})

    def test_unknown_pathway_is_rejected_before_launch(self):
        with self.assertRaises(StoreError):
            runner.run_command(
                self.store,
                pathway_id="pathway-" + "0" * 32,
                argv=[sys.executable, "-c", "print('nope')"],
                declared_parameters={},
            )
        self.assertEqual(self.store.list_atoms(), [])

    def test_declared_parameters_belong_to_the_atom_not_the_pathway(self):
        runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "print('params')"],
            declared_parameters={"batch": "2"},
        )
        atoms = self.store.list_atoms()
        self.assertEqual(atoms[0].declared_parameters, {"batch": "2"})
        unchanged = self.store.load_pathway(self.pathway.id)
        self.assertEqual(unchanged.parameters, {})

    def test_command_runs_in_the_project_root(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "import os; print(os.getcwd())"],
            declared_parameters={},
        )
        reported = (self.store.root / execution.atom.stdout_log).read_bytes()
        self.assertEqual(Path(reported.decode("utf-8").strip()), self.store.root)

    def test_environment_is_inherited_but_never_serialized(self):
        sentinel_name = "OCURA_OSS_RUNNER_SENTINEL"
        sentinel_value = "secret-sentinel-4242"
        old = os.environ.get(sentinel_name)
        os.environ[sentinel_name] = sentinel_value
        try:
            execution = runner.run_command(
                self.store,
                pathway_id=self.pathway.id,
                argv=[
                    sys.executable,
                    "-c",
                    f"import os; print(os.environ[{sentinel_name!r}])",
                ],
                declared_parameters={},
            )
        finally:
            if old is None:
                del os.environ[sentinel_name]
            else:
                os.environ[sentinel_name] = old

        stdout_log = (self.store.root / execution.atom.stdout_log).read_bytes()
        self.assertIn(sentinel_value.encode("utf-8"), stdout_log)
        atom_record = (self.store.atoms_dir / f"{execution.atom.id}.json").read_bytes()
        chokepoint_record = (
            self.store.chokepoints_dir / f"{execution.chokepoint.id}.json"
        ).read_bytes()
        self.assertNotIn(sentinel_value.encode("utf-8"), atom_record)
        self.assertNotIn(sentinel_value.encode("utf-8"), chokepoint_record)

    def test_large_logs_are_hashed_correctly(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('line\\n' * 200000)",
            ],
            declared_parameters={},
        )
        stdout_path = self.store.root / execution.atom.stdout_log
        data = stdout_path.read_bytes()
        self.assertGreaterEqual(len(data), 1_000_000)
        self.assertEqual(execution.atom.stdout_bytes, len(data))
        self.assertEqual(execution.atom.stdout_sha256, hashlib.sha256(data).hexdigest())

    def test_unicode_argv_is_preserved_in_the_raw_record(self):
        word = "caf\u00e9-\u00fcmlaut"
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "import sys; print(len(sys.argv[1]))", word],
            declared_parameters={},
        )
        self.assertEqual(execution.atom.outcome, model.Outcome.PASSED)
        self.assertIn(word, execution.atom.command)
        record = (self.store.atoms_dir / f"{execution.atom.id}.json").read_text("utf-8")
        self.assertIn(word, record)
        stdout_log = (self.store.root / execution.atom.stdout_log).read_bytes()
        self.assertIn(str(len(word)).encode("utf-8"), stdout_log.strip().splitlines()[-1])

    def test_blank_argv_tokens_are_rejected(self):
        for argv in ([], [""], ["ok", ""], [None]):
            with self.subTest(argv=argv), self.assertRaises(StoreError):
                runner.run_command(
                    self.store,
                    pathway_id=self.pathway.id,
                    argv=argv,
                    declared_parameters={},
                )
        self.assertEqual(self.store.list_atoms(), [])

    def test_string_argv_is_rejected_instead_of_split_into_characters(self):
        with self.assertRaisesRegex(StoreError, "argument tokens"):
            runner.run_command(
                self.store,
                pathway_id=self.pathway.id,
                argv="python -V",
                declared_parameters={},
            )
        self.assertEqual(self.store.list_atoms(), [])

    def test_parameter_values_may_contain_equals_signs(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "print('eq')"],
            declared_parameters={"eq": "a=b"},
        )
        self.assertEqual(execution.atom.declared_parameters, {"eq": "a=b"})

    def test_failed_run_log_hashes_match_files(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('err' * 1000); sys.exit(2)",
            ],
            declared_parameters={},
        )
        stderr_data = (self.store.root / execution.atom.stderr_log).read_bytes()
        self.assertEqual(execution.atom.stderr_bytes, len(stderr_data))
        self.assertEqual(execution.atom.stderr_sha256, hashlib.sha256(stderr_data).hexdigest())

    def test_mirror_streams_output_to_terminal_and_log(self):
        import contextlib
        import io

        console = io.BytesIO()

        class BufferedOut:
            def __init__(self, buf):
                self.buffer = buf

        with contextlib.redirect_stdout(BufferedOut(console)):
            execution = runner.run_command(
                self.store,
                pathway_id=self.pathway.id,
                argv=[sys.executable, "-c", "print('tee-marker')"],
                declared_parameters={},
                mirror=True,
            )
        log = (self.store.root / execution.atom.stdout_log).read_bytes()
        self.assertIn(b"tee-marker", log)
        self.assertIn(b"tee-marker", console.getvalue())
        self.assertEqual(execution.atom.outcome, model.Outcome.PASSED)

    def test_quiet_mode_keeps_output_out_of_the_console(self):
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            execution = runner.run_command(
                self.store,
                pathway_id=self.pathway.id,
                argv=[sys.executable, "-c", "print('quiet-marker')"],
                declared_parameters={},
                mirror=False,
            )
        self.assertNotIn("quiet-marker", console.getvalue())
        log = (self.store.root / execution.atom.stdout_log).read_bytes()
        self.assertIn(b"quiet-marker", log)

    def test_injected_clocks_drive_recorded_times(self):
        import datetime

        fixed_start = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        ticks = iter([100.0, 102.5])

        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "print('timed')"],
            declared_parameters={},
            now=lambda: fixed_start,
            monotonic=lambda: next(ticks),
        )
        self.assertEqual(execution.atom.started_at, execution.atom.finished_at)
        self.assertEqual(execution.atom.duration_seconds, 2.5)


class _FakeProcess:
    def __init__(self):
        import io

        self.pid = 12345
        empty = io.BytesIO(b"")
        self.stdout = empty
        self.stderr = io.BytesIO(b"")
        self._wait_calls = 0
        self.terminated = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        self._wait_calls += 1
        if self._wait_calls == 1:
            raise KeyboardInterrupt
        return -2

    def terminate(self):
        self.terminated = True


class InterruptedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name) / "proj"
        self.store = Store(root)
        _, self.pathway = self.store.initialize_state(name="interruption tests")

    def test_keyboard_interrupt_records_interrupted_evidence(self):
        from unittest import mock

        with mock.patch.object(runner.subprocess, "Popen", return_value=_FakeProcess()):
            execution = runner.run_command(
                self.store,
                pathway_id=self.pathway.id,
                argv=["fake-child"],
                declared_parameters={},
                mirror=False,
            )
        atom = execution.atom
        self.assertEqual(atom.outcome, model.Outcome.INTERRUPTED)
        self.assertIsNone(atom.return_code)
        self.assertEqual(atom.launch_error_category, "interrupted")
        chokepoint = self.store.load_chokepoint(execution.chokepoint.id)
        self.assertEqual(chokepoint.outcome, model.Outcome.INTERRUPTED)
        self.assertEqual(chokepoint.kind, "terminal")
        self.assertTrue(chokepoint.branchable)
        for relative in (atom.stdout_log, atom.stderr_log):
            self.assertTrue((self.store.root / relative).is_file())
        report = self.store.verify_state()
        self.assertTrue(report.ok, report.problems)

    def test_interrupted_payload_coherence(self):
        digest = hashlib.sha256(b"").hexdigest()
        atom_id = model.make_id("atom")
        valid = model.Atom(
            id=atom_id,
            pathway_id=self.pathway.id,
            started_at="2026-01-01T00:00:00.000000+00:00",
            finished_at="2026-01-01T00:00:01.000000+00:00",
            duration_seconds=1.0,
            outcome=model.Outcome.INTERRUPTED,
            return_code=None,
            launch_error_category="interrupted",
            declared_parameters={},
            command=("x",),
            stdout_log=f".ocura-oss/logs/{atom_id}.stdout.log",
            stderr_log=f".ocura-oss/logs/{atom_id}.stderr.log",
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_sha256=digest,
            stderr_sha256=digest,
        )
        self.store._save_atom(valid)
        loaded = self.store.load_atom(atom_id)
        self.assertIs(loaded.outcome, model.Outcome.INTERRUPTED)

        wrong_category = model.atom_to_payload(valid)
        wrong_category["launch_error_category"] = "something_else"
        with self.assertRaises(model.ValidationError):
            model.atom_from_payload(wrong_category)

        with_return_code = model.atom_to_payload(valid)
        with_return_code["return_code"] = 15
        with self.assertRaises(model.ValidationError):
            model.atom_from_payload(with_return_code)


if __name__ == "__main__":
    unittest.main()
