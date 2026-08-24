# SPDX-License-Identifier: MPL-2.0

import sys
import tempfile
import types
import unittest
from pathlib import Path

import ocura_oss


class PublicSurfaceTests(unittest.TestCase):
    def test_top_level_exports_are_explicit(self):
        self.assertEqual(
            set(ocura_oss.__all__),
            {
                "Atom",
                "ChildComparison",
                "Chokepoint",
                "ComparisonResult",
                "ComparisonState",
                "DemoError",
                "DemoReport",
                "DemoStep",
                "Den",
                "Initialization",
                "Outcome",
                "ParameterDelta",
                "Pathway",
                "RunExecution",
                "RunSummary",
                "StateVerification",
                "Store",
                "StoreError",
                "__version__",
                "branch",
                "compare",
                "initialize",
                "run",
                "run_demo",
                "verify",
            },
        )
        for name in ocura_oss.__all__:
            self.assertTrue(hasattr(ocura_oss, name), name)

    def test_low_level_serialization_is_not_exported(self):
        for name in ("atom_from_payload", "canonical_json", "checksum_for", "save_atom"):
            self.assertNotIn(name, ocura_oss.__all__)

    def test_public_function_names_do_not_shadow_existing_submodules(self):
        self.assertIsInstance(ocura_oss.demo, types.ModuleType)
        self.assertIsInstance(ocura_oss.runner, types.ModuleType)


@unittest.skipUnless(sys.executable, "requires a Python interpreter")
class ProgrammaticWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "project"

    def test_complete_programmatic_workflow(self):
        initialized = ocura_oss.initialize(self.root, name="API example")
        self.assertEqual(initialized.root, self.root.resolve())
        self.assertEqual(initialized.den.default_pathway_id, initialized.pathway.id)

        baseline = ocura_oss.run(
            [sys.executable, "-c", "print('baseline')"],
            root=self.root,
            parameters={"batch": "1"},
        )
        self.assertIs(baseline.atom.outcome, ocura_oss.Outcome.PASSED)
        self.assertEqual(baseline.atom.pathway_id, initialized.pathway.id)
        store = ocura_oss.Store(self.root)
        store.verify_atom_evidence(baseline.atom)
        self.assertIn("baseline", store.resolve_log_path(baseline.atom, "stdout").read_text())

        child = ocura_oss.branch(
            baseline.chokepoint.id,
            root=self.root,
            reason="increase batch",
            parameters={"batch": "4"},
        )
        partial = ocura_oss.compare(baseline.chokepoint.id, root=self.root)
        self.assertIs(partial.state, ocura_oss.ComparisonState.PARTIAL)
        self.assertEqual(partial.children[0].pathway_id, child.id)

        child_run = ocura_oss.run(
            [sys.executable, "-c", "print('child')"],
            root=self.root,
            pathway_id=child.id,
            parameters={"batch": "4"},
        )
        self.assertIs(child_run.atom.outcome, ocura_oss.Outcome.PASSED)

        ready = ocura_oss.compare(baseline.chokepoint.id, root=self.root)
        self.assertIs(ready.state, ocura_oss.ComparisonState.READY)
        self.assertEqual(
            ready.children[0].run_parameters.changed,
            {"batch": {"source": "1", "child": "4"}},
        )
        report = ocura_oss.verify(self.root)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(report.atoms, 2)
        self.assertEqual(report.logs_checked, 4)

    def test_failed_command_is_returned_as_evidence(self):
        ocura_oss.initialize(self.root)
        result = ocura_oss.run([sys.executable, "-c", "import sys; sys.exit(7)"], root=self.root)
        self.assertIs(result.atom.outcome, ocura_oss.Outcome.FAILED)
        self.assertEqual(result.atom.return_code, 7)
        self.assertTrue(ocura_oss.verify(self.root).ok)

    def test_string_command_is_rejected_as_ambiguous(self):
        ocura_oss.initialize(self.root)
        with self.assertRaisesRegex(ocura_oss.StoreError, "argument tokens"):
            ocura_oss.run("python -V", root=self.root)

    def test_store_rejects_unknown_log_stream(self):
        ocura_oss.initialize(self.root)
        result = ocura_oss.run([sys.executable, "-c", "pass"], root=self.root)
        store = ocura_oss.Store(self.root)
        with self.assertRaisesRegex(ocura_oss.StoreError, "log stream"):
            store.resolve_log_path(result.atom, "combined")

    def test_existing_state_is_required_after_initialization(self):
        for operation in (
            lambda: ocura_oss.run([sys.executable, "-c", "pass"], root=self.root),
            lambda: ocura_oss.compare(root=self.root),
            lambda: ocura_oss.verify(self.root),
        ):
            with self.subTest(operation=operation), self.assertRaises(ocura_oss.StoreError):
                operation()

    def test_store_accepts_string_roots_and_resolves_them(self):
        store = ocura_oss.Store(str(self.root))
        self.assertEqual(store.root, self.root.resolve())
        den, pathway = store.initialize_state(name="store example")
        self.assertEqual(store.load_den(), den)
        self.assertEqual(store.load_pathway(pathway.id), pathway)
        self.assertTrue(store.verify_state().ok)

    def test_public_demo_returns_a_ready_verified_report(self):
        destination = Path(self._temporary.name) / "demo"
        report = ocura_oss.run_demo(destination)
        self.assertEqual(report.root, destination.resolve())
        self.assertIs(report.comparison.state, ocura_oss.ComparisonState.READY)
        self.assertTrue(ocura_oss.verify(destination).ok)


if __name__ == "__main__":
    unittest.main()
