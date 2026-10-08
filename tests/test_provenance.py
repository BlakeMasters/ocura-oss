# SPDX-License-Identifier: MPL-2.0

"""Parameter substitution, retained manifests, and opt-in context capture."""

import contextlib
import hashlib
import io
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ocura_oss
from ocura_oss import cli, context, model, runner
from ocura_oss.store import Store, StoreError

PRINT_ARGUMENTS = "import sys; sys.stdout.write('|'.join(sys.argv[1:]))"
GIT = shutil.which("git")
GIT_IDENTITY = (
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


class ProvenanceTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "proj"
        self.store = Store(self.root)
        _, self.pathway = self.store.initialize_state(name="provenance")

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([arguments[0], "--root", str(self.root), *arguments[1:]])
        return code, stdout.getvalue(), stderr.getvalue()

    def output(self, atom):
        return self.store.read_verified_log(atom.id).decode("utf-8")


class SubstitutionTests(ProvenanceTestCase):
    def run_substituted(self, *tokens, **options):
        return ocura_oss.run(
            [sys.executable, "-c", PRINT_ARGUMENTS, *tokens],
            root=self.root,
            substitute=True,
            **options,
        ).atom

    def test_placeholders_take_declared_values_and_are_recorded_as_run(self):
        atom = self.run_substituted(
            "--batch", "{batch}", "--name=run-{batch}-{lr}", parameters={"batch": "4", "lr": "0.1"}
        )
        self.assertEqual(self.output(atom), "--batch|4|--name=run-4-0.1")
        self.assertEqual(atom.command[3:], ("--batch", "4", "--name=run-4-0.1"))
        self.assertEqual(atom.declared_parameters, {"batch": "4", "lr": "0.1"})

    def test_pathway_parameters_fill_placeholders_and_become_run_labels(self):
        baseline = ocura_oss.run([sys.executable, "-c", "pass"], root=self.root)
        child = ocura_oss.branch(
            baseline.chokepoint.id,
            root=self.root,
            reason="larger batch",
            parameters={"batch": "8", "unused": "kept-on-pathway"},
        )
        atom = self.run_substituted("{batch}", pathway_id=child.id)
        self.assertEqual(self.output(atom), "8")
        self.assertEqual(atom.declared_parameters, {"batch": "8"})

    def test_declared_value_overrides_the_pathway_value(self):
        baseline = ocura_oss.run([sys.executable, "-c", "pass"], root=self.root)
        child = ocura_oss.branch(
            baseline.chokepoint.id, root=self.root, reason="vary", parameters={"batch": "8"}
        )
        atom = self.run_substituted("{batch}", pathway_id=child.id, parameters={"batch": "16"})
        self.assertEqual(self.output(atom), "16")
        self.assertEqual(atom.declared_parameters, {"batch": "16"})

    def test_declared_labels_need_not_appear_in_the_command(self):
        atom = self.run_substituted("{batch}", parameters={"batch": "2", "note": "label only"})
        self.assertEqual(atom.declared_parameters, {"batch": "2", "note": "label only"})

    def test_doubled_braces_are_literal(self):
        atom = self.run_substituted("{{batch}}={batch}", "{{}}", parameters={"batch": "2"})
        self.assertEqual(self.output(atom), "{batch}=2|{}")

    def test_keys_may_contain_periods_and_hyphens(self):
        atom = self.run_substituted("{model.hidden-size}", parameters={"model.hidden-size": "64"})
        self.assertEqual(self.output(atom), "64")

    def test_braces_are_untouched_unless_substitution_is_requested(self):
        atom = ocura_oss.run(
            [sys.executable, "-c", PRINT_ARGUMENTS, "{batch}", "{"],
            root=self.root,
            parameters={"batch": "4"},
        ).atom
        self.assertEqual(self.output(atom), "{batch}|{")

    def test_unknown_or_unbalanced_placeholders_stop_before_anything_runs(self):
        cases = (
            ("{missing}", "command placeholder {missing} has no parameter value"),
            ("{batch", "unbalanced brace"),
            ("batch}", "unbalanced brace"),
            ("{}", "unbalanced brace"),
            ("{not a key}", "unbalanced brace"),
        )
        for token, message in cases:
            with self.subTest(token=token):
                with self.assertRaises(StoreError) as ctx:
                    self.run_substituted(token, parameters={"batch": "4"})
                self.assertIn(message, str(ctx.exception))
        self.assertEqual(self.store.list_atoms(), [])
        self.assertEqual(list(self.store.attempts_dir.iterdir()), [])

    def test_masking_hides_the_token_but_not_the_substituted_label(self):
        atom = self.run_substituted(
            "{batch}", "plain", parameters={"batch": "4"}, masked_arguments=[3]
        )
        self.assertEqual(atom.command[3:], (model.MASKED_ARGUMENT, "plain"))
        self.assertEqual(self.output(atom), "4|plain")
        self.assertEqual(atom.declared_parameters, {"batch": "4"})

    def test_command_line_flag_substitutes(self):
        code, stdout, stderr = self.invoke(
            "run",
            "--json",
            "--param",
            "batch=4",
            "--substitute",
            "--",
            sys.executable,
            "-c",
            PRINT_ARGUMENTS,
            "--batch",
            "{batch}",
        )
        self.assertEqual(code, 0, stderr)
        atom = self.store.load_atom(json.loads(stdout)["atom_id"])
        self.assertEqual(self.output(atom), "--batch|4")

        code, stdout, stderr = self.invoke(
            "run", "--json", "--substitute", "--", sys.executable, "-c", "pass", "{absent}"
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("error: command placeholder {absent} has no parameter value", stderr)


class ManifestTests(ProvenanceTestCase):
    def setUp(self):
        super().setUp()
        self.baseline = ocura_oss.run(
            [sys.executable, "-c", "print('kept')"], root=self.root, parameters={"batch": "1"}
        )

    def rewrite_atom(self, atom_id, **changes):
        """Rewrite a record and its checksum together, as a writer of the state could."""
        path = self.store.atoms_dir / f"{atom_id}.json"
        envelope = json.loads(path.read_text("utf-8"))
        envelope["payload"].update(changes)
        envelope["checksum"] = model.checksum_for(
            envelope["schema_version"], envelope["kind"], envelope["payload"]
        )
        path.write_text(json.dumps(envelope), "utf-8")

    def test_manifest_lists_every_record_and_digest_matches_its_text(self):
        report = ocura_oss.verify(self.root)
        self.assertEqual(len(report.manifest), 4)
        self.assertEqual(list(report.manifest), sorted(report.manifest))
        self.assertEqual(
            sorted(entry.split(" ")[0] for entry in report.manifest),
            ["atom", "chokepoint", "den", "pathway"],
        )
        for entry in report.manifest:
            kind, record_id, checksum = entry.split(" ")
            directory = self.store.state_dir if kind == "den" else self.store.state_dir / f"{kind}s"
            name = "den.json" if kind == "den" else f"{record_id}.json"
            stored = json.loads((directory / name).read_text("utf-8"))
            self.assertEqual(stored["checksum"]["value"], checksum)
        text = "".join(f"{entry}\n" for entry in report.manifest).encode("utf-8")
        self.assertEqual(report.digest, hashlib.sha256(text).hexdigest())
        self.assertEqual(ocura_oss.verify(self.root).digest, report.digest)

    def test_digest_changes_when_a_record_is_added(self):
        before = ocura_oss.verify(self.root)
        ocura_oss.run([sys.executable, "-c", "pass"], root=self.root)
        after = ocura_oss.verify(self.root)
        self.assertNotEqual(before.digest, after.digest)
        self.assertTrue(set(before.manifest) < set(after.manifest))

    def test_failed_verification_has_no_manifest_or_digest(self):
        (self.root / self.baseline.atom.stdout_log).write_bytes(b"changed")
        report = ocura_oss.verify(self.root)
        self.assertFalse(report.ok)
        self.assertEqual(report.manifest, ())
        self.assertIsNone(report.digest)

    def test_retained_manifest_accepts_later_records(self):
        retained = ocura_oss.verify(self.root).manifest
        child = ocura_oss.branch(self.baseline.chokepoint.id, root=self.root, reason="more")
        ocura_oss.run([sys.executable, "-c", "pass"], root=self.root, pathway_id=child.id)
        report = ocura_oss.verify(self.root, against=retained)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(len(report.manifest), 7)

    def test_retained_manifest_detects_a_record_rewritten_with_its_checksum(self):
        retained = ocura_oss.verify(self.root).manifest
        self.rewrite_atom(self.baseline.atom.id, declared_parameters={"batch": "64"})
        self.assertTrue(ocura_oss.verify(self.root).ok, "checksums alone cannot see this change")

        report = ocura_oss.verify(self.root, against=retained)
        self.assertEqual(
            report.problems,
            ((f"{self.baseline.atom.id}.json", "retained atom record is missing or was changed"),),
        )
        self.assertEqual((report.manifest, report.digest), ((), None))

    def test_retained_manifest_detects_a_replaced_log(self):
        retained = ocura_oss.verify(self.root).manifest
        log = self.root / self.baseline.atom.stdout_log
        log.write_bytes(b"forged\n")
        self.rewrite_atom(
            self.baseline.atom.id,
            stdout_bytes=7,
            stdout_sha256=hashlib.sha256(b"forged\n").hexdigest(),
        )
        self.assertTrue(ocura_oss.verify(self.root).ok)
        self.assertFalse(ocura_oss.verify(self.root, against=retained).ok)

    def test_retained_manifest_detects_a_removed_run(self):
        extra = ocura_oss.run([sys.executable, "-c", "pass"], root=self.root)
        retained = ocura_oss.verify(self.root).manifest
        (self.store.atoms_dir / f"{extra.atom.id}.json").unlink()
        (self.store.chokepoints_dir / f"{extra.chokepoint.id}.json").unlink()
        for relative in (extra.atom.stdout_log, extra.atom.stderr_log):
            (self.root / relative).unlink()
        self.assertTrue(ocura_oss.verify(self.root).ok)
        report = ocura_oss.verify(self.root, against=retained)
        self.assertEqual(
            sorted(problem for _record, problem in report.problems),
            [
                "retained atom record is missing or was changed",
                "retained chokepoint record is missing or was changed",
            ],
        )

    def test_retained_manifest_ignores_blank_lines_and_comments(self):
        retained = ["# kept 2026-10-07", "", *ocura_oss.verify(self.root).manifest, "  "]
        self.assertTrue(ocura_oss.verify(self.root, against=retained).ok)

    def test_retained_manifest_without_entries_is_rejected(self):
        self.rewrite_atom(self.baseline.atom.id, declared_parameters={"batch": "64"})
        for lines in ([], [""], ["# exported 2026-10-07", "   "]):
            with self.subTest(lines=lines), self.assertRaises(StoreError) as ctx:
                ocura_oss.verify(self.root, against=lines)
            self.assertIn("retained manifest lists no records", str(ctx.exception))
        self.assertTrue(ocura_oss.verify(self.root).ok, "no manifest at all is still allowed")

    def test_empty_retained_file_from_a_failed_export_is_an_error(self):
        (self.root / self.baseline.atom.stdout_log).write_bytes(b"changed")
        retained = Path(self._temporary.name) / "retained.manifest"
        code, stdout, _stderr = self.invoke("manifest")
        self.assertEqual((code, stdout), (2, ""))
        retained.write_text(stdout, "utf-8")

        code, stdout, stderr = self.invoke("verify", "--json", "--against", str(retained))
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("error: retained manifest lists no records", stderr)

    def test_malformed_retained_entries_are_rejected(self):
        den = self.store.load_den().id
        digest = "0" * 64
        for entry in (
            "atom",
            f"den {den}",
            f"den {den} {digest} extra",
            f"atom {den} {digest}",
            f"den {den} {digest[:-1]}",
            f"den {den} {digest[:-1]}G",
            f"thing {den} {digest}",
        ):
            with self.subTest(entry=entry), self.assertRaises(StoreError) as ctx:
                ocura_oss.verify(self.root, against=[entry])
            self.assertIn("malformed manifest entry", str(ctx.exception))

    def test_command_line_manifest_and_verify_against(self):
        code, stdout, _stderr = self.invoke("manifest")
        self.assertEqual(code, 0)
        report = ocura_oss.verify(self.root)
        self.assertEqual(stdout.splitlines(), list(report.manifest))

        code, stdout, _stderr = self.invoke("manifest", "--json")
        self.assertEqual(
            json.loads(stdout), {"digest": report.digest, "entries": list(report.manifest)}
        )
        code, stdout, _stderr = self.invoke("verify", "--json")
        self.assertEqual(json.loads(stdout)["digest"], report.digest)
        code, stdout, _stderr = self.invoke("verify")
        self.assertIn(f"digest: {report.digest}\n", stdout)

        retained = Path(self._temporary.name) / "retained.manifest"
        retained.write_text("".join(f"{entry}\r\n" for entry in report.manifest), "utf-8")
        code, stdout, _stderr = self.invoke("verify", "--json", "--against", str(retained))
        self.assertEqual(code, 0, stdout)

        self.rewrite_atom(self.baseline.atom.id, declared_parameters={"batch": "64"})
        code, stdout, _stderr = self.invoke("verify", "--json", "--against", str(retained))
        self.assertEqual(code, 2)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["digest"])
        self.assertEqual(
            result["problems"][0]["problem"], "retained atom record is missing or was changed"
        )

    def test_command_line_manifest_refuses_an_unverified_state(self):
        (self.root / self.baseline.atom.stdout_log).write_bytes(b"changed")
        code, stdout, stderr = self.invoke("manifest")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("a manifest requires a fully verified state", stderr)

    def test_missing_retained_file_is_an_error(self):
        code, stdout, stderr = self.invoke("verify", "--against", str(self.root / "absent"))
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("error:", stderr)

    def test_retained_file_written_by_a_shell_redirect_is_read(self):
        text = "".join(f"{entry}\r\n" for entry in ocura_oss.verify(self.root).manifest)
        retained = Path(self._temporary.name) / "retained.manifest"
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                mark = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}.get(encoding, b"")
                retained.write_bytes(mark + text.encode(encoding))
                code, stdout, stderr = self.invoke("verify", "--against", str(retained))
                self.assertEqual(code, 0, stdout + stderr)

    def test_retained_file_that_is_not_text_is_an_error(self):
        retained = Path(self._temporary.name) / "retained.manifest"
        retained.write_bytes(b"den \xff\xff\xff")
        code, stdout, stderr = self.invoke("verify", "--against", str(retained))
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("is not UTF-8 or UTF-16 text", stderr)


class ContextTests(ProvenanceTestCase):
    def run_plain(self, **options):
        return ocura_oss.run([sys.executable, "-c", "pass"], root=self.root, **options).atom

    def git(self, *arguments):
        subprocess.run(
            [GIT, *GIT_IDENTITY, *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def stored_payload(self, atom):
        path = self.store.atoms_dir / f"{atom.id}.json"
        return json.loads(path.read_text("utf-8"))["payload"]

    def test_nothing_is_captured_or_executed_unless_requested(self):
        with (
            mock.patch.object(context.subprocess, "run", side_effect=AssertionError("git ran")),
            mock.patch.object(platform, "system", side_effect=AssertionError("platform read")),
        ):
            atom = self.run_plain()
        self.assertIsNone(atom.context)
        self.assertNotIn("context", self.stored_payload(atom))

    def test_platform_is_recorded_on_request(self):
        atom = self.run_plain(context=True)
        self.assertEqual(
            atom.context.platform,
            {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
        )
        self.assertEqual(atom.context.files, {})
        self.assertEqual(self.store.load_atom(atom.id), atom)
        self.assertTrue(self.store.verify_state().ok)

    def test_git_is_absent_outside_a_work_tree_or_without_git(self):
        with mock.patch.object(context.subprocess, "run", side_effect=FileNotFoundError):
            atom = self.run_plain(context=True)
        self.assertIsNone(atom.context.git_revision)
        self.assertIsNone(atom.context.git_dirty)
        self.assertIsNone(self.stored_payload(atom)["context"]["git"])
        self.assertIsNotNone(atom.context.platform)

    def test_unexpected_git_output_is_not_recorded_as_a_revision(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"not a revision\n", stderr=b"")
        with mock.patch.object(context.subprocess, "run", return_value=completed):
            atom = self.run_plain(context=True)
        self.assertIsNone(atom.context.git_revision)
        self.assertEqual(self.store.load_atom(atom.id), atom)

    @unittest.skipUnless(GIT, "requires git")
    def test_git_revision_and_dirty_state_are_recorded(self):
        self.git("init", "--quiet")
        (self.root / "train.py").write_text("print('v1')\n", "utf-8")
        self.git("add", "train.py")
        self.git("commit", "--quiet", "-m", "first")
        head = subprocess.run(
            [GIT, "rev-parse", "HEAD"], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

        clean = self.run_plain(context=True)
        self.assertEqual(clean.context.git_revision, head)
        self.assertIs(clean.context.git_dirty, False, "the ledger itself must not count as dirty")

        (self.root / "train.py").write_text("print('v2')\n", "utf-8")
        modified = self.run_plain(context=True)
        self.assertEqual(modified.context.git_revision, head)
        self.assertIs(modified.context.git_dirty, True)

        self.git("checkout", "--quiet", "--", "train.py")
        (self.root / "notes.txt").write_text("untracked\n", "utf-8")
        self.assertIs(self.run_plain(context=True).context.git_dirty, True)

    def commit_one_file(self):
        self.git("init", "--quiet")
        (self.root / "train.py").write_text("print('v1')\n", "utf-8")
        self.git("add", "train.py")
        self.git("commit", "--quiet", "-m", "first")

    @unittest.skipUnless(GIT, "requires git")
    def test_untracked_files_count_even_when_git_is_configured_to_hide_them(self):
        self.commit_one_file()
        self.git("config", "status.showUntrackedFiles", "no")
        self.assertIs(self.run_plain(context=True).context.git_dirty, False)
        (self.root / "input.csv").write_text("an input git was told not to show\n", "utf-8")
        self.assertIs(self.run_plain(context=True).context.git_dirty, True)

    @unittest.skipUnless(GIT, "requires git")
    def test_ignored_files_do_not_count_as_dirty(self):
        self.commit_one_file()
        (self.root / ".gitignore").write_text("*.cache\n", "utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "--quiet", "-m", "ignore caches")
        (self.root / "data.cache").write_text("ignored\n", "utf-8")
        self.assertIs(self.run_plain(context=True).context.git_dirty, False)

    @unittest.skipUnless(GIT, "requires git")
    def test_file_names_outside_the_platform_encoding_do_not_stop_capture(self):
        self.commit_one_file()
        # With quoting off, git prints these UTF-8 bytes as they are. Several of
        # them are undefined in the Windows default code page.
        self.git("config", "core.quotePath", "false")
        (self.root / "\u0141\u00f3d\u017a \u6771\u4eac.txt").write_text("untracked\n", "utf-8")
        atom = self.run_plain(context=True)
        self.assertIs(atom.outcome, model.Outcome.PASSED)
        self.assertIs(atom.context.git_dirty, True)
        self.assertEqual(len(atom.context.git_revision), 40)

    def test_git_output_is_never_decoded_as_text(self):
        revision = b"a" * 40 + b"\n"
        status = b"?? \x81\x8d\x8f\x90\x9d\xff.txt\n"
        completed = [
            subprocess.CompletedProcess([], 0, stdout=revision, stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=status, stderr=b""),
        ]
        with mock.patch.object(context.subprocess, "run", side_effect=completed) as run:
            atom = self.run_plain(context=True)
        self.assertEqual(atom.context.git_revision, "a" * 40)
        self.assertIs(atom.context.git_dirty, True)
        for call in run.call_args_list:
            self.assertNotIn("text", call.kwargs)
            self.assertNotIn("encoding", call.kwargs)
            self.assertEqual(call.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
        status_arguments = run.call_args_list[1].args[0]
        self.assertIn("--untracked-files=normal", status_arguments)
        self.assertIn("--ignore-submodules=none", status_arguments)

    @unittest.skipUnless(GIT, "requires git")
    def test_repository_without_commits_has_no_git_state(self):
        self.git("init", "--quiet")
        atom = self.run_plain(context=True)
        self.assertIsNone(atom.context.git_revision)
        self.assertIsNone(atom.context.git_dirty)

    def test_named_files_are_measured_without_other_context(self):
        (self.root / "configs").mkdir()
        inside = self.root / "configs" / "run.yaml"
        inside.write_bytes(b"batch: 4\n")
        outside = Path(self._temporary.name) / "data.bin"
        outside.write_bytes(b"\x00\x01\x02")
        atom = self.run_plain(context_files=[Path("configs") / "run.yaml", outside])

        self.assertIsNone(atom.context.platform)
        self.assertIsNone(atom.context.git_revision)
        self.assertEqual(
            atom.context.files,
            {
                "configs/run.yaml": model.FileDigest(
                    bytes=9, sha256=hashlib.sha256(b"batch: 4\n").hexdigest()
                ),
                outside.as_posix(): model.FileDigest(
                    bytes=3, sha256=hashlib.sha256(b"\x00\x01\x02").hexdigest()
                ),
            },
        )
        self.assertEqual(self.store.load_atom(atom.id), atom)
        inside.write_bytes(b"batch: 8\n")
        self.assertTrue(self.store.verify_state().ok, "context describes launch, not the present")

    def test_unreadable_named_file_stops_before_anything_runs(self):
        with self.assertRaises(StoreError) as ctx:
            self.run_plain(context_files=["absent.yaml"])
        self.assertIn("cannot read context file absent.yaml", str(ctx.exception))
        self.assertEqual(self.store.list_atoms(), [])
        self.assertEqual(list(self.store.attempts_dir.iterdir()), [])

    def test_context_survives_abandonment_and_recovery(self):
        (self.root / "config.json").write_text("{}", "utf-8")

        def die(tokens, store, stdout_handle, stderr_handle, mirror):
            raise KeyboardInterrupt

        with mock.patch.object(runner, "_execute", die), self.assertRaises(KeyboardInterrupt):
            self.run_plain(context=True, context_files=["config.json"])
        ((attempt, _state),) = self.store.list_attempts()
        self.assertEqual(set(attempt.context.files), {"config.json"})
        self.store.recover()
        atom = self.store.load_atom(attempt.id)
        self.assertIs(atom.outcome, model.Outcome.ABANDONED)
        self.assertEqual(atom.context, attempt.context)

    def test_context_fields_from_a_newer_writer_are_ignored(self):
        atom = self.run_plain(context=True)
        path = self.store.atoms_dir / f"{atom.id}.json"
        envelope = json.loads(path.read_text("utf-8"))
        envelope["payload"]["context"]["added_later"] = [1, 2]
        envelope["payload"]["context"]["platform"]["added_later"] = "value"
        envelope["checksum"] = model.checksum_for(2, "atom", envelope["payload"])
        path.write_text(json.dumps(envelope), "utf-8")
        loaded = self.store.load_atom(atom.id)
        self.assertEqual(loaded.context.platform["system"], platform.system())
        self.assertEqual(loaded.context.platform["added_later"], "value")

    def test_malformed_context_is_rejected(self):
        base = model.atom_to_payload(self.run_plain(context=True, context_files=[]))
        digest = "0" * 64
        cases = {
            "context not an object": [],
            "platform not an object": {"platform": "linux"},
            "platform value not a string": {"platform": {"system": 1}},
            "git not an object": {"git": "abc"},
            "git revision too short": {"git": {"revision": "abc123", "dirty": False}},
            "git dirty not a boolean": {"git": {"revision": "a" * 40, "dirty": "no"}},
            "git without dirty": {"git": {"revision": "a" * 40}},
            "files not an object": {"files": []},
            "file entry not an object": {"files": {"a": digest}},
            "file bytes negative": {"files": {"a": {"bytes": -1, "sha256": digest}}},
            "file digest malformed": {"files": {"a": {"bytes": 1, "sha256": "zz"}}},
        }
        for label, value in cases.items():
            with self.subTest(case=label), self.assertRaises(model.ValidationError):
                model.atom_from_payload({**base, "context": value})

    def test_command_line_flags_record_context(self):
        (self.root / "config.json").write_text("{}", "utf-8")
        code, stdout, stderr = self.invoke(
            "run",
            "--json",
            "--context",
            "--context-file",
            "config.json",
            "--",
            sys.executable,
            "-c",
            "pass",
        )
        self.assertEqual(code, 0, stderr)
        atom = self.store.load_atom(json.loads(stdout)["atom_id"])
        self.assertEqual(atom.context.platform["system"], platform.system())
        self.assertEqual(set(atom.context.files), {"config.json"})

        code, stdout, stderr = self.invoke(
            "run", "--json", "--context-file", "absent.json", "--", sys.executable, "-c", "pass"
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("error: cannot read context file absent.json", stderr)


if __name__ == "__main__":
    unittest.main()
