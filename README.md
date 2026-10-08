# Ocura OSS

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
ocura-oss run --json --pathway <child-pathway-id> --param count=2 -- python -c "print(2)"
ocura-oss verify --json
ocura-oss compare --json --from <chokepoint-id>
```

Use the baseline's `chokepoint_id` to branch and the branch result's `id` for the
child run. `compare` reports each variant's relationship to its source, parameter
changes, outcomes, and timing. Read the recorded output logs for workload-specific
metrics, such as validation loss or accuracy.

**Parameters are recorded labels.** Set actual inputs in your command as well:
`--param batch=4 -- python train.py --batch 4`. Each run records its own labels.
To declare a value once, add `--substitute` and write `"{batch}"` in the command.

Omit `--json` for terminal output. By default, `run` streams the command's output
while retaining stdout and stderr logs; `--json` or `--quiet` keeps that output in
the logs without streaming. A first Ctrl+C records an interrupted attempt; a
second exits immediately and leaves the attempt for `ocura-oss recover` to close.

## Autoregressive example: PyTorch, JAX, and Ray

The [autoregressive example](https://github.com/BlakeMasters/ocura-oss/blob/main/examples/autoregressive/README.md) trains a small
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

Use `Store(root).read_verified_log(atom_id, stream="stdout")` to consume recorded
output as bytes checked against the stored atom's byte count and SHA-256 digest.
The method also accepts `stream="stderr"`; decoding and metric interpretation
remain with your script. See the [Python API](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/python-api.md#storeread_verified_log).

An agent can use the same CLI or Python workflow inside its existing execution
environment. The [automation guide](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/automation.md) explains JSON results,
exit codes, parameter labels, and how to inspect earlier attempts.

## Documentation

- [Overview](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/index.md): installation, concepts, and the complete workflow
- [CLI reference](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/cli.md): every command, JSON fields, and exit codes
- [Python API](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/python-api.md): functions, typed results, and `Store`
- [Scripts and AI agents](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/automation.md): JSON workflows and verified output consumption
- [State and verification](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/state-and-verification.md): records and checks
- [Autoregressive example](https://github.com/BlakeMasters/ocura-oss/blob/main/examples/autoregressive/README.md): PyTorch, JAX, and Ray
- [Hosted documentation](https://ocuna-ai.com/docs): the currently published release

The full references are included in this checkout and remain readable directly on GitHub.

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

## Upgrading from 0.4

**Version 0.5 cannot read a `.ocura-oss/` directory written by 0.4 or earlier, and does
not convert one.** Commands pointed at old state stop with exit status 2 and say so;
the directory is left untouched. To keep working with existing records, stay on 0.4
for that project (`python -m pip install "ocura-oss<0.5"`). To record with 0.5, move
the old directory aside and run `ocura-oss init`. The
[upgrade notes](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/state-and-verification.md#upgrading-from-04)
cover each case.

## Project status

Ocura OSS is research software under [MPL-2.0](https://github.com/BlakeMasters/ocura-oss/blob/main/LICENSE). The 0.x interface and record
format may change before 1.0, and there is no production support commitment.

Bug reports and focused pull requests are welcome. See [Contributing](https://github.com/BlakeMasters/ocura-oss/blob/main/CONTRIBUTING.md)
and the [security policy](https://github.com/BlakeMasters/ocura-oss/blob/main/SECURITY.md).
