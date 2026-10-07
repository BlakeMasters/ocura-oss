# Scripts and AI agents

Version 0.5.0.

Ocura OSS can be called from a shell, a Python program, or an AI agent's existing
command tool. Use it when you want a durable record connecting a baseline to later
variations, with reasons, outputs, and verification.

## Command workflow

1. Call `init --json` once and retain the project root and default pathway ID.
2. Execute the baseline through `run --json`; retain its `chokepoint_id`.
3. Call `branch --json --from ID --reason TEXT` with the proposed parameter labels.
4. Pass the returned branch `id` to the next `run --pathway ID --json`.
5. Read `verify --json` and `compare --json --from ID` before using the results.
6. Consume the referenced output through `Store.read_verified_log()` for the metrics your task needs.

All invocations must use the intended `--root`. The returned IDs connect the steps
without scraping text. Parameters label the record; set actual command arguments
separately. Handle nonzero exits: `run` still returns a recorded result for a failed
command, interruption, or launch failure. See [CLI](cli.md).

If a `run` process is killed before it returns a result, its attempt remains
unfinished. `attempts --json` lists it, `verify --json` reports it as a problem, and
`recover --json` closes it as `abandoned` once the command itself has stopped.

The [autoregressive example](../examples/autoregressive/README.md) is an executable
client of this interface, with PyTorch/JAX choices and an optional Ray Core executor.
The [Python API](python-api.md) provides the same operations with typed results.

## Consume saved output

A Python caller can read verified bytes directly from a retained atom ID:

```python
import json

from ocura_oss import Store

store = Store("experiment")
raw = store.read_verified_log("atom-<id>")
metrics = json.loads(raw.decode("utf-8"))  # For a workload that emits UTF-8 JSON.
```

The default stream is `stdout`; pass `stream="stderr"` to read diagnostics, including
output from a failed attempt. The method validates the stored atom and its lineage,
then checks containment, byte count, and SHA-256 for the exact bytes it returns.
Missing, unreadable, or inconsistent evidence raises `StoreError`.

This method reads the whole selected log into memory. It does not parse metrics,
verify the other stream, or verify the complete ledger. Keep the workflow's full
`verify` step; see [state and verification](state-and-verification.md) for the distinction.

## Example instruction for a caller

> Run a baseline and one proposed variation in the selected project. Record both
> through Ocura OSS, with matching command inputs and parameter labels. Branch from
> the baseline chokepoint and explain the change. Verify the evidence, compare the
> attempts, and cite the output logs used to interpret the result. Report failed
> attempts and missing evidence explicitly.

An AI agent can follow that instruction inside its existing execution environment.
The caller supplies command permissions and isolation. Ocura records and checks
local evidence; its checksums detect inconsistency without authenticating the writer.

## Revisiting an experiment

Records persist beyond a script or conversation. Save the source chokepoint ID and
use explicit `compare --from ID` to return to that baseline. Automatic comparison
selects the newest source with child pathways, which may be a different experiment
once more work is added.

Several processes may record runs under one root at the same time, and `verify` and
`compare --from ID` work while they do. Ocura OSS keeps its own records apart; the
commands still share the project root as their working directory, so keep their
workload files apart or give independent experiments separate roots. Ray workers in
the supplied example return their metrics to one recorded driver command.
