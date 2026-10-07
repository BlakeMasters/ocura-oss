# State and verification

Version 0.5.0.

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

The checksum covers the schema version, kind, and canonical payload. It provides local change detection. It is not an authenticated signature and does not establish authorship.

## Format compatibility

Records carry schema version 2. This release reads schema 2 only. State written by Ocura OSS 0.4 and earlier carries schema 1 and is rejected with an error that names the version; there is no migration, so start a new state directory. Legacy `.ocura/` state is not compatible either.

Within one schema version, a record changes only by gaining optional fields:

- A reader ignores payload fields it does not recognize, so a record from a later release that keeps the same schema version stays readable.
- An added field must be safe to ignore. A field that changes the meaning of existing fields, a new required field, a removed or renamed field, a changed type, or a new enumeration value requires a new schema version.
- The envelope's four keys are fixed.

The repository keeps a small ledger written by 0.5.0 as a test fixture. Every later release that keeps schema 2 must verify it unchanged.

This describes how the format changes. It is not a commitment that schema 2 is final: the 0.x status in the [overview](index.md#project-status) still applies.

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

The operating system releases the lock when the recording process exits for any reason, including a forced kill. An attempt record is therefore in one of two states:

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

Running attempts are never touched by recovery.

Limits:

- Ocura OSS does not stop a command when its recorder stops. A command that outlives its recorder keeps running and can keep writing to its logs. Recover only after that command has stopped; otherwise later verification reports its log as changed.
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

Environment values are inherited by the child process but are not serialized. Dependency versions, source revisions, workspace contents, process memory, network activity, and external-system state are not recorded.

### Keeping sensitive values out of records

Two options limit what a run stores. Both are stated in the record, so a reader can tell an omission from an absence:

| Option | Effect | Recorded as |
| --- | --- | --- |
| `run --no-capture`, or `capture=False` | No stdout or stderr is written to disk | `output_capture` is `none`, and the six log fields are null |
| `run --mask-arg POSITION`, or `masked_arguments=[...]` | The command token at that zero-based position is stored as `<masked>` in the attempt and the atom; the command still receives the real value | `masked_arguments` lists the masked positions |

`output_capture` is `full` for an ordinary run. A command token that happens to equal `<masked>` is not listed in `masked_arguments`, so it is not mistaken for a masked one.

These options are explicit, and nothing is detected automatically:

- Masking covers command tokens in records. It does not alter output: a command that prints a masked value still writes it to a captured log. Combine it with `--no-capture` when that matters.
- Declared parameters and branch reasons are stored as given. Keep secrets out of them.
- A masked value remains visible to the operating system as an argument of the running process.

## Execution boundary

Commands run directly on the local machine with `shell=False` and the project root as the working directory. Ocura OSS does not sandbox commands, restrict network access, or isolate process trees. Use it only for trusted, same-owner local workloads.
