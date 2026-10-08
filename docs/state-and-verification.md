# State and verification

Version 0.6.0.

Ocura OSS stores records and command output under `.ocura-oss/` in one project directory. It does not use a service or a global project index.

## Directory layout

```text
.ocura-oss/
  den.json
  pathways/
    <pathway-id>.json
  atoms/
    <atom-id>.json
  chokepoints/
    <chokepoint-id>.json
  attempts/
    <atom-id>.json
    <atom-id>.lock
  logs/
    <atom-id>.stdout.log
    <atom-id>.stderr.log
```

`attempts/` holds one entry for each run that has started and is not yet finalized. It is empty when nothing is running and nothing was abandoned.

## Record envelope

Each JSON record contains an envelope:

```json
{
  "schema_version": 2,
  "kind": "atom",
  "payload": {},
  "checksum": {
    "algorithm": "sha256",
    "value": "<64 lowercase hexadecimal characters>"
  }
}
```

The checksum covers the schema version, kind, and canonical payload. It provides local change detection. It is not an authenticated signature and does not establish authorship: anyone who can write the directory can replace a record and its checksum together. A [retained manifest](#retained-manifests) detects that.

## Format compatibility

Records carry schema version 2. This release reads schema 2 only. State written by Ocura OSS 0.4 and earlier carries schema 1 and is rejected; see [upgrading from 0.4](#upgrading-from-04). Legacy `.ocura/` state is not compatible either.

Within one schema version, a record changes only by gaining optional fields:

- A reader ignores payload fields it does not recognize, so a record from a later release that keeps the same schema version stays readable.
- An added field must be safe to ignore. A field that changes the meaning of existing fields, a new required field, a removed or renamed field, a changed type, or a new enumeration value requires a new schema version.
- The envelope's four keys are fixed.

The repository keeps a small ledger written by 0.5.0 as a test fixture. Every later release that keeps schema 2 must verify it unchanged.

This describes how the format changes. It is not a commitment that schema 2 is final: the 0.x status in the [overview](index.md#project-status) still applies.

### Upgrading from 0.4

Versions 0.5 and later cannot read a `.ocura-oss/` directory written by 0.4 or earlier, and they do not convert one. Every command pointed at such a directory, including `verify`, stops with exit status 2 and a message naming the cause:

```text
error: unsupported schema version 1 in den record den.json; Ocura OSS 0.4 and earlier wrote this state, and this version cannot read or convert it. Keep using "ocura-oss<0.5" for it, or move .ocura-oss aside and run `ocura-oss init`
```

The old directory is never modified. Decide per project before upgrading:

| You want to | Do this |
| --- | --- |
| Keep working with the existing records | Stay on 0.4 for that project: `python -m pip install "ocura-oss<0.5"` in its environment. 0.4 reads and extends the directory as before |
| Start recording with a current version in the same project | Move the old directory aside, then run `ocura-oss init`. For example `mv .ocura-oss .ocura-oss-0.4`, or in PowerShell `Rename-Item .ocura-oss .ocura-oss-0.4` |
| Look at old records after upgrading | The logs under the moved directory's `logs/` are plain files, and the records are plain JSON. To use 0.4's commands on them again, move the directory back to `.ocura-oss` in an environment that has 0.4 |

A new state directory starts empty. Identifiers from the old one are unknown to it, so a branch cannot name an old chokepoint as its source: record the baseline again under 0.5 before branching from it.

If you need comparisons or listings from the old records later, save them with 0.4 first, for example `ocura-oss compare --json --from ID` and `ocura-oss chokepoints --json`.

## Record relationships

- The den identifies one default pathway.
- Every pathway belongs to that den.
- A child pathway identifies both its parent and the source chokepoint on that parent.
- Every atom belongs to an existing pathway.
- Every atom has exactly one chokepoint.
- Every chokepoint belongs to the same pathway as its atom and has the same outcome.
- Record filenames match their payload IDs.

A branch records lineage metadata only. A later run must name the child pathway to attach evidence to it.

## Attempts and recovery

A run is journaled before its command launches, so an attempt stays visible even when the process recording it never gets to finish:

1. `run` takes an operating-system lock on `attempts/<atom-id>.lock`, then writes `attempts/<atom-id>.json`. That attempt record holds the pathway, the identifiers reserved for the atom and chokepoint, the start time, the recorded command, and the declared parameters.
2. The command runs.
3. `run` writes the atom, then the chokepoint, then removes the attempt record and releases the lock.

The operating system releases the lock when the recording process exits for any reason, including a forced kill. The recorder holds the lock exclusively; anything that only asks whether a recorder is alive takes it shared, so concurrent verifiers and listings never mistake one another for a recorder. An attempt record is therefore in one of two states:

| State | Meaning |
| --- | --- |
| `running` | A live process holds the attempt's lock and is still recording it |
| `abandoned` | No process holds the lock; the recorder stopped before finalizing the attempt |

`ocura-oss attempts` and `Store.list_attempts()` list unfinished attempts with their state. `ocura-oss recover` and `recover()` close each abandoned attempt according to how far its recorder got:

| The recorder had written | Action | Result |
| --- | --- | --- |
| Only the attempt record | `abandoned` | An atom with outcome `abandoned` and its terminal chokepoint |
| The atom | `completed` | The missing chokepoint, carrying the atom's recorded outcome |
| The atom and chokepoint | `cleared` | Nothing new; the leftover attempt record is removed |

An `abandoned` atom states what is known and nothing else. It keeps the start time, recorded command, and declared parameters, and has no finish time, duration, return code, or launch category. Whatever output had been captured is retained: recovery measures each log as it finds it and records that size and digest, so the logs verify from then on. An abandoned atom's chokepoint is branchable, like any other.

Running attempts are never touched by recovery. Recovery needs each abandoned attempt's lock to itself, and waits about a quarter of a second for a reader that is checking the same attempt. If a reader holds on longer than that, recovery leaves that attempt for the next call; verification keeps reporting it as abandoned in the meantime.

### When the recorder stops

`ocura-oss run` treats a request to stop it as it treats the first Ctrl+C: it stops the command, records the run as `interrupted`, and exits with status 1. The requests are `SIGTERM` and `SIGHUP` on Linux and macOS, and Ctrl+Break on Windows. A signal the caller already ignores stays ignored, so a run started under `nohup` survives its terminal. On Windows, stopping the command also ends the processes it started; on Linux and macOS only the command itself is signaled.

The Python `run()` function installs no signal handlers. It records an interruption when `KeyboardInterrupt` is raised in the calling thread, so a program that wants the same behavior can set `signal.default_int_handler` for those signals.

A recorder that is killed outright records nothing, and its attempt becomes `abandoned`. What happens to the command depends on the operating system:

| System | The command when its recorder is killed |
| --- | --- |
| Windows | The operating system ends it, together with the processes it started |
| Linux | The kernel kills it; processes it started keep running |
| macOS | It keeps running |

On Linux this applies when the recording process has a single thread as it launches the command, which is always true of `ocura-oss run`. A command that finishes on its own is not affected on any system: processes it left running stay running.

Limits:

- Where a command or a process it started outlives a killed recorder, it can keep writing to its logs. Recover only after it has stopped; otherwise later verification reports its log as changed.
- The locks are advisory operating-system file locks. They are reliable on local filesystems. On a network filesystem, a running attempt may be reported as abandoned or the reverse.

## Concurrent use

Several processes may record runs under one state root at the same time. Each run writes only files named with its own freshly generated identifiers, every record appears atomically, and nothing written by one run is rewritten by another.

`verify` and `compare` may run while other runs are in flight. A running attempt is reported as in progress and is not a problem; its logs are not checked until it is finalized.

Ocura OSS does not coordinate anything outside its own records:

- Concurrent commands share the project root as their working directory. Keeping their workspace files apart is the workload's job.
- Listings order records by recorded timestamp and then identifier. Runs that overlap in time have no other ordering.
- `compare` without an explicit source selects the newest branched source at the moment it runs. Pass the source chokepoint when other work may be adding branches.
- Of several processes initializing the same root at once, exactly one succeeds.

## Verification

`verify()` and `Store.verify_state()` check:

- envelope shape, record kind, and schema version
- canonical payload checksum
- identifier and filename agreement
- required fields and semantic outcome invariants
- den, pathway, atom, and chokepoint references
- lineage cycles and source-parent agreement
- that every atom has exactly one chokepoint
- referenced log containment, existence, size, and SHA-256 digest
- unreferenced files or unexpected directories under `logs/`
- unfinished attempts: a running attempt is listed, and an abandoned attempt is a problem until it is recovered

The report counts records that were structurally readable and logs that passed evidence verification. Problems identify the affected record or log.

Automatic source selection for `compare()` requires the complete state to verify. Supplying a source chokepoint ID performs targeted verification of that source and the child evidence included in its comparison.

Verification establishes consistency among local records and logs. It does not capture or prove the external conditions needed to reproduce a command.

### Retained manifests

When the state is intact, verification also produces a manifest and a digest:

- The manifest has one `KIND ID CHECKSUM` line for every verified record, sorted. `ocura-oss manifest` prints it, and `StateVerification.manifest` holds it.
- The digest is the SHA-256 of those lines, each terminated by a newline. `verify` reports it, and `StateVerification.digest` holds it.

An atom's checksum covers its recorded log sizes and digests, and verification has just matched the logs to them, so a manifest line for an atom also pins that run's output.

Checksums stored inside `.ocura-oss/` cannot show that a record was rewritten by someone able to write there. A manifest kept elsewhere can. Save it where this state's writers cannot change it, such as a commit in another repository or a build artifact, then pass it back:

```console
ocura-oss manifest > ../trusted/experiment.manifest
ocura-oss verify --against ../trusted/experiment.manifest
```

Every retained entry must still be present with the same checksum. Records added since are allowed, so a manifest stays useful while work continues. A manifest that lists no records is rejected rather than treated as a pass: it would check nothing, and an empty file is what a failed export leaves behind. A rewritten record, a replaced log, or a removed run is reported as a retained record that is missing or was changed.

The protection is exactly as strong as the place the manifest is kept. A manifest stored beside the ledger can be replaced along with it. The digest alone identifies one exact set of records; it changes with every new run, so use it to confirm that two copies of a finished ledger match, and use the manifest to check a ledger that is still growing.

## Reading verified output

`Store.read_verified_log(atom_id, stream="stdout")` loads and validates the stored
atom and its lineage, checks the selected log's containment, and verifies the exact
bytes it returns against the atom's recorded byte count and SHA-256 digest. Pass
`stream="stderr"` for the other stream. Both streams can be read for failed attempts.

The complete selected log is held in memory and returned as bytes. Decoding and
metric interpretation belong to the caller. Unlike resolving a path and reading it
later, this method verifies the same bytes your code receives.

The read is scoped to the selected stream: it does not verify the other stream,
unrelated records, or the whole ledger. Continue to use `verify()` or
`Store.verify_state()` for a complete check. It does not lock the ledger or provide
an atomic snapshot; returned bytes remain unchanged if the file changes afterward.
An atom recorded without output capture has no log, and the read raises `StoreError`.
See the [Python API](python-api.md#storeread_verified_log) for usage and errors.

## Stored information

Atoms retain command arguments, outcomes, timing, return codes, declared parameters, log paths, byte counts, and log digests. Logs retain command output. Pathways retain branch reasons and effective declared parameters. An unfinished attempt retains the same command arguments and declared parameters as the atom it becomes.

Environment values are inherited by the child process but are not serialized. Dependency versions, workspace contents, process memory, network activity, and external-system state are not recorded. The source revision is recorded only when [launch context](#optional-launch-context) is requested.

### Optional launch context

A run records nothing about its surroundings unless asked. Two options add a `context` object to the attempt and the atom:

| Option | Records |
| --- | --- |
| `run --context`, or `context=True` | `platform`: the operating system name, release, and machine type. `git`: the checked-out revision and whether the work tree differs from it, or null when the project root is not inside a work tree with at least one commit or git is unavailable |
| `run --context-file PATH`, or `context_files=[...]` | `files`: the size and SHA-256 of each named file, read just before launch. A relative path is resolved against the project root. A file that cannot be read stops the run before the command launches |

The work tree is `dirty` when it has modified or untracked files other than `.ocura-oss/` itself. Untracked files and changes inside submodules count whatever the user's or repository's git settings say about showing them. Files matched by ignore rules do not count. Reading git state runs `git` twice in the project root; that is the only extra process, and only with `--context`.

Context is a description, not a guarantee:

- It records the moment of launch. Verification does not recheck it, and a named file may change afterward.
- It does not record installed package versions. Those belong to whichever interpreter the command uses, which need not be the one running Ocura OSS. Name a lock file with `--context-file` to pin them.
- A revision and file digests narrow down what ran. They do not make a command reproducible or restore a workspace.

The `context` field is absent from a record written without these options.

### Keeping sensitive values out of records

Two options limit what a run stores. Both are stated in the record, so a reader can tell an omission from an absence:

| Option | Effect | Recorded as |
| --- | --- | --- |
| `run --no-capture`, or `capture=False` | No stdout or stderr is written to disk | `output_capture` is `none`, and the six log fields are null |
| `run --mask-arg POSITION`, or `masked_arguments=[...]` | The command token at that zero-based position is stored as `<masked>` in the attempt and the atom; the command still receives the real value | `masked_arguments` lists the masked positions |

`output_capture` is `full` for an ordinary run. A command token that happens to equal `<masked>` is not listed in `masked_arguments`, so it is not mistaken for a masked one.

These options are explicit, and nothing is detected automatically:

- Masking covers command tokens in records. It does not alter output: a command that prints a masked value still writes it to a captured log. Combine it with `--no-capture` when that matters.
- Declared parameters and branch reasons are stored as given. Keep secrets out of them. That includes any parameter passed to the command with `--substitute`: its value is recorded as a label even when its command token is masked.
- A masked value remains visible to the operating system as an argument of the running process.

## Execution boundary

Commands run directly on the local machine with `shell=False` and the project root as the working directory. Ocura OSS does not sandbox commands, restrict network access, or isolate process trees. Use it only for trusted, same-owner local workloads.
