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
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m mypy src/ocura_oss
python tools/check_release.py
python -m build
python -m twine check dist/*
```

Continuous integration runs the tests on Linux, macOS, and Windows for each supported
Python version, and runs the remaining checks once. It then installs the built wheel
and the built source distribution on all three systems and runs the tests against the
installed copy. It also runs the README's commands in bash and in both PowerShell
versions, using `.github/scripts/quickstart.sh` and `.github/scripts/quickstart.ps1`.
When you change a command that the README shows, update those two scripts to match.

The test suite uses `unittest`; pytest is not required. Please add or update tests when behavior changes, and update the README and changelog when a public command or record changes.

When changing examples, also run Ruff on `examples/` and use the optional integration
checks described in [the autoregressive guide](https://github.com/BlakeMasters/ocura-oss/blob/main/examples/autoregressive/README.md).
Continuous integration lints `examples/` and runs the PyTorch and JAX check on Linux;
it does not run the Ray check.
The normal test suite keeps framework imports optional. Examples, their requirements,
and their tests stay in the repository and are excluded from both distributions.
The core package stays dependency-free.

## Pull requests

A pull request should explain what changed, why the change belongs in this research package, and how it was verified. Keep unrelated cleanup separate so reviewers can evaluate the behavior directly.

Contributions accepted into this repository are released under the Apache License 2.0. Only submit work you have the right to contribute.

## Releases

The package version is declared once, as `__version__` in `src/ocura_oss/__init__.py`.
The newest `CHANGELOG.md` heading and the `Version X.Y.Z.` line in each reference
document must name the same version; `python tools/check_release.py` reports any
that do not. A changelog entry may be dated `Unreleased` until its release.

To release, a maintainer:

1. Sets the version, the changelog heading and date, and the document version lines,
   then merges that change to `main`.
2. Tags the merged commit `vX.Y.Z` and pushes the tag.

The release workflow then repeats the checks against the tag, builds the wheel and
source distribution, publishes them to PyPI through Trusted Publishing, and creates
the GitHub release from the changelog entry. It also fails if the wheel gains a
runtime dependency, contains files outside the package, or exceeds its size budget.
