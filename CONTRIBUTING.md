# Contributing to Ocura OSS

Thank you for contributing to Ocura OSS. Its scope is narrow, and focused changes support direct review and verification.

## Before you start

- Search the existing issues before opening a new one.
- Open an issue before proposing a new command, record field, or compatibility surface.
- Keep changes within the local `run -> evidence -> chokepoint -> branch -> rerun -> compare` concept.
- Do not include credentials, private records, generated `.ocura-oss/` state, or sensitive command output.

Bug reports are most useful when they include the operating system, Python version, command used, expected result, and observed result. Remove secrets from commands and logs before sharing them.

## Development setup

Create and activate a virtual environment, then install the package and development tools:

```console
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e . build mypy ruff twine
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`. On Linux or macOS, use `source .venv/bin/activate`.

## Checks

Run these checks before opening a pull request:

```console
python -m unittest discover -s tests
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/ocura_oss
python -m build
python -m twine check dist/*
```

The test suite uses `unittest`; pytest is not required. Please add or update tests when behavior changes, and update the README and changelog when a public command or record changes.

## Pull requests

A pull request should explain what changed, why the change belongs in this research package, and how it was verified. Keep unrelated cleanup separate so reviewers can evaluate the behavior directly.

Contributions accepted into this repository are released under MPL-2.0. Only submit work you have the right to contribute.
