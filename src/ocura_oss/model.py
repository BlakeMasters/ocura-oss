# SPDX-License-Identifier: MPL-2.0

"""Frozen data models, canonical JSON, validation, and summary projections."""

from __future__ import annotations

import datetime
import enum
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

SCHEMA_VERSION = 1
CHECKSUM_ALGORITHM = "sha256"
TERMINAL_KIND = "terminal"

_ID_PATTERN = re.compile(r"^(den|pathway|atom|chokepoint)-([0-9a-f]{32})$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PARAMETER_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

_ID_KINDS = ("den", "pathway", "atom", "chokepoint")


class ModelError(Exception):
    """Invalid value, payload, or parameter declaration."""


class ValidationError(ModelError):
    """A value failed schema or contract validation."""


class Outcome(enum.StrEnum):
    """Terminal outcome recorded for one command attempt."""

    PASSED = "passed"
    FAILED = "failed"
    LAUNCH_FAILED = "launch_failed"
    INTERRUPTED = "interrupted"


class ComparisonState(enum.StrEnum):
    """Evidence completeness reported by a branch comparison."""

    NO_BRANCH = "no_branch"
    PARTIAL = "partial"
    READY = "ready"


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def format_timestamp(value: datetime.datetime) -> str:
    if not isinstance(value, datetime.datetime) or value.tzinfo is None:
        raise ValidationError("timestamps must be timezone-aware")
    return value.astimezone(datetime.UTC).isoformat(timespec="microseconds")


def parse_timestamp(value: object, label: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{label} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} must include a UTC offset")
    return parsed


def make_id(kind: str) -> str:
    if kind not in _ID_KINDS:
        raise ValidationError(f"unknown id kind: {kind}")
    return f"{kind}-{uuid.uuid4().hex}"


def is_valid_id(value: object, prefix: str) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(rf"{re.escape(prefix)}-[0-9a-f]{{32}}", value) is not None


def canonical_json(schema_version: int, kind: str, payload: Mapping[str, object]) -> bytes:
    material = {"schema_version": schema_version, "kind": kind, "payload": payload}
    return json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def checksum_for(schema_version: int, kind: str, payload: Mapping[str, object]) -> dict:
    digest = hashlib.sha256(canonical_json(schema_version, kind, payload)).hexdigest()
    return {"algorithm": CHECKSUM_ALGORITHM, "value": digest}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    if not allow_empty and value == "":
        raise ValidationError(f"{label} must not be empty")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{label} must be at least {minimum}")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(f"{label} must be a finite number")
    if number < 0:
        raise ValidationError(f"{label} must not be negative")
    return number


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be a boolean")
    return value


def _string_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = _string(key, f"{label} key")
        if _PARAMETER_KEY_PATTERN.fullmatch(key_text) is None:
            raise ValidationError(f"{label} key {key_text!r} is not a valid parameter name")
        result[key_text] = _string(item, f"{label}[{key_text}]")
    return result


def _sha256(value: object, label: str) -> str:
    text = _string(value, label)
    _require(_SHA256_PATTERN.fullmatch(text) is not None, f"{label} must be a sha256 hex digest")
    return text


def _id(value: object, prefix: str, label: str) -> str:
    text = _string(value, label)
    _require(is_valid_id(text, prefix), f"{label} must be a {prefix} id")
    return text


def _outcome(value: object, label: str) -> Outcome:
    text = _string(value, label)
    try:
        return Outcome(text)
    except ValueError as exc:
        raise ValidationError(
            f"{label} must be one of: passed, failed, launch_failed, interrupted"
        ) from exc


def parse_parameter(token: str) -> tuple[str, str]:
    if not isinstance(token, str):
        raise ValidationError("parameter declarations must be strings")
    key, separator, value = token.partition("=")
    if not separator:
        raise ValidationError(f"malformed parameter {token!r}: expected KEY=VALUE")
    if not key or _PARAMETER_KEY_PATTERN.fullmatch(key) is None:
        raise ValidationError(f"malformed parameter {token!r}: invalid KEY")
    return key, value


def validate_parameters(mapping: Mapping[str, str]) -> dict:
    return _string_mapping(mapping, "parameters")


def parameter_delta(
    source_parameters: Mapping[str, str], child_parameters: Mapping[str, str]
) -> ParameterDelta:
    inherited = {}
    added = {}
    changed = {}
    for key in sorted(set(source_parameters) | set(child_parameters)):
        in_source = key in source_parameters
        in_child = key in child_parameters
        if in_source and in_child:
            if source_parameters[key] == child_parameters[key]:
                inherited[key] = child_parameters[key]
            else:
                changed[key] = {
                    "source": source_parameters[key],
                    "child": child_parameters[key],
                }
        elif in_child:
            added[key] = child_parameters[key]
    return ParameterDelta(inherited=inherited, added=added, changed=changed)


@dataclass(frozen=True)
class Den:
    """Project-local state identity and default pathway."""

    id: str
    name: str
    created_at: str
    default_pathway_id: str


@dataclass(frozen=True)
class Pathway:
    """One lineage of declared parameters and recorded evidence."""

    id: str
    den_id: str
    created_at: str
    parent_pathway_id: str | None
    source_chokepoint_id: str | None
    reason: str
    parameters: Mapping[str, str]


@dataclass(frozen=True)
class Atom:
    """One recorded command attempt and its referenced output logs."""

    id: str
    pathway_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    outcome: Outcome
    return_code: int | None
    launch_error_category: str | None
    declared_parameters: Mapping[str, str]
    command: tuple[str, ...]
    stdout_log: str
    stderr_log: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True)
class Chokepoint:
    """Terminal evidence boundary that can serve as a branch source."""

    id: str
    pathway_id: str
    atom_id: str
    created_at: str
    kind: str
    outcome: Outcome
    branchable: bool


def den_to_payload(den: Den) -> dict:
    return {
        "id": den.id,
        "name": den.name,
        "created_at": den.created_at,
        "default_pathway_id": den.default_pathway_id,
    }


def den_from_payload(payload: object) -> Den:
    if not isinstance(payload, dict):
        raise ValidationError("den payload must be an object")
    _require(
        set(payload) == {"id", "name", "created_at", "default_pathway_id"},
        "den payload has unexpected fields",
    )
    return Den(
        id=_id(payload["id"], "den", "id"),
        name=_string(payload["name"], "name"),
        created_at=format_timestamp(parse_timestamp(payload["created_at"], "created_at")),
        default_pathway_id=_id(payload["default_pathway_id"], "pathway", "default_pathway_id"),
    )


def pathway_to_payload(pathway: Pathway) -> dict:
    return {
        "id": pathway.id,
        "den_id": pathway.den_id,
        "created_at": pathway.created_at,
        "parent_pathway_id": pathway.parent_pathway_id,
        "source_chokepoint_id": pathway.source_chokepoint_id,
        "reason": pathway.reason,
        "parameters": dict(pathway.parameters),
    }


def pathway_from_payload(payload: object) -> Pathway:
    if not isinstance(payload, dict):
        raise ValidationError("pathway payload must be an object")
    expected = {
        "id",
        "den_id",
        "created_at",
        "parent_pathway_id",
        "source_chokepoint_id",
        "reason",
        "parameters",
    }
    _require(set(payload) == expected, "pathway payload has unexpected fields")
    parent = payload["parent_pathway_id"]
    source = payload["source_chokepoint_id"]
    if parent is not None:
        _id(parent, "pathway", "parent_pathway_id")
    if source is not None:
        _id(source, "chokepoint", "source_chokepoint_id")
    return Pathway(
        id=_id(payload["id"], "pathway", "id"),
        den_id=_id(payload["den_id"], "den", "den_id"),
        created_at=format_timestamp(parse_timestamp(payload["created_at"], "created_at")),
        parent_pathway_id=parent,
        source_chokepoint_id=source,
        reason=_string(payload["reason"], "reason"),
        parameters=_string_mapping(payload["parameters"], "parameters"),
    )


def atom_to_payload(atom: Atom) -> dict:
    return {
        "id": atom.id,
        "pathway_id": atom.pathway_id,
        "started_at": atom.started_at,
        "finished_at": atom.finished_at,
        "duration_seconds": atom.duration_seconds,
        "outcome": atom.outcome.value,
        "return_code": atom.return_code,
        "launch_error_category": atom.launch_error_category,
        "declared_parameters": dict(atom.declared_parameters),
        "command": list(atom.command),
        "stdout_log": atom.stdout_log,
        "stderr_log": atom.stderr_log,
        "stdout_bytes": atom.stdout_bytes,
        "stderr_bytes": atom.stderr_bytes,
        "stdout_sha256": atom.stdout_sha256,
        "stderr_sha256": atom.stderr_sha256,
    }


def atom_from_payload(payload: object) -> Atom:
    if not isinstance(payload, dict):
        raise ValidationError("atom payload must be an object")
    expected = {
        "id",
        "pathway_id",
        "started_at",
        "finished_at",
        "duration_seconds",
        "outcome",
        "return_code",
        "launch_error_category",
        "declared_parameters",
        "command",
        "stdout_log",
        "stderr_log",
        "stdout_bytes",
        "stderr_bytes",
        "stdout_sha256",
        "stderr_sha256",
    }
    _require(set(payload) == expected, "atom payload has unexpected fields")
    outcome = _outcome(payload["outcome"], "outcome")
    return_code = _optional_integer(payload["return_code"], "return_code")
    category = _optional_string(payload["launch_error_category"], "launch_error_category")
    command_value = payload["command"]
    _require(isinstance(command_value, list), "command must be an array")
    _require(len(command_value) > 0, "command must not be empty")
    command = tuple(
        _string(token, f"command[{index}]") for index, token in enumerate(command_value)
    )
    if outcome is Outcome.LAUNCH_FAILED:
        _require(
            return_code is None and category is not None,
            "launch_failed atoms need a launch error category and no return code",
        )
    elif outcome is Outcome.INTERRUPTED:
        _require(
            category == "interrupted",
            "interrupted atoms need the 'interrupted' category",
        )
        _require(
            return_code is None,
            "interrupted atoms must not carry a return code",
        )
    else:
        _require(
            return_code is not None and category is None,
            "launched atoms need a return code and no launch error category",
        )
        if outcome is Outcome.PASSED:
            _require(return_code == 0, "passed atoms require return code 0")
        else:
            _require(return_code != 0, "failed atoms require a nonzero return code")
    return Atom(
        id=_id(payload["id"], "atom", "id"),
        pathway_id=_id(payload["pathway_id"], "pathway", "pathway_id"),
        started_at=format_timestamp(parse_timestamp(payload["started_at"], "started_at")),
        finished_at=format_timestamp(parse_timestamp(payload["finished_at"], "finished_at")),
        duration_seconds=_number(payload["duration_seconds"], "duration_seconds"),
        outcome=outcome,
        return_code=return_code,
        launch_error_category=category,
        declared_parameters=_string_mapping(payload["declared_parameters"], "declared_parameters"),
        command=command,
        stdout_log=_string(payload["stdout_log"], "stdout_log"),
        stderr_log=_string(payload["stderr_log"], "stderr_log"),
        stdout_bytes=_integer(payload["stdout_bytes"], "stdout_bytes", minimum=0),
        stderr_bytes=_integer(payload["stderr_bytes"], "stderr_bytes", minimum=0),
        stdout_sha256=_sha256(payload["stdout_sha256"], "stdout_sha256"),
        stderr_sha256=_sha256(payload["stderr_sha256"], "stderr_sha256"),
    )


def chokepoint_to_payload(chokepoint: Chokepoint) -> dict:
    return {
        "id": chokepoint.id,
        "pathway_id": chokepoint.pathway_id,
        "atom_id": chokepoint.atom_id,
        "created_at": chokepoint.created_at,
        "kind": chokepoint.kind,
        "outcome": chokepoint.outcome.value,
        "branchable": chokepoint.branchable,
    }


def chokepoint_from_payload(payload: object) -> Chokepoint:
    if not isinstance(payload, dict):
        raise ValidationError("chokepoint payload must be an object")
    expected = {
        "id",
        "pathway_id",
        "atom_id",
        "created_at",
        "kind",
        "outcome",
        "branchable",
    }
    _require(set(payload) == expected, "chokepoint payload has unexpected fields")
    kind = _string(payload["kind"], "kind")
    _require(kind == TERMINAL_KIND, f"kind must be {TERMINAL_KIND!r}")
    return Chokepoint(
        id=_id(payload["id"], "chokepoint", "id"),
        pathway_id=_id(payload["pathway_id"], "pathway", "pathway_id"),
        atom_id=_id(payload["atom_id"], "atom", "atom_id"),
        created_at=format_timestamp(parse_timestamp(payload["created_at"], "created_at")),
        kind=kind,
        outcome=_outcome(payload["outcome"], "outcome"),
        branchable=_boolean(payload["branchable"], "branchable"),
    )


@dataclass(frozen=True)
class RunSummary:
    """Command outcome fields used in comparisons and public summaries."""

    id: str
    pathway_id: str
    outcome: Outcome
    started_at: str
    duration_seconds: float
    return_code: int | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pathway_id": self.pathway_id,
            "outcome": self.outcome.value,
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "return_code": self.return_code,
        }


def run_summary_from_atom(atom: Atom) -> RunSummary:
    return RunSummary(
        id=atom.id,
        pathway_id=atom.pathway_id,
        outcome=atom.outcome,
        started_at=atom.started_at,
        duration_seconds=atom.duration_seconds,
        return_code=atom.return_code,
    )


@dataclass(frozen=True)
class PathwaySummary:
    id: str
    parent_pathway_id: str | None
    source_chokepoint_id: str | None
    created_at: str
    reason: str
    parameters: Mapping[str, str]
    has_terminal_evidence: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "parent_pathway_id": self.parent_pathway_id,
            "source_chokepoint_id": self.source_chokepoint_id,
            "created_at": self.created_at,
            "reason": self.reason,
            "parameters": dict(sorted(self.parameters.items())),
            "has_terminal_evidence": self.has_terminal_evidence,
        }


@dataclass(frozen=True)
class ChokepointSummary:
    id: str
    pathway_id: str
    atom_id: str
    outcome: Outcome
    created_at: str
    branchable: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pathway_id": self.pathway_id,
            "atom_id": self.atom_id,
            "outcome": self.outcome.value,
            "created_at": self.created_at,
            "branchable": self.branchable,
        }


@dataclass(frozen=True)
class ParameterDelta:
    """Inherited, added, and changed declared parameters."""

    inherited: Mapping[str, str]
    added: Mapping[str, str]
    changed: Mapping[str, Mapping[str, str]]

    def to_dict(self) -> dict:
        return {
            "inherited": dict(sorted(self.inherited.items())),
            "added": dict(sorted(self.added.items())),
            "changed": {
                key: dict(sorted(value.items())) for key, value in sorted(self.changed.items())
            },
        }

    def phrase(self) -> str:
        parts = []
        for key, value in sorted(self.added.items()):
            parts.append(f"added {key}={value}")
        for key, delta in sorted(self.changed.items()):
            parts.append(f"changed {key} ({delta['source']} -> {delta['child']})")
        for key, value in sorted(self.inherited.items()):
            parts.append(f"inherited {key}={value}")
        return "; ".join(parts) if parts else "(none)"


@dataclass(frozen=True)
class ChildComparison:
    """Comparison of one child pathway with its source run."""

    pathway_id: str
    reason: str
    source_chokepoint_id: str
    parameters: ParameterDelta
    source_run: RunSummary
    child_run: RunSummary | None
    run_parameters: ParameterDelta | None
    missing_evidence: bool

    def to_dict(self) -> dict:
        return {
            "pathway_id": self.pathway_id,
            "reason": self.reason,
            "source_chokepoint_id": self.source_chokepoint_id,
            "parameters": self.parameters.to_dict(),
            "source_run": self.source_run.to_dict(),
            "child_run": self.child_run.to_dict() if self.child_run else None,
            "run_parameters": (self.run_parameters.to_dict() if self.run_parameters else None),
            "missing_evidence": self.missing_evidence,
        }


@dataclass(frozen=True)
class ComparisonResult:
    """Comparison state and every child associated with a source chokepoint."""

    state: ComparisonState
    source_chokepoint_id: str
    source_pathway_id: str
    source_run: RunSummary
    children: tuple[ChildComparison, ...]

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "source_chokepoint_id": self.source_chokepoint_id,
            "source_pathway_id": self.source_pathway_id,
            "source_run": self.source_run.to_dict(),
            "children": [child.to_dict() for child in self.children],
        }
