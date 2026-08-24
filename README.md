# Ocura OSS

Ocura OSS creates integrity-checked, branchable records of local command executions. Project-local state stores command arguments, outcomes, timing, output logs, declared parameters, and branch lineage.

Ocura OSS packages an adapted early Ocura research concept as a small local command-line tool. Current Ocura engine development is separate from this package.

## Workflow

`run -> evidence -> chokepoint -> branch -> rerun -> compare`

While a command runs, its output streams to your terminal and is recorded under `.ocura-oss/logs/`. If you press Ctrl+C, the attempt is recorded with outcome `interrupted`; a second Ctrl+C exits immediately and may leave that attempt unrecorded.

## Terms

- **Den**: the local container for state.
- **Pathway**: one lineage of declared work and evidence.
- **Atom**: one recorded execution and its output logs.
- **Chokepoint**: a terminal record that can start a metadata branch.

## Requirements and install

Ocura OSS requires Python 3.11 or later and uses only the standard library at runtime. Until a package index release is available, install from a source checkout with `pip install .`.

## Quick start

`ocura-oss demo --root ./ocura-oss-demo`

The demo creates a new directory, runs a baseline command, creates a chokepoint, branches with a reason and parameter override, reruns on the child pathway, verifies all records and referenced logs, and prints a comparison. It returns exit code 2 if the destination already exists; the directory is kept for inspection either way.

## Direct use

```console
ocura-oss init --name example
ocura-oss run -- python -c "print('baseline')"
ocura-oss verify
ocura-oss chokepoints
ocura-oss branch --from <chokepoint-id> --reason "batch 2" --param batch=2
ocura-oss run --pathway <child-pathway-id> -- python -c "print('batch 2')"
ocura-oss compare --from <chokepoint-id>
```

Everything after `--` becomes the executed command; put run's own options before it. `run --quiet -- COMMAND...` retains command output in logs without streaming it to the terminal. `branch` accepts `--root PATH`; without it, `branch` uses the current directory. Add `--json` to `pathways`, `chokepoints`, `branch`, `compare`, `verify`, or `demo` for JSON output. Each subcommand's `--help` lists its flags and exit semantics. `branch` and `compare` return exit code 2 if required records or logs fail verification.

Example text comparison:

```console
comparison: ready
source chokepoint: chokepoint-<id>
source pathway: pathway-<id>
source run: passed 0.031000s
child pathway: pathway-<id2>
  reason: batch 2
  parameters: added batch=2
  source run: passed 0.031000s
  child run: passed 0.047000s
  run parameters: changed batch (1 -> 4)
```

## Local state

Records and logs live under `.ocura-oss/`: `den.json`, `pathways/<pathway-id>.json`, `atoms/<atom-id>.json`, `chokepoints/<chokepoint-id>.json`, and `logs/<atom-id>.stdout.log` / `.stderr.log`.

Each record is a JSON envelope with `schema_version`, `kind`, `payload`, and a SHA-256 checksum over the canonical payload. Atom payloads contain the outcome, timing, return code, declared parameters, command arguments, log paths, byte counts, and log checksums. Command arguments, declared parameters, branch reasons, and command output are stored locally. Keep secrets out of these fields. Checksums provide local change detection without authentication or authorship claims.

One mutating CLI process per state root is supported at a time. Delete `.ocura-oss/` to discard all state.

## Boundaries

Commands run directly on your machine with `shell=False` and inherit the invoking process's environment. Ocura OSS does not sandbox commands, restrict network access, or isolate child processes. Use it only for trusted, same-owner local workloads. Branches contain lineage metadata; they do not copy or rewind a process, workspace, memory image, checkpoint, artifact, or external system. Legacy `.ocura/` records are unsupported.

## Status

Ocura OSS is research software. Production support is outside the 0.1 scope, and the interface and record format may change before 1.0. The source code and tests are licensed under the [Mozilla Public License 2.0](https://github.com/BlakeMasters/ocura-oss/blob/main/LICENSE).

## Contributing

Bug reports and focused pull requests are welcome. Read the [contributor guide](https://github.com/BlakeMasters/ocura-oss/blob/main/CONTRIBUTING.md) for setup, checks, and project scope. Please follow the [security policy](https://github.com/BlakeMasters/ocura-oss/blob/main/SECURITY.md) when reporting a security issue.
