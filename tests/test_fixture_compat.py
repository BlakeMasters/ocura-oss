# SPDX-License-Identifier: MPL-2.0

"""A ledger written by 0.5.0 must stay readable for as long as schema 2 is kept.

The fixture under ``tests/fixtures/ledger-0.5.0/`` is frozen. Do not regenerate
or edit it: a change that makes these tests fail is a format break and needs a
new schema version.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import ocura_oss
from ocura_oss import model
from ocura_oss.store import STATE_DIR_NAME, Store

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ledger-0.5.0" / "state"

DEN = "den-13d7ee10f1134fdfacbc6baa7eb34084"
DEFAULT_PATHWAY = "pathway-c6132f322f6d463ab8b7876c695eb1e9"
CHILD_PATHWAY = "pathway-1ac386d75d104e4abb977fd43ea0bb55"
BASELINE = ("atom-17a15137e9a14cfdbbe777be50d7446d", "chokepoint-1592911372f143208bfd75af9a5c83a5")
FAILED = ("atom-be8ce17fdceb49debd9cf29e56923107", "chokepoint-2e49e6425f0949a9bd67e02fe5d3974d")
UNLAUNCHED = (
    "atom-50f6086ef6d64817a3360313043adfb8",
    "chokepoint-da5671fe22bc467ca6e09578ed99ac2a",
)
ABANDONED = ("atom-bd901829c6b44e91a8def06ffb2e9c64", "chokepoint-79b6741543fe4a8cbd424ffcd52eec5a")
UNCAPTURED = (
    "atom-869bc8340bf043569f61d779a0beccfa",
    "chokepoint-eb99f2380d7040ea802c62a0b67d16a7",
)


class FrozenLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        shutil.copytree(FIXTURE, self.root / STATE_DIR_NAME)
        self.store = Store(self.root)

    def test_complete_state_verifies(self):
        report = ocura_oss.verify(self.root)
        self.assertEqual(report.problems, ())
        self.assertEqual(
            (report.pathways, report.atoms, report.chokepoints, report.logs_checked), (2, 5, 5, 8)
        )
        self.assertEqual((report.running_attempts, report.abandoned_attempts), ((), ()))
        self.assertEqual(len(report.manifest), 13)
        self.assertEqual(
            report.digest, "db93479659913104ed914d9e290cb0625808d1a1b7d9b221fb6a946c961ca570"
        )
        self.assertEqual(self.store.list_attempts(), [])
        self.assertEqual(ocura_oss.recover(self.root), ())

    def test_den_and_lineage_are_read(self):
        den = self.store.load_den()
        self.assertEqual(
            (den.id, den.name, den.default_pathway_id), (DEN, "fixture 0.5.0", DEFAULT_PATHWAY)
        )
        child = self.store.load_pathway(CHILD_PATHWAY)
        self.assertEqual(child.parent_pathway_id, DEFAULT_PATHWAY)
        self.assertEqual(child.source_chokepoint_id, BASELINE[1])
        self.assertEqual(child.reason, "try batch 4")
        self.assertEqual(child.parameters, {"batch": "4"})
        self.assertEqual(
            [item.id for item in self.store.list_pathways()], [DEFAULT_PATHWAY, CHILD_PATHWAY]
        )

    def test_every_outcome_is_read_with_its_chokepoint(self):
        expected = {
            BASELINE: (model.Outcome.PASSED, 0, None),
            FAILED: (model.Outcome.FAILED, 3, None),
            UNLAUNCHED: (model.Outcome.LAUNCH_FAILED, None, "executable_not_found"),
            ABANDONED: (model.Outcome.ABANDONED, None, None),
            UNCAPTURED: (model.Outcome.PASSED, 0, None),
        }
        for (atom_id, chokepoint_id), (outcome, return_code, category) in expected.items():
            with self.subTest(atom=atom_id):
                atom = self.store.load_atom(atom_id)
                self.assertIs(atom.outcome, outcome)
                self.assertEqual(atom.return_code, return_code)
                self.assertEqual(atom.launch_error_category, category)
                chokepoint = self.store.load_chokepoint(chokepoint_id)
                self.assertEqual(chokepoint.atom_id, atom_id)
                self.assertIs(chokepoint.outcome, outcome)
                self.assertTrue(chokepoint.branchable)
        self.assertEqual(len(self.store.list_atoms()), len(expected))

    def test_captured_output_is_read_verified(self):
        self.assertEqual(self.store.read_verified_log(BASELINE[0]), b"loss=0.50\n")
        self.assertEqual(
            self.store.read_verified_log(BASELINE[0], stream="stderr"), b"baseline warning\n"
        )
        self.assertEqual(self.store.read_verified_log(FAILED[0], stream="stderr"), b"boom\n")
        baseline = self.store.load_atom(BASELINE[0])
        self.assertIs(baseline.output_capture, model.OutputCapture.FULL)
        self.assertEqual(baseline.declared_parameters, {"batch": "1"})
        self.assertEqual(baseline.masked_arguments, ())
        self.assertEqual(baseline.stdout_bytes, 10)

    def test_abandoned_atom_keeps_what_was_known(self):
        atom = self.store.load_atom(ABANDONED[0])
        self.assertEqual(atom.pathway_id, CHILD_PATHWAY)
        self.assertEqual(atom.command, ("python", "train.py", "--batch", "4"))
        self.assertIsNone(atom.finished_at)
        self.assertIsNone(atom.duration_seconds)
        self.assertEqual(self.store.read_verified_log(atom.id), b"partial\n")
        self.assertEqual(self.store.read_verified_log(atom.id, stream="stderr"), b"")

    def test_uncaptured_masked_atom_is_read(self):
        atom = self.store.load_atom(UNCAPTURED[0])
        self.assertIs(atom.output_capture, model.OutputCapture.NONE)
        self.assertEqual(atom.command, ("python", "-c", "pass", "--token", model.MASKED_ARGUMENT))
        self.assertEqual(atom.masked_arguments, (4,))
        self.assertIsNone(atom.stdout_log)
        self.assertIsNone(atom.stderr_sha256)
        self.assertIsNotNone(atom.duration_seconds)

    def test_comparison_uses_the_newest_child_run(self):
        for source in (None, BASELINE[1]):
            with self.subTest(source=source):
                comparison = ocura_oss.compare(source, root=self.root)
                self.assertIs(comparison.state, model.ComparisonState.READY)
                self.assertEqual(comparison.source_chokepoint_id, BASELINE[1])
                (child,) = comparison.children
                self.assertEqual(child.pathway_id, CHILD_PATHWAY)
                self.assertEqual(child.child_run.id, UNCAPTURED[0])
                self.assertEqual(
                    child.parameters.to_dict(),
                    {"inherited": {}, "added": {"batch": "4"}, "changed": {}},
                )
                self.assertEqual(
                    child.run_parameters.to_dict()["changed"],
                    {"batch": {"child": "4", "source": "1"}},
                )

    def test_the_ledger_accepts_new_work(self):
        execution = ocura_oss.run(["ocura-fixture-no-such-executable"], root=self.root)
        self.assertIs(execution.atom.outcome, model.Outcome.LAUNCH_FAILED)
        child = ocura_oss.branch(ABANDONED[1], root=self.root, reason="retry the abandoned run")
        self.assertEqual(child.parent_pathway_id, CHILD_PATHWAY)
        report = ocura_oss.verify(self.root)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual((report.pathways, report.atoms), (3, 6))


if __name__ == "__main__":
    unittest.main()
