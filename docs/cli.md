# Command-line reference

Version 0.6.0.

The `ocura-oss` command records trusted local command attempts, creates metadata branches, compares branch evidence, and verifies project-local state.

```text
ocura-oss COMMAND [OPTIONS]
```

Commands run against one project root. Unless `--root PATH` is supplied, the root is the current working directory. State is stored below that root in `.ocura-oss/`.

## Command summary

| Command | Purpose |
| --- | --- |
| `init` | Create one den and its default pathway |
| `run` | Run one command and record an atom, logs, and a chokepoint |
| `pathways` | List pathway lineage and evidence status |
| `chokepoints` | List terminal evidence boundaries |
| `branch` | Create a metadata-only child pathway |
| `compare` | Compare a source run with runs on its child pathways |
| `verify` | Recheck records, relationships, and referenced logs |
| `manifest` | Print one checksum line per verified record, to keep elsewhere |
| `attempts` | List runs that have started and are not yet finalized |
| `recover` | Close attempts whose recording process stopped |
| `demo` | Run the complete workflow in a new retained directory |

Run `ocura-oss COMMAND --help` for command-specific help.

## Conventions

### Project root

`--root PATH` identifies the directory that contains `.ocura-oss/`. Relative paths and `~` are resolved before use. The command does not change the parent process working directory.

`init` and `demo` create state. All other commands require initialized state. Without it they exit with status 2 and name `ocura-oss init`. When a directory above the root already holds state, the message names that directory, so that a command run from a subdirectory is not answered by creating a second ledger.

### Identifiers

Every record has an identifier made of its kind and 32 hexadecimal characters, such as `chokepoint-447ca3002f0b4d7c9a1e5b6c7d8e9f01`. Output, JSON results, and records always carry the full identifier.

`branch --from`, `compare --from`, and `run --pathway` also accept a prefix: at least four of the identifier's leading hexadecimal characters, with or without the kind word, such as `447c` or `chokepoint-447ca3`. A prefix must match exactly one stored record of that kind. One that matches several is rejected with the matching identifiers listed, and one that matches none is rejected, both with exit status 2 and before anything is written or run.

Prefixes are for typing by hand. A script or agent already holds the full identifier and should pass it, because a prefix that is unique today can match a second record later.

### Declared parameters

`--param KEY=VALUE` records a string label. The option is repeatable. Keys must begin with a letter or underscore and may then contain letters, digits, underscores, periods, or hyphens. Keys and values must not be empty. A key may appear only once in one invocation.

A run records its pathway's effective parameters together with its own declarations. A run on a branch therefore carries the branch's parameters without repeating them; a `--param` on the run replaces the pathway's value for the same key in that run's record and adds any other key. A pathway's parameters are fixed when it is created.

Declared parameters do not configure the child process. Pass process arguments after `--` in `run`. To declare a value once and also pass it, use `run --substitute` and write `{KEY}` in the command, quoted in PowerShell.

### JSON output

Every command accepts `--json`. JSON mode writes one result document to standard output, with Unicode escaped so it also works on legacy Windows encodings. Diagnostic messages go to standard error.

`run --json` retains child stdout and stderr in their logs without streaming them. A recorded command failure, interruption, or launch failure still returns a JSON result with its existing nonzero exit status. Invalid input or a state error returns no result document.

### Summary data

Default summaries and listing output omit command arguments, environment values, and log contents. `run` streams child output unless `--quiet` or `--json` is present, so streamed output is separate from the final summary.

Raw atom records retain command arguments, and log files retain command output. Keep secrets out of command arguments, declared parameters, branch reasons, and output. When a run cannot avoid one, `run --mask-arg` keeps a command token out of the records and `run --no-capture` keeps output off disk; see [keeping sensitive values out of records](state-and-verification.md#keeping-sensitive-values-out-of-records).

## `init`

Create one den and one default pathway.

```text
ocura-oss init [--root PATH] [--name NAME] [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root in which `.ocura-oss/` is created |
| `--name NAME` | string | `ocura-oss` | Nonblank name stored in the den record |
| `--json` | flag | false | Emit project paths and identifiers as JSON |

#### Behavior

`init` creates `.ocura-oss/`, one den record, and one unbranched default pathway. Initialization fails if `.ocura-oss/` already exists, including when the directory contains incomplete state.

The text response identifies the den, default pathway, and state directory.

#### JSON output

| Field | Type | Description |
| --- | --- | --- |
| `root` | string | Resolved project root |
| `state_dir` | string | Resolved `.ocura-oss` directory |
| `den_id` | string | Persisted den identifier |
| `default_pathway_id` | string | Persisted default pathway identifier |

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | State was initialized |
| 2 | Input was invalid, state already existed, or a filesystem operation failed |

#### Example

```console
ocura-oss init --root ./experiment --name "batch study"
```

## `run`

Run one trusted local command and record terminal evidence.

```text
ocura-oss run [--root PATH] [--pathway ID] [--param KEY=VALUE] [--quiet] [--json]
              [--no-capture] [--mask-arg POSITION] [--substitute]
              [--context] [--context-file PATH] -- COMMAND...
```

The `--` separator is required. Ocura OSS options belong before it. Every token after it is passed to the child command as one argument token.

#### Arguments and options

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--pathway ID` | pathway ID or unique prefix | den default | Pathway that receives the recorded atom |
| `--param KEY=VALUE` | string pair | none | Declared run parameter, recorded with the pathway's parameters and replacing the pathway's value for the same key; repeatable |
| `--quiet` | flag | false | Retain output without mirroring it to the terminal |
| `--json` | flag | false | Emit one result as JSON; retain child output in logs without streaming |
| `--no-capture` | flag | false | Retain no stdout or stderr; the record states that output was not captured |
| `--mask-arg POSITION` | integer | none | Record `<masked>` in place of the `COMMAND` token at this zero-based position; repeatable |
| `--substitute` | flag | false | Replace `{KEY}` in `COMMAND` tokens with that parameter's value |
| `--context` | flag | false | Record the platform and the git revision and dirty state at launch |
| `--context-file PATH` | path | none | Record the size and SHA-256 of this file at launch; repeatable |
| `COMMAND...` | argument tokens | required | Executable and arguments placed after `--` |

#### Execution

The command runs with `shell=False` and the project root as its working directory. It inherits the invoking process environment. Environment keys and values are not serialized.

Stdout and stderr are captured as separate files under `.ocura-oss/logs/`. Unless `--quiet` or `--json` is present, the same bytes are also streamed to the terminal.

With `--no-capture`, no log files are written. Output still streams to the terminal unless `--quiet` or `--json` is present, and is otherwise discarded.

`--mask-arg POSITION` counts `COMMAND` tokens from zero, so position 0 is the executable. The command receives the real token; the attempt and atom records store `<masked>` there and list the position. Masking does not alter output, so a command that prints the value still writes it to a captured log. A position outside `COMMAND` is rejected before anything runs.

With `--substitute`, each `{KEY}` in a `COMMAND` token is replaced by that parameter's value before the command launches. A value comes from this run's `--param` declarations first and then from the pathway's effective parameters, so a run on a branch can use the branch's values without restating them. These are the same labels the run records, and the recorded command is the substituted one. Write `{{` or `}}` for a literal brace. A placeholder with no value, or an unbalanced brace, is rejected before anything runs. Without `--substitute`, braces are passed through untouched. Quote a placeholder, as in `"{batch}"`, when the shell is PowerShell: it reads bare braces as a script block and does not pass them to the command.

`--context` and `--context-file` add an optional `context` object to the record; see [optional launch context](state-and-verification.md#optional-launch-context). Neither runs or reads anything unless it is present.

One run produces:

- one atom containing timing, outcome, the pathway's and the run's declared parameters, command arguments, and log metadata
- one stdout log and one stderr log, unless `--no-capture` is present
- one branchable terminal chokepoint

The attempt is journaled under `.ocura-oss/attempts/` before the command launches, and that entry is removed once the atom and chokepoint are written.

A passing command records `passed`. A nonzero return code records `failed`. A launch error records `launch_failed`. The first Ctrl+C stops the command and records `interrupted` with partial output retained. So does a request to stop the `run` process: `SIGTERM` or `SIGHUP` on Linux and macOS, Ctrl+Break on Windows. A second Ctrl+C exits immediately; so does a killed `run` process. Either leaves the attempt unfinished until [`recover`](#recover) closes it as `abandoned`. The operating system then ends the command on Windows and Linux; on macOS the command keeps running. See [when the recorder stops](state-and-verification.md#when-the-recorder-stops).

#### Text output

The final text summary includes the atom, chokepoint, pathway, outcome, duration, log paths, and the return code or launch category when available. With `--no-capture`, it states `output: not captured` in place of the log paths. It does not repeat command arguments or parameter values; `pathways` lists a pathway's parameters and `compare` lists what each run recorded.

#### JSON output

| Field | Type | Description |
| --- | --- | --- |
| `atom_id` | string | Recorded command attempt |
| `chokepoint_id` | string | Terminal evidence for branching |
| `pathway_id` | string | Pathway containing the run |
| `outcome` | string | `passed`, `failed`, `interrupted`, or `launch_failed` |
| `duration_seconds` | number | Recorded elapsed command time |
| `return_code` | integer or null | Child exit status when available |
| `launch_error_category` | string or null | Error category or `interrupted` |
| `output_capture` | string | `full`, or `none` when `--no-capture` was used |
| `stdout_log` | string or null | Project-relative stdout log path; null without capture |
| `stderr_log` | string or null | Project-relative stderr log path; null without capture |

Use returned IDs to continue the [automated workflow](automation.md). The [training example](../examples/autoregressive/README.md) provides an executable client.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | The child command returned 0 and `passed` evidence was recorded |
| 1 | The child returned nonzero or the attempt was interrupted; evidence was recorded |
| 2 | Input or state was invalid, or evidence could not be persisted |
| 3 | The child process could not be launched; `launch_failed` evidence was recorded |

#### Examples

```console
ocura-oss run -- python script.py --epochs 4
```

```console
ocura-oss run --pathway pathway-<id> --quiet -- python script.py --batch 4
```

Declare a value once and pass it to the command:

```console
ocura-oss run --param batch=4 --substitute -- python train.py --batch "{batch}"
```

Record the revision, the platform, and the digest of a lock file with the run:

```console
ocura-oss run --context --context-file requirements.lock -- python train.py
```

Keep a token out of the records and its output off disk. Position 3 is the value after `--token`:

```console
ocura-oss run --no-capture --mask-arg 3 -- python upload.py --token <value>
```

## `pathways`

List structurally valid pathway records in creation order, oldest first.

```text
ocura-oss pathways [--root PATH] [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--json` | flag | false | Emit one machine-readable JSON document |

#### Output

Each entry contains:

| Field | Type | Description |
| --- | --- | --- |
| `id` | string | Pathway identifier |
| `parent_pathway_id` | string or null | Parent pathway for a branch |
| `source_chokepoint_id` | string or null | Chokepoint from which the branch was created |
| `created_at` | string | UTC ISO 8601 timestamp |
| `reason` | string | Branch reason; `default pathway` for the default pathway |
| `parameters` | object | Effective declared pathway parameters |
| `has_terminal_evidence` | boolean | Whether any atom is recorded on the pathway |

JSON mode returns `{"pathways": [...]}`.

The listing is fail-closed. A malformed or misnamed record causes exit status 2 instead of producing a partial list. This command does not verify pathway relationships or logs; use `verify` for complete state verification.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | The validated list was emitted |
| 2 | State was missing, malformed, inconsistent, or unreadable |

## `chokepoints`

List structurally valid terminal chokepoint records in reverse creation order, newest first.

```text
ocura-oss chokepoints [--root PATH] [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--json` | flag | false | Emit one machine-readable JSON document |

#### Output

Each entry contains:

| Field | Type | Description |
| --- | --- | --- |
| `id` | string | Chokepoint identifier |
| `pathway_id` | string | Pathway that owns the source atom |
| `atom_id` | string | Recorded command attempt |
| `outcome` | string | `passed`, `failed`, `launch_failed`, `interrupted`, or `abandoned` |
| `created_at` | string | UTC ISO 8601 timestamp |
| `branchable` | boolean | Whether the chokepoint can serve as a branch source |

JSON mode returns `{"chokepoints": [...]}`. Command arguments and log contents are not included.

The listing is fail-closed. A malformed or misnamed record causes exit status 2. This command does not verify atom relationships or logs; use `verify` for complete state verification.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | The validated list was emitted |
| 2 | State was missing, malformed, inconsistent, or unreadable |

## `branch`

Create a metadata-only child pathway from verified terminal evidence.

```text
ocura-oss branch --from CHOKEPOINT_ID --reason TEXT [--param KEY=VALUE] [--root PATH] [--json]
```

#### Arguments and options

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--from CHOKEPOINT_ID` | chokepoint ID or unique prefix | required | Terminal branchable source chokepoint |
| `--reason TEXT` | string | required | Nonblank explanation recorded on the child pathway |
| `--param KEY=VALUE` | string pair | none | Override applied to the parent's effective parameters; repeatable |
| `--root PATH` | path | current directory | Project root containing the source |
| `--json` | flag | false | Emit the child pathway summary as JSON |

#### Verification and state effects

Before writing, `branch` validates the chokepoint, its atom, its pathway, their relationships, and the logs the atom recorded. Missing, checksum-mismatched, nonterminal, nonbranchable, or otherwise unverifiable sources are rejected.

The child inherits the parent's effective parameters and applies the supplied overrides. The operation records the parent pathway, source chokepoint, reason, creation time, and effective parameters.

It does not copy a workspace, process, memory image, checkpoint, artifact, source atom, or external state. It does not run a command. Use `run --pathway CHILD_ID` to attach later evidence; that run records the child's effective parameters.

#### JSON output

JSON mode returns the fields documented for a pathway listing. `has_terminal_evidence` is `false` for the new child.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | The child pathway was created |
| 2 | Input, source records, source logs, or state were invalid |

#### Example

```console
ocura-oss branch --from chokepoint-<id> --reason "increase batch" --param batch=4
```

## `compare`

Compare one verified source run with the newest recorded run on each child pathway created from its chokepoint.

```text
ocura-oss compare [--from CHOKEPOINT_ID] [--root PATH] [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--from CHOKEPOINT_ID` | chokepoint ID or unique prefix | newest branched source, or newest terminal source when no branches exist | Explicit source for targeted verification |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--json` | flag | false | Emit a structured comparison document |

#### Source selection

With `--from`, comparison verifies the selected source and the child evidence used in the result.

Without `--from`, the complete state must pass verification before Ocura OSS selects the newest chokepoint referenced by a child pathway. A state with no child pathways selects its newest terminal chokepoint and reports `no_branch`. A malformed record, broken reference, unverified log, orphaned log file, unexpected log directory, atom without a chokepoint, or abandoned attempt anywhere in state blocks automatic selection. Attempts that another process is still recording do not.

#### Comparison state

| State | Meaning |
| --- | --- |
| `ready` | Every child pathway has terminal evidence |
| `partial` | At least one child pathway lacks terminal evidence |
| `no_branch` | No child pathway was created from the source chokepoint |

For a child with multiple atoms, comparison uses the newest atom by start time and identifier. It reports pathway parameter differences and, when child evidence exists, run-level declared parameter differences. The pathway delta compares the two pathways' effective parameters. The run delta compares the labels the source run and the child run recorded, which include each run's own declarations.

Parameter deltas contain inherited, added, and changed values. Removed parameters are outside the comparison schema.

Comparison reads stored records and logs. It does not rerun commands.

#### JSON output

The top-level document contains:

| Field | Type | Description |
| --- | --- | --- |
| `state` | string | `ready`, `partial`, or `no_branch` |
| `source_chokepoint_id` | string | Selected source chokepoint |
| `source_pathway_id` | string | Pathway containing the source run |
| `source_run` | object | Reduced source atom summary |
| `children` | array | One comparison for each child pathway |

Each child contains its pathway ID, reason, source chokepoint, pathway parameter delta, source run, optional child run, optional run parameter delta, and `missing_evidence` flag.

A run summary contains `id`, `pathway_id`, `outcome`, `started_at`, `duration_seconds`, and `return_code`. `duration_seconds` is null for an `abandoned` run, and the text view prints `abandoned (duration unknown)`.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | A comparison was emitted, including `partial` or `no_branch` |
| 2 | Selection, state, source evidence, or child evidence was invalid |

## `verify`

Recheck every state record and every log referenced by a recorded run.

```text
ocura-oss verify [--root PATH] [--json] [--against FILE]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--json` | flag | false | Emit counts and the complete problem list as JSON |
| `--against FILE` | path | none | Manifest retained from an earlier `manifest`; every record it lists must still be present and unchanged |

#### Checks

Verification covers:

- record envelopes, kinds, schema versions, and canonical checksums
- payload identifiers and agreement between identifiers and filenames
- required fields and outcome invariants
- den, pathway, atom, and chokepoint references
- pathway lineage cycles and source-parent agreement
- that every atom has exactly one chokepoint
- referenced log containment, existence, byte count, and SHA-256 digest
- unreferenced files and unexpected directories under `.ocura-oss/logs/`
- unfinished attempts
- with `--against`, that every retained manifest entry is still present with the same checksum

`logs_checked` counts individual logs that passed verification. A valid atom normally contributes two logs, and none when it was recorded without capture.

Verification may run while other processes record runs under the same root. An attempt that a live process is still recording is listed under `attempts.running` and is not a problem; its logs are not checked until it is finalized. An abandoned attempt is a problem until [`recover`](#recover) closes it.

#### JSON output

| Field | Type | Description |
| --- | --- | --- |
| `den` | string | Den identifier |
| `status` | string | `ok` or `failed` |
| `digest` | string or null | SHA-256 of the manifest when the state is intact; null otherwise |
| `counts.pathways` | integer | Pathway records found |
| `counts.atoms` | integer | Atom records found |
| `counts.chokepoints` | integer | Chokepoint records found |
| `counts.logs_checked` | integer | Referenced logs that passed verification |
| `attempts.running` | array | Identifiers of attempts a live process is still recording |
| `attempts.abandoned` | array | Identifiers of attempts whose recorder stopped; each is also a problem |
| `problems` | array | Objects containing `record` and `problem` strings |

The text view prints the digest on its own line when the state is intact.

Verification establishes consistency among local records and logs. It does not establish authorship or reproduce external execution conditions. Checksums kept inside the state cannot show that a record was rewritten together with its checksum; `--against` with a manifest kept elsewhere can.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | No verification problems were found |
| 2 | State was missing, unreadable, malformed, inconsistent, or failed verification; or the `--against` file was unreadable, malformed, or listed no records |

## `manifest`

Verify the complete state, then print one checksum line for every record.

```text
ocura-oss manifest [--root PATH] [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--json` | flag | false | Emit the digest and entries as JSON |

#### Output

Each line is `KIND ID CHECKSUM`, where `KIND` is `den`, `pathway`, `atom`, or `chokepoint` and `CHECKSUM` is that record's stored SHA-256. Lines are sorted. JSON mode returns `{"digest": "...", "entries": [...]}`; the digest is the SHA-256 of the lines, each terminated by a newline.

Keep the output where this state's writers cannot change it and pass it to `verify --against` later. See [retained manifests](state-and-verification.md#retained-manifests) for what that detects and its limits.

A manifest is printed only for a state that verifies completely. Attempts still being recorded are not records yet and are not listed.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | The state verified and the manifest was emitted |
| 2 | State was missing or failed verification; no manifest was emitted |

## `attempts`

List runs that have started and are not yet finalized, oldest first.

```text
ocura-oss attempts [--root PATH] [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--json` | flag | false | Emit one machine-readable JSON document |

#### Output

Each entry contains:

| Field | Type | Description |
| --- | --- | --- |
| `id` | string | Identifier of the atom the attempt becomes when finalized |
| `pathway_id` | string | Pathway the run was started on |
| `started_at` | string | UTC ISO 8601 timestamp |
| `state` | string | `running` while a live process is recording it; `abandoned` once that process has stopped |

JSON mode returns `{"attempts": [...]}`. Command arguments and log contents are not included. A finalized run is an atom and is not listed, so the list is empty when nothing is running and nothing was abandoned.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | The validated list was emitted |
| 2 | State was missing, malformed, inconsistent, or unreadable |

## `recover`

Close every attempt whose recording process stopped before finalizing it.

```text
ocura-oss recover [--root PATH] [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | current directory | Project root containing initialized state |
| `--json` | flag | false | Emit the closed attempts as JSON |

#### Behavior

Each abandoned attempt is closed according to how far its recorder got:

| Action | When | Result |
| --- | --- | --- |
| `abandoned` | No outcome had been recorded | An atom with outcome `abandoned` and its terminal chokepoint |
| `completed` | The atom existed without its chokepoint | The missing chokepoint, with the atom's recorded outcome |
| `cleared` | The atom and chokepoint both existed | The leftover attempt entry is removed |

An `abandoned` atom has no finish time, duration, or return code; none is invented. Output captured before the recorder stopped is retained and measured, so it verifies afterward.

Running attempts are left alone, so `recover` is safe to call while other runs are in flight. Run it only after the abandoned command itself has stopped: a command that outlived its recorder can keep writing to logs that recovery has already measured.

#### JSON output

JSON mode returns `{"recovered": [...]}`. Each entry contains:

| Field | Type | Description |
| --- | --- | --- |
| `atom_id` | string | Atom the attempt was closed as |
| `chokepoint_id` | string | That atom's terminal chokepoint |
| `pathway_id` | string | Pathway containing the atom |
| `action` | string | `abandoned`, `completed`, or `cleared` |

The text view prints one line per closed attempt and a final `recovered: N` count.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | Every abandoned attempt was closed, including when there were none |
| 2 | State was missing, or an attempt record or its evidence was malformed or unreadable |

## `demo`

Run the complete workflow in a new retained directory.

```text
ocura-oss demo --root PATH [--json]
```

#### Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--root PATH` | path | required | Destination directory; it must not exist |
| `--json` | flag | false | Emit completed stages and comparison as JSON |

#### Behavior

The demonstration performs:

- initialization
- a passing baseline run with declared `batch=1`
- selection of the resulting terminal chokepoint
- a metadata branch with an explicit reason and effective `batch=4`
- a passing run explicitly attached to the child pathway
- complete state verification
- a `ready` comparison

Both commands must pass and verification must report no problems before the demonstration reports completion.

The destination is retained on success and failure. An existing destination produces exit status 2. If a stage fails, the diagnostic names that stage.

#### JSON output

The document contains the resolved `root`, an ordered `steps` array, and the complete `comparison` object.

#### Exit status

| Status | Meaning |
| --- | --- |
| 0 | Every demonstration stage completed |
| 2 | The destination existed, a stage failed, or resulting state did not verify |

## Exit status summary

| Status | Commands | Meaning |
| --- | --- | --- |
| 0 | all | Requested operation completed; for `run`, the child passed |
| 1 | `run` | Child failed or was interrupted and terminal evidence was recorded |
| 2 | all | Invalid input, missing or invalid state, verification failure, or demo failure |
| 3 | `run` | Child could not launch and launch-failed evidence was recorded |

Argument parsing errors also use status 2.

## Security and execution boundary

`run` executes command tokens directly with `shell=False`. Ocura OSS does not sandbox commands, restrict network access, contain hostile code, or guarantee process-tree isolation.

Use owner-authorized workloads inside an execution environment with suitable permissions and isolation. See [state and verification](state-and-verification.md) and [Python API reference](python-api.md) for the corresponding programmatic contract.
