# Dream-RSI pilot: results and limitations

This study asks whether Ocura OSS supports implementing and inspecting a Dream-RSI
workflow. The bounded training experiment exercises that integration; it is not
designed to establish a training-performance or resource-efficiency improvement.
No comparative developer-time measurement was made.

The completed pilot used Ocura's public `run()`, `branch()`,
`Store.read_verified_log()`, and `Store.verify_state()` APIs. Agent proposals,
training evaluations, replay selection, fresh deployment, and report reconstruction
all passed through the same execution and evidence interface. The optional
example owns the agents, training state, simulator, scoring, and resource limits.

The [runnable recipe](README.md) accompanies a [curated measurement summary](evidence-summary.json).
Measurements below come from retained local reports and a separate reconstruction
audit performed on September 22, 2026. Raw execution ledgers, checkpoints, and agent
responses are not included in this repository. The summary cannot independently
verify the historical run; the recipe produces verifiable records for new runs.

## Configuration

The workload adapted [Dream-RSI, section 3](https://arxiv.org/pdf/2609.14858) to
learning-rate schedule search using a fixed nanoGPT model and Tiny Shakespeare.
It used the pinned sources in [common.py](../dream_replay/common.py), CPU PyTorch
2.6.0, two layers, four attention heads, width 128, context 64, and batch size eight.
Each attempt trained for 16 updates with one CPU worker. Agents saw validation
feedback only; test checkpoints were selected by validation after agent work ended.

Training ran locally; agent inference used the external Codex service. Discovery
and exploration-policy roles both used low reasoning effort. The run allowed three
adaptive rounds, three discovery attempts per world, and one policy revision
between adaptive rounds. A shared bootstrap was followed by two fresh adaptive
worlds and two independent fixed-control worlds with paired seeds.

Limits were 17 agent requests, 90 seconds per agent, 60 seconds per evaluator,
and 480 seconds overall. Capacity checks required host load at most 50% and at
least 3,072 MiB free RAM, with at most 60 seconds of waiting within the overall
allowance. Historical agent deployment details are not a reproducibility target;
the recipe requires an explicitly selected model available to the reader.

## Results

All five worlds, 15 training attempts, two policy revisions, and four held-out
evaluations completed. The first revision won selection on bootstrap replay and
guided fresh training. The second tied with it, so the incumbent was retained.
Reconstruction verified 80 execution records, 160 logs, and 20 checkpoints.
The separate audit reproduced the canonical report, checked six source archives,
and confirmed identical initial checkpoint bytes within each paired seed.

| Seed | Selected policy validation loss | Fixed control validation loss | Selected policy test loss | Fixed control test loss |
| --- | --- | --- | --- | --- |
| 5102 | 2.645549 | 2.703233 | 2.707264 | 2.753831 |
| 5103 | 2.699420 | 2.770822 | 2.734298 | 2.824170 |

Under these bounded conditions, the adaptation produced little evidence of useful
policy improvement. The selected policy spent all 48 updates continuing one
branch. The fixed control trained a branch for 32 updates and spent the remaining
16 on a restart. The lower test losses are therefore consistent with simply
training one branch longer.

The selected policy tied a simple greedy rule in replay. Its initial replay-score
gain of 0.01, from -2.724722 to -2.714722, did not come from a better training
result. It requested an unrecorded continuation on the final replay round, leaving
two revealed attempts instead of three to count against the cost term. In the
live run, that choice generated and trained a real third candidate. The replay
gain did not establish a more effective search strategy or saved computation.

These short searches on a tiny model and two fresh comparison seeds may offer too
few meaningful choices for policy evolution to help. Greedy and always-continue
policies were not evaluated as separate live controls. The generated policy also
lacked a guard for failed nodes with null scores; all pilot attempts succeeded,
so general failure robustness was not demonstrated. These results describe this
training adaptation, not the paper's original tasks or longer searches.

## Cost

The final pilot took 475.875 seconds. Agent commands took 229.050 seconds and
evaluator commands took 123.330 seconds, including 6.439 seconds of training loops
over 122,880 training tokens. All 17 agent calls reported usage: 236,107 input
tokens, including 96,000 cached, and 7,430 output tokens.

Development also included a deadline-limited run, a completed run whose policy
requests timed out, a policy-only diagnostic, and two capacity-deferred runs.
The aggregate includes the final pilot:

| Measurement | Six development runs and diagnostics |
| --- | --- |
| Agent calls | 54 |
| Completed training attempts | 43 |
| Agent command time | 1,477.737 s |
| Evaluator command time | 314.412 s |
| Training-loop time | 17.297 s |
| Training tokens | 352,256 |
| Reported input tokens, including cached | 642,194 |
| Cached input tokens | 128,000 |
| Reported output tokens | 29,341 |
| Calls missing usage | 7 |

Seven earlier timed-out calls lacked usage, so aggregate token counts are lower
bounds. Cached input is part of input; training time is part of evaluator time.
Elapsed timings are not process CPU time or billed prices. All collection and
policy-development work counts, including interrupted runs; replay-skipped work
is not computation already saved. No net resource saving was demonstrated.

## Validation

The recorded development validation ran 225 tests with five optional skips. A
separate real CPU test passed exact checkpoint continuation across a schedule
change, held-out checkpoint preservation, and corrupted-checkpoint rejection.
These checks support the execution, replay, and save/restore mechanics. They do
not establish a learned-policy advantage. See [test commands](README.md#setup)
to validate the current checkout independently.

Ocura supplied reusable recording, lineage, verification, and checked output
reads throughout the experiment. Reports could be reconstructed without calling
an agent or importing PyTorch. That is the implementation capability demonstrated
by this example; a broader efficacy study would need stronger live controls,
more seeds, and a workload with meaningful allocation choices.
