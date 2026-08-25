# SPDX-License-Identifier: MPL-2.0

import datetime
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from ocura_oss import branching, model, runner
from ocura_oss.store import Store, StoreError


def make_store(root: Path) -> tuple[Store, model.Pathway]:
    store = Store(root)
    _, pathway = store.initialize_state(name="branching tests")
    return store, pathway


class BranchSourceTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "proj"
        self.store, self.pathway = make_store(self.root)

    def test_valid_source_loads(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=["true"],
            declared_parameters={},
        )
        chokepoint, atom, pathway = branching.load_verified_source(
            self.store, execution.chokepoint.id
        )
        self.assertEqual(chokepoint.id, execution.chokepoint.id)
        self.assertEqual(atom.id, execution.atom.id)
        self.assertEqual(pathway.id, self.pathway.id)

    def test_missing_source_is_rejected(self):
        with self.assertRaises(StoreError):
            branching.load_verified_source(self.store, "chokepoint-" + "0" * 32)

    def test_checksum_tampered_source_is_rejected(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=["true"],
            declared_parameters={},
        )
        path = self.store.chokepoints_dir / f"{execution.chokepoint.id}.json"
        envelope = json.loads(path.read_text("utf-8"))
        envelope["payload"]["outcome"] = "failed"
        path.write_text(json.dumps(envelope), "utf-8")
        with self.assertRaises(StoreError):
            branching.load_verified_source(self.store, execution.chokepoint.id)

    def test_nonterminal_source_is_rejected(self):
        identifier = model.make_id("chokepoint")
        self.store._save_chokepoint(
            model.Chokepoint(
                id=identifier,
                pathway_id=self.pathway.id,
                atom_id=model.make_id("atom"),
                created_at=model.format_timestamp(model.utc_now()),
                kind="checkpoint",
                outcome=model.Outcome.PASSED,
                branchable=True,
            )
        )
        with self.assertRaises(StoreError):
            branching.load_verified_source(self.store, identifier)

    def test_nonbranchable_source_is_rejected(self):
        identifier = model.make_id("chokepoint")
        self.store._save_chokepoint(
            model.Chokepoint(
                id=identifier,
                pathway_id=self.pathway.id,
                atom_id=model.make_id("atom"),
                created_at=model.format_timestamp(model.utc_now()),
                kind="terminal",
                outcome=model.Outcome.PASSED,
                branchable=False,
            )
        )
        with self.assertRaises(StoreError):
            branching.load_verified_source(self.store, identifier)

    def test_source_with_missing_evidence_is_rejected(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=["true"],
            declared_parameters={},
        )
        (self.store.logs_dir / f"{execution.atom.id}.stdout.log").unlink()
        with self.assertRaises(StoreError):
            branching.load_verified_source(self.store, execution.chokepoint.id)

    def test_source_with_tampered_log_is_rejected(self):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "print('evidence')"],
            declared_parameters={},
        )
        log = self.store.logs_dir / f"{execution.atom.id}.stdout.log"
        log.write_bytes(b"rewritten\n")
        with self.assertRaises(StoreError):
            branching.load_verified_source(self.store, execution.chokepoint.id)


class BranchCreationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "proj"
        self.store, self.pathway = make_store(self.root)
        self.execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=["true"],
            declared_parameters={},
        )
        self.source = branching.load_verified_source(self.store, self.execution.chokepoint.id)

    def test_branch_is_metadata_only(self):
        atoms_before = {item.id for item in self.store.list_atoms()}
        logs_before = sorted(p.name for p in self.store.logs_dir.iterdir())
        root_entries_before = sorted(p.name for p in self.root.iterdir())

        child = branching.create_child_pathway(
            self.store,
            source_chokepoint=self.source[0],
            source_pathway=self.source[2],
            reason="try a change",
            overrides={"batch": "2"},
        )

        self.assertEqual({item.id for item in self.store.list_atoms()}, atoms_before)
        self.assertEqual(sorted(p.name for p in self.store.logs_dir.iterdir()), logs_before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), root_entries_before)
        self.assertEqual(child.parent_pathway_id, self.pathway.id)
        self.assertEqual(child.source_chokepoint_id, self.execution.chokepoint.id)
        self.assertEqual(child.reason, "try a change")
        self.assertEqual(child.parameters, {"batch": "2"})

    def test_effective_parameters_merge_parent_and_overrides(self):
        parent = branching.create_child_pathway(
            self.store,
            source_chokepoint=self.source[0],
            source_pathway=self.source[2],
            reason="first",
            overrides={"batch": "2"},
        )
        second_source = branching.load_verified_source(self.store, self.execution.chokepoint.id)
        grandchild = branching.create_child_pathway(
            self.store,
            source_chokepoint=second_source[0],
            source_pathway=parent,
            reason="second",
            overrides={"batch": "3", "extra": "x"},
        )
        self.assertEqual(grandchild.parameters, {"batch": "3", "extra": "x"})
        self.assertNotEqual(grandchild.id, parent.id)

    def test_blank_reason_is_rejected(self):
        for reason in ("", "   "):
            with self.assertRaises(StoreError):
                branching.create_child_pathway(
                    self.store,
                    source_chokepoint=self.source[0],
                    source_pathway=self.source[2],
                    reason=reason,
                    overrides={},
                )


class CompareTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "proj"
        self.store, self.pathway = make_store(self.root)

    def _source(self, *, now=None):
        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=["true"],
            declared_parameters={},
            now=now,
        )
        return branching.load_verified_source(self.store, execution.chokepoint.id), execution

    def test_no_branch_state(self):
        (_chokepoint, _atom, _pathway), execution = self._source()
        result = branching.compare(self.store, execution.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.NO_BRANCH)
        self.assertEqual(result.children, ())

    def test_partial_state_when_child_has_no_run(self):
        (_chokepoint, _atom, pathway), execution = self._source()
        branching.create_child_pathway(
            self.store,
            source_chokepoint=execution.chokepoint,
            source_pathway=pathway,
            reason="not run yet",
            overrides={"a": "1"},
        )
        result = branching.compare(self.store, execution.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.PARTIAL)
        self.assertTrue(result.children[0].missing_evidence)
        self.assertIsNone(result.children[0].child_run)

    def test_ready_state_with_parameter_deltas(self):
        (_chokepoint, _atom, pathway), execution = self._source()
        first = branching.create_child_pathway(
            self.store,
            source_chokepoint=execution.chokepoint,
            source_pathway=pathway,
            reason="add batch",
            overrides={"batch": "2", "shared": "same"},
        )
        first_run = runner.run_command(
            self.store,
            pathway_id=first.id,
            argv=["true"],
            declared_parameters={"batch": "2"},
        )
        result = branching.compare(self.store, execution.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.READY)
        child = result.children[0]
        self.assertFalse(child.missing_evidence)
        self.assertEqual(child.parameters.added, {"batch": "2", "shared": "same"})
        self.assertEqual(child.parameters.changed, {})
        self.assertEqual(child.parameters.inherited, {})

        second = branching.create_child_pathway(
            self.store,
            source_chokepoint=first_run.chokepoint,
            source_pathway=first,
            reason="change batch",
            overrides={"batch": "9"},
        )
        runner.run_command(
            self.store,
            pathway_id=second.id,
            argv=["true"],
            declared_parameters={"batch": "9"},
        )
        deep = branching.compare(self.store, first_run.chokepoint.id)
        self.assertIs(deep.state, model.ComparisonState.READY)
        changed = deep.children[0]
        self.assertEqual(changed.pathway_id, second.id)
        self.assertEqual(changed.parameters.inherited, {"shared": "same"})
        self.assertEqual(changed.parameters.changed, {"batch": {"source": "2", "child": "9"}})
        self.assertEqual(changed.parameters.added, {})

    def test_compare_without_branches_selects_newest_chokepoint(self):
        (_c1, _a1, _p1), first_execution = self._source()
        (_c2, _a2, _p2), second_execution = self._source()
        result = branching.compare(self.store, None)
        self.assertEqual(result.source_chokepoint_id, second_execution.chokepoint.id)

    def test_compare_selects_branched_source_over_newer_unbranched_chokepoint(self):
        source, first_execution = self._source(
            now=lambda: datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        )
        branching.create_child_pathway(
            self.store,
            source_chokepoint=source[0],
            source_pathway=source[2],
            reason="test a branch",
            overrides={},
            now=lambda: datetime.datetime(2026, 1, 1, 1, tzinfo=datetime.UTC),
        )
        self._source(now=lambda: datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC))

        result = branching.compare(self.store, None)

        self.assertEqual(result.source_chokepoint_id, first_execution.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.PARTIAL)

    def test_compare_selects_newest_source_when_multiple_have_children(self):
        first_source, _first_execution = self._source(
            now=lambda: datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        )
        branching.create_child_pathway(
            self.store,
            source_chokepoint=first_source[0],
            source_pathway=first_source[2],
            reason="first branch",
            overrides={},
            now=lambda: datetime.datetime(2026, 1, 1, 1, tzinfo=datetime.UTC),
        )
        second_source, second_execution = self._source(
            now=lambda: datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)
        )
        branching.create_child_pathway(
            self.store,
            source_chokepoint=second_source[0],
            source_pathway=second_source[2],
            reason="second branch",
            overrides={},
            now=lambda: datetime.datetime(2026, 1, 2, 1, tzinfo=datetime.UTC),
        )

        result = branching.compare(self.store, None)

        self.assertEqual(result.source_chokepoint_id, second_execution.chokepoint.id)

    def test_compare_fails_closed_on_invalid_chokepoint(self):
        (_chokepoint, _atom, _pathway), _first = self._source()
        (_c2, _a2, _p2), second_execution = self._source()
        path = self.store.chokepoints_dir / f"{second_execution.chokepoint.id}.json"
        path.write_text("{broken", "utf-8")
        with self.assertRaises(StoreError):
            branching.compare(self.store, None)

    def test_automatic_selection_refuses_unrelated_malformed_atom(self):
        (_chokepoint, _atom, _pathway), _execution = self._source()
        bad = self.store.atoms_dir / ("atom-" + "a" * 32 + ".json")
        bad.write_text("{broken", "utf-8")
        with self.assertRaises(StoreError):
            branching.compare(self.store, None)

    def test_automatic_selection_refuses_broken_references_anywhere(self):
        (_chokepoint, _atom, _pathway), _execution = self._source()
        ghost_atom = model.make_id("atom")
        self.store._save_chokepoint(
            model.Chokepoint(
                id=model.make_id("chokepoint"),
                pathway_id=self.pathway.id,
                atom_id=ghost_atom,
                created_at=model.format_timestamp(model.utc_now()),
                kind="terminal",
                outcome=model.Outcome.PASSED,
                branchable=True,
            )
        )
        with self.assertRaises(StoreError):
            branching.compare(self.store, None)

    def test_automatic_selection_refuses_ghost_pathway_atom(self):
        (_chokepoint, _atom, _pathway), _execution = self._source()
        ghost_pathway = "pathway-" + "f" * 32
        atom_id = model.make_id("atom")
        empty_digest = hashlib.sha256(b"").hexdigest()
        for suffix in ("stdout", "stderr"):
            (self.store.logs_dir / f"{atom_id}.{suffix}.log").write_bytes(b"")
        self.store._save_atom(
            model.Atom(
                id=atom_id,
                pathway_id=ghost_pathway,
                started_at="2026-01-01T00:00:00.000000+00:00",
                finished_at="2026-01-01T00:00:01.000000+00:00",
                duration_seconds=1.0,
                outcome=model.Outcome.PASSED,
                return_code=0,
                launch_error_category=None,
                declared_parameters={},
                command=("x",),
                stdout_log=f".ocura-oss/logs/{atom_id}.stdout.log",
                stderr_log=f".ocura-oss/logs/{atom_id}.stderr.log",
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_sha256=empty_digest,
                stderr_sha256=empty_digest,
            )
        )
        with self.assertRaises(StoreError):
            branching.compare(self.store, None)

    def test_automatic_selection_refuses_duplicated_atom_record(self):
        (_chokepoint, _atom, _pathway), execution = self._source()
        original = self.store.atoms_dir / f"{execution.atom.id}.json"
        other_id = model.make_id("atom")
        (self.store.atoms_dir / f"{other_id}.json").write_bytes(original.read_bytes())
        with self.assertRaises(StoreError):
            branching.compare(self.store, None)

    def test_compare_reports_changed_declared_run_parameters(self):
        source_execution = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "print('baseline')"],
            declared_parameters={"batch": "1"},
        )
        _cp, _atom, root_pathway = branching.load_verified_source(
            self.store, source_execution.chokepoint.id
        )
        child = branching.create_child_pathway(
            self.store,
            source_chokepoint=source_execution.chokepoint,
            source_pathway=root_pathway,
            reason="try batch four",
            overrides={"batch": "4"},
        )
        runner.run_command(
            self.store,
            pathway_id=child.id,
            argv=[sys.executable, "-c", "print('child')"],
            declared_parameters={"batch": "4"},
        )
        result = branching.compare(self.store, source_execution.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.READY)
        comparison = result.children[0]
        self.assertIsNotNone(comparison.run_parameters)
        self.assertEqual(
            comparison.run_parameters.changed,
            {"batch": {"source": "1", "child": "4"}},
        )
        self.assertEqual(comparison.run_parameters.inherited, {})
        self.assertEqual(comparison.parameters.added, {"batch": "4"})

    def test_select_source_raises_without_chokepoints(self):
        with self.assertRaises(StoreError):
            branching.select_source_chokepoint(self.store)

    def test_failed_and_launch_failed_sources_are_branchable(self):
        failed = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=[sys.executable, "-c", "import sys; sys.exit(1)"],
            declared_parameters={},
        )
        self.assertEqual(failed.atom.outcome, model.Outcome.FAILED)
        child = branching.create_child_pathway(
            self.store,
            source_chokepoint=failed.chokepoint,
            source_pathway=self.pathway,
            reason="from failure",
            overrides={},
        )
        result = branching.compare(self.store, failed.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.PARTIAL)
        self.assertEqual(result.children[0].pathway_id, child.id)

        launched = runner.run_command(
            self.store,
            pathway_id=self.pathway.id,
            argv=["definitely-not-a-real-executable-ocura-oss"],
            declared_parameters={},
        )
        self.assertEqual(launched.atom.outcome, model.Outcome.LAUNCH_FAILED)
        second_child = branching.create_child_pathway(
            self.store,
            source_chokepoint=launched.chokepoint,
            source_pathway=self.pathway,
            reason="from launch failure",
            overrides={},
        )
        result = branching.compare(self.store, launched.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.PARTIAL)
        self.assertEqual(result.children[0].pathway_id, second_child.id)

    def test_children_are_listed_in_creation_order(self):
        (_chokepoint, _atom, pathway), execution = self._source()
        import datetime

        base = datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC)
        ticks = iter(range(1, 10))
        clock = lambda: base + datetime.timedelta(seconds=next(ticks))  # noqa: E731
        created = []
        for index in range(3):
            child = branching.create_child_pathway(
                self.store,
                source_chokepoint=execution.chokepoint,
                source_pathway=pathway,
                reason=f"child {index}",
                overrides={},
                now=clock,
            )
            created.append(child.id)
        result = branching.compare(self.store, execution.chokepoint.id)
        self.assertIs(result.state, model.ComparisonState.PARTIAL)
        self.assertEqual([item.pathway_id for item in result.children], created)

    def test_deep_chain_compares_against_immediate_parent(self):
        (_c1, _a1, root_pathway), root_execution = self._source()
        first = branching.create_child_pathway(
            self.store,
            source_chokepoint=root_execution.chokepoint,
            source_pathway=root_pathway,
            reason="level one",
            overrides={"level": "1"},
        )
        first_run = runner.run_command(
            self.store,
            pathway_id=first.id,
            argv=["true"],
            declared_parameters={},
        )
        second = branching.create_child_pathway(
            self.store,
            source_chokepoint=first_run.chokepoint,
            source_pathway=first,
            reason="level two",
            overrides={"level": "2"},
        )
        runner.run_command(
            self.store,
            pathway_id=second.id,
            argv=["true"],
            declared_parameters={},
        )
        deep = branching.compare(self.store, first_run.chokepoint.id)
        self.assertEqual(deep.source_pathway_id, first.id)
        self.assertEqual([item.pathway_id for item in deep.children], [second.id])
        self.assertEqual(deep.children[0].parameters.inherited, {})
        self.assertEqual(
            deep.children[0].parameters.changed,
            {"level": {"source": "1", "child": "2"}},
        )

    def test_compare_rejects_child_with_unverified_evidence(self):
        (_chokepoint, _atom, pathway), execution = self._source()
        child = branching.create_child_pathway(
            self.store,
            source_chokepoint=execution.chokepoint,
            source_pathway=pathway,
            reason="later broken",
            overrides={},
        )
        child_run = runner.run_command(
            self.store,
            pathway_id=child.id,
            argv=[sys.executable, "-c", "print('child')"],
            declared_parameters={},
        )
        (self.store.logs_dir / f"{child_run.atom.id}.stdout.log").unlink()
        with self.assertRaises(StoreError):
            branching.compare(self.store, execution.chokepoint.id)


if __name__ == "__main__":
    unittest.main()
