# SPDX-License-Identifier: MPL-2.0

"""State-root resolution, record layout, atomic writes, reads, and listings."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar

from ocura_oss import model

STATE_DIR_NAME = ".ocura-oss"

_ENVELOPE_KEYS = {"schema_version", "kind", "payload", "checksum"}
_HASH_CHUNK = 1024 * 1024

_T = TypeVar("_T")


@dataclass(frozen=True)
class StateVerification:
    """Result of verifying every record and log under a state directory."""

    pathways: int
    atoms: int
    chokepoints: int
    logs_checked: int
    problems: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        return not self.problems


class StoreError(Exception):
    """State, schema, or integrity failure. The CLI maps this to exit code 2."""


def resolve_root(root: os.PathLike[str] | str | None = None) -> Path:
    """Resolve the project root without changing the process working directory."""
    base = Path(root).expanduser() if root else Path.cwd()
    return base.resolve()


class Store:
    """Read and verify Ocura OSS state under one project root.

    The documented loading, listing, initialization, and verification methods
    form the provisional low-level API. Record writing remains internal to the
    package workflows.
    """

    def __init__(self, root: os.PathLike[str] | str | None = None) -> None:
        self.root = resolve_root(root)
        self.state_dir = self.root / STATE_DIR_NAME
        self.pathways_dir = self.state_dir / "pathways"
        self.atoms_dir = self.state_dir / "atoms"
        self.chokepoints_dir = self.state_dir / "chokepoints"
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
        StoreError on the first failing log.
        """
        for relative, recorded_size, recorded_digest in (
            (atom.stdout_log, atom.stdout_bytes, atom.stdout_sha256),
            (atom.stderr_log, atom.stderr_bytes, atom.stderr_sha256),
        ):
            self._verify_log(atom.id, relative, recorded_size, recorded_digest)

    def resolve_log_path(self, atom: model.Atom, stream: Literal["stdout", "stderr"]) -> Path:
        """Resolve one recorded log path after containment checks.

        Call :meth:`verify_atom_evidence` first when the log's recorded size and
        digest must also be checked. Use :meth:`read_verified_log` to consume
        bytes whose size and digest are checked as part of the same read.
        """
        if stream == "stdout":
            relative = atom.stdout_log
        elif stream == "stderr":
            relative = atom.stderr_log
        else:
            raise StoreError("log stream must be 'stdout' or 'stderr'")
        return self._evidence_path(atom.id, relative)

    def read_verified_log(
        self, atom_id: str, *, stream: Literal["stdout", "stderr"] = "stdout"
    ) -> bytes:
        """Read one complete log and verify the exact bytes returned.

        Loads the stored atom and validates its lineage, then checks the selected
        log's containment, byte count, and SHA-256 digest. Missing, unreadable, or
        inconsistent evidence raises StoreError. The other log and the rest of
        the ledger are not verified. The complete log is held in memory; decoding
        and interpretation are left to the caller.
        """
        atom = self.load_atom(atom_id)
        if stream == "stdout":
            relative, size, digest = atom.stdout_log, atom.stdout_bytes, atom.stdout_sha256
        elif stream == "stderr":
            relative, size, digest = atom.stderr_log, atom.stderr_bytes, atom.stderr_sha256
        else:
            raise StoreError("log stream must be 'stdout' or 'stderr'")
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

    def verify_state(self) -> StateVerification:
        """Verify every state record and every log referenced by a valid atom.

        Also reports files under ``.ocura-oss/logs/`` that no atom record
        references. Returns a report whose ``problems`` list is empty when the
        state is fully intact; ``logs_checked`` counts the individual logs that
        actually passed verification. A missing or unreadable den raises
        StoreError, because nothing can be verified without it.
        """
        den = self.load_den()
        problems: list[tuple[str, str]] = []
        pathways = self._scan_records(
            self.pathways_dir, "pathway", model.pathway_from_payload, problems
        )
        atoms = self._scan_records(self.atoms_dir, "atom", model.atom_from_payload, problems)
        chokepoints = self._scan_records(
            self.chokepoints_dir, "chokepoint", model.chokepoint_from_payload, problems
        )
        for pathway in pathways:
            try:
                self.load_pathway(pathway.id)
            except StoreError as exc:
                problems.append((f"{pathway.id}.json", str(exc)))
        logs_checked = 0
        referenced_logs: set[Path] = set()
        for atom in atoms:
            try:
                self.load_atom(atom.id)
            except StoreError as exc:
                problems.append((f"{atom.id}.json", str(exc)))
            for relative, size_field, digest_field in (
                (atom.stdout_log, atom.stdout_bytes, atom.stdout_sha256),
                (atom.stderr_log, atom.stderr_bytes, atom.stderr_sha256),
            ):
                try:
                    referenced_logs.add(self._evidence_path(atom.id, relative))
                    self._verify_log(atom.id, relative, size_field, digest_field)
                    logs_checked += 1
                except StoreError as exc:
                    problems.append((f"{atom.id}.json", str(exc)))
        self._scan_orphaned_logs(referenced_logs, problems)
        for chokepoint in chokepoints:
            try:
                self.load_chokepoint(chokepoint.id)
            except StoreError as exc:
                problems.append((f"{chokepoint.id}.json", str(exc)))
        if all(item.id != den.default_pathway_id for item in pathways):
            problems.append(("den.json", "default pathway record is missing"))
        return StateVerification(
            pathways=len(pathways),
            atoms=len(atoms),
            chokepoints=len(chokepoints),
            logs_checked=logs_checked,
            problems=tuple(problems),
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
        for directory in (
            self.state_dir,
            self.pathways_dir,
            self.atoms_dir,
            self.chokepoints_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
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
            raise StoreError(
                f"unsupported schema version {schema_version} in {expected_kind} record {path.name}"
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

    def _load_listed(self, path: Path, kind: str, converter: Callable[[dict], _T]) -> _T:
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
        return record

    def _scan_records(
        self,
        directory: Path,
        kind: str,
        converter: Callable[[dict], _T],
        problems: list[tuple[str, str]],
    ) -> list[_T]:
        """Collect valid records and report invalid ones instead of raising."""
        records: list[_T] = []
        if not directory.is_dir():
            return records
        for path in sorted(directory.glob("*.json")):
            try:
                records.append(self._load_listed(path, kind, converter))
            except StoreError as exc:
                problems.append((path.name, str(exc)))
        return records

    def _scan_orphaned_logs(
        self, referenced_logs: set[Path], problems: list[tuple[str, str]]
    ) -> None:
        """Report files under the logs directory that no atom record references."""
        if not self.logs_dir.is_dir():
            return
        for entry in sorted(self.logs_dir.iterdir()):
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


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


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
