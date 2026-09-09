# Scripts and AI agents

Ocura OSS can be called from a shell, a Python program, or an AI agent's existing
command tool. Use it when you want a durable record connecting a baseline to later
variations, with reasons, outputs, and verification.

## Command workflow

1. Call `init --json` once and retain the project root and default pathway ID.
2. Execute the baseline through `run --json`; retain its `chokepoint_id`.
3. Call `branch --json --from ID --reason TEXT` with the proposed parameter labels.
4. Pass the returned branch `id` to the next `run --pathway ID --json`.
5. Read `verify --json` and `compare --json --from ID` before using the results.
6. Read the referenced logs for the metrics your task needs.

All invocations must use the intended `--root`. The returned IDs connect the steps
without scraping text. Parameters label the record; set actual command arguments
separately. Handle nonzero exits: `run` still returns a recorded result for a failed
command, interruption, or launch failure. See [CLI](cli.md).

The [autoregressive example](https://github.com/BlakeMasters/ocura-oss/blob/main/examples/autoregressive/README.md) is an executable
client of this interface, with PyTorch/JAX choices and an optional Ray Core executor.
The [Python API](python-api.md) provides the same operations with typed results.

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

Serialize writes to a shared root. For parallel independent experiments, use separate
project roots and manage workload files separately. Ray workers in the supplied
example return their metrics to the recorded driver command; they never mutate
the same Ocura root concurrently.
