# SPDX-License-Identifier: MPL-2.0

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ocura_oss import Store, StoreError, initialize, run


class VerifiedLogReadTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        initialize(self.root)
        self.store = Store(self.root)
        self.stdout = b"out\x00\xff\r\n"
        self.stderr = b"err\x80\r\n"
        execution = run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.buffer.write({self.stdout!r}); "
                f"sys.stderr.buffer.write({self.stderr!r}); sys.exit(7)",
            ],
            root=self.root,
        )
        self.atom = execution.atom
        self.stdout_path = self.store.resolve_log_path(self.atom, "stdout")
        self.stderr_path = self.store.resolve_log_path(self.atom, "stderr")

    def test_returns_exact_binary_output_from_failed_command_without_writing(self):
        paths = [path for path in self.store.state_dir.rglob("*") if path.is_file()]
        before = {path: path.read_bytes() for path in paths}

        self.assertEqual(self.atom.return_code, 7)
        self.assertEqual(self.store.read_verified_log(self.atom.id), self.stdout)
        self.assertEqual(self.store.read_verified_log(self.atom.id, stream="stderr"), self.stderr)
        after = {
            path: path.read_bytes() for path in self.store.state_dir.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_empty_log_is_valid(self):
        execution = run([sys.executable, "-c", "pass"], root=self.root)
        self.assertEqual(self.store.read_verified_log(execution.atom.id), b"")

    def test_checks_only_selected_log(self):
        self.stderr_path.unlink()
        (self.store.logs_dir / "unrelated.log").write_bytes(b"orphan")
        self.assertEqual(self.store.read_verified_log(self.atom.id), self.stdout)
        self.assertFalse(self.store.verify_state().ok)

    def test_rejects_unknown_stream(self):
        with self.assertRaisesRegex(StoreError, "log stream must be"):
            self.store.read_verified_log(self.atom.id, stream="combined")

    def test_rejects_missing_log(self):
        self.stdout_path.unlink()
        with self.assertRaisesRegex(StoreError, "log is missing"):
            self.store.read_verified_log(self.atom.id)

    def test_rejects_size_and_digest_mismatches_for_either_stream(self):
        for stream, path, original in (
            ("stdout", self.stdout_path, self.stdout),
            ("stderr", self.stderr_path, self.stderr),
        ):
            for changed, error in (
                (original + b"extra", "log size mismatch"),
                (original[:-1], "log size mismatch"),
                (b"X" + original[1:], "log checksum mismatch"),
            ):
                with self.subTest(stream=stream, error=error):
                    path.write_bytes(changed)
                    with self.assertRaisesRegex(StoreError, error):
                        self.store.read_verified_log(self.atom.id, stream=stream)
            path.write_bytes(original)

    def test_rejects_log_changed_at_read_after_earlier_verification(self):
        self.store.verify_atom_evidence(self.atom)
        original_read = Path.read_bytes

        def change_at_read(path):
            if path == self.stdout_path:
                path.write_bytes(b"X" + self.stdout[1:])
            return original_read(path)

        with (
            patch.object(Path, "read_bytes", autospec=True, side_effect=change_at_read),
            self.assertRaisesRegex(StoreError, "log checksum mismatch"),
        ):
            self.store.read_verified_log(self.atom.id)

    def test_returns_verified_contents_when_file_changes_after_read(self):
        original_read = Path.read_bytes

        def change_after_read(path):
            data = original_read(path)
            if path == self.stdout_path:
                path.write_bytes(b"X" + self.stdout[1:])
            return data

        with patch.object(Path, "read_bytes", autospec=True, side_effect=change_after_read):
            self.assertEqual(self.store.read_verified_log(self.atom.id), self.stdout)
        with self.assertRaisesRegex(StoreError, "log checksum mismatch"):
            self.store.read_verified_log(self.atom.id)

    def test_wraps_log_read_errors_as_store_error(self):
        original_read = Path.read_bytes

        def deny_log_read(path):
            if path == self.stdout_path:
                raise PermissionError("read denied")
            return original_read(path)

        with (
            patch.object(Path, "read_bytes", autospec=True, side_effect=deny_log_read),
            self.assertRaisesRegex(StoreError, "log is unreadable") as caught,
        ):
            self.store.read_verified_log(self.atom.id)
        self.assertIsInstance(caught.exception.__cause__, PermissionError)

    def test_rejects_corrupt_stored_atom(self):
        record = self.store.atoms_dir / f"{self.atom.id}.json"
        record.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "malformed atom record"):
            self.store.read_verified_log(self.atom.id)

    def test_rejects_missing_lineage(self):
        (self.store.pathways_dir / f"{self.atom.pathway_id}.json").unlink()
        with self.assertRaisesRegex(StoreError, "missing pathway record"):
            self.store.read_verified_log(self.atom.id)

    def test_rejects_invalid_filesystem_path_as_store_error(self):
        self.store._save_atom(replace(self.atom, stdout_log=".ocura-oss/logs/bad\x00.log"))
        with self.assertRaises(StoreError):
            self.store.read_verified_log(self.atom.id)

    def test_rejects_log_outside_log_directory(self):
        (self.root / "outside.log").write_bytes(self.stdout)
        for relative in ("outside.log", ".ocura-oss/logs/../../outside.log"):
            with self.subTest(relative=relative):
                self.store._save_atom(replace(self.atom, stdout_log=relative))
                with self.assertRaisesRegex(StoreError, "log path"):
                    self.store.read_verified_log(self.atom.id)


if __name__ == "__main__":
    unittest.main()
