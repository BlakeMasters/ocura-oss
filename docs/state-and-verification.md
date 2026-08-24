# State and verification

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
  logs/
    <atom-id>.stdout.log
    <atom-id>.stderr.log
```

One mutating process per state root is supported at a time.

## Record envelope

Each JSON record contains an envelope:

```json
{
  "schema_version": 1,
  "kind": "atom",
  "payload": {},
  "checksum": {
    "algorithm": "sha256",
    "value": "<64 lowercase hexadecimal characters>"
  }
}
```

The checksum covers the schema version, kind, and canonical payload. It provides local change detection. It is not an authenticated signature and does not establish authorship.

## Record relationships

- The den identifies one default pathway.
- Every pathway belongs to that den.
- A child pathway identifies both its parent and the source chokepoint on that parent.
- Every atom belongs to an existing pathway.
- Every chokepoint belongs to the same pathway as its atom and has the same outcome.
- Record filenames match their payload IDs.

A branch records lineage metadata only. A later run must name the child pathway to attach evidence to it.

## Verification

`verify()` and `Store.verify_state()` check:

- envelope shape, record kind, and schema version
- canonical payload checksum
- identifier and filename agreement
- required fields and semantic outcome invariants
- den, pathway, atom, and chokepoint references
- lineage cycles and source-parent agreement
- referenced log containment, existence, size, and SHA-256 digest
- unreferenced files or unexpected directories under `logs/`

The report counts records that were structurally readable and logs that passed evidence verification. Problems identify the affected record or log.

Automatic source selection for `compare()` requires the complete state to verify. Supplying a source chokepoint ID performs targeted verification of that source and the child evidence included in its comparison.

Verification establishes consistency among local records and logs. It does not capture or prove the external conditions needed to reproduce a command.

## Stored information

Atoms retain command arguments, outcomes, timing, return codes, declared parameters, log paths, byte counts, and log digests. Logs retain command output. Pathways retain branch reasons and effective declared parameters.

Environment values are inherited by the child process but are not serialized. Dependency versions, source revisions, workspace contents, process memory, network activity, and external-system state are not recorded.

Keep secrets out of command arguments, parameters, reasons, and command output.

## Execution boundary

Commands run directly on the local machine with `shell=False` and the project root as the working directory. Ocura OSS does not sandbox commands, restrict network access, or isolate process trees. Use it only for trusted, same-owner local workloads.

Legacy `.ocura/` state is not compatible with `.ocura-oss/` records.
