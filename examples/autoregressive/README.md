# Small autoregressive experiment

Train a character-level next-token model with **PyTorch** or **JAX**, either directly
or inside a **Ray Core task**. Ocura OSS records the baseline, the reason for a
variation, both outputs, and their lineage. Inspect the experiment later without
training again.

## What runs

The included corpus has nine short original training sentences and three distinct
validation sentences. An eight-character context is one-hot encoded, passed through
a 32-unit tanh layer, and projected to character logits. Full-batch SGD minimizes
next-character cross-entropy. Generation starts with `red ` and feeds each predicted
character back into the context until a newline or 40 new characters.

This small finite-context autoregressive model uses CPU execution and included data.
PyTorch and JAX share the data, initialization, objective, and update rule. Each
baseline and variant starts from the same seeded initialization; the branch connects
their records while the workload manages model state.

Defaults compare **40 training steps against 120**, with learning rate `0.5` and
seed `7`. The report includes training/validation loss and generated text. The
longer run's changed training budget explains any improvement. Results describe
this tiny corpus and configuration. Command timing includes imports, JIT compilation,
and (when selected) Ray startup, so use it as a workflow example rather than a
framework speed benchmark.

## Install only the backend you want

From a repository checkout, use a virtual
environment with Python 3.11 or later, subject to your framework's platform support:

```console
python -m pip install .
```

Scripts and requirements live only in this repository; neither the wheel nor the
source distribution includes them. The core package has no PyTorch, JAX, or Ray
dependency. An installed package can run a separately copied
`examples/autoregressive` directory unchanged.

For PyTorch:

```console
python -m pip install -r examples/autoregressive/requirements-pytorch.txt
python examples/autoregressive/experiment.py run --backend pytorch --root ./ar-pytorch
```

For JAX:

```console
python -m pip install -r examples/autoregressive/requirements-jax.txt
python examples/autoregressive/experiment.py run --backend jax --root ./ar-jax
```

Follow the [PyTorch installation guide](https://pytorch.org/get-started/locally/) or
[JAX installation guide](https://docs.jax.dev/en/latest/installation.html) for your
platform. Optional backends have their own Python/OS support requirements.

## Ray option

Add Ray to either backend installation:

```console
python -m pip install -r examples/autoregressive/requirements-ray.txt
python examples/autoregressive/experiment.py run --backend pytorch --executor ray --root ./ar-ray-pytorch
python examples/autoregressive/experiment.py run --backend jax --executor ray --root ./ar-ray-jax
```

Use the command for your installed backend. Each recorded command starts a fresh
local Ray runtime with one CPU, submits one training task with retries disabled,
receives its metrics, and shuts down in `finally`. It explicitly starts locally
instead of attaching to an existing cluster. This demonstrates the
[Ray Core task interface](https://docs.ray.io/en/latest/ray-core/tasks.html).

Ocura records the driver command and its returned metrics. Ray manages its worker
and internal logs. Worker console forwarding is disabled; the returned task result
is printed as the driver's recorded JSON output. Only the outer command writes
Ocura state. This example uses single-machine execution; Ray Train, Tune, and
multi-node configurations are outside its tested workflow.

## Read the saved result

```console
python examples/autoregressive/experiment.py report --root ./ar-pytorch
ocura-oss compare --root ./ar-pytorch --json
ocura-oss verify --root ./ar-pytorch --json
```

Reporting needs only Ocura OSS and the standard library. It verifies the records,
reads both stdout logs, checks that reported settings match the recorded labels,
and calculates `variant.validation_loss - baseline.validation_loss`. A negative
delta means lower validation loss. The generic `ocura-oss compare` command returns
execution summaries and parameter deltas; the example supplies metric interpretation.

The report includes the source chokepoint ID, atom IDs, and log paths. It expects
the example's single baseline/variant pair. If you extend the root with other
branches, use the CLI/API to select the intended source explicitly.

## Vary the experiment

```console
python examples/autoregressive/experiment.py run --backend pytorch --root ./ar-more-steps --baseline-steps 80 --variant-steps 160 --learning-rate 0.3 --seed 9
```

Each run destination must be new. State and logs are retained even after a failed
attempt. Inspect the log paths in an error result for workload diagnostics. The
driver passes every setting to the command as well as recording its parameter label.

A single workload can also run directly:

```console
python examples/autoregressive/train.py --backend pytorch --steps 40
python examples/autoregressive/ray_train.py --backend jax --steps 40
```

## Automate and test

The experiment driver uses `python -m ocura_oss` and parses JSON results. It is a
normal script; an AI agent with a shell can drive the same commands. See the
[automation guide](../../docs/automation.md) for caller instructions and exit handling.

Core tests do not require frameworks. To exercise both real backends, install both
backend requirement files and set `OCURA_OSS_EXAMPLE_TESTS=local`. Set it to `ray`
for both Ray variants, or `all` for all four combinations, then run:

```console
python -m unittest discover -s tests -p test_autoregressive_example.py
```

The integration tests check real training, lineage, matching configuration labels,
reconstruction in a fresh process, refusal to overwrite an existing experiment,
and rejection of a modified log. They require the example sources alongside tests.
