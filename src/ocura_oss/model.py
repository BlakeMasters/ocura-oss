# SPDX-License-Identifier: Apache-2.0

"""Frozen data models, canonical JSON, validation, and summary projections."""

from __future__ import annotations

import datetime
import enum
import hashlib
import json
import math
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

SCHEMA_VERSION = 2
CHECKSUM_ALGORITHM = "sha256"
TERMINAL_KIND = "terminal"
MASKED_ARGUMENT = "<masked>"

_ID_PATTERN = re.compile(r"^(den|pathway|atom|chokepoint)-([0-9a-f]{32})$")
ID_PREFIX_MIN = 4
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PARAMETER_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_PLACEHOLDER_PATTERN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_.-]*)\}|[{}]")
_GIT_REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

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
    ABANDONED = "abandoned"


class OutputCapture(enum.StrEnum):
    """Whether a command attempt's stdout and stderr were retained as logs."""

    FULL = "full"
    NONE = "none"


class AttemptState(enum.StrEnum):
    """Whether the process recording an unfinished attempt is still alive."""

    RUNNING = "running"
    ABANDONED = "abandoned"


class RecoveryAction(enum.StrEnum):
    """What closing an unfinished attempt had to do."""

    ABANDONED = "abandoned"
    COMPLETED = "completed"
    CLEARED = "cleared"


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


def id_stem(value: object, prefix: str) -> str | None:
    """Return ``prefix-HEX`` when *value* abbreviates an id of that kind.

    An abbreviation is at least ``ID_PREFIX_MIN`` of the id's leading
    hexadecimal characters, with or without the kind word in front.
    """
    if not isinstance(value, str):
        return None
    pattern = rf"(?:{re.escape(prefix)}-)?([0-9a-f]{{{ID_PREFIX_MIN},32}})"
    match = re.fullmatch(pattern, value)
    return None if match is None else f"{prefix}-{match.group(1)}"


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


def _fields(payload: object, label: str, required: frozenset[str]) -> dict:
    """Return a payload object after checking it carries every required field.

    Fields this version does not know are ignored. Within one schema version a
    record only ever gains optional fields, so records from a newer writer
    stay readable.
    """
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} payload must be an object")
    missing = sorted(required - set(payload))
    _require(not missing, f"{label} payload is missing fields: {', '.join(missing)}")
    return payload


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
        choices = ", ".join(item.value for item in Outcome)
        raise ValidationError(f"{label} must be one of: {choices}") from exc


def _output_capture(value: object, label: str) -> OutputCapture:
    text = _string(value, label)
    try:
        return OutputCapture(text)
    except ValueError as exc:
        choices = ", ".join(item.value for item in OutputCapture)
        raise ValidationError(f"{label} must be one of: {choices}") from exc


def _command(payload: dict) -> tuple[tuple[str, ...], tuple[int, ...]]:
    command_value = payload["command"]
    _require(isinstance(command_value, list), "command must be an array")
    _require(len(command_value) > 0, "command must not be empty")
    command = tuple(
        _string(token, f"command[{index}]") for index, token in enumerate(command_value)
    )
    masked_value = payload["masked_arguments"]
    _require(isinstance(masked_value, list), "masked_arguments must be an array")
    masked = tuple(
        _integer(position, f"masked_arguments[{index}]", minimum=0)
        for index, position in enumerate(masked_value)
    )
    _require(list(masked) == sorted(set(masked)), "masked_arguments must be unique and ascending")
    for position in masked:
        _require(
            position < len(command) and command[position] == MASKED_ARGUMENT,
            f"masked_arguments names command[{position}], which is not a masked token",
        )
    return command, masked


def mask_command(
    command: Sequence[str], positions: Iterable[int]
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Return the command as recorded, with the tokens at *positions* masked."""
    tokens = list(command)
    masked = set()
    for position in positions:
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < len(tokens)
        ):
            raise ValidationError(
                f"masked argument {position!r} is not a command token position"
                f" (0 to {len(tokens) - 1})"
            )
        masked.add(position)
        tokens[position] = MASKED_ARGUMENT
    return tuple(tokens), tuple(sorted(masked))


def substitute_parameters(
    command: Sequence[str], parameters: Mapping[str, str]
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Replace ``{KEY}`` in command tokens with parameter values.

    ``{{`` and ``}}`` stand for literal braces. Returns the expanded command
    and the parameters it used, so the recorded labels match what was run.
    """
    used: dict[str, str] = {}

    def expand(match: re.Match[str]) -> str:
        text = match.group(0)
        if text in ("{{", "}}"):
            return text[0]
        key = match.group(1)
        if key is None:
            raise ValidationError(
                f"unbalanced brace in command token {match.string!r};"
                " write {{ or }} for a literal brace"
            )
        if key not in parameters:
            raise ValidationError(f"command placeholder {{{key}}} has no parameter value")
        used[key] = parameters[key]
        return parameters[key]

    return tuple(_PLACEHOLDER_PATTERN.sub(expand, token) for token in command), used


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
class FileDigest:
    """Size and SHA-256 digest of one file named for context capture."""

    bytes: int
    sha256: str


@dataclass(frozen=True)
class RunContext:
    """Facts about where a command attempt started, captured only on request.

    ``platform`` and the git fields are ``None`` when they were not requested
    or, for git, when the project root is not inside a readable work tree.
    """

    platform: Mapping[str, str] | None
    git_revision: str | None
    git_dirty: bool | None
    files: Mapping[str, FileDigest]


@dataclass(frozen=True)
class Atom:
    """One recorded command attempt and its referenced output logs."""

    id: str
    pathway_id: str
    started_at: str
    finished_at: str | None
    duration_seconds: float | None
    outcome: Outcome
    return_code: int | None
    launch_error_category: str | None
    declared_parameters: Mapping[str, str]
    command: tuple[str, ...]
    stdout_log: str | None
    stderr_log: str | None
    stdout_bytes: int | None
    stderr_bytes: int | None
    stdout_sha256: str | None
    stderr_sha256: str | None
    output_capture: OutputCapture = OutputCapture.FULL
    masked_arguments: tuple[int, ...] = ()
    context: RunContext | None = None


@dataclass(frozen=True)
class Attempt:
    """A command attempt recorded before launch and not yet finalized.

    Its ``id`` is the identifier of the atom that finalizing it writes.
    """

    id: str
    pathway_id: str
    chokepoint_id: str
    started_at: str
    declared_parameters: Mapping[str, str]
    command: tuple[str, ...]
    stdout_log: str | None
    stderr_log: str | None
    output_capture: OutputCapture = OutputCapture.FULL
    masked_arguments: tuple[int, ...] = ()
    context: RunContext | None = None


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


_LOG_FIELDS = (
    "stdout_log",
    "stderr_log",
    "stdout_bytes",
    "stderr_bytes",
    "stdout_sha256",
    "stderr_sha256",
)
_DEN_FIELDS = frozenset({"id", "name", "created_at", "default_pathway_id"})
_PATHWAY_FIELDS = frozenset(
    {
        "id",
        "den_id",
        "created_at",
        "parent_pathway_id",
        "source_chokepoint_id",
        "reason",
        "parameters",
    }
)
_ATOM_FIELDS = frozenset(
    {
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
        "masked_arguments",
        "output_capture",
        *_LOG_FIELDS,
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "id",
        "pathway_id",
        "chokepoint_id",
        "started_at",
        "declared_parameters",
        "command",
        "masked_arguments",
        "output_capture",
        "stdout_log",
        "stderr_log",
    }
)
_CHOKEPOINT_FIELDS = frozenset(
    {"id", "pathway_id", "atom_id", "created_at", "kind", "outcome", "branchable"}
)


def _context_entry(context: RunContext | None) -> dict:
    """Return the optional ``context`` payload field, absent when not captured."""
    if context is None:
        return {}
    git = (
        None
        if context.git_revision is None
        else {"revision": context.git_revision, "dirty": context.git_dirty}
    )
    return {
        "context": {
            "platform": None if context.platform is None else dict(context.platform),
            "git": git,
            "files": {
                path: {"bytes": item.bytes, "sha256": item.sha256}
                for path, item in sorted(context.files.items())
            },
        }
    }


def _context(value: object) -> RunContext | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("context must be an object")
    platform_value = value.get("platform")
    platform: dict[str, str] | None = None
    if platform_value is not None:
        _require(isinstance(platform_value, dict), "context.platform must be an object")
        platform = {
            _string(key, "context.platform key"): _string(
                item, f"context.platform[{key}]", allow_empty=True
            )
            for key, item in platform_value.items()
        }
    git_value = value.get("git")
    revision: str | None = None
    dirty: bool | None = None
    if git_value is not None:
        _require(isinstance(git_value, dict), "context.git must be an object")
        revision = _string(git_value.get("revision"), "context.git.revision")
        _require(
            _GIT_REVISION_PATTERN.fullmatch(revision) is not None,
            "context.git.revision must be a full hexadecimal object name",
        )
        dirty = _boolean(git_value.get("dirty"), "context.git.dirty")
    files_value = value.get("files", {})
    _require(isinstance(files_value, dict), "context.files must be an object")
    files: dict[str, FileDigest] = {}
    for path, item in files_value.items():
        label = f"context.files[{path}]"
        _require(isinstance(item, dict), f"{label} must be an object")
        files[_string(path, "context.files key")] = FileDigest(
            bytes=_integer(item.get("bytes"), f"{label}.bytes", minimum=0),
            sha256=_sha256(item.get("sha256"), f"{label}.sha256"),
        )
    return RunContext(platform=platform, git_revision=revision, git_dirty=dirty, files=files)


def den_to_payload(den: Den) -> dict:
    return {
        "id": den.id,
        "name": den.name,
        "created_at": den.created_at,
        "default_pathway_id": den.default_pathway_id,
    }


def den_from_payload(payload: object) -> Den:
    payload = _fields(payload, "den", _DEN_FIELDS)
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
    payload = _fields(payload, "pathway", _PATHWAY_FIELDS)
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
        "masked_arguments": list(atom.masked_arguments),
        "output_capture": atom.output_capture.value,
        "stdout_log": atom.stdout_log,
        "stderr_log": atom.stderr_log,
        "stdout_bytes": atom.stdout_bytes,
        "stderr_bytes": atom.stderr_bytes,
        "stdout_sha256": atom.stdout_sha256,
        "stderr_sha256": atom.stderr_sha256,
        **_context_entry(atom.context),
    }


def atom_from_payload(payload: object) -> Atom:
    payload = _fields(payload, "atom", _ATOM_FIELDS)
    outcome = _outcome(payload["outcome"], "outcome")
    return_code = _optional_integer(payload["return_code"], "return_code")
    category = _optional_string(payload["launch_error_category"], "launch_error_category")
    command, masked = _command(payload)
    capture = _output_capture(payload["output_capture"], "output_capture")
    captured = capture is OutputCapture.FULL
    _require(
        captured or all(payload[name] is None for name in _LOG_FIELDS),
        "atoms without captured output must not carry log fields",
    )
    finished_at: str | None = None
    duration: float | None = None
    if outcome is not Outcome.ABANDONED:
        finished_at = format_timestamp(parse_timestamp(payload["finished_at"], "finished_at"))
        duration = _number(payload["duration_seconds"], "duration_seconds")
    if outcome is Outcome.ABANDONED:
        _require(
            payload["finished_at"] is None and payload["duration_seconds"] is None,
            "abandoned atoms must not carry a finish time or duration",
        )
        _require(
            return_code is None and category is None,
            "abandoned atoms must not carry a return code or launch error category",
        )
    elif outcome is Outcome.LAUNCH_FAILED:
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
        finished_at=finished_at,
        duration_seconds=duration,
        outcome=outcome,
        return_code=return_code,
        launch_error_category=category,
        declared_parameters=_string_mapping(payload["declared_parameters"], "declared_parameters"),
        command=command,
        stdout_log=_string(payload["stdout_log"], "stdout_log") if captured else None,
        stderr_log=_string(payload["stderr_log"], "stderr_log") if captured else None,
        stdout_bytes=(
            _integer(payload["stdout_bytes"], "stdout_bytes", minimum=0) if captured else None
        ),
        stderr_bytes=(
            _integer(payload["stderr_bytes"], "stderr_bytes", minimum=0) if captured else None
        ),
        stdout_sha256=_sha256(payload["stdout_sha256"], "stdout_sha256") if captured else None,
        stderr_sha256=_sha256(payload["stderr_sha256"], "stderr_sha256") if captured else None,
        output_capture=capture,
        masked_arguments=masked,
        context=_context(payload.get("context")),
    )


def attempt_to_payload(attempt: Attempt) -> dict:
    return {
        "id": attempt.id,
        "pathway_id": attempt.pathway_id,
        "chokepoint_id": attempt.chokepoint_id,
        "started_at": attempt.started_at,
        "declared_parameters": dict(attempt.declared_parameters),
        "command": list(attempt.command),
        "masked_arguments": list(attempt.masked_arguments),
        "output_capture": attempt.output_capture.value,
        "stdout_log": attempt.stdout_log,
        "stderr_log": attempt.stderr_log,
        **_context_entry(attempt.context),
    }


def attempt_from_payload(payload: object) -> Attempt:
    payload = _fields(payload, "attempt", _ATTEMPT_FIELDS)
    command, masked = _command(payload)
    capture = _output_capture(payload["output_capture"], "output_capture")
    captured = capture is OutputCapture.FULL
    _require(
        captured or (payload["stdout_log"] is None and payload["stderr_log"] is None),
        "attempts without captured output must not carry log paths",
    )
    return Attempt(
        id=_id(payload["id"], "atom", "id"),
        pathway_id=_id(payload["pathway_id"], "pathway", "pathway_id"),
        chokepoint_id=_id(payload["chokepoint_id"], "chokepoint", "chokepoint_id"),
        started_at=format_timestamp(parse_timestamp(payload["started_at"], "started_at")),
        declared_parameters=_string_mapping(payload["declared_parameters"], "declared_parameters"),
        command=command,
        stdout_log=_string(payload["stdout_log"], "stdout_log") if captured else None,
        stderr_log=_string(payload["stderr_log"], "stderr_log") if captured else None,
        output_capture=capture,
        masked_arguments=masked,
        context=_context(payload.get("context")),
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
    payload = _fields(payload, "chokepoint", _CHOKEPOINT_FIELDS)
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
    duration_seconds: float | None
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
