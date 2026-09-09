# CLI reference

This reference describes version `0.3.0`. JSON uses ASCII-safe
Unicode escapes, preserving decoded values across console encodings. Every command accepts
`--help`. See [automation](automation.md) for scripted and AI-agent use and the
[Python API](python-api.md) for typed results.

## Initialize

```console
ocura-oss init [--root PATH] [--name NAME] [--json]
```

Create project-local state and a default pathway. The root defaults to the current
directory. Existing `.ocura-oss/` state is never overwritten. JSON fields are `root`
(absolute project directory), `state_dir` (absolute state directory), `den_id`, and
`default_pathway_id`. Exit `0` on success or `2` for invalid input/state.

## Run

```console
ocura-oss run [--root PATH] [--pathway ID] [--param KEY=VALUE ...] [--quiet] [--json] -- COMMAND...
```

Place Ocura flags before `--` and command arguments after it. The default pathway
is used when `--pathway` is omitted. Parameter labels attach to this run only; they
are not passed to the command or automatically inherited from pathway labels.
For example, `--param steps=120 -- python train.py --steps 120` records and applies
the same choice explicitly.

The command runs with `shell=False`, the project root as its working directory, and
the invoking environment's permissions. Its stdout and stderr are recorded separately.
Text mode streams both by default; `--quiet` suppresses streaming. `--json` also
suppresses streaming, including when combined with `--quiet`, and prints exactly one
object after an attempt is recorded:

```json
{
  "atom_id": "atom-<id>",
  "chokepoint_id": "chokepoint-<id>",
  "pathway_id": "pathway-<id>",
  "outcome": "passed",
  "duration_seconds": 0.123,
  "return_code": 0,
  "launch_error_category": null,
  "stdout_log": ".ocura-oss/logs/atom-<id>.stdout.log",
  "stderr_log": ".ocura-oss/logs/atom-<id>.stderr.log"
}
```

Log paths are relative to the selected project root. JSON summarizes the attempt;
it does not inline command arguments, parameter labels, environment values, or logs.
The actual child return code is retained separately from the CLI exit code.

| Exit | Meaning | JSON stdout |
| --- | --- | --- |
| 0 | Recorded command passed | Result object |
| 1 | Recorded command failed or was interrupted | Result object |
| 2 | Invalid input/state or an error preventing completion of the record | Empty; diagnostic on stderr |
| 3 | Recorded command could not launch | Result object |

Launch failure has outcome `launch_failed`, a null `return_code`, and a category
such as `executable_not_found`. A first Ctrl+C records an `interrupted` attempt with
a null return code and category `interrupted`; a second exits immediately and may
prevent recording or a JSON result. Abrupt process termination likewise cannot
guarantee a completed record.

## List pathways and chokepoints

```console
ocura-oss pathways [--root PATH] [--json]
ocura-oss chokepoints [--root PATH] [--json]
```

`pathways` lists lineages oldest first with IDs, parents, reasons, parameter labels,
and terminal-evidence presence. `chokepoints` lists terminal evidence newest first
with IDs, associated runs/pathways, outcomes, creation times, and branchability.
JSON contains a `pathways` or `chokepoints` array respectively. Summaries omit command
arguments and log contents. Exit `0` on success, `2` on invalid state.

## Branch

```console
ocura-oss branch --from CHOKEPOINT_ID --reason TEXT [--root PATH] [--param KEY=VALUE ...] [--json]
```

Verify the selected terminal source and its referenced evidence, then create a child
pathway. The nonblank reason explains the proposed variation. Effective pathway
labels combine the parent pathway labels and supplied overrides. The workload manages
its workspace and checkpoints; the branch records lineage. Later runs must select
the returned pathway ID explicitly.

JSON fields are `id`, `parent_pathway_id`, `source_chokepoint_id`, `created_at`,
`reason`, `parameters`, and `has_terminal_evidence`. Exit `0` on success, `2` for
invalid arguments, an unusable source, or failed evidence verification.

## Compare

```console
ocura-oss compare [--from CHOKEPOINT_ID] [--root PATH] [--json]
```

Compare a source run with the newest run on each direct child pathway. This reads
evidence and never executes the workload. An explicit source selects targeted
verification. Automatic selection first verifies the complete state and selects the
newest source that has child pathways; with no branches it selects the newest terminal
chokepoint. A root without any chokepoints returns an error.

JSON contains `state`, `source_chokepoint_id`, `source_pathway_id`, `source_run`,
and `children`. Each child includes its pathway ID, reason, source ID, pathway parameter
delta, source/child run summaries, run-parameter delta, and `missing_evidence`.
Run summaries contain ID, pathway ID, outcome, start time, duration, and return code.

| State | Meaning |
| --- | --- |
| `ready` | Every child has terminal evidence |
| `partial` | At least one child has no recorded run |
| `no_branch` | The source has no child pathways |

These states return exit `0`; `ready` means evidence is present. Inspect outcomes
to determine whether runs passed, and workload metrics to assess improvement.
Invalid or unverifiable evidence returns exit `2`.

## Verify

```console
ocura-oss verify [--root PATH] [--json]
```

Check records, relationships, and referenced logs. JSON includes `den`, `status`,
`counts`, and `problems`; see [State and verification](state-and-verification.md) for scope.
Exit `0` for intact state, `2` for failed verification or invalid state. When a
verification report can be produced, JSON includes the problems even on exit `2`.

## Demo

```console
ocura-oss demo --root NEW_PATH [--json]
```

Run a dependency-free demonstration of initialization, baseline execution, branching,
child execution, verification, and comparison. The destination must not exist and is
retained on success or failure. JSON contains `root`, `steps`, and `comparison`.
Exit `0` on completion, `2` if a stage fails. The separate
[autoregressive example](https://github.com/BlakeMasters/ocura-oss/blob/main/examples/autoregressive/README.md) adds model training.

## Operating guidance

Use one mutating process per root. JSON modes keep diagnostics on stderr; `--help`
is a human-readable usage page. Commands run within your existing execution
environment; provide its isolation and permission policy when needed. See
[the README](../README.md#local-state-and-operating-model) for storage and trust.
