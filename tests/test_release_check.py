# SPDX-License-Identifier: Apache-2.0

"""Release parity checks for the version, changelog, documents, tag, and built files."""

import contextlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_release_check():
    spec = importlib.util.spec_from_file_location(
        "check_release", REPO / "tools" / "check_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_release = load_release_check()


def write_checkout(
    root,
    *,
    version="1.2.3",
    changelog_version="1.2.3",
    date="2026-01-02",
    document_version="1.2.3",
    project='dynamic = ["version"]\ndependencies = []\n',
):
    (root / "src" / "ocura_oss").mkdir(parents=True)
    (root / "src" / "ocura_oss" / "__init__.py").write_text(
        f'"""Package."""\n\n__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "ocura-oss"\n{project}', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {changelog_version} - {date}\n\n- Newest change.\n\n"
        "## 1.2.2 - 2025-12-01\n\n- Older change.\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "index.md").write_text(
        f"# Overview\n\nVersion {document_version}.\n", encoding="utf-8"
    )
    (root / "examples" / "sample").mkdir(parents=True)
    (root / "examples" / "sample" / "README.md").write_text("# Sample\n", encoding="utf-8")


def core_metadata(version, extra=""):
    return f"Metadata-Version: 2.4\nName: ocura-oss\nVersion: {version}\n{extra}\nText\n"


MATCHING_METADATA = core_metadata("1.2.3")


def write_distributions(
    dist,
    *,
    metadata_extra="",
    wheel_extra=(),
    sdist_extra=(),
    sdist_metadata=MATCHING_METADATA,
):
    version = "1.2.3"
    dist.mkdir()
    info = f"ocura_oss-{version}.dist-info"
    with zipfile.ZipFile(dist / f"ocura_oss-{version}-py3-none-any.whl", "w") as archive:
        archive.writestr("ocura_oss/__init__.py", "")
        archive.writestr(f"{info}/METADATA", core_metadata(version, metadata_extra))
        for name in wheel_extra:
            archive.writestr(name, "")
    members = {name: b"" for name in sdist_extra}
    if sdist_metadata is not None:
        members[f"ocura_oss-{version}/PKG-INFO"] = sdist_metadata.encode("utf-8")
    with tarfile.open(dist / f"ocura_oss-{version}.tar.gz", "w:gz") as archive:
        for name, body in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))


class SourceParityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_this_checkout_is_consistent(self):
        self.assertEqual(check_release.check_sources(REPO), [])

    def test_matching_checkout_has_no_problems(self):
        write_checkout(self.root)
        self.assertEqual(check_release.check_sources(self.root), [])
        self.assertEqual(check_release.check_sources(self.root, tag="v1.2.3"), [])

    def test_changelog_version_mismatch_is_reported(self):
        write_checkout(self.root, changelog_version="1.2.2")
        problems = check_release.check_sources(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("CHANGELOG.md starts at 1.2.2", problems[0])

    def test_document_version_mismatch_is_reported(self):
        write_checkout(self.root, document_version="1.2.2")
        problems = check_release.check_sources(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("docs/index.md states version 1.2.2", problems[0])

    def test_reference_document_without_version_line_is_reported(self):
        write_checkout(self.root)
        (self.root / "docs" / "extra.md").write_text("# Extra\n", encoding="utf-8")
        problems = check_release.check_sources(self.root)
        self.assertEqual(problems, ["docs/extra.md has no 'Version X.Y.Z.' line"])

    def test_static_version_and_dependencies_are_reported(self):
        write_checkout(self.root, project='version = "1.2.3"\ndependencies = ["requests"]\n')
        problems = check_release.check_sources(self.root)
        self.assertEqual(len(problems), 2)
        self.assertIn("must take its version from", problems[0])
        self.assertIn("declares runtime dependencies", problems[1])

    def test_unreleased_entry_passes_until_tagged(self):
        write_checkout(self.root, date="Unreleased")
        self.assertEqual(check_release.check_sources(self.root), [])
        problems = check_release.check_sources(self.root, tag="v1.2.3")
        self.assertEqual(problems, ["CHANGELOG.md still marks 1.2.3 as Unreleased"])

    def test_malformed_changelog_date_is_reported(self):
        write_checkout(self.root, date="January")
        problems = check_release.check_sources(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("neither YYYY-MM-DD nor Unreleased", problems[0])

    def test_tag_must_name_the_package_version(self):
        write_checkout(self.root)
        problems = check_release.check_sources(self.root, tag="v1.2.4")
        self.assertEqual(problems, ["tag v1.2.4 does not match package version 1.2.3"])

    def test_missing_version_assignment_stops_the_check(self):
        write_checkout(self.root)
        (self.root / "src" / "ocura_oss" / "__init__.py").write_text("", encoding="utf-8")
        with self.assertRaises(check_release.ReleaseError):
            check_release.check_sources(self.root)

    def test_notes_are_the_newest_entry_only(self):
        write_checkout(self.root)
        self.assertEqual(
            check_release.changelog_entry(self.root), ("1.2.3", "2026-01-02", "- Newest change.")
        )


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dist = Path(self._tmp.name) / "dist"

    def test_expected_distributions_pass(self):
        write_distributions(self.dist)
        self.assertEqual(check_release.check_distributions(self.dist, "1.2.3"), [])

    def test_other_versions_in_the_directory_are_reported(self):
        write_distributions(self.dist)
        (self.dist / "ocura_oss-1.2.2.tar.gz").write_bytes(b"")
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(len(problems), 1)
        self.assertIn("must contain exactly", problems[0])

    def test_missing_directory_is_reported(self):
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(len(problems), 1)
        self.assertIn("found: nothing", problems[0])

    def test_runtime_dependency_is_reported(self):
        write_distributions(self.dist, metadata_extra="Requires-Dist: requests\n")
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(problems, ["wheel metadata declares runtime dependencies"])

    def test_unexpected_wheel_member_is_reported(self):
        write_distributions(self.dist, wheel_extra=("examples/train.py",))
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(problems, ["wheel contains unexpected files: examples/train.py"])

    def test_source_distribution_metadata_version_is_checked(self):
        write_distributions(self.dist, sdist_metadata=core_metadata("1.2.2"))
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(
            problems, ["source distribution metadata states version 1.2.2, expected 1.2.3"]
        )

    def test_source_distribution_runtime_dependency_is_reported(self):
        write_distributions(
            self.dist, sdist_metadata=core_metadata("1.2.3", "Requires-Dist: requests\n")
        )
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(problems, ["source distribution metadata declares runtime dependencies"])

    def test_source_distribution_without_metadata_is_reported(self):
        write_distributions(self.dist, sdist_metadata=None, sdist_extra=("ocura_oss-1.2.3/x",))
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(problems, ["source distribution has no ocura_oss-1.2.3/PKG-INFO"])

    def test_examples_in_the_source_distribution_are_reported(self):
        write_distributions(self.dist, sdist_extra=("ocura_oss-1.2.3/examples/train.py",))
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(problems, ["source distribution includes repository-only examples"])

    def test_oversized_wheel_is_reported(self):
        write_distributions(self.dist)
        wheel = self.dist / "ocura_oss-1.2.3-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "a", zipfile.ZIP_STORED) as archive:
            archive.writestr("ocura_oss/padding.py", b"#" * check_release.WHEEL_BUDGET_BYTES)
        problems = check_release.check_distributions(self.dist, "1.2.3")
        self.assertEqual(len(problems), 1)
        self.assertIn("the budget is", problems[0])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _main(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = check_release.main(["--repo", str(self.root), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_consistent_checkout_exits_zero(self):
        write_checkout(self.root)
        code, stdout, _stderr = self._main("--tag", "v1.2.3")
        self.assertEqual(code, 0)
        self.assertIn("release check passed: ocura-oss 1.2.3", stdout)

    def test_problems_exit_one_and_are_listed(self):
        write_checkout(self.root, document_version="1.2.2")
        code, _stdout, stderr = self._main()
        self.assertEqual(code, 1)
        self.assertIn("problem: docs/index.md states version 1.2.2", stderr)

    def test_unreadable_checkout_exits_two(self):
        code, _stdout, stderr = self._main()
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("error: "))

    def test_notes_prints_only_the_newest_entry(self):
        write_checkout(self.root)
        code, stdout, _stderr = self._main("--notes")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "- Newest change.\n")


if __name__ == "__main__":
    unittest.main()
