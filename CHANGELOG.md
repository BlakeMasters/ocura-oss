# Changelog

This file records user-visible changes to Ocura OSS.

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
