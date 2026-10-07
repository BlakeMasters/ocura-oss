# Ledger written by Ocura OSS 0.5.0

`state/` is the `.ocura-oss/` directory of a small project recorded with 0.5.0
(schema version 2). `tests/test_fixture_compat.py` copies it into a temporary
project and checks that the current code still verifies and reads it.

This fixture is frozen. Do not regenerate or edit it. Every release that keeps
schema version 2 must read these exact bytes; if a change makes that impossible,
it is a format break and needs a new schema version and a new fixture beside
this one.

It holds two pathways and five runs: a passing baseline, a failed run, a launch
failure, an attempt closed as `abandoned`, and a run recorded without output
capture and with one masked argument.
