# Changelog

This file records user-visible changes to Ocura OSS.

## 0.6.0 - Unreleased

- **License:** Ocura OSS is now released under the Apache License 2.0. Versions 0.5.0
  and earlier remain under MPL-2.0. Contributions are accepted under Apache-2.0.
- The record format is unchanged. This release reads and extends state written by
  0.5.0.

## 0.5.0 - 2026-10-07

- **Breaking:** change the record format to schema version 2. State written by 0.4 and
  earlier is rejected with exit status 2 and left untouched; there is no migration. To
  keep using existing records, stay on 0.4 for that project (`ocura-oss<0.5`). To record
  with 0.5, move the old `.ocura-oss/` aside and run `ocura-oss init`. See "Upgrading
  from 0.4" in `docs/state-and-verification.md`. `Atom` log fields, `finished_at`, and `duration_seconds`
  may now be `None`, and `RunSummary.duration_seconds` may be `None`.
- Journal each run under `.ocura-oss/attempts/` before its command launches. An attempt
  whose recording process is interrupted twice or killed stays visible instead of leaving
  orphaned logs. Add `ocura-oss attempts` and `Store.list_attempts()` to list unfinished
  attempts as `running` or `abandoned`.
- Add `ocura-oss recover`, `recover()`, and `Store.recover()` to close abandoned attempts.
  One with no recorded outcome becomes an atom with the new `abandoned` outcome: its
  captured output is retained, and no finish time, duration, or return code is invented.
- Support several processes recording runs under one state root at once. `verify` and
  `compare` work while runs are in flight and report them as running rather than as damage.
  Of several concurrent `init` calls, exactly one succeeds.
- Report an atom that does not have exactly one chokepoint as a verification problem.
- Add `run --no-capture` and `run(capture=False)` to retain no stdout or stderr. The atom
  records `output_capture: none` instead of log paths.
- Add `run --mask-arg POSITION` and `run(masked_arguments=[...])` to store a placeholder for
  chosen command tokens while the command still receives the real values.
- Add `run --substitute` and `run(substitute=True)` to replace `{KEY}` in command tokens
  with declared or pathway parameter values and record every parameter used, so one
  declaration both labels and configures a run.
- Add opt-in launch context: `run --context` records the platform and the git revision and
  dirty state, and `run --context-file PATH` records a file's size and SHA-256. Nothing is
  queried, executed, or read for context unless requested.
- Add `ocura-oss manifest`, `verify --against FILE`, and `verify(against=...)`. A manifest
  kept outside the state detects records later rewritten together with their checksums.
  `verify` also reports a digest of the manifest.
- State how the format evolves: readers ignore payload fields they do not recognize, so a
  schema version can gain optional fields without another break.
- Publish releases from GitHub Actions through PyPI Trusted Publishing.
- Keep the core runtime dependency-free.

## 0.4.0 - 2026-09-16

- Add `Store.read_verified_log()` to read stdout or stderr as bytes and verify
  their exact byte count and SHA-256 digest against the stored atom. Update the
  autoregressive report to use this method when consuming its saved output.

## 0.3.0 - 2026-09-08

- Add `init --json` and `run --json` for complete machine-readable command workflows.
  Execution JSON retains the existing exit codes and keeps subprocess output in logs.
- Escape Unicode in CLI JSON so paths and labels round-trip on legacy Windows encodings.
- Add a small repository-only autoregressive character-model example with optional PyTorch and JAX
  backends, an optional local Ray Core executor, a baseline/variant experiment,
  and a report reconstructed from saved evidence. Example scripts, requirements, and
  tests are excluded from both package distributions; documentation links to the repository.
- Explain local experiment workflows for terminal users, scripts, and AI agents;
  clarify recorded parameter labels and provide a repository CLI reference.
- Keep the core runtime dependency-free and preserve the existing state format.

## 0.2.2 - 2026-08-24

- Make automatic comparison select the newest chokepoint that has child pathways, while preserving `no_branch` for states with no branches.
- Retain valid log references during verification so checksum-mismatched logs receive one accurate problem report.

## 0.2.1 - 2026-08-24

- Point package metadata and repository documentation to the hosted Ocura OSS reference at `https://ocuna-ai.com/docs`.

## 0.2.0 - 2026-08-24

- Add a provisional typed Python API for initialization, execution, branching, comparison, verification, and demonstration.
- Export `Store` as the low-level state initialization, inspection, log-resolution, and verification interface; direct record writers remain internal.
- Publish a `py.typed` marker and package API reference.

## 0.1.1 - 2026-08-24

- Correct the installation guidance for the published PyPI package.

## 0.1.0 - 2026-08-24

- Initial public research package for recording, branching, verifying, and comparing trusted local command runs.
