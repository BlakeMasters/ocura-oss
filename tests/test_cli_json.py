# SPDX-License-Identifier: MPL-2.0

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ocura_oss import Store, cli


class CliJsonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project with spaces-caf\u00e9-\u96ea"
        initialized = self.invoke("init", "--name", "JSON example", "--json")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.initial = json.loads(initialized.stdout)

    def invoke(self, command, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "ocura_oss", command, "--root", str(self.root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )

    def test_init_json_identifies_persisted_state(self):
        store = Store(self.root)
        self.assertEqual(self.initial["root"], str(self.root.resolve()))
        self.assertEqual(self.initial["state_dir"], str(store.state_dir))
        self.assertEqual(self.initial["den_id"], store.load_den().id)
        self.assertEqual(self.initial["default_pathway_id"], store.load_den().default_pathway_id)

    def test_noisy_command_keeps_json_clean_and_exact_logs(self):
        command = (
            "import sys; sys.stdout.buffer.write(b'not JSON\\x00\\xff\\n'); "
            "sys.stderr.buffer.write(b'error output\\n')"
        )
        for flags in (("--json",), ("--json", "--quiet")):
            with self.subTest(flags=flags):
                result = self.invoke(
                    "run",
                    *flags,
                    "--param",
                    "private=PARAMETER-SENTINEL",
                    "--",
                    sys.executable,
                    "-c",
                    command,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                payload = json.loads(result.stdout)
                self.assertNotIn("PARAMETER-SENTINEL", result.stdout)
                self.assertNotIn("command", payload)
                self.assertEqual(payload["outcome"], "passed")
                self.assertIsNone(payload["launch_error_category"])
                self.assertEqual(
                    (self.root / payload["stdout_log"]).read_bytes(), b"not JSON\x00\xff\n"
                )
                self.assertEqual(
                    (self.root / payload["stderr_log"]).read_bytes(), b"error output\n"
                )
                store = Store(self.root)
                atom = store.load_atom(payload["atom_id"])
                self.assertEqual(store.load_chokepoint(payload["chokepoint_id"]).atom_id, atom.id)
                self.assertEqual(payload["pathway_id"], atom.pathway_id)
                self.assertTrue(store.verify_state().ok)

    def test_nonzero_and_launch_failure_still_return_json(self):
        cases = (
            ([sys.executable, "-c", "import sys; sys.exit(17)"], 1, "failed", 17),
            (["ocura-json-no-such-executable"], 3, "launch_failed", None),
        )
        for command, code, outcome, return_code in cases:
            with self.subTest(outcome=outcome):
                result = self.invoke("run", "--json", "--", *command)
                self.assertEqual(result.returncode, code, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["outcome"], outcome)
                self.assertEqual(data["return_code"], return_code)
                self.assertTrue(Store(self.root).verify_state().ok)

    def test_invalid_input_has_no_result_and_does_not_launch(self):
        before = Store(self.root).list_atoms()
        for command, arguments in (
            ("init", ("--json",)),
            ("run", ("--json", "--")),
            ("run", ("--json", "--param", "bad", "--", sys.executable, "-c", "pass")),
        ):
            with self.subTest(command=command, arguments=arguments):
                result = self.invoke(command, *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
        self.assertEqual(Store(self.root).list_atoms(), before)

    def test_recorded_interruption_returns_json_and_exit_one(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("ocura_oss.runner._execute", return_value=(None, "interrupted", True)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = cli.main(["run", "--root", str(self.root), "--json", "--", "unused"])
        self.assertEqual(code, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["outcome"], "interrupted")
        self.assertIsNone(result["return_code"])
        self.assertEqual(result["launch_error_category"], "interrupted")
        self.assertTrue(Store(self.root).verify_state().ok)

    def test_complete_workflow_uses_only_returned_ids(self):
        baseline = json.loads(
            self.invoke(
                "run", "--json", "--param", "count=1", "--", sys.executable, "-c", "print(1)"
            ).stdout
        )
        child = json.loads(
            self.invoke(
                "branch",
                "--json",
                "--from",
                baseline["chokepoint_id"],
                "--reason",
                "try two",
                "--param",
                "count=2",
            ).stdout
        )
        attempt = self.invoke(
            "run",
            "--json",
            "--pathway",
            child["id"],
            "--param",
            "count=2",
            "--",
            sys.executable,
            "-c",
            "print(2)",
        )
        self.assertEqual(attempt.returncode, 0, attempt.stderr)
        comparison = json.loads(
            self.invoke("compare", "--json", "--from", baseline["chokepoint_id"]).stdout
        )
        self.assertEqual(comparison["state"], "ready")
        self.assertEqual(
            comparison["children"][0]["child_run"]["id"], json.loads(attempt.stdout)["atom_id"]
        )
        self.assertEqual(self.invoke("verify", "--json").returncode, 0)
