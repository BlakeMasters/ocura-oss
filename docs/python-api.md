# Python API reference

Version 0.6.0.

The `ocura_oss` package exposes typed workflow functions, read-oriented state access, frozen result and record types, enums, and public exceptions.

Names listed in `ocura_oss.__all__` form the documented top-level API. Internal record writers and implementation helpers remain package internals.

## API status

The Python API is provisional during the 0.x series. Minor releases may refine signatures, result fields, and record semantics before 1.0.

Ocura OSS supports CPython 3.11 through 3.14 and includes a `py.typed` marker. The runtime uses only the Python standard library.

Optional root parameters accept a path-like object, a string, or `None`. `None` resolves to the current working directory. Relative paths and `~` are resolved without changing the process working directory.

File operations can propagate `OSError` when a rejected read, write, replacement, or directory operation remains an operating-system error. `run_demo()` wraps stage failures in `DemoError`.

A crafted pathway lineage that exceeds Python's recursion limit can propagate `RecursionError` from programmatic loading, verification, branching, or comparison. The CLI converts that condition to exit status 2.

Declared parameter mappings require nonempty string values. Keys must begin with a letter or underscore and may then contain letters, digits, underscores, periods, or hyphens. Values are stored without type conversion.

Identifier parameters take full identifiers. The shortened identifiers that the CLI accepts are expanded by [`Store.resolve_id`](#storeresolve_id).

Use these operations from a script or an AI agent inside its existing execution environment. The [automation guide](automation.md) describes caller responsibilities; the [training example](../examples/autoregressive/README.md) supplies PyTorch/JAX workloads with an optional local Ray executor.

## Workflow example

```python
import sys
from pathlib import Path

from ocura_oss import ComparisonState, branch, compare, initialize, run, verify

root = Path("experiment")
initial = initialize(root, name="batch study")

baseline = run(
    [sys.executable, "-c", "print('baseline')"],
    root=root,
    pathway_id=initial.pathway.id,
    parameters={"batch": "1"},
)

child = branch(
    baseline.chokepoint.id,
    root=root,
    reason="increase batch",
    parameters={"batch": "4"},
)

run(
    [sys.executable, "-c", "print('child')"],
    root=root,
    pathway_id=child.id,
)

comparison = compare(baseline.chokepoint.id, root=root)

assert comparison.state is ComparisonState.READY
assert verify(root).ok
```

## Workflow functions

### `ocura_oss.initialize`

```python
initialize(
    root: os.PathLike[str] | str | None = None,
    *,
    name: str = "ocura-oss",
) -> Initialization
```

Create one den and one default pathway under a project root.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `root` | path-like, string, or None | `None` | Project root that will contain `.ocura-oss/`; `None` selects the current working directory |
| `name` | string | `ocura-oss` | Nonblank name stored in the den record |

#### Returns

`Initialization` containing the resolved root, created den, and default pathway.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | `.ocura-oss/` already exists, `name` is blank or invalid, or state cannot be written |

#### Notes

Initialization fails if `.ocura-oss/` already exists, including when the directory contains incomplete state. All created state remains under the selected project root.

#### Example

```python
from ocura_oss import initialize

initial = initialize("experiment", name="optimizer study")
print(initial.pathway.id)
```

### `ocura_oss.run`

```python
run(
    command: Sequence[str],
    *,
    root: os.PathLike[str] | str | None = None,
    pathway_id: str | None = None,
    parameters: Mapping[str, str] | None = None,
    mirror: bool = False,
    capture: bool = True,
    masked_arguments: Iterable[int] = (),
    substitute: bool = False,
    context: bool = False,
    context_files: Iterable[os.PathLike[str] | str] = (),
) -> RunExecution
```

Run one trusted local command and record terminal evidence.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `command` | sequence of strings | required | Executable followed by its argument tokens; a plain string or bytes object is rejected |
| `root` | path-like, string, or None | `None` | Project root containing initialized state |
| `pathway_id` | string or None | `None` | Pathway that receives the atom; `None` selects the den's default pathway |
| `parameters` | mapping of string to string, or None | `None` | Labels stored on the atom together with the pathway's effective parameters, replacing the pathway's value for the same key; they are not passed to the child process |
| `mirror` | boolean | `False` | Stream stdout and stderr to the current terminal while retaining the same bytes in logs |
| `capture` | boolean | `True` | Retain stdout and stderr as logs; `False` writes no output to disk and records that it was not captured |
| `masked_arguments` | iterable of integers | `()` | Zero-based positions in `command` whose tokens are stored as `<masked>`; the command still receives the real values |
| `substitute` | boolean | `False` | Replace `{KEY}` in command tokens with the value the run records for that key |
| `context` | boolean | `False` | Record the platform and the git revision and dirty state at launch |
| `context_files` | iterable of paths | `()` | Files whose size and SHA-256 are recorded at launch; relative paths are resolved against the project root |

#### Returns

`RunExecution` containing the recorded `Atom` and terminal `Chokepoint`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | Command tokens, masked positions, placeholders, parameter data, state, pathway, or evidence persistence is invalid, or a context file cannot be read |

Child command failures and launch failures are returned as atom outcomes rather than raised as exceptions.

#### Outcome mapping

| Outcome | `return_code` | `launch_error_category` | Meaning |
| --- | --- | --- | --- |
| `Outcome.PASSED` | `0` | `None` | Process launched and returned zero |
| `Outcome.FAILED` | nonzero integer | `None` | Process launched and returned nonzero |
| `Outcome.LAUNCH_FAILED` | `None` | category string | Process could not be launched |
| `Outcome.INTERRUPTED` | `None` | `interrupted` | First Ctrl+C interrupted the attempt and partial output was retained |

Launch categories include `executable_not_found`, `permission_denied`, `not_a_directory`, `invalid_argument`, and `os_error`.

`run()` never returns `Outcome.ABANDONED`. That outcome is written by [`recover()`](#ocura_ossrecover) for an attempt whose recording process stopped first.

#### Notes

The child runs with `shell=False` and the project root as its working directory. It inherits the invoking process environment, while the inherited environment remains outside recorded state.

Stdout and stderr are stored separately under `.ocura-oss/logs/`. With `capture=False`, no log files are written: the returned atom has `output_capture` set to `OutputCapture.NONE` and `None` in its six log fields, and `mirror=True` still streams the output.

Each position in `masked_arguments` must index `command`; position 0 is the executable. The attempt and atom records store `<masked>` at those positions and list them. Masking does not alter output, so a command that prints a masked value still writes it to a captured log.

The returned atom's `declared_parameters` are the pathway's effective parameters with `parameters` laid over them, so a run on a branch carries the branch's labels without repeating them. With `substitute=True`, placeholders take their values from those labels, and the atom's `command` is the substituted command. `{{` and `}}` are literal braces; a placeholder without a value, or an unbalanced brace, raises `StoreError` before anything runs. Braces are untouched when `substitute` is false.

`context` and `context_files` fill the returned atom's `context` with a `RunContext`; it is `None` when neither is used. Nothing is queried, executed, or read for context otherwise. See [optional launch context](state-and-verification.md#optional-launch-context) for what is recorded and what it does not establish.

The attempt is journaled under `.ocura-oss/attempts/` before the command launches. One atom and one branchable terminal chokepoint are written after the command ends, and the journal entry is then removed. A second Ctrl+C, or a killed process, leaves the entry in place; `recover()` closes it. `run()` installs no signal handlers, and what happens to the command when the calling process is killed depends on the operating system; see [when the recorder stops](state-and-verification.md#when-the-recorder-stops). Several processes may call `run()` against one root at the same time.

The host machine, network, and process tree form the execution context. The trust model is trusted, same-owner local work.

#### Example

```python
import sys

from ocura_oss import Outcome, run

execution = run(
    [sys.executable, "-c", "print('recorded')"],
    root="experiment",
    parameters={"batch": "1"},
    mirror=True,
)

assert execution.atom.outcome is Outcome.PASSED
```

### `ocura_oss.branch`

```python
branch(
    source_chokepoint_id: str,
    *,
    reason: str,
    root: os.PathLike[str] | str | None = None,
    parameters: Mapping[str, str] | None = None,
) -> Pathway
```

Create a metadata-only child pathway from verified terminal evidence.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `source_chokepoint_id` | string | required | Identifier of a terminal, branchable chokepoint |
| `reason` | string | required | Nonblank branch reason stored on the child pathway |
| `root` | path-like, string, or None | `None` | Project root containing the source evidence |
| `parameters` | mapping of string to string, or None | `None` | Overrides applied to the parent pathway's effective parameters |

#### Returns

The created child `Pathway`. It has no atom until a later `run()` explicitly names its ID.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | State or input is invalid; the source is missing, nonterminal, nonbranchable, or unverifiable; or the child cannot be written |

#### Notes

Source verification covers the chokepoint, atom, pathway, their relationships, and both referenced logs before the write occurs.

Leading and trailing whitespace is removed from `reason` before storage. The resulting pathway contains lineage metadata only: parent, source chokepoint, reason, creation time, and effective parameters.

#### Example

```python
from ocura_oss import branch

child = branch(
    "chokepoint-<id>",
    root="experiment",
    reason="increase batch",
    parameters={"batch": "4"},
)
```

### `ocura_oss.compare`

```python
compare(
    source_chokepoint_id: str | None = None,
    *,
    root: os.PathLike[str] | str | None = None,
) -> ComparisonResult
```

Compare a verified source run with the newest run on each child pathway created from its chokepoint.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `source_chokepoint_id` | string or None | `None` | Explicit source chokepoint; `None` selects the newest chokepoint referenced by a child pathway after full-state verification, or the newest terminal chokepoint when no branches exist |
| `root` | path-like, string, or None | `None` | Project root containing initialized state |

#### Returns

`ComparisonResult` with the source summary, ordered child comparisons, and a `ComparisonState`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | State, source evidence, selected child evidence, or automatic source selection is invalid |

#### Notes

Explicit selection verifies the pinned source and relevant child evidence. Automatic selection requires complete state verification and selects the newest chokepoint referenced by a child pathway. A state with no child pathways selects its newest terminal chokepoint and reports `NO_BRANCH`. Any verification problem causes automatic selection to fail.

Comparison is a read-only operation over recorded state. For a child with more than one atom, it selects the newest atom by start time and identifier.

`ComparisonState.READY` means every child has evidence. `PARTIAL` means at least one child lacks evidence. `NO_BRANCH` means the source has no child pathways.

Parameter deltas contain inherited, added, and changed values.

#### Example

```python
from ocura_oss import ComparisonState, compare

result = compare("chokepoint-<id>", root="experiment")
if result.state is ComparisonState.PARTIAL:
    missing = [child.pathway_id for child in result.children if child.missing_evidence]
```

### `ocura_oss.verify`

```python
verify(
    root: os.PathLike[str] | str | None = None,
    *,
    against: Iterable[str] | None = None,
) -> StateVerification
```

Recheck every state record and every log referenced by a recorded run.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `root` | path-like, string, or None | `None` | Project root containing initialized state |
| `against` | iterable of strings, or None | `None` | Manifest lines retained from an earlier verification; each must still be present unchanged. Blank lines and lines starting with `#` are skipped; a manifest left with no entries is an error |

#### Returns

`StateVerification`. `report.ok` is true when `report.problems` is empty. The report contains record counts, the number of individual logs that passed verification, and the identifiers of running and abandoned attempts.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | Initialized state is absent; the den record is missing, unreadable, malformed, or unverifiable; or `against` has a malformed entry or lists no records |

Problems in other records and logs are normally collected in the returned report instead of raised.

#### Notes

Verification checks record envelopes, checksums, identifiers, filenames, required fields, semantic relationships, lineage, that every atom has exactly one chokepoint, referenced log containment, byte counts, log digests, orphaned log files, unexpected directories under `logs/`, and unfinished attempts.

It may run while other processes record runs under the same root. An attempt that a live process is still recording appears in `running_attempts` and is not a problem. An abandoned attempt is a problem until `recover()` closes it.

When the state is intact, `report.manifest` holds one `KIND ID CHECKSUM` line per verified record and `report.digest` is the SHA-256 of those lines. Retain the manifest where this state's writers cannot change it and pass it back as `against` to detect a record that was rewritten together with its checksum. Records added since are allowed. See [retained manifests](state-and-verification.md#retained-manifests).

Verification establishes local consistency among records and logs. Authorship and execution-environment reproduction require separate evidence.

#### Example

```python
from pathlib import Path

from ocura_oss import verify

report = verify("experiment")
for record, problem in report.problems:
    print(record, problem)

retained = Path("trusted/experiment.manifest")
if report.ok:
    retained.write_text("".join(f"{line}\n" for line in report.manifest))

# Later, possibly after more runs:
assert verify("experiment", against=retained.read_text().splitlines()).ok
```

### `ocura_oss.recover`

```python
recover(
    root: os.PathLike[str] | str | None = None,
) -> tuple[RecoveredAttempt, ...]
```

Close every attempt whose recording process stopped before finalizing it.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `root` | path-like, string, or None | `None` | Project root containing initialized state |

#### Returns

A tuple with one `RecoveredAttempt` for each attempt that was closed. It is empty when nothing was abandoned.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | Initialized state is absent, or an attempt record or its evidence is malformed or unreadable |

#### Notes

What recovery writes depends on how far the recorder got:

| `RecoveredAttempt.action` | When | Result |
| --- | --- | --- |
| `RecoveryAction.ABANDONED` | No outcome had been recorded | An atom with `Outcome.ABANDONED` and its terminal chokepoint |
| `RecoveryAction.COMPLETED` | The atom existed without its chokepoint | The missing chokepoint, with the atom's recorded outcome |
| `RecoveryAction.CLEARED` | The atom and chokepoint both existed | The leftover attempt record is removed |

An abandoned atom has `None` for `finished_at`, `duration_seconds`, `return_code`, and `launch_error_category`. Output captured before the recorder stopped is retained and measured, so it verifies afterward.

Attempts that a live process is still recording are left alone, so recovery is safe while other runs are in flight. Call it only after the abandoned command itself has stopped: a command that outlived its recorder can keep writing to logs that recovery has measured.

#### Example

```python
from ocura_oss import recover, verify

for closed in recover("experiment"):
    print(closed.atom_id, closed.action.value)

assert verify("experiment").ok
```

### `ocura_oss.run_demo`

```python
run_demo(
    root: os.PathLike[str] | str,
) -> DemoReport
```

Run the complete retained demonstration in a new directory.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `root` | path-like or string | required | Destination directory; it must not exist |

#### Returns

`DemoReport` containing the retained root, completed stages, and a ready comparison.

#### Raises

| Exception | Condition |
| --- | --- |
| `DemoError` | The destination exists or any demonstration stage fails |

#### Notes

The directory is retained on success and failure. Both demonstration commands must pass and the final state must verify before a report is returned.

Stages are `initialize`, `baseline-run`, `chokepoint`, `branch`, `rerun`, `verify`, and `compare`.

#### Example

```python
from ocura_oss import run_demo

report = run_demo("ocura-oss-demo")
print(report.comparison.state.value)
```

## Store

### `ocura_oss.Store`

```python
Store(
    root: os.PathLike[str] | str | None = None,
)
```

Read and verify Ocura OSS state under one project root.

The documented constructor, attributes, loading methods, listing methods, initialization method, verification methods, and recovery method form the provisional low-level API. Direct record writers are internal.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `root` | path-like, string, or None | `None` | Project root; `None` selects the current working directory |

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `root` | `pathlib.Path` | Resolved project root |
| `state_dir` | `pathlib.Path` | Resolved `.ocura-oss/` directory |
| `pathways_dir` | `pathlib.Path` | Pathway record directory |
| `atoms_dir` | `pathlib.Path` | Atom record directory |
| `chokepoints_dir` | `pathlib.Path` | Chokepoint record directory |
| `attempts_dir` | `pathlib.Path` | Unfinished attempt records and their lock files |
| `logs_dir` | `pathlib.Path` | Recorded log directory |
| `den_path` | `pathlib.Path` | Den record path |

Constructing a `Store` resolves paths only. `exists()`, `require()`, and `initialize_state()` provide the state-presence operations.

#### Example

```python
from ocura_oss import Store

store = Store("experiment")
if store.exists():
    print(store.load_den().name)
```

### `Store.exists`

```python
store.exists() -> bool
```

Return whether the `.ocura-oss/` state directory exists.

#### Returns

`True` when the state directory exists, including when its contents are incomplete. Use `require()` or `verify_state()` for stronger checks.

### `Store.require`

```python
store.require() -> None
```

Require an initialized den under the project root.

#### Returns

`None`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | `den.json` is not a file |

This method checks `den.json` presence only. `load_den()` parses the den, and `verify_state()` performs complete verification.

When the root has no `.ocura-oss/` directory, the message names `ocura-oss init`. When a directory above the root holds state, the message names the nearest one instead.

### `Store.resolve_id`

```python
store.resolve_id(
    value: str,
    kind: Literal["pathway", "atom", "chokepoint"],
) -> str
```

Return the full identifier that a full or shortened identifier names.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `value` | string | A full identifier, or at least four of its leading hexadecimal characters with or without the kind word |
| `kind` | string | Record kind to search: `pathway`, `atom`, or `chokepoint` |

#### Returns

The full identifier. A full identifier is returned as given, without checking that its record exists.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | `value` is neither a full identifier nor a valid prefix, or the prefix matches no stored record or more than one |

A prefix is matched against record file names only; no record is read. The error for an ambiguous prefix lists up to five matching identifiers. The CLI calls this method for `branch --from`, `compare --from`, and `run --pathway`.

### `Store.load_den`

```python
store.load_den() -> Den
```

Load and validate the den record.

#### Returns

The parsed `Den`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | The record is missing, unreadable, malformed, checksum-mismatched, or fails payload validation |

### `Store.load_pathway`

```python
store.load_pathway(
    pathway_id: str,
) -> Pathway
```

Load a pathway and validate its den and lineage references.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `pathway_id` | string | Identifier of the pathway record to load |

#### Returns

The parsed `Pathway`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | The identifier or record is invalid; the den, parent, or source reference is invalid; or lineage is cyclic |
| `RecursionError` | A crafted lineage exceeds the Python recursion limit |

Child pathways must set parent and source fields together, and their source chokepoint must belong to the parent pathway.

### `Store.load_atom`

```python
store.load_atom(
    atom_id: str,
) -> Atom
```

Load an atom and validate its pathway reference.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `atom_id` | string | Identifier of the atom record to load |

#### Returns

The parsed `Atom`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | The identifier, record, payload, outcome invariants, or referenced pathway is invalid |

This method validates the atom and its log metadata fields. `verify_atom_evidence()` reads and verifies the referenced files.

### `Store.load_chokepoint`

```python
store.load_chokepoint(
    chokepoint_id: str,
) -> Chokepoint
```

Load a chokepoint and validate its atom, pathway, and outcome.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `chokepoint_id` | string | Identifier of the chokepoint record to load |

#### Returns

The parsed `Chokepoint`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | The identifier or record is invalid; the atom or pathway is invalid; or atom and chokepoint fields disagree |

`verify_atom_evidence()` provides the corresponding log-file verification.

### `Store.list_pathways`

```python
store.list_pathways() -> list[Pathway]
```

Return pathway records in deterministic creation order.

#### Returns

A list sorted by `created_at`, then `id`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | Any listed JSON record has an invalid envelope, checksum, payload, kind, or filename-to-ID relationship |

The method fails closed instead of skipping an invalid record. Use `load_pathway()` or `verify_state()` when reference validation is required.

### `Store.list_atoms`

```python
store.list_atoms() -> list[Atom]
```

Return atom records in deterministic start order.

#### Returns

A list sorted by `started_at`, then `id`.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | Any listed JSON record has an invalid envelope, checksum, payload, outcome invariants, kind, or filename-to-ID relationship |

`load_atom()` adds pathway validation. `verify_state()` adds complete relationship and log verification.

### `Store.list_chokepoints`

```python
store.list_chokepoints() -> list[Chokepoint]
```

Return chokepoint records in reverse deterministic creation order.

#### Returns

A list sorted by `created_at`, then `id`, both descending.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | Any listed JSON record has an invalid envelope, checksum, payload, kind, or filename-to-ID relationship |

`load_chokepoint()` adds atom relationship validation. `verify_state()` adds complete relationship and log verification.

### `Store.has_terminal_evidence`

```python
store.has_terminal_evidence(
    pathway_id: str,
) -> bool
```

Return whether any listed atom records the supplied pathway ID.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `pathway_id` | string | Pathway identifier to match against atom records |

#### Returns

`True` when at least one atom names the pathway, otherwise `False`.

#### Raises

`StoreError` if atom listing fails.

The result reflects atom membership only. Pathway loading and log verification are separate operations.

### `Store.resolve_log_path`

```python
store.resolve_log_path(
    atom: Atom,
    stream: Literal["stdout", "stderr"],
) -> pathlib.Path
```

Resolve one recorded log path after containment checks.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `atom` | `Atom` | Atom whose recorded log path is resolved |
| `stream` | `stdout` or `stderr` | Selects the corresponding log field |

#### Returns

The resolved `pathlib.Path` directly inside this store's `.ocura-oss/logs/` directory.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | `stream` is invalid; the atom was recorded without output capture; or the recorded path is empty, absolute, escaping, or outside the direct logs directory |

The returned path has passed containment checks only. Use `read_verified_log()` to consume bytes that have been checked against the stored atom; `verify_atom_evidence()` checks both logs without returning their contents.

### `Store.read_verified_log`

```python
store.read_verified_log(
    atom_id: str,
    *,
    stream: Literal["stdout", "stderr"] = "stdout",
) -> bytes
```

Read one complete recorded log and verify the exact bytes returned. Added in 0.4.0.

#### Parameters

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `atom_id` | string | required | Identifier of the stored atom whose output is read |
| `stream` | `stdout` or `stderr` | `stdout` | Selects the recorded output stream; keyword-only |

#### Returns

The selected log as `bytes`, unchanged, after loading and validating the stored atom and its lineage, checking path containment, and verifying the returned bytes' count and SHA-256 digest against the atom.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | The atom or lineage is invalid; the stream is invalid; the atom was recorded without output capture; or the selected log is missing, unreadable, outside containment, or inconsistent with its recorded byte count or digest |
| `RecursionError` | A crafted lineage exceeds the Python recursion limit |

#### Notes

The complete log is read into memory. Binary output is preserved, and output from failed commands can be read. Decoding and metric interpretation belong to the caller.

This is a targeted check of one stream. It does not verify the other stream or the whole ledger; use `verify_state()` for the complete state check. Reading does not lock the ledger or create an atomic snapshot of records and logs. The returned bytes remain the verified contents even if the log file later changes.

#### Example

```python
import json

from ocura_oss import Store

store = Store("experiment")
raw = store.read_verified_log("atom-<id>", stream="stdout")
metrics = json.loads(raw.decode("utf-8"))  # For a workload that emits UTF-8 JSON.

diagnostics = store.read_verified_log("atom-<id>", stream="stderr")
```

### `Store.verify_atom_evidence`

```python
store.verify_atom_evidence(
    atom: Atom,
) -> None
```

Verify the log files referenced by one atom.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `atom` | `Atom` | Atom containing stdout and stderr paths, byte counts, and SHA-256 digests |

#### Returns

`None` when both logs pass verification. An atom recorded without output capture has no logs and passes.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | A path escapes containment, a log is missing, or a byte count or SHA-256 digest differs |

The checksum supports local change detection. Authorship requires separate authenticated evidence.

### `Store.initialize_state`

```python
store.initialize_state(
    *,
    name: str,
) -> tuple[Den, Pathway]
```

Create one den and one default pathway.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `name` | string | Nonblank name stored in the den |

#### Returns

A `(den, pathway)` tuple.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | State already exists, the name is invalid, or state cannot be written |

Use top-level `initialize()` when the resolved root should be included in the result.

### `Store.verify_state`

```python
store.verify_state(
    *,
    against: Iterable[str] | None = None,
) -> StateVerification
```

Verify every state record and every log referenced by a valid atom.

#### Returns

`StateVerification`. Problems outside the den are collected by record name when possible. `logs_checked` counts individual log files that passed verification. `running_attempts` and `abandoned_attempts` name unfinished attempts. `manifest` and `digest` are filled when the state is intact. `against` behaves as described for top-level [`verify()`](#ocura_ossverify).

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | The den is missing, unreadable, malformed, checksum-mismatched, or invalid |

#### Notes

The scan reports invalid record files, broken relationships, invalid lineage, source-parent disagreement, atoms without exactly one chokepoint, missing or changed logs, orphaned files, unexpected directories under `logs/`, and abandoned attempts.

Other processes may record runs under the same root during the scan. A running attempt is not a problem, and its logs are not checked until it is finalized.

### `Store.list_attempts`

```python
store.list_attempts() -> list[tuple[Attempt, AttemptState]]
```

Return unfinished attempts with their state, ordered by start time and then identifier.

#### Returns

A list of `(attempt, state)` pairs. The state is `AttemptState.RUNNING` while a live process is recording the attempt and `AttemptState.ABANDONED` once that process has stopped without finalizing it. A finalized attempt is an atom and is not listed.

#### Raises

`StoreError` if an attempt record is malformed, checksum-mismatched, misnamed, or unreadable.

### `Store.recover`

```python
store.recover() -> tuple[RecoveredAttempt, ...]
```

Close every unfinished attempt whose recording process has stopped. Top-level [`recover()`](#ocura_ossrecover) calls this method and documents the actions, results, and limits.

#### Raises

| Exception | Condition |
| --- | --- |
| `StoreError` | Initialized state is absent, or an attempt record or its evidence is malformed or unreadable |

## Record types

Public records and results are frozen dataclasses. Their fields cannot be reassigned. Mapping fields should be treated as read-only values.

The dataclasses describe values returned by workflows and `Store`. Direct construction creates an in-memory value only; workflow functions and `Store` loading provide persistence and validation.

Identifiers use a kind prefix followed by 32 lowercase hexadecimal characters. Timestamps are normalized UTC ISO 8601 strings with offsets.

### `ocura_oss.Den`

```python
Den(
    id: str,
    name: str,
    created_at: str,
    default_pathway_id: str,
)
```

Project-local state identity and default pathway.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `id` | string | Den identifier |
| `name` | string | User-supplied state name |
| `created_at` | string | UTC ISO 8601 creation timestamp |
| `default_pathway_id` | string | Pathway selected when `run()` omits `pathway_id` |

### `ocura_oss.Pathway`

```python
Pathway(
    id: str,
    den_id: str,
    created_at: str,
    parent_pathway_id: str | None,
    source_chokepoint_id: str | None,
    reason: str,
    parameters: Mapping[str, str],
)
```

One lineage of declared parameters and recorded evidence.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `id` | string | Pathway identifier |
| `den_id` | string | Owning den identifier |
| `created_at` | string | UTC ISO 8601 creation timestamp |
| `parent_pathway_id` | string or None | Parent for a branch; `None` on the default pathway |
| `source_chokepoint_id` | string or None | Source boundary for a branch; `None` on the default pathway |
| `reason` | string | Branch reason or `default pathway` |
| `parameters` | mapping of string to string | Effective pathway parameters after inheritance and overrides |

Parent and source fields are either both set or both `None`.

### `ocura_oss.Atom`

```python
Atom(
    id: str,
    pathway_id: str,
    started_at: str,
    finished_at: str | None,
    duration_seconds: float | None,
    outcome: Outcome,
    return_code: int | None,
    launch_error_category: str | None,
    declared_parameters: Mapping[str, str],
    command: tuple[str, ...],
    stdout_log: str | None,
    stderr_log: str | None,
    stdout_bytes: int | None,
    stderr_bytes: int | None,
    stdout_sha256: str | None,
    stderr_sha256: str | None,
    output_capture: OutputCapture = OutputCapture.FULL,
    masked_arguments: tuple[int, ...] = (),
    context: RunContext | None = None,
)
```

One recorded command attempt and its referenced output logs.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `id` | string | Atom identifier |
| `pathway_id` | string | Pathway receiving this evidence |
| `started_at` | string | UTC ISO 8601 start timestamp |
| `finished_at` | string or None | UTC ISO 8601 finish timestamp; `None` for an abandoned atom |
| `duration_seconds` | float or None | Nonnegative finite elapsed duration, rounded to six decimal places for package-created atoms; `None` for an abandoned atom |
| `outcome` | `Outcome` | Terminal command outcome |
| `return_code` | integer or None | Process return code for passed and failed outcomes |
| `launch_error_category` | string or None | Normalized launch category, or `interrupted` |
| `declared_parameters` | mapping of string to string | Labels recorded for this run: its pathway's effective parameters with the run's own declarations laid over them |
| `command` | tuple of strings | Executable and argument tokens retained in the raw record, with `<masked>` at each masked position |
| `stdout_log` | string or None | Project-relative stdout log path |
| `stderr_log` | string or None | Project-relative stderr log path |
| `stdout_bytes` | integer or None | Recorded stdout byte count |
| `stderr_bytes` | integer or None | Recorded stderr byte count |
| `stdout_sha256` | string or None | Lowercase SHA-256 digest of stdout bytes |
| `stderr_sha256` | string or None | Lowercase SHA-256 digest of stderr bytes |
| `output_capture` | `OutputCapture` | Whether stdout and stderr were retained as logs |
| `masked_arguments` | tuple of integers | Ascending positions in `command` that hold the masked placeholder |
| `context` | `RunContext` or None | Launch context, when it was requested |

Outcome, return code, and launch category must satisfy the invariants described by `run()`. An abandoned atom carries neither a return code nor a launch category.

The six log fields are all set when `output_capture` is `OutputCapture.FULL` and all `None` when it is `OutputCapture.NONE`. For an abandoned atom, the byte counts and digests describe each log as recovery found it.

### `ocura_oss.Attempt`

```python
Attempt(
    id: str,
    pathway_id: str,
    chokepoint_id: str,
    started_at: str,
    declared_parameters: Mapping[str, str],
    command: tuple[str, ...],
    stdout_log: str | None,
    stderr_log: str | None,
    output_capture: OutputCapture = OutputCapture.FULL,
    masked_arguments: tuple[int, ...] = (),
    context: RunContext | None = None,
)
```

One run that has started and is not yet finalized, as returned by `Store.list_attempts()`.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `id` | string | Identifier of the atom this attempt becomes when finalized |
| `pathway_id` | string | Pathway the run was started on |
| `chokepoint_id` | string | Identifier reserved for the atom's terminal chokepoint |
| `started_at` | string | UTC ISO 8601 start timestamp |
| `declared_parameters` | mapping of string to string | Labels recorded for this run: its pathway's effective parameters with the run's own declarations laid over them |
| `command` | tuple of strings | Executable and argument tokens, with `<masked>` at each masked position |
| `stdout_log` | string or None | Project-relative stdout log path; `None` without capture |
| `stderr_log` | string or None | Project-relative stderr log path; `None` without capture |
| `output_capture` | `OutputCapture` | Whether stdout and stderr are being retained as logs |
| `masked_arguments` | tuple of integers | Ascending positions in `command` that hold the masked placeholder |
| `context` | `RunContext` or None | Launch context, when it was requested |

An attempt has no outcome, byte counts, or digests: its logs may still be growing.

### `ocura_oss.RunContext`

```python
RunContext(
    platform: Mapping[str, str] | None,
    git_revision: str | None,
    git_dirty: bool | None,
    files: Mapping[str, FileDigest],
)
```

Facts about where a command attempt started, recorded only on request.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `platform` | mapping of string to string, or None | `system`, `release`, and `machine` of the recording machine; `None` unless `context=True` |
| `git_revision` | string or None | Full object name of the checked-out commit; `None` unless `context=True` found a work tree with a commit |
| `git_dirty` | boolean or None | Whether the work tree had modified or untracked files other than `.ocura-oss/`; `None` whenever `git_revision` is |
| `files` | mapping of string to `FileDigest` | One entry per file named in `context_files`, keyed by the path as given with forward slashes |

Context describes the moment of launch. Verification does not recheck it.

### `ocura_oss.FileDigest`

```python
FileDigest(
    bytes: int,
    sha256: str,
)
```

Size and lowercase SHA-256 digest of one file named for context capture.

### `ocura_oss.RecoveredAttempt`

```python
RecoveredAttempt(
    atom_id: str,
    chokepoint_id: str,
    pathway_id: str,
    action: RecoveryAction,
)
```

One unfinished attempt closed by `recover()`.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `atom_id` | string | Atom the attempt was closed as |
| `chokepoint_id` | string | That atom's terminal chokepoint |
| `pathway_id` | string | Pathway containing the atom |
| `action` | `RecoveryAction` | What closing the attempt had to do |

### `ocura_oss.Chokepoint`

```python
Chokepoint(
    id: str,
    pathway_id: str,
    atom_id: str,
    created_at: str,
    kind: str,
    outcome: Outcome,
    branchable: bool,
)
```

Terminal evidence boundary that can serve as a branch source.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `id` | string | Chokepoint identifier |
| `pathway_id` | string | Pathway shared with the source atom |
| `atom_id` | string | Source atom identifier |
| `created_at` | string | UTC ISO 8601 creation timestamp |
| `kind` | string | `terminal` in schema version 1 |
| `outcome` | `Outcome` | Outcome that must agree with the source atom |
| `branchable` | boolean | Whether branching may use this boundary |

### `ocura_oss.Initialization`

```python
Initialization(
    root: pathlib.Path,
    den: Den,
    pathway: Pathway,
)
```

State created by `initialize()`.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `root` | `pathlib.Path` | Resolved project root |
| `den` | `Den` | Created den |
| `pathway` | `Pathway` | Created default pathway |

### `ocura_oss.RunExecution`

```python
RunExecution(
    atom: Atom,
    chokepoint: Chokepoint,
)
```

Records produced by one command attempt.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `atom` | `Atom` | Recorded attempt and log metadata |
| `chokepoint` | `Chokepoint` | Terminal boundary created for the atom |

### `ocura_oss.RunSummary`

```python
RunSummary(
    id: str,
    pathway_id: str,
    outcome: Outcome,
    started_at: str,
    duration_seconds: float | None,
    return_code: int | None,
)
```

Reduced command fields used in comparisons and public summaries.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `id` | string | Source atom identifier |
| `pathway_id` | string | Pathway containing the atom |
| `outcome` | `Outcome` | Terminal outcome |
| `started_at` | string | UTC ISO 8601 start timestamp |
| `duration_seconds` | float or None | Recorded elapsed duration; `None` for an abandoned run |
| `return_code` | integer or None | Process return code when applicable |

#### Methods

`to_dict() -> dict` returns these fields with `outcome` converted to its string value.

### `ocura_oss.ParameterDelta`

```python
ParameterDelta(
    inherited: Mapping[str, str],
    added: Mapping[str, str],
    changed: Mapping[str, Mapping[str, str]],
)
```

Declared parameter relationship between source and child values.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `inherited` | mapping of string to string | Keys present with the same value in source and child |
| `added` | mapping of string to string | Keys present only in the child |
| `changed` | nested string mapping | Changed keys, each with `source` and `child` values |

Removed source keys are outside the parameter-delta representation.

#### Methods

| Method | Returns | Description |
| --- | --- | --- |
| `to_dict()` | dictionary | Deterministically sorted `inherited`, `added`, and `changed` mappings |
| `phrase()` | string | Concise text summary, or `(none)` when the delta has no represented entries |

### `ocura_oss.ChildComparison`

```python
ChildComparison(
    pathway_id: str,
    reason: str,
    source_chokepoint_id: str,
    parameters: ParameterDelta,
    source_run: RunSummary,
    child_run: RunSummary | None,
    run_parameters: ParameterDelta | None,
    missing_evidence: bool,
)
```

Comparison of one child pathway with its source run.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `pathway_id` | string | Child pathway identifier |
| `reason` | string | Recorded branch reason |
| `source_chokepoint_id` | string | Shared source chokepoint |
| `parameters` | `ParameterDelta` | Parent-to-child pathway parameter delta |
| `source_run` | `RunSummary` | Source atom summary |
| `child_run` | `RunSummary` or None | Newest child atom summary, or `None` when evidence is missing |
| `run_parameters` | `ParameterDelta` or None | Source-atom-to-child-atom parameter delta when child evidence exists |
| `missing_evidence` | boolean | Whether the child has no atom |

#### Methods

`to_dict() -> dict` returns a nested JSON-compatible representation.

### `ocura_oss.ComparisonResult`

```python
ComparisonResult(
    state: ComparisonState,
    source_chokepoint_id: str,
    source_pathway_id: str,
    source_run: RunSummary,
    children: tuple[ChildComparison, ...],
)
```

Comparison state and every child associated with one source chokepoint.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `state` | `ComparisonState` | Evidence completeness across children |
| `source_chokepoint_id` | string | Selected source boundary |
| `source_pathway_id` | string | Pathway containing the source atom |
| `source_run` | `RunSummary` | Source atom summary |
| `children` | tuple of `ChildComparison` | Child comparisons in creation order |

#### Methods

`to_dict() -> dict` returns a nested JSON-compatible representation with enum values converted to strings.

### `ocura_oss.StateVerification`

```python
StateVerification(
    pathways: int,
    atoms: int,
    chokepoints: int,
    logs_checked: int,
    problems: tuple[tuple[str, str], ...],
    running_attempts: tuple[str, ...] = (),
    abandoned_attempts: tuple[str, ...] = (),
    manifest: tuple[str, ...] = (),
    digest: str | None = None,
)
```

Result of verifying records and referenced logs under one state directory.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `pathways` | integer | Valid pathway records collected by the scan |
| `atoms` | integer | Valid atom records collected by the scan |
| `chokepoints` | integer | Valid chokepoint records collected by the scan |
| `logs_checked` | integer | Individual referenced logs that passed verification |
| `problems` | tuple of pairs | `(record, problem)` entries collected during verification |
| `running_attempts` | tuple of strings | Attempts a live process is still recording; not problems |
| `abandoned_attempts` | tuple of strings | Attempts whose recorder stopped; each also appears in `problems` |
| `manifest` | tuple of strings | Sorted `KIND ID CHECKSUM` lines for every verified record; empty when there are problems |
| `digest` | string or None | SHA-256 of the manifest lines, each newline-terminated; `None` when there are problems |

#### Properties

`ok -> bool` is `True` when `problems` is empty.

### `ocura_oss.DemoStep`

```python
DemoStep(
    step: str,
    detail: str,
)
```

One completed stage in a demonstration report.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `step` | string | Stable stage label |
| `detail` | string | Recorded summary of that stage |

### `ocura_oss.DemoReport`

```python
DemoReport(
    root: pathlib.Path,
    steps: tuple[DemoStep, ...],
    comparison: ComparisonResult,
)
```

Retained demonstration location, completed stages, and comparison.

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `root` | `pathlib.Path` | Resolved retained destination |
| `steps` | tuple of `DemoStep` | Completed stages in execution order |
| `comparison` | `ComparisonResult` | Final ready comparison |

#### Methods

`to_dict() -> dict` returns a JSON-compatible representation. `root` is converted to a string.

## Enumerations

### `ocura_oss.Outcome`

String enum describing one terminal command attempt.

| Member | Value | Meaning |
| --- | --- | --- |
| `Outcome.PASSED` | `passed` | Process returned zero |
| `Outcome.FAILED` | `failed` | Process returned nonzero |
| `Outcome.LAUNCH_FAILED` | `launch_failed` | Process could not be launched |
| `Outcome.INTERRUPTED` | `interrupted` | Attempt was interrupted and partial output was recorded |
| `Outcome.ABANDONED` | `abandoned` | The recording process stopped before an outcome was recorded; the attempt was closed by `recover()` |

`Outcome` derives from `enum.StrEnum`, so members also behave as strings.

### `ocura_oss.OutputCapture`

String enum stating whether a run's stdout and stderr were retained as logs.

| Member | Value | Meaning |
| --- | --- | --- |
| `OutputCapture.FULL` | `full` | Both streams were written to logs |
| `OutputCapture.NONE` | `none` | No output was written to disk |

### `ocura_oss.AttemptState`

String enum describing an unfinished attempt.

| Member | Value | Meaning |
| --- | --- | --- |
| `AttemptState.RUNNING` | `running` | A live process is still recording the attempt |
| `AttemptState.ABANDONED` | `abandoned` | The recording process stopped without finalizing the attempt |

### `ocura_oss.RecoveryAction`

String enum describing what closing an unfinished attempt had to do.

| Member | Value | Meaning |
| --- | --- | --- |
| `RecoveryAction.ABANDONED` | `abandoned` | No outcome had been recorded; an abandoned atom and its chokepoint were written |
| `RecoveryAction.COMPLETED` | `completed` | The atom existed; its missing chokepoint was written |
| `RecoveryAction.CLEARED` | `cleared` | The atom and chokepoint existed; the leftover attempt record was removed |

### `ocura_oss.ComparisonState`

String enum describing evidence completeness for one comparison.

| Member | Value | Meaning |
| --- | --- | --- |
| `ComparisonState.NO_BRANCH` | `no_branch` | Source has no child pathways |
| `ComparisonState.PARTIAL` | `partial` | At least one child lacks evidence |
| `ComparisonState.READY` | `ready` | Every child has evidence |

`ComparisonState` derives from `enum.StrEnum`.

## Exceptions

### `ocura_oss.StoreError`

```python
StoreError(message)
```

Raised for invalid workflow input and missing, malformed, checksum-mismatched, semantically inconsistent, unverifiable, or unwritable state.

`run()` returns normal command failures and launch failures as `Outcome` values. It raises `StoreError` when the workflow or evidence persistence fails.

### `ocura_oss.DemoError`

```python
DemoError(
    step: str,
    message: str,
)
```

Raised when a retained demonstration cannot complete.

#### Parameters

| Name | Type | Description |
| --- | --- | --- |
| `step` | string | Stage that failed |
| `message` | string | Failure detail |

#### Attributes

| Name | Type | Description |
| --- | --- | --- |
| `step` | string | Stage that failed |
| `message` | string | Failure detail |

The destination is retained when it was created before the failure.

## Module metadata

### `ocura_oss.__version__`

```python
ocura_oss.__version__: str
```

Installed package version. Version 0.6.0 reports `"0.6.0"`.

## Typing

The wheel includes `py.typed`, so package annotations are visible to compatible static type checkers.

```python
from pathlib import Path

from ocura_oss import Initialization, Store, initialize

initial: Initialization = initialize(Path("experiment"))
store: Store = Store(initial.root)
```

Dataclass fields use concrete record and result types. Parameter inputs use `collections.abc.Sequence` and `Mapping` so callers can supply compatible typed containers.

## State and concurrency

The supported programmatic workflows write project-local `.ocura-oss/` state in schema version 2. State written by 0.4 and earlier, and legacy `.ocura/` records, are not compatible.

Several processes may record runs under one state root at the same time, and verification and comparison may run while they do. Public workflow functions perform record mutation; direct record writers remain internal. The liveness of an unfinished attempt is read from an operating-system file lock, which is reliable on local filesystems only.

Raw atoms retain command arguments and logs retain command output. Keep secrets out of command arguments, parameter mappings, branch reasons, and output. Where one cannot be avoided, `masked_arguments` keeps a command token out of the records and `capture=False` keeps output off disk.

For command syntax and exit status, see the [CLI reference](cli.md). For the record layout and execution boundary, see the [documentation overview](index.md).
