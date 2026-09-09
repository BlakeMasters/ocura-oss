# Ocura OSS

A local execution ledger for recording, branching, and comparing command runs.

Record a baseline, explain the next variation, and keep its result connected to the
original run. Ocura OSS stores command arguments, outcomes, timing, output logs,
declared parameters, and branch lineage in your project. It verifies the relevant
records and logs before branching or comparing them.

Use it from your terminal, Python scripts, or an AI agent with a shell. The CLI
provides JSON output throughout the workflow; the Python API returns typed results.

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

Omit `--json` for terminal output. By default, `run` streams the command's output
while retaining stdout and stderr logs; `--json` or `--quiet` keeps that output in
the logs without streaming. A first Ctrl+C records an interrupted attempt; a
second exits immediately and may leave that attempt unrecorded.

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

An agent can use the same CLI or Python workflow inside its existing execution
environment. The [automation guide](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/automation.md) explains JSON results,
exit codes, parameter labels, and how to inspect earlier attempts.

## Documentation

- [CLI reference](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/cli.md): every command, JSON fields, and exit codes
- [Python API](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/python-api.md): functions, typed results, and `Store`
- [State and verification](https://github.com/BlakeMasters/ocura-oss/blob/main/docs/state-and-verification.md): records and checks
- [Autoregressive example](https://github.com/BlakeMasters/ocura-oss/blob/main/examples/autoregressive/README.md): PyTorch, JAX, and Ray
- [Hosted documentation](https://ocuna-ai.com/docs): the currently published release

Repository references describe this checkout and remain readable directly on GitHub.

## Local state and operating model

State lives under `.ocura-oss/` in the project root. A **den** identifies that state;
a **pathway** groups runs in a lineage; an **atom** records one command attempt;
a **chokepoint** identifies terminal evidence from which a branch can be created.

Each branch records its source and reason. It represents a new line of work;
workspace files, process state, and model weights are managed by the workload.
One mutating process per state root is supported at a time. Delete `.ocura-oss/`
to discard that project's records. Legacy `.ocura/` records are unsupported.

Commands run with `shell=False` in the project root and inherit the invoking
environment's permissions. Use trusted, owner-authorized workloads. For generated
or otherwise untrusted commands, provide isolation and permissions through your
execution environment; Ocura OSS itself does not sandbox or restrict them.

Records retain command arguments, parameter labels, reasons, and output. Keep secrets
out of these fields. SHA-256 checksums detect local inconsistencies; they do not
authenticate authorship or prevent a writer from replacing records and checksums.

## Project status

Ocura OSS is research software under [MPL-2.0](https://github.com/BlakeMasters/ocura-oss/blob/main/LICENSE). The 0.x interface and record
format may change before 1.0, and there is no production support commitment.

The package adapts an early Ocura research concept. Current Ocura engine development
is a separate project.

Bug reports and focused pull requests are welcome. See [Contributing](https://github.com/BlakeMasters/ocura-oss/blob/main/CONTRIBUTING.md)
and the [security policy](https://github.com/BlakeMasters/ocura-oss/blob/main/SECURITY.md).
