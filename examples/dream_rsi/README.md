# Dream-RSI training adaptation with Ocura OSS

This example studies implementation support: how Ocura OSS helps developers build
and inspect a Dream-RSI workflow. A small, bounded CPU training experiment exercises
the integration; it does not establish better performance or resource savings.

A discovery agent writes learning-rate schedules. A second agent revises the
policy that chooses which training branch to pursue. Replay evaluates those
policies against saved discovery histories before selection and fresh deployment.
Training runs locally on CPU; agent inference uses the external Codex service.
The completed pilot exercised this loop with 15 training attempts and four held-out
evaluations. See the [results and limitations](RESEARCH_NOTES.md).

This is an independent adaptation of [Dream-RSI, section 3](https://arxiv.org/pdf/2609.14858)
to a small nanoGPT model on Tiny Shakespeare, not a reproduction of the paper's
tasks or reported gains. The earlier [fixed-recipe example](../dream_replay/README.md)
demonstrates checkpointing and replay without a live agent.

## Package integration

Both agent requests and training jobs pass through `record_command()` in
[experiment.py](experiment.py). It uses Ocura's public APIs:

| Operation | Package support |
| --- | --- |
| Record a command and its outcome | `run()` retains arguments, status, timing, stdout, and stderr. |
| Link an attempt to its predecessor | `branch()` records the parent execution. |
| Read recorded output | `Store.read_verified_log()` checks byte counts and SHA-256 before parsing. |
| Reconstruct a report | `Store.verify_state()` and verified logs supply checked inputs to [rsi_report.py](rsi_report.py). |

The example owns agent access, checkpoints, artifact digests, resource limits,
replay, scoring, and policy selection. Ocura lineage identifies execution
relationships; it does not restore model state. The example uses Ocura OSS 0.4.0
and stays outside the package distributions and runtime dependencies.

## Setup

Use a repository checkout and an activated Python 3.12 virtual environment.
The live runner supports Windows and Linux host-capacity checks. Other operating
systems defer when that check is unavailable. Docker and a GPU are not required.

Install the released package from the repository root:

```console
python -m pip install ocura-oss==0.4.0
python -m unittest discover -s tests -p "test_dream_rsi_*.py" -v
```

These default tests use synthetic agents and evaluators: no remote calls, dataset
downloads, or training. The optional real training check is skipped by default.
Reports and replay need only Ocura; training additionally needs CPU PyTorch:

```console
python -m pip install -r examples/dream_replay/requirements.txt --index-url https://download.pytorch.org/whl/cpu
python examples/dream_replay/experiment.py prepare --root .tmp-tests/dream-inputs --download
```

Preparation explicitly downloads and verifies the pinned nanoGPT model definition,
its MIT license, and about 1.1 MB of Tiny Shakespeare text. Reuse an existing
prepared directory instead of preparing it again. The [source revisions and
existing-file option](../dream_replay/README.md#setup) are shared with the fixed-recipe
example. The example's own code is covered by the repository's [MPL-2.0 license](../../LICENSE);
downloaded inputs retain their upstream provenance and are not redistributed here.

## Run

A live run requires a signed-in Codex CLI on `PATH` and consumes your account's
agent usage. The adapter uses `codex exec` with JSON events, a response schema,
`--ignore-user-config`, `--ephemeral`, and a read-only sandbox. Check that your CLI
supports these flags before running. The adapter removes `OPENAI_API_KEY` and
`CODEX_API_KEY` from child-process environments; it uses the CLI's existing sign-in.
Select a model available to your account in place of `MODEL`.

For a small mechanics run, use the smoke model and two updates per attempt:

```console
python examples/dream_rsi/experiment.py run --root .tmp-tests/dream-rsi-smoke --prepared .tmp-tests/dream-inputs --profile smoke --model MODEL --effort low --seed 101 --rounds 2 --attempts 2 --updates 2 --policy-revisions 1 --max-agent-calls 7 --max-seconds 480
python examples/dream_rsi/experiment.py report --root .tmp-tests/dream-rsi-smoke
```

This permits up to six discovery calls and one policy revision: a shared bootstrap,
one fresh adaptive world, and one independent fixed-control world. A revision can
lose or tie, leaving the incumbent in place. The smoke profile does not imply
cheap agent calls; request startup and generation can dominate this tiny workload.

For the published pilot's training configuration, use a new destination:

```console
python examples/dream_rsi/experiment.py run --root .tmp-tests/dream-rsi-pilot --prepared .tmp-tests/dream-inputs --profile cpu --model MODEL --effort low --seed 5101 --rounds 3 --attempts 3 --updates 16 --policy-revisions 1 --max-agent-calls 17 --max-seconds 480
python examples/dream_rsi/experiment.py report --root .tmp-tests/dream-rsi-pilot
```

This permits nine adaptive discovery attempts and six independent control attempts,
including the shared bootstrap, plus two policy-agent requests. It reproduces the
experiment configuration, not the historical agent generations or results. Seeding
the training worker does not make a remote agent deterministic.

Every destination must be new. There is no automatic resume. Keep an interrupted
directory for inspection and use another directory for the next run. Commands emit
JSON. Reporting reconstructs the retained work without invoking an agent or
importing PyTorch; keep the complete run directory, including requests and
checkpoints. Training also needs the prepared inputs.

## Resource limits

Training is serial, with one CPU worker and one training thread. Before each agent
request or evaluator operation, the runner checks host capacity. Defaults require
at least 3,072 MiB free RAM and host load at most 50%. Windows uses processor load;
Linux uses one-minute load average divided by logical CPU count, so these are
different proxies for contention. A busy check waits at most 60 seconds inside
the overall allowance, then defers.

Agent and evaluator deadlines default to 90 and 60 seconds. The commands above
explicitly choose a 480-second overall allowance and low reasoning effort. Bare
CLI defaults are larger: 900 seconds, medium effort, two policy revisions between
rounds, and 20 agent calls. The manifest freezes the chosen configuration.
Process startup and cleanup can extend elapsed time around a deadline. These
checks are local safeguards, not a scheduler or a compute reservation.

## Replay

The first rollout uses a fixed policy: extend a branch to depth two, then reopen
the root. Later adaptive rounds use the highest-scoring evaluated policy; the
comparison arm retains the fixed policy and its own discovery history. Paired
arms use the same initialization seed and separate executions after the bootstrap.

A policy defines a stateless `choose(observation)` function. It returns distinct
legal node IDs, or an empty list to stop. The root starts a new branch; a leaf
continues one. Replay reveals only recorded outcomes: the root's earliest unseen
child or a selected leaf's recorded child. A missing continuation reveals nothing
but still consumes a decision round. Replay never generates or trains a candidate.

Selection averages the section 3 objective across available adaptive histories:

```text
score = best_revealed_score - beta_cost * N
        + beta_parallel * N / max(1, decision_rounds)
```

Scores are negative validation losses, including the initial root. `N` counts
revealed non-root attempts. Coefficients are fixed for each experiment; defaults
are `beta_cost=0.01` and `beta_parallel=0`. The interface supports batches, but this
runner has one worker and demonstrates no parallel speedup.

Selecting against the incumbent prevents a lower score on those same replay
histories. It guarantees neither better fresh outcomes nor improved training.
Missing continuations can change the cost term without improving any result;
the [pilot analysis](RESEARCH_NOTES.md#results) documents this case.

## Checkpoints and evidence

A continuation restores model parameters, AdamW state, the update counter, CPU
random state, and the batch generator. The adapter verifies checkpoint bytes
before loading them. Held-out tests run only after all discovery and policy calls
finish, on checkpoints chosen using validation. Agents never receive test results.

The opt-in CPU test compares split and continuous training across a schedule
change, checks exact loaded model/optimizer/random states, preserves checkpoint
bytes during held-out evaluation, and rejects a corrupted checkpoint. In PowerShell:

```powershell
$env:OCURA_DREAM_RSI_TRAINING_TEST = '1'
$env:OCURA_DREAM_RSI_PREPARED = (Resolve-Path .tmp-tests/dream-inputs).Path
python -m unittest discover -s tests -p "test_dream_rsi_training.py" -v
Remove-Item Env:OCURA_DREAM_RSI_TRAINING_TEST, Env:OCURA_DREAM_RSI_PREPARED
```

This targets one CPU/runtime configuration, not bitwise equality across hardware
or PyTorch releases. Source restrictions and operation/time budgets constrain
accidental runaway code; they are not a security sandbox. Use trusted local inputs.
Ocura checks local consistency, not authorship or safety of arbitrary code.

Run directories retain prompts, agent events, commands, output, source, and paths.
Inspect and redact them before sharing. The [public evidence summary](evidence-summary.json)
contains selected measurements, not a portable execution ledger. It cannot by
itself reconstruct or independently verify the historical pilot.
