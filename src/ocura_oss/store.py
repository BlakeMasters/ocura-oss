# SPDX-License-Identifier: MPL-2.0

"""State-root resolution, record layout, atomic writes, reads, and listings."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar

from ocura_oss import locking, model

STATE_DIR_NAME = ".ocura-oss"

_ENVELOPE_KEYS = {"schema_version", "kind", "payload", "checksum"}
_HASH_CHUNK = 1024 * 1024
_REMOVE_RETRIES = 200
_REMOVE_DELAY = 0.01

_T = TypeVar("_T")


@dataclass(frozen=True)
class StateVerification:
    """Result of verifying every record and log under a state directory.

    ``running_attempts`` names attempts another live process is still
    recording; they are not problems. ``abandoned_attempts`` names attempts
    whose recording process stopped first; each also appears in ``problems``
    until :meth:`Store.recover` closes it.

    When the state is intact, ``manifest`` holds one ``KIND ID CHECKSUM`` line
    for every verified record, sorted, and ``digest`` is the SHA-256 of those
    lines joined and terminated by newlines. Both are empty otherwise.
    """

    pathways: int
    atoms: int
    chokepoints: int
    logs_checked: int
    problems: tuple[tuple[str, str], ...]
    running_attempts: tuple[str, ...] = ()
    abandoned_attempts: tuple[str, ...] = ()
    manifest: tuple[str, ...] = ()
    digest: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class RecoveredAttempt:
    """One unfinished attempt closed by :meth:`Store.recover`."""

    atom_id: str
    chokepoint_id: str
    pathway_id: str
    action: model.RecoveryAction


class StoreError(Exception):
    """State, schema, or integrity failure. The CLI maps this to exit code 2."""


def resolve_root(root: os.PathLike[str] | str | None = None) -> Path:
    """Resolve the project root without changing the process working directory."""
    base = Path(root).expanduser() if root else Path.cwd()
    return base.resolve()


class Store:
    """Read and verify Ocura OSS state under one project root.

    The documented loading, listing, initialization, verification, and
    recovery methods form the provisional low-level API. Record writing
    remains internal to the package workflows.
    """

    def __init__(self, root: os.PathLike[str] | str | None = None) -> None:
        self.root = resolve_root(root)
        self.state_dir = self.root / STATE_DIR_NAME
        self.pathways_dir = self.state_dir / "pathways"
        self.atoms_dir = self.state_dir / "atoms"
        self.chokepoints_dir = self.state_dir / "chokepoints"
        self.attempts_dir = self.state_dir / "attempts"
        self.logs_dir = self.state_dir / "logs"
        self.den_path = self.state_dir / "den.json"

    def exists(self) -> bool:
        """Return whether the state directory exists."""
        return self.state_dir.exists()

    def require(self) -> None:
        """Require an initialized den under this project root."""
        if not self.den_path.is_file():
            raise StoreError(f"no Ocura OSS state found under {self.root}")

    def load_den(self) -> model.Den:
        """Load and validate the den record."""
        payload = self._read_envelope(self.den_path, "den")
        try:
            return model.den_from_payload(payload)
        except model.ValidationError as exc:
            raise StoreError(f"invalid den record den.json: {exc}") from exc

    def load_pathway(self, pathway_id: str) -> model.Pathway:
        """Load a pathway and validate its den and lineage references."""
        return self._load_pathway(pathway_id, frozenset())

    def _load_pathway(self, pathway_id: str, seen_ids: frozenset[str]) -> model.Pathway:
        pathway = self._load_structural(
            "pathways", pathway_id, "pathway", "pathway", model.pathway_from_payload
        )
        if pathway_id in seen_ids:
            raise StoreError("cyclic lineage reference in state records")
        seen = seen_ids | {pathway_id}
        den = self.load_den()
        if pathway.den_id != den.id:
            raise StoreError(f"pathway {pathway_id} references an unknown den")
        if (pathway.parent_pathway_id is None) != (pathway.source_chokepoint_id is None):
            raise StoreError(
                f"pathway {pathway_id} must set parent pathway and source chokepoint together"
            )
        if pathway.parent_pathway_id is not None:
            self._load_pathway(pathway.parent_pathway_id, seen)
        if pathway.source_chokepoint_id is not None:
            source = self._load_chokepoint(pathway.source_chokepoint_id, seen)
            if (
                pathway.parent_pathway_id is not None
                and source.pathway_id != pathway.parent_pathway_id
            ):
                raise StoreError(
                    f"pathway {pathway_id} source chokepoint does not belong to its parent pathway"
                )
        return pathway

    def load_atom(self, atom_id: str) -> model.Atom:
        """Load an atom and validate its pathway reference."""
        return self._load_atom(atom_id, frozenset())

    def _load_atom(self, atom_id: str, seen_ids: frozenset[str]) -> model.Atom:
        atom = self._load_structural("atoms", atom_id, "atom", "atom", model.atom_from_payload)
        if atom_id in seen_ids:
            raise StoreError("cyclic lineage reference in state records")
        self._load_pathway(atom.pathway_id, seen_ids | {atom_id})
        return atom

    def load_chokepoint(self, chokepoint_id: str) -> model.Chokepoint:
        """Load a chokepoint and validate its atom, pathway, and outcome."""
        return self._load_chokepoint(chokepoint_id, frozenset())

    def _load_chokepoint(self, chokepoint_id: str, seen_ids: frozenset[str]) -> model.Chokepoint:
        chokepoint = self._load_structural(
            "chokepoints",
            chokepoint_id,
            "chokepoint",
            "chokepoint",
            model.chokepoint_from_payload,
        )
        if chokepoint_id in seen_ids:
            raise StoreError("cyclic lineage reference in state records")
        seen = seen_ids | {chokepoint_id}
        atom = self._load_atom(chokepoint.atom_id, seen)
        if atom.pathway_id != chokepoint.pathway_id:
            raise StoreError(f"chokepoint {chokepoint_id} and its atom disagree on the pathway")
        if atom.outcome is not chokepoint.outcome:
            raise StoreError(f"chokepoint {chokepoint_id} outcome disagrees with its atom")
        return chokepoint

    def _save_den(self, den: model.Den) -> None:
        self._write_record(self.den_path, "den", model.den_to_payload(den))

    def _save_pathway(self, pathway: model.Pathway) -> None:
        path = self._record_path(self.pathways_dir, pathway.id, "pathway")
        self._write_record(path, "pathway", model.pathway_to_payload(pathway))

    def _save_atom(self, atom: model.Atom) -> None:
        path = self._record_path(self.atoms_dir, atom.id, "atom")
        self._write_record(path, "atom", model.atom_to_payload(atom))

    def _save_chokepoint(self, chokepoint: model.Chokepoint) -> None:
        path = self._record_path(self.chokepoints_dir, chokepoint.id, "chokepoint")
        self._write_record(path, "chokepoint", model.chokepoint_to_payload(chokepoint))

    def _attempt_path(self, attempt_id: str) -> Path:
        return self._record_path(self.attempts_dir, attempt_id, "atom")

    def _attempt_lock(self, attempt_id: str) -> Path:
        return self._attempt_path(attempt_id).with_suffix(".lock")

    @contextlib.contextmanager
    def _recording(self, attempt: model.Attempt) -> Iterator[None]:
        """Journal an attempt for as long as this process is recording it.

        The lock is taken before the record is written and outlives it, so a
        reader that finds the record unlocked knows its recorder is gone.
        """
        self.attempts_dir.mkdir(parents=True, exist_ok=True)
        with locking.hold(self._attempt_lock(attempt.id)):
            self._write_record(
                self._attempt_path(attempt.id), "attempt", model.attempt_to_payload(attempt)
            )
            yield

    def _remove_attempt(self, attempt_id: str) -> None:
        _remove_file(self._attempt_path(attempt_id))

    def list_attempts(self) -> list[tuple[model.Attempt, model.AttemptState]]:
        """Return unfinished attempts with their state, in start order.

        An attempt is ``running`` while a live process is recording it and
        ``abandoned`` once that process has stopped without finalizing it.
        Finalized attempts are atoms and are not listed.
        """
        problems: list[tuple[str, str]] = []
        attempts = self._scan_attempts(problems)
        if problems:
            record, problem = problems[0]
            raise StoreError(f"{record}: {problem}")
        attempts.sort(key=lambda item: (item[0].started_at, item[0].id))
        return attempts

    def recover(
        self, *, now: Callable[[], datetime.datetime] | None = None
    ) -> tuple[RecoveredAttempt, ...]:
        """Close every unfinished attempt whose recording process has stopped.

        An attempt with no recorded outcome becomes an ``abandoned`` atom with
        a terminal chokepoint; whatever output it had captured is measured and
        retained. Its outcome, finish time, and duration are not known and are
        not invented. An attempt whose atom was already written only has its
        missing chokepoint completed or its leftover journal entry cleared.
        Attempts another live process is still recording are left alone.

        Recover only after the command itself has stopped: a command that
        outlived its recorder can keep writing to logs measured here.
        """
        self.require()
        clock = now or model.utc_now
        recovered: list[RecoveredAttempt] = []
        if not self.attempts_dir.is_dir():
            return ()
        for path in sorted(self.attempts_dir.glob("*.json")):
            with locking.try_hold(path.with_suffix(".lock")) as held:
                if not held or not path.exists():
                    continue
                attempt = self._load_listed(path, "attempt", model.attempt_from_payload)
                recovered.append(self._close_attempt(attempt, clock))
        for lock in sorted(self.attempts_dir.glob("*.lock")):
            if not lock.with_suffix(".json").exists():
                # Holding a lock file removes it on release; one still held
                # belongs to a run that has not written its record yet.
                with locking.try_hold(lock):
                    pass
        return tuple(recovered)

    def _close_attempt(
        self, attempt: model.Attempt, clock: Callable[[], datetime.datetime]
    ) -> RecoveredAttempt:
        atom_path = self._record_path(self.atoms_dir, attempt.id, "atom")
        chokepoint_path = self._record_path(
            self.chokepoints_dir, attempt.chokepoint_id, "chokepoint"
        )
        if atom_path.is_file():
            atom = self.load_atom(attempt.id)
            action = (
                model.RecoveryAction.CLEARED
                if chokepoint_path.is_file()
                else model.RecoveryAction.COMPLETED
            )
            created_at = atom.finished_at or model.format_timestamp(clock())
        else:
            atom = self._abandoned_atom(attempt)
            self._save_atom(atom)
            action = model.RecoveryAction.ABANDONED
            created_at = model.format_timestamp(clock())
        if not chokepoint_path.is_file():
            self._save_chokepoint(
                model.Chokepoint(
                    id=attempt.chokepoint_id,
                    pathway_id=atom.pathway_id,
                    atom_id=atom.id,
                    created_at=created_at,
                    kind=model.TERMINAL_KIND,
                    outcome=atom.outcome,
                    branchable=True,
                )
            )
        self._remove_attempt(attempt.id)
        return RecoveredAttempt(
            atom_id=atom.id,
            chokepoint_id=attempt.chokepoint_id,
            pathway_id=atom.pathway_id,
            action=action,
        )

    def _abandoned_atom(self, attempt: model.Attempt) -> model.Atom:
        measured: list[tuple[int, str]] = []
        for relative in (attempt.stdout_log, attempt.stderr_log):
            if relative is None:
                continue
            path = self._evidence_path(attempt.id, relative)
            try:
                # A recorder stopped before opening a log captured nothing.
                open(path, "ab").close()
                measured.append(size_and_sha256(path))
            except OSError as exc:
                raise StoreError(f"attempt {attempt.id} log is unreadable: {relative}") from exc
        captured = len(measured) == 2
        return model.Atom(
            id=attempt.id,
            pathway_id=attempt.pathway_id,
            started_at=attempt.started_at,
            finished_at=None,
            duration_seconds=None,
            outcome=model.Outcome.ABANDONED,
            return_code=None,
            launch_error_category=None,
            declared_parameters=attempt.declared_parameters,
            command=attempt.command,
            stdout_log=attempt.stdout_log,
            stderr_log=attempt.stderr_log,
            stdout_bytes=measured[0][0] if captured else None,
            stderr_bytes=measured[1][0] if captured else None,
            stdout_sha256=measured[0][1] if captured else None,
            stderr_sha256=measured[1][1] if captured else None,
            output_capture=attempt.output_capture,
            masked_arguments=attempt.masked_arguments,
            context=attempt.context,
        )

    def list_pathways(self) -> list[model.Pathway]:
        """Return validated pathways in creation order."""
        records = self._list_typed(self.pathways_dir, "pathway", model.pathway_from_payload)
        records.sort(key=lambda item: (item.created_at, item.id))
        return records

    def list_atoms(self) -> list[model.Atom]:
        """Return validated atoms in start order."""
        records = self._list_typed(self.atoms_dir, "atom", model.atom_from_payload)
        records.sort(key=lambda item: (item.started_at, item.id))
        return records

    def list_chokepoints(self) -> list[model.Chokepoint]:
        """Return validated chokepoints in reverse creation order."""
        records = self._list_typed(
            self.chokepoints_dir, "chokepoint", model.chokepoint_from_payload
        )
        records.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return records

    def has_terminal_evidence(self, pathway_id: str) -> bool:
        """Return whether an atom is recorded on a pathway."""
        return any(atom.pathway_id == pathway_id for atom in self.list_atoms())

    def verify_atom_evidence(self, atom: model.Atom) -> None:
        """Verify one atom's recorded logs.

        Each log must stay directly inside ``.ocura-oss/logs/``, exist on disk,
        and match the recorded byte count and SHA-256 digest. Raises
        StoreError on the first failing log. An atom recorded without output
        capture has no logs to verify.
        """
        for relative, recorded_size, recorded_digest in _recorded_logs(atom):
            self._verify_log(atom.id, relative, recorded_size, recorded_digest)

    def resolve_log_path(self, atom: model.Atom, stream: Literal["stdout", "stderr"]) -> Path:
        """Resolve one recorded log path after containment checks.

        Call :meth:`verify_atom_evidence` first when the log's recorded size and
        digest must also be checked. Use :meth:`read_verified_log` to consume
        bytes whose size and digest are checked as part of the same read.
        Raises StoreError for an atom recorded without output capture.
        """
        return self._evidence_path(atom.id, _recorded_log(atom, stream)[0])

    def read_verified_log(
        self, atom_id: str, *, stream: Literal["stdout", "stderr"] = "stdout"
    ) -> bytes:
        """Read one complete log and verify the exact bytes returned.

        Loads the stored atom and validates its lineage, then checks the selected
        log's containment, byte count, and SHA-256 digest. Missing, unreadable, or
        inconsistent evidence raises StoreError. The other log and the rest of
        the ledger are not verified. The complete log is held in memory; decoding
        and interpretation are left to the caller. An atom recorded without
        output capture has no log to read and raises StoreError.
        """
        atom = self.load_atom(atom_id)
        relative, size, digest = _recorded_log(atom, stream)
        try:
            path = self.resolve_log_path(atom, stream)
            if not path.is_file():
                raise StoreError(f"atom {atom_id} log is missing: {relative}")
            data = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise StoreError(f"atom {atom_id} log is unreadable: {relative}") from exc
        if len(data) != size:
            raise StoreError(
                f"atom {atom_id} log size mismatch for {relative}: "
                f"recorded {size}, found {len(data)}"
            )
        if hashlib.sha256(data).hexdigest() != digest:
            raise StoreError(f"atom {atom_id} log checksum mismatch for {relative}")
        return data

    def _verify_log(
        self, atom_id: str, relative: str, recorded_size: int, recorded_digest: str
    ) -> Path:
        candidate = self._evidence_path(atom_id, relative)
        if not candidate.is_file():
            raise StoreError(f"atom {atom_id} log is missing: {relative}")
        actual_size = candidate.stat().st_size
        if actual_size != recorded_size:
            raise StoreError(
                f"atom {atom_id} log size mismatch for {relative}: "
                f"recorded {recorded_size}, found {actual_size}"
            )
        actual_digest = sha256_of_file(candidate)
        if actual_digest != recorded_digest:
            raise StoreError(f"atom {atom_id} log checksum mismatch for {relative}")
        return candidate

    def verify_state(self, *, against: Iterable[str] | None = None) -> StateVerification:
        """Verify every state record and every log referenced by a valid atom.

        Also reports files under ``.ocura-oss/logs/`` that no record references,
        atoms without exactly one terminal chokepoint, and abandoned attempts.
        Returns a report whose ``problems`` list is empty when the state is
        fully intact; ``logs_checked`` counts the individual logs that actually
        passed verification. A missing or unreadable den raises StoreError,
        because nothing can be verified without it.

        Other processes may record runs under the same root while this runs.
        An attempt a live process is still recording is reported in
        ``running_attempts`` and is not a problem; its logs are not checked
        until it is finalized.

        *against* is a manifest retained from an earlier verification. Each of
        its entries must still be present unchanged; records added since are
        allowed. Kept somewhere this state's writers cannot reach, it detects
        records that were later rewritten together with their checksums.
        """
        retained = _parse_manifest(against)
        den = self.load_den()
        problems: list[tuple[str, str]] = []
        manifest = [_manifest_line("den", den.id, self._read_envelope(self.den_path, "den"))]
        # A run writes its attempt before its logs and its atom before its
        # chokepoint, and removes the attempt last. Reading in that same order
        # never mistakes a run in progress for damage.
        log_entries = sorted(self.logs_dir.iterdir()) if self.logs_dir.is_dir() else []
        attempts = self._scan_attempts(problems)
        pathways = self._scan_records(
            self.pathways_dir, "pathway", model.pathway_from_payload, problems, manifest
        )
        atoms = self._scan_records(
            self.atoms_dir, "atom", model.atom_from_payload, problems, manifest
        )
        chokepoints = self._scan_records(
            self.chokepoints_dir, "chokepoint", model.chokepoint_from_payload, problems, manifest
        )
        for pathway in pathways:
            try:
                self.load_pathway(pathway.id)
            except StoreError as exc:
                problems.append((f"{pathway.id}.json", str(exc)))
        logs_checked = 0
        referenced_logs: set[Path] = set()
        running: list[str] = []
        abandoned: list[str] = []
        for attempt, state in attempts:
            label = f"attempts/{attempt.id}.json"
            if state is model.AttemptState.RUNNING:
                running.append(attempt.id)
            else:
                abandoned.append(attempt.id)
                problems.append(
                    (label, "attempt was not finalized; run `ocura-oss recover` to close it")
                )
            for relative in (attempt.stdout_log, attempt.stderr_log):
                if relative is None:
                    continue
                try:
                    referenced_logs.add(self._evidence_path(attempt.id, relative))
                except StoreError as exc:
                    problems.append((label, str(exc)))
        for atom in atoms:
            try:
                self.load_atom(atom.id)
            except StoreError as exc:
                problems.append((f"{atom.id}.json", str(exc)))
            for relative, size_field, digest_field in _recorded_logs(atom):
                try:
                    referenced_logs.add(self._evidence_path(atom.id, relative))
                    self._verify_log(atom.id, relative, size_field, digest_field)
                    logs_checked += 1
                except StoreError as exc:
                    problems.append((f"{atom.id}.json", str(exc)))
        self._report_orphaned_logs(log_entries, referenced_logs, problems)
        for chokepoint in chokepoints:
            try:
                self.load_chokepoint(chokepoint.id)
            except StoreError as exc:
                problems.append((f"{chokepoint.id}.json", str(exc)))
        self._report_missing_chokepoints(
            atoms, chokepoints, {attempt.id for attempt, _state in attempts}, problems
        )
        if all(item.id != den.default_pathway_id for item in pathways):
            problems.append(("den.json", "default pathway record is missing"))
        manifest.sort()
        current = set(manifest)
        for entry in retained:
            if entry not in current:
                kind, record_id, _checksum = entry.split(" ")
                problems.append(
                    (f"{record_id}.json", f"retained {kind} record is missing or was changed")
                )
        intact = not problems
        return StateVerification(
            pathways=len(pathways),
            atoms=len(atoms),
            chokepoints=len(chokepoints),
            logs_checked=logs_checked,
            problems=tuple(problems),
            running_attempts=tuple(running),
            abandoned_attempts=tuple(abandoned),
            manifest=tuple(manifest) if intact else (),
            digest=manifest_digest(manifest) if intact else None,
        )

    def initialize_state(
        self,
        *,
        name: str,
    ) -> tuple[model.Den, model.Pathway]:
        """Create one den and one default pathway; fail if state already exists."""
        if self.exists():
            raise StoreError(f"cannot initialize: state directory already exists: {self.state_dir}")
        if not isinstance(name, str) or not name.strip():
            raise StoreError("den name must not be blank")
        created_at = model.format_timestamp(model.utc_now())
        den = model.Den(
            id=model.make_id("den"),
            name=name,
            created_at=created_at,
            default_pathway_id=model.make_id("pathway"),
        )
        pathway = model.Pathway(
            id=den.default_pathway_id,
            den_id=den.id,
            created_at=created_at,
            parent_pathway_id=None,
            source_chokepoint_id=None,
            reason="default pathway",
            parameters={},
        )
        try:
            # Creating the directory is the claim: of two racing initializers,
            # exactly one succeeds.
            self.state_dir.mkdir(parents=True)
        except FileExistsError:
            raise StoreError(
                f"cannot initialize: state directory already exists: {self.state_dir}"
            ) from None
        for directory in (
            self.pathways_dir,
            self.atoms_dir,
            self.chokepoints_dir,
            self.attempts_dir,
            self.logs_dir,
        ):
            directory.mkdir()
        self._save_den(den)
        self._save_pathway(pathway)
        return den, pathway

    def _dir_for(self, subdir: str) -> Path:
        return self.state_dir / subdir

    def _record_path(self, directory: Path, record_id: str, prefix: str) -> Path:
        if not model.is_valid_id(record_id, prefix):
            raise StoreError(f"malformed {prefix} identifier: {record_id!r}")
        return directory / f"{record_id}.json"

    def _load_structural(
        self,
        subdir: str,
        record_id: str,
        prefix: str,
        kind: str,
        converter: Callable[[dict], _T],
    ) -> _T:
        path = self._record_path(self._dir_for(subdir), record_id, prefix)
        payload = self._read_envelope(path, kind)
        try:
            record = converter(payload)
        except model.ValidationError as exc:
            raise StoreError(f"invalid {kind} record {record_id}.json: {exc}") from exc
        if getattr(record, "id", None) != record_id:
            raise StoreError(
                f"{kind} record {record_id}.json payload id does not match its filename"
            )
        return record

    def _read_envelope(self, path: Path, expected_kind: str) -> dict:
        if not path.is_file():
            raise StoreError(f"missing {expected_kind} record: {path}")
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError(f"malformed {expected_kind} record {path.name}: unreadable") from exc
        if not isinstance(data, dict) or set(data) != _ENVELOPE_KEYS:
            raise StoreError(f"malformed {expected_kind} record {path.name}: unexpected envelope")
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise StoreError(f"malformed {expected_kind} record {path.name}: bad schema version")
        if schema_version != model.SCHEMA_VERSION:
            hint = (
                "; Ocura OSS 0.4 and earlier wrote it, and this version reads schema"
                f" {model.SCHEMA_VERSION} only"
                if schema_version == 1
                else ""
            )
            raise StoreError(
                f"unsupported schema version {schema_version} in {expected_kind} record"
                f" {path.name}{hint}"
            )
        if data["kind"] != expected_kind:
            raise StoreError(
                f"kind mismatch in {expected_kind} record {path.name}: {data['kind']!r}"
            )
        checksum = data["checksum"]
        if (
            not isinstance(checksum, dict)
            or set(checksum) != {"algorithm", "value"}
            or checksum["algorithm"] != model.CHECKSUM_ALGORITHM
        ):
            raise StoreError(f"malformed checksum in {expected_kind} record {path.name}")
        expected = model.checksum_for(schema_version, data["kind"], data["payload"])["value"]
        if checksum["value"] != expected:
            raise StoreError(f"checksum mismatch in {expected_kind} record {path.name}")
        if not isinstance(data["payload"], dict):
            raise StoreError(
                f"malformed {expected_kind} record {path.name}: payload must be an object"
            )
        return data["payload"]

    def _write_record(self, path: Path, kind: str, payload: dict) -> None:
        envelope = {
            "schema_version": model.SCHEMA_VERSION,
            "kind": kind,
            "payload": payload,
            "checksum": model.checksum_for(model.SCHEMA_VERSION, kind, payload),
        }
        text = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
        _atomic_write_bytes(path, text.encode("utf-8"))

    def _list_typed(self, directory: Path, kind: str, converter: Callable[[dict], _T]) -> list[_T]:
        records: list[_T] = []
        if not directory.is_dir():
            return records
        for path in sorted(directory.glob("*.json")):
            records.append(self._load_listed(path, kind, converter))
        return records

    def _load_listed(
        self,
        path: Path,
        kind: str,
        converter: Callable[[dict], _T],
        manifest: list[str] | None = None,
    ) -> _T:
        try:
            payload = self._read_envelope(path, kind)
        except StoreError as exc:
            raise StoreError(f"invalid {kind} record {path.name}: {exc}") from exc
        try:
            record = converter(payload)
        except model.ValidationError as exc:
            raise StoreError(f"invalid {kind} record {path.name}: {exc}") from exc
        if getattr(record, "id", None) != path.stem:
            raise StoreError(f"{kind} record {path.name} payload id does not match its filename")
        if manifest is not None:
            manifest.append(_manifest_line(kind, path.stem, payload))
        return record

    def _scan_records(
        self,
        directory: Path,
        kind: str,
        converter: Callable[[dict], _T],
        problems: list[tuple[str, str]],
        manifest: list[str] | None = None,
    ) -> list[_T]:
        """Collect valid records and report invalid ones instead of raising."""
        records: list[_T] = []
        if not directory.is_dir():
            return records
        for path in sorted(directory.glob("*.json")):
            try:
                records.append(self._load_listed(path, kind, converter, manifest))
            except StoreError as exc:
                problems.append((path.name, str(exc)))
        return records

    def _scan_attempts(
        self, problems: list[tuple[str, str]]
    ) -> list[tuple[model.Attempt, model.AttemptState]]:
        """Collect unfinished attempts and whether each is still being recorded."""
        attempts: list[tuple[model.Attempt, model.AttemptState]] = []
        if not self.attempts_dir.is_dir():
            return attempts
        for path in sorted(self.attempts_dir.glob("*.json")):
            try:
                attempt = self._load_listed(path, "attempt", model.attempt_from_payload)
            except StoreError as exc:
                # A record that vanished was finalized while it was being read.
                if path.exists():
                    problems.append((f"attempts/{path.name}", str(exc)))
                continue
            if locking.is_held(self._attempt_lock(attempt.id)):
                attempts.append((attempt, model.AttemptState.RUNNING))
            elif path.exists():
                # The record is removed before its lock is released, so an
                # unlocked record that still exists was left by a dead recorder.
                attempts.append((attempt, model.AttemptState.ABANDONED))
        return attempts

    def _report_orphaned_logs(
        self,
        log_entries: list[Path],
        referenced_logs: set[Path],
        problems: list[tuple[str, str]],
    ) -> None:
        """Report entries under the logs directory that no record references."""
        for entry in log_entries:
            if entry.resolve() in referenced_logs:
                continue
            if entry.is_dir():
                problems.append((f"logs/{entry.name}", "unexpected directory under logs/"))
            else:
                problems.append(
                    (
                        f"logs/{entry.name}",
                        "orphaned file: no atom record references this log",
                    )
                )

    def _report_missing_chokepoints(
        self,
        atoms: list[model.Atom],
        chokepoints: list[model.Chokepoint],
        attempt_ids: set[str],
        problems: list[tuple[str, str]],
    ) -> None:
        """Report atoms that do not have exactly one terminal chokepoint."""
        counts: dict[str, int] = {}
        for chokepoint in chokepoints:
            counts[chokepoint.atom_id] = counts.get(chokepoint.atom_id, 0) + 1
        missing: list[model.Atom] = []
        for atom in atoms:
            count = counts.get(atom.id, 0)
            if count > 1:
                problems.append((f"{atom.id}.json", f"atom has {count} chokepoints"))
            elif count == 0 and atom.id not in attempt_ids:
                missing.append(atom)
        # A run that began after the attempts were listed may sit between its
        # atom and its chokepoint. Its attempt record is removed only once the
        # chokepoint exists, so check the record first and the chokepoints after.
        missing = [atom for atom in missing if not self._attempt_path(atom.id).exists()]
        if not missing:
            return
        current = {
            chokepoint.atom_id
            for chokepoint in self._scan_records(
                self.chokepoints_dir, "chokepoint", model.chokepoint_from_payload, []
            )
        }
        for atom in missing:
            if atom.id not in current:
                problems.append((f"{atom.id}.json", "atom has no terminal chokepoint"))

    def _evidence_path(self, atom_id: str, relative: str) -> Path:
        """Resolve a recorded log path; reject paths outside the logs dir."""
        if not isinstance(relative, str) or not relative:
            raise StoreError(f"atom {atom_id} has an empty log path")
        posix = PurePosixPath(relative)
        if posix.is_absolute() or ".." in posix.parts:
            raise StoreError(f"atom {atom_id} log path escapes the state directory: {relative}")
        candidate = (self.root / Path(*posix.parts)).resolve()
        if candidate.parent != self.logs_dir.resolve():
            raise StoreError(
                f"atom {atom_id} log path does not point inside .ocura-oss/logs/: {relative}"
            )
        return candidate


def size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(_HASH_CHUNK):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def sha256_of_file(path: Path) -> str:
    return size_and_sha256(path)[1]


def manifest_digest(entries: Iterable[str]) -> str:
    """Return the SHA-256 of manifest entries, sorted and newline-terminated."""
    text = "".join(f"{entry}\n" for entry in sorted(entries))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest_line(kind: str, record_id: str, payload: dict) -> str:
    checksum = model.checksum_for(model.SCHEMA_VERSION, kind, payload)["value"]
    return f"{kind} {record_id} {checksum}"


def _parse_manifest(lines: Iterable[str] | None) -> list[str]:
    """Normalize retained manifest lines; blank lines and ``#`` comments are skipped."""
    entries: list[str] = []
    for raw in lines or ():
        line = raw.strip() if isinstance(raw, str) else ""
        if not line or line.startswith("#"):
            continue
        parts = line.split(" ")
        if (
            len(parts) != 3
            or not model.is_valid_id(parts[1], parts[0])
            or len(parts[2]) != 64
            or set(parts[2]) - set("0123456789abcdef")
        ):
            raise StoreError(f"malformed manifest entry: {line!r}")
        entries.append(line)
    return entries


def _recorded_logs(atom: model.Atom) -> list[tuple[str, int, str]]:
    """Return ``(path, bytes, sha256)`` for each log an atom recorded."""
    logs: list[tuple[str, int, str]] = []
    for relative, size, digest in (
        (atom.stdout_log, atom.stdout_bytes, atom.stdout_sha256),
        (atom.stderr_log, atom.stderr_bytes, atom.stderr_sha256),
    ):
        if relative is not None and size is not None and digest is not None:
            logs.append((relative, size, digest))
    return logs


def _recorded_log(atom: model.Atom, stream: str) -> tuple[str, int, str]:
    if stream not in ("stdout", "stderr"):
        raise StoreError("log stream must be 'stdout' or 'stderr'")
    logs = _recorded_logs(atom)
    if len(logs) != 2:
        raise StoreError(f"atom {atom.id} was recorded without output capture")
    return logs[0] if stream == "stdout" else logs[1]


def _remove_file(path: Path) -> None:
    """Remove a file that a concurrent reader may briefly hold open.

    Windows refuses to remove an open file, so a reader verifying the state
    at that instant would otherwise make the removal fail.
    """
    for _ in range(_REMOVE_RETRIES):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(_REMOVE_DELAY)
    path.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
