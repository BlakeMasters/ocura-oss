# Ocura OSS

Version 0.6.0.

A local execution ledger for recording, branching, and comparing command runs.

Record a baseline, explain the next variation, and keep its result connected to the
original run. Ocura OSS stores command arguments, outcomes, timing, output logs,
declared parameters, and branch lineage in your project. It verifies the relevant
records and logs before branching or comparing them.

Use it from your terminal, Python scripts, or an AI agent with a shell. The CLI
provides JSON output throughout the workflow; the Python API returns typed results.

## Why use it

Ocura OSS aims to be the easiest way to start recording runs in a new project,
especially for local development:

- **No dependencies.** The package uses only the Python standard library. There is
  no server, database, account, or configuration file.
- **Almost no setup.** `ocura-oss init` is the only setup step. Everything is kept
  as JSON records and logs in one `.ocura-oss/` directory inside your project.
- **Easy to hand to an agent.** Every command accepts `--json` and returns a
  documented exit code, so an AI agent with a shell can set it up and run the
  whole workflow.

## New in 0.6.0

- **Apache-2.0 license.** Version 0.6.0 and later are released under the Apache
  License 2.0. Versions 0.5.0 and earlier remain under MPL-2.0.
- **IDs can be shortened.** `branch --from`, `compare --from`, and `run --pathway`
  accept the first four or more hexadecimal characters of an ID when they match
  one record.
- **A run on a branch records the branch's parameters.** The child `run` no longer
  repeats them. A `--param` on the run still replaces the value for its key.
- **The first error says what to do.** A command run where there is no state names
  `ocura-oss init`, or the directory above that already holds state.

The record format is unchanged: 0.6.0 reads and extends state written by 0.5.0.

## New in 0.5.0

- **Unfinished runs stay visible.** Each run is journaled before its command starts.
  If the recording process is interrupted twice or killed, `ocura-oss attempts` shows
  the attempt and `ocura-oss recover` closes it as `abandoned` without inventing an outcome.
- **Several runs can share one root.** Concurrent `run` processes are supported, and
  `verify` and `compare` work while runs are in flight.
- **Sensitive values can stay out of records.** `run --no-capture` writes no output
  to disk, and `run --mask-arg` stores a placeholder for a chosen command token.
- **One declaration can label and configure a run.** `run --substitute` fills `{KEY}`
  in the command from the declared parameters and records what it used.
- **Launch context is available on request.** `run --context` records the platform and
  git revision; `--context-file` records a file's digest.
- **Later rewrites can be detected.** `ocura-oss manifest` prints checksums to keep
  elsewhere, and `verify --against` checks the state against them.
- **Verification is stricter.** Every atom must have exactly one chokepoint.
- **Format changes follow a stated rule.** Readers ignore fields they do not know,
  so later releases can add optional fields without another format break.

The core runtime still uses only the standard library. See
[state and verification](state-and-verification.md) for the format, attempts,
concurrent use, and the limits of each option.

**Upgrading from 0.4:** version 0.5.0 changed the record format to schema 2. Versions
0.5.0 and later cannot read or convert a `.ocura-oss/` directory written by 0.4 or earlier. Commands pointed
at old state stop with exit status 2 and leave it untouched. Stay on 0.4 for a project
whose records you still need (`python -m pip install "ocura-oss<0.5"`), or move the old
directory aside and run `ocura-oss init`. See
[upgrading from 0.4](state-and-verification.md#upgrading-from-04) before you upgrade.

## Install and try

Python 3.11 or later is required. Ocura OSS uses only the standard library at runtime.

Install from [PyPI](https://pypi.org/project/ocura-oss/) in a virtual environment:

```console
python -m pip install ocura-oss
ocura-oss demo --root ./ocura-oss-demo
```

The demo runs the complete workflow and retains a new directory for inspection.
Its destination must not already exist. To install a repository checkout instead,
run `python -m pip install .` from its root.

## Compare two runs

```console
ocura-oss init --name example --json
ocura-oss run --json --param count=1 -- python -c "print(1)"
ocura-oss branch --json --from <chokepoint-id> --reason "try count 2" --param count=2
ocura-oss run --json --pathway <child-pathway-id> -- python -c "print(2)"
ocura-oss verify --json
ocura-oss compare --json --from <chokepoint-id>
```

Use the baseline's `chokepoint_id` to branch and the branch result's `id` for the
child run. When typing an ID by hand, its first four or more hexadecimal characters
are enough if they match one record. `compare` reports each variant's relationship
to its source, parameter changes, outcomes, and timing. Read the recorded output
logs for workload-specific metrics, such as validation loss or accuracy.

**Parameters are recorded labels.** Set actual inputs in your command as well:
`--param batch=4 -- python train.py --batch 4`. A run on a branch records the
branch's parameters without repeating them, and can add or replace labels with its
own `--param`. To declare a value once, add `--substitute` and write `"{batch}"` in
the command.

Omit `--json` for terminal output. By default, `run` streams the command's output
while retaining stdout and stderr logs; `--json` or `--quiet` keeps that output in
the logs without streaming. A first Ctrl+C records an interrupted attempt; a
second exits immediately and leaves the attempt for `ocura-oss recover` to close.

## Autoregressive example: PyTorch, JAX, and Ray

The [autoregressive example](../examples/autoregressive/README.md) trains a small
character-level next-token model on an included text corpus. Choose PyTorch or
JAX, run a baseline and a longer-training variant, then reconstruct the comparison
from saved evidence. An optional Ray Core executor runs either backend as a local
task. The example stays in the source repository; neither the wheel nor the source
distribution includes its scripts or framework dependencies. Install only what you use.

From a repository checkout, after installing Ocura:

```console
python -m pip install -r examples/autoregressive/requirements-pytorch.txt
python examples/autoregressive/experiment.py run --backend pytorch --root ./ar-pytorch
python examples/autoregressive/experiment.py report --root ./ar-pytorch
```

The guide includes JAX and Ray commands. The script supplies metric reading and
interpretation; the package supplies recording, lineage, and verification.

## Python and automation

```python
import sys
from ocura_oss import branch, compare, initialize, run, verify

root = initialize("experiment", name="example").root
baseline = run([sys.executable, "-c", "print(1)"], root=root, parameters={"count": "1"})
child = branch(baseline.chokepoint.id, root=root, reason="try count 2", parameters={"count": "2"})
run([sys.executable, "-c", "print(2)"], root=root, pathway_id=child.id, parameters={"count": "2"})
assert verify(root).ok
comparison = compare(baseline.chokepoint.id, root=root)
```

Use `Store(root).read_verified_log(atom_id, stream="stdout")` to consume verified
output bytes. The method also accepts `stream="stderr"`; use `verify(root)` for a
complete state check. See the [Python API](python-api.md#storeread_verified_log).

An agent can use the same CLI or Python workflow inside its existing execution
environment. The [automation guide](automation.md) explains JSON results,
exit codes, parameter labels, and how to inspect earlier attempts.

## Documentation

- [Scripts and AI agents](automation.md): JSON workflows and verified output consumption
- [CLI reference](cli.md): every command, JSON fields, and exit codes
- [Python API](python-api.md): functions, typed results, and `Store`
- [State and verification](state-and-verification.md): records and checks
- [Autoregressive example](../examples/autoregressive/README.md): PyTorch, JAX, and Ray
- [Hosted documentation](https://ocuna-ai.com/docs): the currently published release

Repository references describe this checkout and remain readable directly on GitHub.

## Local state and operating model

State lives under `.ocura-oss/` in the project root. A **den** identifies that state;
a **pathway** groups runs in a lineage; an **atom** records one command attempt;
a **chokepoint** identifies terminal evidence from which a branch can be created.

Each branch records its source and reason. It represents a new line of work;
workspace files, process state, and model weights are managed by the workload.
Several processes may record runs under one state root at once. Delete `.ocura-oss/`
to discard that project's records. State from 0.4 and earlier, and legacy `.ocura/`
records, are unsupported.

Commands run with `shell=False` in the project root and inherit the invoking
environment's permissions. Use trusted, owner-authorized workloads. For generated
or otherwise untrusted commands, provide isolation and permissions through your
execution environment; Ocura OSS itself does not sandbox or restrict them.

Records retain command arguments, parameter labels, reasons, and output. Keep secrets
out of these fields; `run --mask-arg` and `run --no-capture` cover a command token or
output that cannot be avoided. SHA-256 checksums detect local inconsistencies; they do not
authenticate authorship or prevent a writer from replacing records and checksums.

## Project status

Ocura OSS is research software under the [Apache License 2.0](../LICENSE).
Versions 0.5.0 and earlier were released under MPL-2.0. The 0.x interface and record
format may change before 1.0, and there is no production support commitment.

Bug reports and focused pull requests are welcome. See [Contributing](../CONTRIBUTING.md)
and the [security policy](../SECURITY.md).

## Command help

The installed command also provides command-specific usage:

```console
ocura-oss --help
ocura-oss run --help
```
