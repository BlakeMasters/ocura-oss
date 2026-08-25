# SPDX-License-Identifier: MPL-2.0

import contextlib
import copy
import datetime
import hashlib
import json
import random
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ocura_oss import model
from ocura_oss.store import STATE_DIR_NAME, Store, StoreError


@contextlib.contextmanager
def temp_root():
    with tempfile.TemporaryDirectory() as temporary:
        yield Path(temporary)


def new_store(root: Path) -> Store:
    store = Store(root)
    store.initialize_state(name="test den")
    return store


class InitializationTests(unittest.TestCase):
    def test_init_creates_layout_and_valid_default_records(self):
        with temp_root() as root:
            store = new_store(root)
            self.assertTrue((store.state_dir / "den.json").is_file())
            for sub in ("pathways", "atoms", "chokepoints", "logs"):
                self.assertTrue((store.state_dir / sub).is_dir())

            den = store.load_den()
            self.assertEqual(den.name, "test den")
            self.assertTrue(model.is_valid_id(den.id, "den"))
            parsed = model.parse_timestamp(den.created_at, "created_at")
            self.assertIsNotNone(parsed.utcoffset())

            pathways = store.list_pathways()
            self.assertEqual(len(pathways), 1)
            pathway = pathways[0]
            self.assertEqual(pathway.id, den.default_pathway_id)
            self.assertIsNone(pathway.parent_pathway_id)
            self.assertIsNone(pathway.source_chokepoint_id)
            self.assertEqual(pathway.parameters, {})
            self.assertTrue(model.is_valid_id(pathway.id, "pathway"))

            envelope = json.loads((store.state_dir / "den.json").read_text("utf-8"))
            self.assertEqual(envelope["schema_version"], 1)
            self.assertEqual(envelope["kind"], "den")
            expected = model.checksum_for(1, "den", envelope["payload"])["value"]
            self.assertEqual(envelope["checksum"]["algorithm"], "sha256")
            self.assertEqual(envelope["checksum"]["value"], expected)

    def test_init_refuses_existing_state(self):
        with temp_root() as root:
            store = new_store(root)
            before = (store.state_dir / "den.json").read_bytes()
            with self.assertRaises(StoreError):
                store.initialize_state(name="again")
            self.assertEqual((store.state_dir / "den.json").read_bytes(), before)

    def test_init_refuses_existing_empty_state_dir(self):
        with temp_root() as root:
            (root / STATE_DIR_NAME).mkdir()
            with self.assertRaises(StoreError):
                Store(root).initialize_state(name="again")

    def test_init_rejects_blank_name(self):
        with temp_root() as root:
            store = Store(root)
            for name in ("", "   "):
                with self.assertRaises(StoreError):
                    store.initialize_state(name=name)
            self.assertFalse(store.exists())

    def test_missing_state_is_rejected(self):
        with temp_root() as root:
            store = Store(root)
            with self.assertRaises(StoreError):
                store.require()


class ReadValidationTests(unittest.TestCase):
    @staticmethod
    def _rewrite(path: Path, schema_version, kind, payload):
        checksum = model.checksum_for(schema_version, kind, payload)
        path.write_text(
            json.dumps(
                {
                    "schema_version": schema_version,
                    "kind": kind,
                    "payload": payload,
                    "checksum": checksum,
                }
            ),
            "utf-8",
        )

    def test_tampered_payload_fails_checksum(self):
        with temp_root() as root:
            store = new_store(root)
            path = store.state_dir / "den.json"
            envelope = json.loads(path.read_text("utf-8"))
            envelope["payload"]["name"] = "rewritten"
            path.write_text(json.dumps(envelope), "utf-8")
            with self.assertRaises(StoreError):
                store.load_den()

    def test_wrong_kind_is_rejected(self):
        with temp_root() as root:
            store = new_store(root)
            den_path = store.state_dir / "den.json"
            envelope = json.loads(den_path.read_text("utf-8"))
            self._rewrite(den_path, 1, "atom", envelope["payload"])
            with self.assertRaises(StoreError):
                store.load_den()

    def test_unsupported_schema_version_is_rejected(self):
        with temp_root() as root:
            store = new_store(root)
            den_path = store.state_dir / "den.json"
            envelope = json.loads(den_path.read_text("utf-8"))
            self._rewrite(den_path, 2, "den", envelope["payload"])
            with self.assertRaises(StoreError):
                store.load_den()

    def test_missing_required_field_is_rejected(self):
        with temp_root() as root:
            store = new_store(root)
            den_path = store.state_dir / "den.json"
            envelope = json.loads(den_path.read_text("utf-8"))
            del envelope["payload"]["default_pathway_id"]
            self._rewrite(den_path, 1, "den", envelope["payload"])
            with self.assertRaises(StoreError):
                store.load_den()

    def test_malformed_identifier_is_rejected(self):
        with temp_root() as root:
            store = new_store(root)
            with self.assertRaises(StoreError):
                store.load_pathway("../escape")
            with self.assertRaises(StoreError):
                store.load_chokepoint("chokepoint-not-hex")


class ListingTests(unittest.TestCase):
    def test_listings_are_deterministic(self):
        fixed = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        counter = {"value": 0}

        def clock():
            counter["value"] += 1
            return fixed + datetime.timedelta(seconds=counter["value"])

        with temp_root() as root:
            store = new_store(root)
            den = store.load_den()
            ids = [den.default_pathway_id]
            for index in range(3):
                identifier = model.make_id("pathway")
                ids.append(identifier)
                store._save_pathway(
                    model.Pathway(
                        id=identifier,
                        den_id=den.id,
                        created_at=model.format_timestamp(clock()),
                        parent_pathway_id=None,
                        source_chokepoint_id=None,
                        reason=f"extra {index}",
                        parameters={},
                    )
                )
            listed = [item.id for item in store.list_pathways()]
            self.assertEqual(listed, ids[1:] + [ids[0]])
            again = [item.id for item in store.list_pathways()]
            self.assertEqual(listed, again)

    def test_invalid_records_fail_listings_closed(self):
        with temp_root() as root:
            store = new_store(root)
            good = model.make_id("chokepoint")
            store._save_chokepoint(
                model.Chokepoint(
                    id=good,
                    pathway_id=store.load_den().default_pathway_id,
                    atom_id=model.make_id("atom"),
                    created_at=model.format_timestamp(model.utc_now()),
                    kind="terminal",
                    outcome=model.Outcome.PASSED,
                    branchable=True,
                )
            )
            bad = store.chokepoints_dir / "chokepoint-ffffffffffffffffffffffffffffffff.json"
            bad.write_text("{not json", "utf-8")
            with self.assertRaises(StoreError) as ctx:
                store.list_chokepoints()
            self.assertIn(bad.name, str(ctx.exception))


class PayloadValidationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = new_store(Path(self._temporary.name) / "proj")
        self.pathway_id = self.store.load_den().default_pathway_id

    @staticmethod
    def valid_atom_payload(pathway_id):
        empty = hashlib.sha256(b"").hexdigest()
        return {
            "id": model.make_id("atom"),
            "pathway_id": pathway_id,
            "started_at": "2026-01-01T00:00:00.000000+00:00",
            "finished_at": "2026-01-01T00:00:01.000000+00:00",
            "duration_seconds": 1.0,
            "outcome": "passed",
            "return_code": 0,
            "launch_error_category": None,
            "declared_parameters": {},
            "command": ["x"],
            "stdout_log": ".ocura-oss/logs/x.stdout.log",
            "stderr_log": ".ocura-oss/logs/x.stderr.log",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_sha256": empty,
            "stderr_sha256": empty,
        }

    def _write_atom_payload(self, payload):
        path = self.store.atoms_dir / f"{payload['id']}.json"
        checksum = model.checksum_for(1, "atom", payload)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "atom",
                    "payload": payload,
                    "checksum": checksum,
                }
            ),
            "utf-8",
        )
        return payload["id"]

    def test_malformed_atom_payloads_are_rejected(self):
        base = self.valid_atom_payload(self.pathway_id)

        missing = copy.deepcopy(base)
        del missing["command"]

        extra = copy.deepcopy(base)
        extra["surprise"] = 1

        wrong_prefix = copy.deepcopy(base)
        wrong_prefix["id"] = model.make_id("den")

        bad_outcome = copy.deepcopy(base)
        bad_outcome["outcome"] = "exploded"

        incoherent_launch = copy.deepcopy(base)
        incoherent_launch["outcome"] = "launch_failed"

        incoherent_passed = copy.deepcopy(base)
        incoherent_passed["return_code"] = None

        negative_duration = copy.deepcopy(base)
        negative_duration["duration_seconds"] = -0.5

        bad_digest = copy.deepcopy(base)
        bad_digest["stdout_sha256"] = "zz-not-hex"

        empty_command = copy.deepcopy(base)
        empty_command["command"] = []

        nonstring_token = copy.deepcopy(base)
        nonstring_token["command"] = ["ok", 7]

        naive_timestamp = copy.deepcopy(base)
        naive_timestamp["started_at"] = "2026-01-01T00:00:00"

        nonstring_param = copy.deepcopy(base)
        nonstring_param["declared_parameters"] = {"a": 2}

        boolean_code = copy.deepcopy(base)
        boolean_code["return_code"] = True

        passed_nonzero = copy.deepcopy(base)
        passed_nonzero["return_code"] = 3

        failed_zero = copy.deepcopy(base)
        failed_zero["outcome"] = "failed"
        failed_zero["return_code"] = 0

        nan_duration = copy.deepcopy(base)
        nan_duration["duration_seconds"] = float("nan")

        cases = [
            ("missing field", missing),
            ("extra field", extra),
            ("wrong id prefix", wrong_prefix),
            ("invalid outcome", bad_outcome),
            ("launch_failed with code", incoherent_launch),
            ("passed without code", incoherent_passed),
            ("negative duration", negative_duration),
            ("non-finite duration", nan_duration),
            ("bad digest", bad_digest),
            ("empty command", empty_command),
            ("non-string token", nonstring_token),
            ("naive timestamp", naive_timestamp),
            ("non-string parameter", nonstring_param),
            ("boolean return code", boolean_code),
            ("passed with nonzero code", passed_nonzero),
            ("failed with zero code", failed_zero),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                atom_id = self._write_atom_payload(payload)
                with self.assertRaises(StoreError):
                    self.store.load_atom(atom_id)

    def test_valid_atom_payload_round_trips(self):
        payload = self.valid_atom_payload(self.pathway_id)
        atom_id = self._write_atom_payload(payload)
        atom = self.store.load_atom(atom_id)
        self.assertEqual(atom.command, ("x",))
        self.assertEqual(atom.outcome, model.Outcome.PASSED)


class FuzzReaderTests(unittest.TestCase):
    def test_seeded_mutations_always_raise_store_error(self):
        with temp_root() as root:
            store = new_store(root)
            den_path = store.state_dir / "den.json"
            pristine = json.loads(den_path.read_text("utf-8"))

            def mutate(envelope, rng):
                mutated = copy.deepcopy(envelope)
                choice = rng.randrange(9)
                if choice == 0:
                    key = rng.choice(sorted(mutated))
                    del mutated[key]
                elif choice == 1:
                    mutated["extra"] = True
                elif choice == 2:
                    mutated["schema_version"] = rng.choice([2, True, "1", 1.0, None])
                elif choice == 3:
                    mutated["kind"] = rng.choice(["atom", "", None, "DEN"])
                elif choice == 4:
                    value = mutated["checksum"]["value"]
                    digit = "0" if value[0] != "0" else "1"
                    mutated["checksum"]["value"] = digit + value[1:]
                elif choice == 5:
                    mutated["checksum"]["algorithm"] = rng.choice(["md5", "", None])
                elif choice == 6:
                    keys = sorted(mutated["payload"])
                    key = rng.choice(keys)
                    mutated["payload"][key] = rng.choice([None, 123, {}, ["x"]])
                elif choice == 7:
                    keys = sorted(mutated["payload"])
                    del mutated["payload"][rng.choice(keys)]
                else:
                    mutated["checksum"] = rng.choice([None, "sha256", {}, {"value": "x"}])
                return mutated

            for seed in range(200):
                rng = random.Random(seed)
                corrupted = mutate(pristine, rng)
                if rng.randrange(2) == 0:
                    text = json.dumps(corrupted)
                else:
                    text = json.dumps(corrupted)[: max(1, len(json.dumps(corrupted)) // 2)]
                den_path.write_text(text, "utf-8")
                try:
                    store.load_den()
                except StoreError:
                    pass
                except Exception as exc:
                    self.fail(f"seed {seed} leaked {type(exc).__name__}: {exc}")
                else:
                    self.fail(f"seed {seed} accepted a corrupted record")

            den_path.write_text(json.dumps(pristine), "utf-8")
            self.assertEqual(store.load_den().name, "test den")


class LineageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = new_store(Path(self._temporary.name) / "proj")
        self.pathway_id = self.store.load_den().default_pathway_id
        self.empty_digest = hashlib.sha256(b"").hexdigest()

    def _save_atom(self, atom_id, pathway_id):
        self.store._save_atom(
            model.Atom(
                id=atom_id,
                pathway_id=pathway_id,
                started_at="2026-01-01T00:00:00.000000+00:00",
                finished_at="2026-01-01T00:00:01.000000+00:00",
                duration_seconds=1.0,
                outcome=model.Outcome.PASSED,
                return_code=0,
                launch_error_category=None,
                declared_parameters={},
                command=("x",),
                stdout_log=f"{STATE_DIR_NAME}/logs/{atom_id}.stdout.log",
                stderr_log=f"{STATE_DIR_NAME}/logs/{atom_id}.stderr.log",
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_sha256=self.empty_digest,
                stderr_sha256=self.empty_digest,
            )
        )

    def test_cyclic_lineage_is_rejected(self):
        atom_id = model.make_id("atom")
        chokepoint_id = model.make_id("chokepoint")
        self._save_atom(atom_id, self.pathway_id)
        self.store._save_chokepoint(
            model.Chokepoint(
                id=chokepoint_id,
                pathway_id=self.pathway_id,
                atom_id=atom_id,
                created_at="2026-01-01T00:00:02.000000+00:00",
                kind="terminal",
                outcome=model.Outcome.PASSED,
                branchable=True,
            )
        )
        pathway = self.store.load_pathway(self.pathway_id)
        cyclic = replace(pathway, source_chokepoint_id=chokepoint_id)
        self.store._write_record(
            self.store.pathways_dir / f"{self.pathway_id}.json",
            "pathway",
            model.pathway_to_payload(cyclic),
        )
        with self.assertRaises(StoreError):
            self.store.load_pathway(self.pathway_id)

    def test_chokepoint_atom_pathway_disagreement_is_rejected(self):
        other = model.make_id("pathway")
        self.store._save_pathway(
            model.Pathway(
                id=other,
                den_id=self.store.load_den().id,
                created_at="2026-01-01T00:00:00.000000+00:00",
                parent_pathway_id=None,
                source_chokepoint_id=None,
                reason="other",
                parameters={},
            )
        )
        atom_id = model.make_id("atom")
        self._save_atom(atom_id, other)
        chokepoint_id = model.make_id("chokepoint")
        self.store._save_chokepoint(
            model.Chokepoint(
                id=chokepoint_id,
                pathway_id=self.pathway_id,
                atom_id=atom_id,
                created_at="2026-01-01T00:00:02.000000+00:00",
                kind="terminal",
                outcome=model.Outcome.PASSED,
                branchable=True,
            )
        )
        with self.assertRaises(StoreError):
            self.store.load_chokepoint(chokepoint_id)


class TieBreakTests(unittest.TestCase):
    def test_chokepoints_with_equal_timestamps_sort_by_id_descending(self):
        with temp_root() as root:
            store = new_store(root)
            pathway_id = store.load_den().default_pathway_id
            first = "chokepoint-" + "0" * 31 + "a"
            second = "chokepoint-" + "0" * 31 + "b"
            created = "2026-01-01T00:00:00.000000+00:00"
            for identifier in (first, second):
                store._save_chokepoint(
                    model.Chokepoint(
                        id=identifier,
                        pathway_id=pathway_id,
                        atom_id=model.make_id("atom"),
                        created_at=created,
                        kind="terminal",
                        outcome=model.Outcome.PASSED,
                        branchable=True,
                    )
                )
            listed = [item.id for item in store.list_chokepoints()]
            self.assertEqual(listed, [second, first])


class EvidenceVerificationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = new_store(Path(self._temporary.name) / "proj")
        self.pathway_id = self.store.load_den().default_pathway_id
        self.empty_digest = hashlib.sha256(b"").hexdigest()

    def _run_once(self):
        from ocura_oss import runner

        return runner.run_command(
            self.store,
            pathway_id=self.pathway_id,
            argv=[sys.executable, "-c", "print('evidence')"],
            declared_parameters={},
        )

    def test_intact_evidence_passes(self):
        execution = self._run_once()
        self.store.verify_atom_evidence(execution.atom)
        report = self.store.verify_state()
        self.assertTrue(report.ok)
        self.assertEqual(report.pathways, 1)
        self.assertEqual(report.atoms, 1)
        self.assertEqual(report.chokepoints, 1)
        self.assertEqual(report.logs_checked, 2)

    def test_missing_log_is_rejected(self):
        execution = self._run_once()
        (self.store.logs_dir / f"{execution.atom.id}.stdout.log").unlink()
        with self.assertRaises(StoreError):
            self.store.verify_atom_evidence(execution.atom)

    def test_size_mismatch_is_rejected(self):
        execution = self._run_once()
        log = self.store.logs_dir / f"{execution.atom.id}.stderr.log"
        log.write_bytes(b"x")
        with self.assertRaises(StoreError):
            self.store.verify_atom_evidence(execution.atom)

    def test_content_tamper_is_rejected(self):
        execution = self._run_once()
        log = self.store.logs_dir / f"{execution.atom.id}.stdout.log"
        data = bytearray(log.read_bytes())
        data[0] = data[0] ^ 0x20
        log.write_bytes(bytes(data))
        with self.assertRaises(StoreError):
            self.store.verify_atom_evidence(execution.atom)

    def test_escaping_log_paths_are_rejected(self):
        cases = ["../escape.log", ".ocura-oss/logs/../../escape.log", "C:\\evil\\x.log"]
        for index, stdout_log in enumerate(cases):
            with self.subTest(path=stdout_log):
                atom = model.Atom(
                    id=model.make_id("atom"),
                    pathway_id=self.pathway_id,
                    started_at="2026-01-01T00:00:00.000000+00:00",
                    finished_at="2026-01-01T00:00:01.000000+00:00",
                    duration_seconds=1.0,
                    outcome=model.Outcome.PASSED,
                    return_code=0,
                    launch_error_category=None,
                    declared_parameters={},
                    command=("x",),
                    stdout_log=stdout_log,
                    stderr_log=f".ocura-oss/logs/{index}.stderr.log",
                    stdout_bytes=0,
                    stderr_bytes=0,
                    stdout_sha256=self.empty_digest,
                    stderr_sha256=self.empty_digest,
                )
                self.store._save_atom(atom)
                with self.assertRaises(StoreError):
                    self.store.verify_atom_evidence(atom)

    def test_verify_state_counts_only_logs_it_checked(self):
        execution = self._run_once()
        (self.store.logs_dir / f"{execution.atom.id}.stdout.log").unlink()
        report = self.store.verify_state()
        self.assertFalse(report.ok)
        self.assertEqual(report.atoms, 1)
        self.assertEqual(report.logs_checked, 1)

    def test_verify_state_keeps_checksum_mismatch_out_of_orphan_report(self):
        execution = self._run_once()
        log = self.store.logs_dir / f"{execution.atom.id}.stdout.log"
        contents = bytearray(log.read_bytes())
        contents[0] ^= 0x20
        log.write_bytes(contents)

        report = self.store.verify_state()

        self.assertFalse(report.ok)
        self.assertEqual(report.logs_checked, 1)
        self.assertTrue(
            any("log checksum mismatch" in problem for _record, problem in report.problems)
        )
        self.assertFalse(
            any(
                record == f"logs/{log.name}" and "orphaned" in problem
                for record, problem in report.problems
            )
        )

    def test_verify_state_detects_orphaned_log_files(self):
        self._run_once()
        orphan = self.store.logs_dir / "orphaned.stdout.log"
        orphan.write_bytes(b"unreferenced")
        report = self.store.verify_state()
        self.assertFalse(report.ok)
        self.assertTrue(
            any(record == "logs/orphaned.stdout.log" for record, _problem in report.problems)
        )

    def test_verify_state_reports_problems_with_record_names(self):
        self._run_once()
        target = sorted(self.store.chokepoints_dir.glob("*.json"))[0]
        target.write_text("{broken", "utf-8")
        report = self.store.verify_state()
        self.assertFalse(report.ok)
        self.assertTrue(any(record == target.name for record, _problem in report.problems))


class DuplicateRecordTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = new_store(Path(self._temporary.name) / "proj")

    def _run_once(self):
        from ocura_oss import runner

        return runner.run_command(
            self.store,
            pathway_id=self.store.load_den().default_pathway_id,
            argv=[sys.executable, "-c", "print('duplicate test')"],
            declared_parameters={},
        )

    def _copy_atom_under_new_name(self):
        execution = self._run_once()
        original = self.store.atoms_dir / f"{execution.atom.id}.json"
        other_id = model.make_id("atom")
        copy_path = self.store.atoms_dir / f"{other_id}.json"
        copy_path.write_bytes(original.read_bytes())
        return other_id

    def test_listing_fails_closed_on_duplicated_record(self):
        other_id = self._copy_atom_under_new_name()
        with self.assertRaises(StoreError) as ctx:
            self.store.list_atoms()
        self.assertIn(f"{other_id}.json", str(ctx.exception))

    def test_verify_state_reports_copied_filename_while_original_passes(self):
        other_id = self._copy_atom_under_new_name()
        report = self.store.verify_state()
        self.assertFalse(report.ok)
        self.assertTrue(any(record == f"{other_id}.json" for record, _problem in report.problems))
        mismatch_records = [
            record
            for record, problem in report.problems
            if "does not match its filename" in problem
        ]
        self.assertEqual(mismatch_records, [f"{other_id}.json"])

    def test_chokepoint_copy_fails_chokepoint_listing(self):
        self._run_once()
        original = sorted(self.store.chokepoints_dir.glob("*.json"))[0]
        other_id = model.make_id("chokepoint")
        (self.store.chokepoints_dir / f"{other_id}.json").write_bytes(original.read_bytes())
        with self.assertRaises(StoreError):
            self.store.list_chokepoints()


class SemanticInvariantTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = new_store(Path(self._temporary.name) / "proj")
        self.pathway_id = self.store.load_den().default_pathway_id
        self.den_id = self.store.load_den().id
        self.empty_digest = hashlib.sha256(b"").hexdigest()

    def _craft_atom(self, pathway_id, *, outcome=model.Outcome.PASSED, return_code=0):
        atom_id = model.make_id("atom")
        for suffix in ("stdout", "stderr"):
            (self.store.logs_dir / f"{atom_id}.{suffix}.log").write_bytes(b"")
        atom = model.Atom(
            id=atom_id,
            pathway_id=pathway_id,
            started_at="2026-01-01T00:00:00.000000+00:00",
            finished_at="2026-01-01T00:00:01.000000+00:00",
            duration_seconds=1.0,
            outcome=outcome,
            return_code=return_code,
            launch_error_category=None,
            declared_parameters={},
            command=("x",),
            stdout_log=f"{STATE_DIR_NAME}/logs/{atom_id}.stdout.log",
            stderr_log=f"{STATE_DIR_NAME}/logs/{atom_id}.stderr.log",
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_sha256=self.empty_digest,
            stderr_sha256=self.empty_digest,
        )
        self.store._save_atom(atom)
        return atom

    def _craft_chokepoint(self, atom_id, outcome=model.Outcome.PASSED):
        chokepoint = model.Chokepoint(
            id=model.make_id("chokepoint"),
            pathway_id=self.pathway_id,
            atom_id=atom_id,
            created_at="2026-01-01T00:00:02.000000+00:00",
            kind="terminal",
            outcome=outcome,
            branchable=True,
        )
        self.store._save_chokepoint(chokepoint)
        return chokepoint

    def test_atom_with_nonexistent_pathway_fails_verification(self):
        ghost = "pathway-" + "f" * 32
        atom = self._craft_atom(ghost)
        report = self.store.verify_state()
        self.assertFalse(report.ok)
        self.assertTrue(
            any(
                record == f"{atom.id}.json" and "pathway" in problem
                for record, problem in report.problems
            )
        )

    def test_filename_id_mismatch_is_rejected(self):
        atom = self._craft_atom(self.pathway_id)
        original = self.store.atoms_dir / f"{atom.id}.json"
        other_id = model.make_id("atom")
        copied = self.store.atoms_dir / f"{other_id}.json"
        copied.write_bytes(original.read_bytes())
        with self.assertRaises(StoreError):
            self.store.load_atom(other_id)

    def test_chokepoint_outcome_must_match_atom_outcome(self):
        atom = self._craft_atom(self.pathway_id, outcome=model.Outcome.PASSED)
        chokepoint = self._craft_chokepoint(atom.id, outcome=model.Outcome.FAILED)
        with self.assertRaises(StoreError):
            self.store.load_chokepoint(chokepoint.id)

    def test_source_chokepoint_must_belong_to_parent_pathway(self):
        from ocura_oss import runner

        execution = runner.run_command(
            self.store,
            pathway_id=self.pathway_id,
            argv=[sys.executable, "-c", "print('base')"],
            declared_parameters={},
        )
        legitimate = model.Pathway(
            id=model.make_id("pathway"),
            den_id=self.den_id,
            created_at=execution.chokepoint.created_at,
            parent_pathway_id=self.pathway_id,
            source_chokepoint_id=execution.chokepoint.id,
            reason="legitimate branch",
            parameters={},
        )
        self.store._save_pathway(legitimate)
        swapped = model.Pathway(
            id=model.make_id("pathway"),
            den_id=self.den_id,
            created_at=legitimate.created_at,
            parent_pathway_id=legitimate.id,
            source_chokepoint_id=execution.chokepoint.id,
            reason="source belongs to grandparent, not parent",
            parameters={},
        )
        self.store._save_pathway(swapped)
        with self.assertRaises(StoreError):
            self.store.load_pathway(swapped.id)


if __name__ == "__main__":
    unittest.main()
