# Changelog

This file records user-visible changes to Ocura OSS.

## Unreleased

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
