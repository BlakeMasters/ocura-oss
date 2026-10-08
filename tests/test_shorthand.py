# SPDX-License-Identifier: Apache-2.0

"""Identifier prefixes, inherited run parameters, and the missing-state hint."""

import contextlib
import io
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ocura_oss
from ocura_oss import cli, model
from ocura_oss.store import Store, StoreError

PASS = (sys.executable, "-c", "pass")


class ShorthandTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.parent = Path(self._temporary.name)
        self.root = self.parent / "proj"
        self.store = Store(self.root)
        self.store.initialize_state(name="shorthand")

    def invoke(self, *arguments, root=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([arguments[0], "--root", str(root or self.root), *arguments[1:]])
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_json(self, *arguments):
        code, stdout, stderr = self.invoke(arguments[0], "--json", *arguments[1:])
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def run_json(self, *options):
        return self.invoke_json("run", *options, "--", *PASS)


class MissingStateTests(ShorthandTestCase):
    def test_every_state_command_names_init(self):
        empty = self.parent / "empty"
        empty.mkdir()
        for command in ("pathways", "chokepoints", "compare", "verify", "manifest", "attempts"):
            with self.subTest(command=command):
                code, stdout, stderr = self.invoke(command, root=empty)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn(f"no Ocura OSS state found under {empty.resolve()}", stderr)
                self.assertIn("`ocura-oss init`", stderr)

    def test_run_names_init_and_launches_nothing(self):
        empty = self.parent / "empty"
        empty.mkdir()
        marker = empty / "ran.txt"
        code, _stdout, stderr = self.invoke(
            "run",
            "--",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close()",
            root=empty,
        )
        self.assertEqual(code, 2)
        self.assertIn("`ocura-oss init`", stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((empty / ".ocura-oss").exists())

    def test_a_subdirectory_is_pointed_at_the_state_above_it(self):
        nested = self.root / "src" / "package"
        nested.mkdir(parents=True)
        code, _stdout, stderr = self.invoke("chokepoints", root=nested)
        self.assertEqual(code, 2)
        self.assertIn(f"there is state under {self.root.resolve()}:", stderr)
        self.assertIn("use that as the root", stderr)

    def test_the_nearest_state_above_is_the_one_named(self):
        inner = self.root / "inner"
        Store(inner).initialize_state(name="inner")
        nested = inner / "deeper"
        nested.mkdir()
        _code, _stdout, stderr = self.invoke("chokepoints", root=nested)
        self.assertIn(f"there is state under {inner.resolve()}:", stderr)

    def test_an_incomplete_state_directory_is_not_answered_with_init(self):
        broken = self.parent / "broken"
        (broken / ".ocura-oss").mkdir(parents=True)
        code, _stdout, stderr = self.invoke("verify", root=broken)
        self.assertEqual(code, 2)
        self.assertIn("exists but has no den record", stderr)
        self.assertNotIn("ocura-oss init", stderr)

    def test_the_python_api_raises_the_same_message(self):
        empty = self.parent / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(StoreError, "run `ocura-oss init` there"):
            ocura_oss.verify(empty)


class IdentifierPrefixTests(ShorthandTestCase):
    def setUp(self):
        super().setUp()
        self.baseline = self.run_json("--param", "count=1")
        self.chokepoint = self.baseline["chokepoint_id"]
        self.hex = self.chokepoint.removeprefix("chokepoint-")

    def test_branch_accepts_bare_and_kind_word_prefixes(self):
        for source in (self.hex[:4], self.hex[:12], f"chokepoint-{self.hex[:6]}", self.hex):
            with self.subTest(source=source):
                child = self.invoke_json("branch", "--from", source, "--reason", "prefix")
                self.assertEqual(child["source_chokepoint_id"], self.chokepoint)

    def test_run_accepts_a_pathway_prefix_and_records_the_full_id(self):
        child = self.invoke_json("branch", "--from", self.chokepoint, "--reason", "prefix")
        short = child["id"].removeprefix("pathway-")[:8]
        for pathway in (short, f"pathway-{short}"):
            with self.subTest(pathway=pathway):
                result = self.run_json("--pathway", pathway)
                self.assertEqual(result["pathway_id"], child["id"])
                self.assertEqual(self.store.load_atom(result["atom_id"]).pathway_id, child["id"])

    def test_compare_accepts_a_prefix(self):
        self.invoke_json("branch", "--from", self.chokepoint, "--reason", "prefix")
        comparison = self.invoke_json("compare", "--from", self.hex[:8])
        self.assertEqual(comparison["source_chokepoint_id"], self.chokepoint)
        self.assertEqual(comparison["state"], "partial")

    def test_text_output_shows_the_full_id(self):
        code, stdout, _stderr = self.invoke("branch", "--from", self.hex[:8], "--reason", "prefix")
        self.assertEqual(code, 0)
        self.assertIn(f"source chokepoint: {self.chokepoint}", stdout)

    def test_too_short_or_malformed_prefixes_are_rejected_before_any_write(self):
        other_kind = self.baseline["pathway_id"]
        for source in (self.hex[:3], "", self.hex[:8].upper() + "G", "chokepoint-", other_kind):
            with self.subTest(source=source):
                code, stdout, stderr = self.invoke("branch", "--from", source, "--reason", "no")
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("malformed chokepoint identifier", stderr)
                self.assertIn("at least 4 of its leading hexadecimal characters", stderr)
        self.assertEqual(len(self.store.list_pathways()), 1)

    def test_a_prefix_that_matches_nothing_is_rejected(self):
        absent = "0000" if not self.hex.startswith("0000") else "1111"
        code, _stdout, stderr = self.invoke("compare", "--from", absent)
        self.assertEqual(code, 2)
        self.assertIn(f"no chokepoint identifier starts with chokepoint-{absent}", stderr)

    def test_a_pathway_prefix_that_matches_nothing_launches_nothing(self):
        absent = "0000" if not self.baseline["pathway_id"].startswith("pathway-0000") else "1111"
        code, _stdout, stderr = self.invoke("run", "--pathway", absent, "--", *PASS)
        self.assertEqual(code, 2)
        self.assertIn(f"no pathway identifier starts with pathway-{absent}", stderr)
        self.assertEqual(len(self.store.list_atoms()), 1)

    def test_files_that_are_not_records_do_not_count_as_matches(self):
        stray = self.store.chokepoints_dir / f"chokepoint-{self.hex[:8]}-copy.json"
        stray.write_text("{}", encoding="utf-8")
        self.assertEqual(self.store.resolve_id(self.hex[:8], "chokepoint"), self.chokepoint)

    def test_a_full_id_is_returned_without_looking_it_up(self):
        unknown = "chokepoint-" + "0" * 32
        self.assertEqual(self.store.resolve_id(unknown, "chokepoint"), unknown)
        code, _stdout, stderr = self.invoke("branch", "--from", unknown, "--reason", "no")
        self.assertEqual(code, 2)
        self.assertNotIn("starts with", stderr)


class AmbiguousPrefixTests(ShorthandTestCase):
    def record_runs(self, count):
        numbers = itertools.count(1)

        def shared_prefix(kind):
            return f"{kind}-abcd{next(numbers):028x}"

        with mock.patch.object(model, "make_id", side_effect=shared_prefix):
            return [self.run_json()["chokepoint_id"] for _ in range(count)]

    def test_an_ambiguous_prefix_lists_its_matches_and_writes_nothing(self):
        first, second = self.record_runs(2)
        code, stdout, stderr = self.invoke("branch", "--from", "abcd", "--reason", "no")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("chokepoint identifier prefix chokepoint-abcd is ambiguous", stderr)
        self.assertIn(f"it matches 2 records: {first}, {second}", stderr)
        self.assertEqual(len(self.store.list_pathways()), 1)

    def test_a_longer_prefix_settles_it(self):
        first, second = self.record_runs(2)
        for chokepoint in (first, second):
            child = self.invoke_json(
                "branch", "--from", chokepoint.removeprefix("chokepoint-"), "--reason", "exact"
            )
            self.assertEqual(child["source_chokepoint_id"], chokepoint)

    def test_a_long_list_of_matches_is_cut_short(self):
        chokepoints = self.record_runs(7)
        _code, _stdout, stderr = self.invoke("compare", "--from", "chokepoint-abcd")
        self.assertIn("it matches 7 records:", stderr)
        self.assertIn(", ".join(chokepoints[:5]) + ", and 2 more", stderr)
        self.assertNotIn(chokepoints[5], stderr)


class InheritedParameterTests(ShorthandTestCase):
    def setUp(self):
        super().setUp()
        self.baseline = self.run_json("--param", "count=1")
        self.child = self.invoke_json(
            "branch",
            "--from",
            self.baseline["chokepoint_id"],
            "--reason",
            "try count 2",
            "--param",
            "count=2",
            "--param",
            "rate=0.1",
        )

    def labels(self, result):
        return dict(self.store.load_atom(result["atom_id"]).declared_parameters)

    def test_a_run_on_a_branch_records_the_branch_parameters(self):
        result = self.run_json("--pathway", self.child["id"])
        self.assertEqual(self.labels(result), {"count": "2", "rate": "0.1"})

    def test_a_declaration_replaces_the_same_key_and_adds_others(self):
        result = self.run_json(
            "--pathway", self.child["id"], "--param", "count=3", "--param", "n=x"
        )
        self.assertEqual(self.labels(result), {"count": "3", "rate": "0.1", "n": "x"})

    def test_a_run_on_a_pathway_without_parameters_records_only_its_own(self):
        self.assertEqual(self.labels(self.baseline), {"count": "1"})
        self.assertEqual(self.labels(self.run_json()), {})

    def test_compare_shows_the_change_without_a_repeated_declaration(self):
        self.run_json("--pathway", self.child["id"])
        comparison = self.invoke_json("compare")
        self.assertEqual(comparison["state"], "ready")
        run_delta = comparison["children"][0]["run_parameters"]
        self.assertEqual(run_delta["changed"], {"count": {"source": "1", "child": "2"}})
        self.assertEqual(run_delta["added"], {"rate": "0.1"})
        _code, text, _stderr = self.invoke("compare")
        self.assertIn("run parameters: added rate=0.1; changed count (1 -> 2)", text)

    def test_a_grandchild_run_records_every_inherited_parameter(self):
        child_run = self.run_json("--pathway", self.child["id"])
        grandchild = self.invoke_json(
            "branch",
            "--from",
            child_run["chokepoint_id"],
            "--reason",
            "deeper",
            "--param",
            "depth=4",
        )
        result = self.run_json("--pathway", grandchild["id"])
        self.assertEqual(self.labels(result), {"count": "2", "rate": "0.1", "depth": "4"})

    def test_run_summaries_still_omit_inherited_parameter_values(self):
        marked = self.invoke_json(
            "branch",
            "--from",
            self.baseline["chokepoint_id"],
            "--reason",
            "marked",
            "--param",
            "label=INHERITED-SENTINEL",
        )
        for flags in (("--quiet",), ("--json",)):
            with self.subTest(flags=flags):
                code, text, _stderr = self.invoke(
                    "run", *flags, "--pathway", marked["id"], "--", *PASS
                )
                self.assertEqual(code, 0)
                self.assertNotIn("INHERITED-SENTINEL", text)

    def test_the_python_api_inherits_the_same_way(self):
        execution = ocura_oss.run(
            list(PASS), root=self.root, pathway_id=self.child["id"], parameters={"rate": "0.5"}
        )
        self.assertEqual(execution.atom.declared_parameters, {"count": "2", "rate": "0.5"})

    def test_an_unfinished_attempt_carries_the_inherited_parameters(self):
        seen = {}
        original = Store._save_atom

        def capture(store, atom):
            seen.update(store.list_attempts()[0][0].declared_parameters)
            return original(store, atom)

        with mock.patch.object(Store, "_save_atom", capture):
            self.run_json("--pathway", self.child["id"])
        self.assertEqual(seen, {"count": "2", "rate": "0.1"})

    def test_state_still_verifies(self):
        self.run_json("--pathway", self.child["id"])
        self.assertTrue(ocura_oss.verify(self.root).ok)


if __name__ == "__main__":
    unittest.main()
