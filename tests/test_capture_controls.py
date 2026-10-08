# SPDX-License-Identifier: Apache-2.0

"""Runs recorded without output capture and with masked command arguments."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import ocura_oss
from ocura_oss import cli, model, runner
from ocura_oss.store import Store, StoreError

SECRET = "TOKEN-SENTINEL-7f3a"
ECHO_ARGUMENTS = "import sys; print(' '.join(sys.argv[1:])); sys.stderr.write('warned')"


class BufferedConsole:
    def __init__(self):
        self.buffer = io.BytesIO()

    def flush(self):
        pass


class CaptureControlTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "proj"
        self.store = Store(self.root)
        _, self.pathway = self.store.initialize_state(name="capture controls")

    def record_text(self):
        """Return every byte the ledger holds, decoded for substring checks."""
        return b"".join(
            path.read_bytes() for path in sorted(self.store.state_dir.rglob("*")) if path.is_file()
        ).decode("utf-8", "replace")


class NoCaptureTests(CaptureControlTestCase):
    def test_uncaptured_run_retains_no_output_and_says_so(self):
        # The printed text is assembled at run time, so it appears only in output.
        script = "import sys; print('STD' + 'OUT-TEXT'); sys.stderr.write('STD' + 'ERR-TEXT')"
        execution = ocura_oss.run([sys.executable, "-c", script], root=self.root, capture=False)
        atom = execution.atom
        self.assertIs(atom.outcome, model.Outcome.PASSED)
        self.assertIs(atom.output_capture, model.OutputCapture.NONE)
        for value in (
            atom.stdout_log,
            atom.stderr_log,
            atom.stdout_bytes,
            atom.stderr_bytes,
            atom.stdout_sha256,
            atom.stderr_sha256,
        ):
            self.assertIsNone(value)
        self.assertEqual(list(self.store.logs_dir.iterdir()), [])
        self.assertNotIn("STDOUT-TEXT", self.record_text())
        self.assertNotIn("STDERR-TEXT", self.record_text())
        self.assertEqual(self.store.load_atom(atom.id), atom)

        report = self.store.verify_state()
        self.assertTrue(report.ok, report.problems)
        self.assertEqual((report.atoms, report.logs_checked), (1, 0))

    def test_uncaptured_run_has_no_log_to_read_or_resolve(self):
        atom = ocura_oss.run([sys.executable, "-c", "pass"], root=self.root, capture=False).atom
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                with self.assertRaises(StoreError) as ctx:
                    self.store.read_verified_log(atom.id, stream=stream)
                self.assertIn("recorded without output capture", str(ctx.exception))
                with self.assertRaises(StoreError):
                    self.store.resolve_log_path(atom, stream)
        self.store.verify_atom_evidence(atom)

    def test_uncaptured_failure_still_records_its_return_code(self):
        atom = ocura_oss.run(
            [sys.executable, "-c", "import sys; sys.exit(9)"], root=self.root, capture=False
        ).atom
        self.assertIs(atom.outcome, model.Outcome.FAILED)
        self.assertEqual(atom.return_code, 9)

    def test_mirrored_uncaptured_run_streams_without_retaining(self):
        console, errors = BufferedConsole(), BufferedConsole()
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(errors):
            execution = runner.run_command(
                self.store,
                pathway_id=self.pathway.id,
                argv=[sys.executable, "-c", ECHO_ARGUMENTS, "shown"],
                declared_parameters={},
                mirror=True,
                capture=False,
            )
        self.assertIn(b"shown", console.buffer.getvalue())
        self.assertIn(b"warned", errors.buffer.getvalue())
        self.assertIs(execution.atom.output_capture, model.OutputCapture.NONE)
        self.assertEqual(list(self.store.logs_dir.iterdir()), [])

    def test_uncaptured_run_can_be_a_branch_source_and_compared(self):
        baseline = ocura_oss.run([sys.executable, "-c", "pass"], root=self.root, capture=False)
        child = ocura_oss.branch(baseline.chokepoint.id, root=self.root, reason="vary")
        ocura_oss.run([sys.executable, "-c", "pass"], root=self.root, pathway_id=child.id)
        comparison = ocura_oss.compare(root=self.root)
        self.assertIs(comparison.state, model.ComparisonState.READY)


class MaskedArgumentTests(CaptureControlTestCase):
    def test_masked_token_reaches_the_command_but_not_the_records(self):
        execution = ocura_oss.run(
            [sys.executable, "-c", ECHO_ARGUMENTS, "--token", SECRET, "plain"],
            root=self.root,
            capture=False,
            masked_arguments=[4],
        )
        atom = execution.atom
        self.assertIs(atom.outcome, model.Outcome.PASSED)
        self.assertEqual(
            atom.command,
            (sys.executable, "-c", ECHO_ARGUMENTS, "--token", model.MASKED_ARGUMENT, "plain"),
        )
        self.assertEqual(atom.masked_arguments, (4,))
        self.assertNotIn(SECRET, self.record_text())
        self.assertEqual(self.store.load_atom(atom.id), atom)
        self.assertTrue(self.store.verify_state().ok)

    def test_masking_does_not_alter_captured_output(self):
        atom = ocura_oss.run(
            [sys.executable, "-c", ECHO_ARGUMENTS, SECRET], root=self.root, masked_arguments=[3]
        ).atom
        self.assertNotIn(SECRET, atom.command)
        self.assertEqual(self.store.read_verified_log(atom.id).strip(), SECRET.encode())

    def test_repeated_and_unordered_positions_are_recorded_once_in_order(self):
        atom = ocura_oss.run(
            [sys.executable, "-c", "pass", "a", "b"], root=self.root, masked_arguments=[4, 3, 4]
        ).atom
        self.assertEqual(atom.masked_arguments, (3, 4))
        self.assertEqual(atom.command[3:], (model.MASKED_ARGUMENT, model.MASKED_ARGUMENT))

    def test_literal_placeholder_argument_is_not_reported_as_masked(self):
        atom = ocura_oss.run(
            [sys.executable, "-c", "pass", model.MASKED_ARGUMENT], root=self.root
        ).atom
        self.assertEqual(atom.masked_arguments, ())
        self.assertEqual(atom.command[-1], model.MASKED_ARGUMENT)

    def test_invalid_positions_are_rejected_before_anything_runs(self):
        marker = self.root / "ran"
        command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
        for positions in ([3], [-1], [True], ["1"], [1.0]):
            with self.subTest(positions=positions):
                with self.assertRaises(StoreError) as ctx:
                    ocura_oss.run(command, root=self.root, masked_arguments=positions)
                self.assertIn("is not a command token position", str(ctx.exception))
        self.assertFalse(marker.exists())
        self.assertEqual(self.store.list_atoms(), [])
        self.assertEqual(list(self.store.attempts_dir.iterdir()), [])


class CommandLineTests(CaptureControlTestCase):
    def invoke(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "ocura_oss", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    def test_no_capture_and_mask_arg_flags(self):
        result = self.invoke(
            "run",
            "--root",
            str(self.root),
            "--json",
            "--no-capture",
            "--mask-arg",
            "3",
            "--",
            sys.executable,
            "-c",
            ECHO_ARGUMENTS,
            SECRET,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["output_capture"], "none")
        self.assertIsNone(payload["stdout_log"])
        self.assertIsNone(payload["stderr_log"])
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        atom = self.store.load_atom(payload["atom_id"])
        self.assertEqual(atom.masked_arguments, (3,))
        self.assertNotIn(SECRET, self.record_text())

    def test_captured_run_reports_full_capture(self):
        result = self.invoke(
            "run", "--root", str(self.root), "--json", "--", sys.executable, "-c", "pass"
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["output_capture"], "full")
        self.assertTrue((self.root / payload["stdout_log"]).is_file())

    def test_text_output_states_that_output_was_not_captured(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(
                [
                    "run",
                    "--root",
                    str(self.root),
                    "--quiet",
                    "--no-capture",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("output: not captured", stdout.getvalue())
        self.assertNotIn("stdout log:", stdout.getvalue())

    def test_out_of_range_mask_position_is_an_input_error(self):
        result = self.invoke(
            "run", "--root", str(self.root), "--json", "--mask-arg", "9", "--", "anything"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("error: masked argument 9 is not a command token position", result.stderr)
        self.assertEqual(self.store.list_atoms(), [])


if __name__ == "__main__":
    unittest.main()
