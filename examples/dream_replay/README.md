# Replay a training search with Ocura OSS

Which training run should get the next block of CPU time? This example records
small GPT training segments, replays allocation policies against their saved
results, then executes the selected policy on a fresh seed alongside a simple
baseline. Replay launches zero training or task-evaluation processes.

This is inspired by [Dream-RSI, section 3](https://arxiv.org/pdf/2609.14858).
It demonstrates a collect/replay/select/deploy cycle using fixed recipes and a
small policy catalogue. It does not reproduce the paper's agents, policy-code
generation, or performance results. One historical seed and one fresh seed show
the mechanism; they do not establish compute savings.

All model, checkpoint, scoring, and policy code lives in this optional repository
example. Ocura records commands, verifies their evidence, and records branch
lineage. Its core API, stored format, and dependencies are unchanged.

## Setup

Use a repository checkout and Python 3.12 (the initial training target).
The pinned PyTorch version may not provide wheels for newer Python versions.
Docker is not required; the example has been exercised directly on Windows with
CPU PyTorch.
From the repository root, in an activated virtual environment:

```console
python -m pip install ocura-oss==0.4.0
python -m pip install -r examples/dream_replay/requirements.txt --index-url https://download.pytorch.org/whl/cpu
python examples/dream_replay/experiment.py prepare --root .tmp-tests/dream-inputs --download
```

Preparation explicitly downloads about 1.1 MB of corpus plus the pinned nanoGPT
model definition and its MIT license. It verifies SHA-256 digests and stores these
files outside the source tree. Collection never silently downloads anything.
To use existing files instead, replace `--download` with
`--upstream PATH_TO_NANOGPT --data PATH_TO_INPUT_TXT`. The bytes must match the pins;
use `git -c core.autocrlf=false clone` for a checkout with unchanged line endings.
Keep `LICENSE.nanoGPT` alongside the prepared model file.

Sources are pinned in [common.py](common.py):

- [nanoGPT model](https://github.com/karpathy/nanoGPT/blob/3adf61e154c3fe3fca428ad6bc3818b27a3b8291/model.py),
  revision `3adf61e154c3fe3fca428ad6bc3818b27a3b8291`.
- [Tiny Shakespeare](https://github.com/karpathy/char-rnn/blob/6f9487a6fe5b420b7ca9afb0d7c078e37c1d1b4e/data/tinyshakespeare/input.txt),
  revision `6f9487a6fe5b420b7ca9afb0d7c078e37c1d1b4e`.

We use nanoGPT's small standalone model with an example-owned training loop. This is
an adaptation, including the optimizer configuration and 80/10/10 contiguous
train/validation/test split. Vocabulary comes from training characters, with ID
zero for unknown characters. Evaluation uses fixed windows within each split.
The example's own code uses the repository's [MPL-2.0 license](../../LICENSE).
Prepared third-party files retain their upstream provenance and are not bundled
with this repository or the package distributions.

## First run: smoke profile

```console
python examples/dream_replay/experiment.py collect --prepared .tmp-tests/dream-inputs --root .tmp-tests/dream-history --profile smoke --seed 101 --max-seconds 120
python examples/dream_replay/experiment.py replay --root .tmp-tests/dream-history --budget-quanta 8
python examples/dream_replay/experiment.py evaluate --history .tmp-tests/dream-history --root .tmp-tests/dream-fresh --seed 201 --budget-quanta 8 --max-seconds 120
python examples/dream_replay/experiment.py report --root .tmp-tests/dream-fresh/selected
```

Each command emits JSON to stdout; progress and diagnostics go to stderr. New
destinations must not exist. Keep the prepared inputs and checkpoints for actual
training continuation. The ledger alone supports score replay and reporting,
without importing PyTorch or reading model/checkpoint files.

`evaluate` freezes the selected policy and baseline in each fresh run's manifest.
The baseline is the best development result among uniform allocation, fixed recipe
ranking, successive halving, and current-loss greedy after opening all recipes.
If the selected policy is that same baseline, it executes once and reports the
shared result explicitly. Otherwise the arms run serially on the same fresh seed.
Only after choosing each arm's best revealed validation checkpoint does it measure
that checkpoint on the test split. The test score is never given to a policy.

This first version selects policies using one development history. Repeating
the walkthrough on several seeds is exploratory; a larger, independently held-out
evaluation protocol is needed for a benefit claim.

## Compute limits

Both profiles run serially, on CPU, with one PyTorch intra-op thread and one
inter-op thread, float32, deterministic algorithms, and no compilation or GPU use.
There are no background workers or automatic retries.

| Profile | Model | Work per quantum | Complete history |
| --- | --- | --- | --- |
| `smoke` (default) | 1 layer, 2 heads, width 32, context 16, batch 2 | 2 updates / 64 tokens | 16 segments / 1,024 training tokens |
| `cpu` | 2 layers, 4 heads, width 128, context 64, batch 8 | 32 updates / 16,384 tokens | 16 segments / 262,144 training tokens |

The smoke profile tests mechanics; it is too short to support a training-quality
or policy-benefit claim. The larger profile is an initial calibration candidate,
not a validated useful search benchmark. Use a new history directory with
`--profile cpu` when the machine is available.

`--max-seconds` bounds an invocation cooperatively: the controller checks between
segments and each worker checks between training updates and evaluation windows.
A worker gets at most 60 seconds and at most the remaining invocation allowance.
Imports, a single tensor operation, and checkpoint IO can overrun the allowance;
this is not a hard process deadline or resource scheduler. Ctrl+C interrupts the
current Ocura command and retains evidence. After a timeout, use `report` to
inspect the incomplete history. Restart in a new directory; automatic experiment
recovery is outside this example.

## Save/restore and evidence

Each completed segment writes a new checkpoint containing weights, AdamW state,
global update, CPU RNG state, and a dedicated batch generator's state. Configuration
and the full schedule horizon are bound by a manifest/recipe digest. The next
segment restores RNG state **after** constructing and loading the model/optimizer
and **before** drawing its next batch. There is no batch prefetch across boundaries.
Validation uses fixed windows and no training randomness.

The result records the checkpoint path and digest, input digest, update range,
token counts, loss, runtime versions, and timings. The next command:

1. Reads its parent's result with `Store.read_verified_log()`.
2. Branches from that parent's terminal evidence with `branch()`.
3. Reads the checkpoint, checks its digest, and deserializes those same bytes using
   PyTorch's `weights_only=True` loader.
4. Restores state and trains the next segment through Ocura's `run()`.

An Ocura chokepoint is a terminal evidence boundary. The example adapter owns the
model checkpoint. Ocura verifies records/logs; the adapter checks checkpoint bytes.
These are local consistency checks, not signatures or a sandbox. Use trusted local
inputs. The continuation test targets one fixed CPU/runtime configuration;
cross-version or cross-hardware bitwise reproducibility is not promised.

## Replay and costs

Policies receive immutable revealed observations and legal actions. They can
open the next recipe in a fixed per-seed order, continue an opened recipe by one
quantum, or stop. They receive neither the ledger nor hidden future/test scores.
This is a code interface for trusted policies, not isolation from hostile code.

The catalogue contains uniform round robin, a fixed recipe ranking learned on
development data, successive halving (rungs 1/2/4), and current-loss greedy with
initial exploration of 1, 2, or 4 recipes. Recipes vary warmup, cosine decay, and
dropout at the same model size and per-quantum token budget. Ties use fixed recipe
order; policy selection ties prefer the first candidate, uniform.

Selection minimizes the best revealed validation loss within the declared budget.
Every revealed ancestor costs one quantum. Failed attempts consume a full reserved
quantum and close their branch; their actual partial training token count is unknown.
Reports therefore distinguish **reserved training tokens** from measured command
seconds. Successful segment logs contain actual training/evaluation token counts.
Missing continuations produce a coverage error. Selection requires a complete
development collection; an incomplete history cannot supply invented outcomes.

Collection costs are real even for branches a replayed policy skips. Output includes
collection command time, replay time, and fresh command times separately. Preparation
download time is in `preparation.json`; Python/controller overhead is not included
in the summed command times. Historical durations are estimates of replayed work,
not future wall-time predictions. Retrospectively skipped work is not compute already
saved. Compare all costs before claiming a benefit.

## Tests

Validation on 2026-09-17: the standard-library evidence/policy checks and both real
PyTorch integration tests pass on Windows 11, Python 3.12.6, and PyTorch 2.6.0+cpu.
The continuation test took 42.4 seconds and the complete smoke-workflow test took
131.8 seconds on the development machine. These include fresh-process startup;
they are observations, not runtime guarantees. Exact continuation matched loaded
model, optimizer, and RNG state, including nonzero dropout and an interleaved branch.

An initial larger-profile calibration segment completed 32 updates in 4.9 seconds
end to end, with about 0.7 seconds of training. This supports bounded local testing;
it does not establish policy benefit or a general performance benchmark.

A retained `cpu` run also completed: 16 development segments on seed 101 in 115.7
seconds, six-policy replay in 1.25 seconds, and eight fresh segments plus test scoring
on seed 201 in 56.5 seconds. Fixed recipe ranking tied the adaptive policies during
development replay and was also the strongest simple baseline; fresh evaluation
therefore used one shared execution. This is workflow proof, with no measured
advantage over that baseline. The retained real-training reports also reconstructed
through an installed Ocura 0.4.0 wheel in an environment without PyTorch.

The normal test suite runs the evidence and policy checks without PyTorch:

```console
python -m unittest discover -s tests -p test_dream_replay_example.py -v
```

Optional real CPU tests use the prepared sources. On PowerShell:

```powershell
$env:OCURA_DREAM_TESTS = 'cpu'
$env:OCURA_DREAM_PREPARED = (Resolve-Path .tmp-tests/dream-inputs).Path
python -m unittest discover -s tests -p test_dream_replay_example.py -v
Remove-Item Env:OCURA_DREAM_TESTS, Env:OCURA_DREAM_PREPARED
```

They compare 128 uninterrupted updates with four fresh-process segments of 32,
using the tiny model with nonzero dropout and cosine LR; check exact loaded model,
optimizer, and RNG state; interleave another branch; and reject a modified checkpoint.
A second integration test exercises complete smoke collection, replay, fresh policy
execution, final test scoring, and restored reporting. Run these serially when local
compute is available. No larger-profile training test runs automatically.
