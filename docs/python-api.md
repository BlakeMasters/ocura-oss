# Python API

The public Python API supports the same local workflow as the command-line interface. It also provides typed access to stored records and verification reports.

Documented names are provisional during the 0.x series. The supported top-level surface is defined by `ocura_oss.__all__`; submodule helpers that are not re-exported may change without notice.

## Complete workflow

```python
import sys
from pathlib import Path

from ocura_oss import ComparisonState, branch, compare, initialize, run, verify

root = Path("experiment")
initial = initialize(root, name="batch study")

baseline = run(
    [sys.executable, "-c", "print('baseline')"],
    root=root,
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
    parameters={"batch": "4"},
)

comparison = compare(baseline.chokepoint.id, root=root)
assert comparison.state is ComparisonState.READY
assert verify(root).ok
```

Call this API from scripts or an AI agent's existing execution environment. Commands
inherit that environment's permissions; provide isolation there when needed.
Command arguments, parameter labels, branch reasons, and output are stored locally.
Keep secrets out of those fields. Parameters record labels; pass actual workload
settings in the command too. See the [autoregressive example](https://github.com/BlakeMasters/ocura-oss/blob/main/examples/autoregressive/README.md)
for PyTorch/JAX workloads and an optional Ray executor.

## Workflow functions

All optional roots accept `str`, `os.PathLike[str]`, or `None`. An omitted root resolves to the current working directory.

### `initialize`

```python
initialize(root=None, *, name="ocura-oss") -> Initialization
```

Creates `.ocura-oss/`, one den, and one default pathway. Existing state is never replaced. The returned `Initialization` contains the resolved root, den, and default pathway.

### `run`

```python
run(
    command,
    *,
    root=None,
    pathway_id=None,
    parameters=None,
    mirror=False,
) -> RunExecution
```

Runs one command with `shell=False` and the project root as its working directory. The invoking environment is inherited but not serialized. When `pathway_id` is omitted, the den's default pathway receives the atom.

A command that returns nonzero produces an atom with outcome `failed`; it does not raise an exception. A launch failure produces `launch_failed` evidence. Invalid input or state raises `StoreError`.

`mirror=True` streams stdout and stderr to the invoking terminal while retaining the same bytes in local logs.

### `branch`

```python
branch(
    source_chokepoint_id,
    *,
    reason,
    root=None,
    parameters=None,
) -> Pathway
```

Verifies the selected chokepoint, source atom, pathway, and referenced logs before creating a child pathway. The child records lineage, a reason, and effective parameters. No process, workspace, checkpoint, artifact, or runtime state is copied.

### `compare`

```python
compare(source_chokepoint_id=None, *, root=None) -> ComparisonResult
```

Compares a verified source atom with every child pathway created from its chokepoint. An explicit ID performs targeted source verification. When the ID is omitted, automatic selection verifies the complete state and chooses the newest chokepoint referenced by a child pathway. A state with no child pathways uses its newest terminal chokepoint and reports `no_branch`.

The result state is `no_branch`, `partial`, or `ready`. Comparison reads recorded evidence and never reruns a command.

### `verify`

```python
verify(root=None) -> StateVerification
```

Rechecks record envelopes, payload checksums, identifiers, semantic relationships, referenced log sizes, and SHA-256 log digests. The returned report includes counts and a tuple of `(record, problem)` entries. `report.ok` is true when no problems were found.

### `run_demo`

```python
run_demo(root) -> DemoReport
```

Performs the complete workflow in a new retained directory. The destination must not exist. Both commands must pass and the retained state must verify before the report is returned.

## Store

`Store` is the provisional low-level interface to one project-local state root.

```python
from ocura_oss import Store

store = Store("experiment")
report = store.verify_state()
```

Construction resolves `~`, relative paths, and the current directory without changing the process working directory.

### State access

- `exists()` reports whether the `.ocura-oss/` directory exists.
- `require()` raises `StoreError` when initialized state is unavailable.
- `load_den()` returns the den after checksum and payload validation.
- `load_pathway(id)` returns a pathway after lineage validation.
- `load_atom(id)` returns an atom after pathway-reference validation.
- `load_chokepoint(id)` returns a chokepoint after atom, pathway, and outcome validation.
- `list_pathways()`, `list_atoms()`, and `list_chokepoints()` return deterministic validated listings.
- `has_terminal_evidence(pathway_id)` reports whether a pathway has an atom.
- `resolve_log_path(atom, stream)` returns a containment-checked `stdout` or `stderr` path.
- `read_verified_log(atom_id, *, stream="stdout")` returns the selected log as `bytes`
  after loading and validating the stored atom and its lineage, checking path containment,
  and verifying the byte count and SHA-256 digest of the exact bytes returned.

Use `read_verified_log` when consuming recorded output:

```python
import json

raw = store.read_verified_log(atom_id, stream="stdout")
metrics = json.loads(raw.decode("utf-8"))  # Only for workloads that emit UTF-8 JSON.
```

The method reads the complete log into memory, preserves binary output unchanged,
and raises `StoreError` for missing, unreadable, or inconsistent evidence. Both
`stdout` and `stderr` are supported, including output from failed commands. It
does not parse metrics, verify the other stream, or verify the whole ledger; use
`verify_state()` for the complete state check. The returned bytes remain the
verified contents even if the file later changes. Reading does not lock the ledger
or establish an atomic snapshot of its records and logs.

### Initialization and verification

- `initialize_state(name=...)` creates a den and default pathway and refuses existing state.
- `verify_atom_evidence(atom)` verifies the two logs referenced by an atom.
- `verify_state()` returns a complete `StateVerification` report.

Record writing is internal to the workflow functions. `Store` does not expose a public transaction or direct record-mutation interface.

## Data models

Public records and results are frozen dataclasses.

| Type | Meaning |
| --- | --- |
| `Den` | State identity, name, creation time, and default pathway |
| `Pathway` | Lineage, branch reason, and effective declared parameters |
| `Atom` | Command, outcome, timing, return code, parameters, and referenced logs |
| `Chokepoint` | Terminal boundary for an atom and its branchability |
| `RunExecution` | Atom and chokepoint created by one command attempt |
| `Initialization` | Resolved root, den, and default pathway |
| `RunSummary` | Reduced atom fields used by comparisons |
| `ParameterDelta` | Inherited, added, and changed parameters |
| `ChildComparison` | One child pathway compared with its source run |
| `ComparisonResult` | Comparison state, source summary, and child comparisons |
| `StateVerification` | Verified counts and reported state problems |
| `DemoReport` | Retained demo root, completed stages, and comparison |

`Outcome` contains `passed`, `failed`, `launch_failed`, and `interrupted`. `ComparisonState` contains `no_branch`, `partial`, and `ready`.

## Exceptions

- `StoreError` reports missing, malformed, checksum-mismatched, or semantically inconsistent state and invalid workflow input.
- `DemoError` reports the stage that prevented a demonstration from completing. The destination is retained for inspection.

Command failures are data, not exceptions. Inspect `RunExecution.atom.outcome` and `return_code`.

## Typing

Ocura OSS includes a `py.typed` marker. Type annotations are available to type checkers from an installed wheel. The package requires Python 3.11 or later.
