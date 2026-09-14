# Progress

## Milestones

| M | State | Notes |
|---|---|---|
| M0 Scaffold | **done** | layout, uv + pnpm, Makefile, verify, design |
| M1 Core math | **done** | usage, pricing, stats, tokenize, budget; 138 tests, 98% cover |
| M2 Registry and adapters | **done** | prices verified vs. Anthropic docs; both adapters; cassettes; resume test |
| M3 Workloads | **done** | generator, handbook, grading, runner, ledger, fixtures/test/ |
| M4 Optimization | **done** | lint, findings, scorers, cascade, proof, report; 406 tests, 95% cover |
| M5 Optimize screen | **done** | graph, findings, proof, frontier chart, trace drawer; 15 e2e pass |
| M6 Remaining screens | **done** | Inspect+Brief, Compare, Spend, Settings, New experiment; 38 e2e pass |
| M7 Finish | **done** | README, DEMO, ARCHITECTURE, Dockerfile, critique, spec review, CI gate |
| M8 Pluggable scorers | **done** | scorer registry, `self-consistency-v1`, sampling charged, comparison |
| M9 Proof without gold | **done** | `grading: judged`, `tokop annotate`, cost-optimal allocation, adversarial judge test |
| M10 Label-free calibration, priced checkability | **done** | `penalized-v1` pseudo-labels, B2c contract lever, negative-delta disclosure |

## Next

**M11: U5 and U8** — certificates that expire, and dataset provenance. A proof is currently a
snapshot with a price hash and nothing invalidates it when a provider ships a new model revision
under the same name; `tokop canary` re-runs a stratified subset on a schedule with alpha spending
across looks. U8 then makes every certificate carry where its task set came from, and refuses to
certify above a synthetic-share threshold.

Still outstanding, and still the blocker for the same two things: **a live recording of a small
split**. Every number turns from simulated to recorded, and the scorer comparison in D27 becomes
a real comparison instead of a statement about the simulator's noise model. M9 adds a third: the
judge's error rates are invented parameters, and only a recording can say what a real cheap judge
costs in interval width.

## Proving the demo without its answer key (M9, UPGRADE_V3.md U1 and U2)

`grading: judged` is now a thing a workload can declare. The demo keeps its answer key and gains
a judge, so the two estimates sit side by side and the interesting number is the relationship
between them:

| Estimate | Accuracy difference | 95% CI | What it cost |
| --- | --- | --- | --- |
| The cheap judge alone (`claude-haiku-4-5`, 400 answers) | +5.5 pt | [-1.5, +12.5] | $0.3907 |
| Corrected by 44 strong-graded tasks (22%) | -3.5 pt | [-12.5, +5.6] | $0.6008 |
| The gold answers, for comparison | +0.5 pt | [-3.0, +4.0] | not available to a real workload |

The judge is biased by **+9.0 points** on this split, measured rather than assumed away, and the
judge-only *accuracy* is 12 points low on the baseline arm. The corrected interval covers the
gold-graded difference; `tokop report --check` fails if it stops doing so.

**The uncomfortable number, reported rather than buried.** On this workload the cheap judge
disagrees with the strong grader often enough — mean square error 0.239 against a strong-grader
variance of 0.105 — that the cost-optimal sampling rate is **1.0**: grading everything is the
cheapest route to a given interval width, and strong-only grading would have reached the same
width with 50 items at $0.62 against the $0.60 spent here. The mixed design does not pay at this
judge quality. Tokop says so and names the lever that would change it (U4's checkable output
contract), rather than selling a saving that is not there.

What does pay is **where** the budget goes: the cost-optimal policy cuts the estimator's variance
by 64% against uniform sampling at the same expected spend, computed exactly rather than from a
draw. The squared correction term averages 0.81 on pairs the two arms disagree about against 0.02
on pairs they agree on, which is the whole reason the policy samples discordant pairs hard.

**The release blocker.** `tests/test_judged_proof.py::TestTheAdversarialJudge` injects a judge
that marks 20% of one class of correct answers wrong. The naive judge-only interval stops
covering the true accuracy; the corrected interval still covers it at every committed seed. Two
stronger versions run beside it: a judge that is wrong about *everything* (`G = 1 - H`) is still
corrected to an unbiased estimate over 3,000 draws, and a judge whose error hits one arm only
biases the naive delta by more than five points while the corrected interval holds. `make verify`
runs the check by name. D30 records why the corrupted class is `number` rather than `yes_no`.

Fixtures grew from 4,914 cassettes to 5,450: 400 cheap-judge calls, 88 strong-grader calls, and
their pre-warming. `fixtures/test/annotations.json` records every rate, every draw, every verdict
and every dollar, and `tokop fixtures-check` replays all 488 verdicts through the parser and
fails if one disagrees with the call it came from.

Two bugs the acceptance tests found, both real: `uncertainty_proportional_rates` did not produce
the mean rate it promised once the floor clipped (D32), and the strong grader's price projection
was 38% low because it carried the cheap model's token counts across a tokenizer-generation
boundary (D29.6).

## Calibrating without labels, and pricing checkability (M10, UPGRADE_V3.md U3 and U4)

**U3.** `optimize/pseudolabels.py` stands in for an answer key on the calibration split, with two
registered kinds so the comparison is always in front of you: `majority-vote`, implemented
honestly rather than as a straw man, and `penalized-v1`, which borrows RESTRAIN's mechanism — a
tier does not vote on its own label, a distribution with no clear winner is excluded and counted,
and confident disagreement with the consensus is weighted **up** rather than softened.
`tokop prove --calibration penalized-v1` runs the whole proof on a label-free operating point.

**And the demo's fixtures cannot say whether it works.** Every pseudo-label on the calibration
split agrees with the answer key, under *both* kinds. That is a fact about the noise model, not
about the method: the simulated provider draws wrong answers independently and each is a
different perturbation, so the correct answer is the unique plurality almost every time and a
majority vote cannot go wrong here. The report computes that diagnosis rather than asserting it,
and prints it on the screen and in the CLI.

So the acceptance check is a constructed calibration set, built around the failure rather than
around the result: a cheap tier whose mistakes *repeat* — the same confusable answer five draws
out of five, on 60% of the tasks it gets wrong, with the mid tier sharing it. At every committed
seed the naive vote reads that repetition as correctness and the search routes **everything** to
the cheap tier (threshold 0.00 against gold's 0.78–0.98), with an accuracy floor 21 to 31 points
too low. The penalized version excludes those tasks, agrees with the answer key on what survives,
and lands within 0.012 of the gold accuracy floor. Three earlier fixtures were tried and rejected
for measuring noise; D34 records why.

**U4.** B2c is B2's prompt plus one line showing how the quoted rule produces the answer — a new
waterfall row between the rewrite and the cascade, found from the spec rather than hardcoded.
`tokop annotate --contract` judges the B2/B2c pair with the same judge, the same prompt and the
same prices as the proof's own annotation run, so the agreement figures are one comparison rather
than two experiments.

| Pipeline | Judge agrees with the strong grader | Strong labels needed | Annotation | Accuracy | Generation |
| --- | --- | --- | --- | --- | --- |
| B2 CLEAR rewrite | 83.7% | 89% of tasks | $1.2412 | 97.0% | $1.2567 |
| B2c Checkable contract | 93.0% | 78% of tasks | $1.1064 | 94.5% | $1.4521 |

Both priced at the same stated target: a standard error of one accuracy point. The contract buys
$0.1347 of annotation. It costs 7,406 extra output tokens ($0.1955) and **2.5 accuracy points**.
Net on the evaluation split: **−$0.0607. It does not pay here.**

That is the headline, and the machinery exists to make sure it stays the headline. The accuracy
delta is rendered with its sign on the screen, in the CLI and in the README's generated block,
and `make verify` runs the disclosure check by name. The saving and the premium are reported
apart rather than netted: the annotation saving is paid once per evaluation, the generation
premium on every task the pipeline ever runs.

The proof cost moved from $15.10 to $16.61, repaying after 367 tasks instead of 334, because
B2c's test run is money spent exploring the waterfall and lands where B1's always has.

## Demo result (simulated test fixtures, 200-task test split)

Generated into README.md by `tokop report --write-readme`. Headline: B0 $0.05172 per successful
task, B3 $0.00425 — a 91.8% reduction, accuracy +0.5 points with 95% CI [-3.0, +4.0], verdict
**inconclusive** because the lower bound sits exactly on the 3-point margin; about 4 more tasks
would settle it. The cascade answers 34.5% of tasks at the cheap tier, 42.5% at the mid tier and
23.0% at the frontier — but those are not its spend: because an escalated task pays for every
attempt it made, the cheap tier is 23.4% of the money and the frontier 36.1%. The proof itself
cost $15.10 and repays after 334 tasks.

These are simulated, not recorded (DECISIONS.md D1).

## Known issues

- `make verify` runs all 17 checks with none skipped.
- **The demo's own calibration is still gold, by choice.** `--calibration penalized-v1` runs the
  label-free path end to end, and the demo reports what it would have chosen; the shipped
  operating point stays gold because the fixtures cannot show whether the label-free one is any
  good (D34.3). One flag switches it.
- **The legibility tax and the checkability bonus are invented parameters.** The mechanism runs
  end to end over recorded calls; their sizes come from `workloads/demo/`, and every surface that
  shows a number derived from them labels it simulated.
- **A judged proof needs one sample per task.** The judge reviews the answer the cascade
  returned, and only the first generation of each task has been judged, so `tokop annotate`
  refuses an operating point that draws more. The demo's is `logistic-v1` at k=1.
- The Dockerfile has never been built: Docker is unavailable in this environment (D22).
- The GitHub workflows run green on GitHub Actions: `verify`'s run 6 passes all twelve checks on
  `ubuntu-latest` in 3m33s, and `proof gate` exits 1 on the inconclusive verdict as designed
  (D25). It took six runs to get there, and the failures were real (D26).
- One `make verify` run failed on a Playwright ENOENT after I ran the spec-review subagent
  concurrently — two runs of the suite wiped each other's `test-results/`. Playwright now gets a
  per-run output directory so concurrent runs cannot collide.
- `tiktoken` cannot load `o200k_base` in this environment (D3), so token estimates name
  `bytes-bpe-approx-v1` as their method until the vocabulary is reachable.
- `tokop sync-models` is implemented and unit-tested but cannot run live here: openrouter.ai is
  blocked by the egress policy (D2). The committed snapshot is a hand-built fixture and says so
  in its own `_tokop_source` field, which the registry surfaces as the price provenance note.

## Open decisions

See DECISIONS.md. D1 (no credentials, simulated fixtures) shapes every later milestone.

## Spec review (SPEC.md section 10, final gate)

A fresh subagent reviewed the repository against SPEC.md sections 3, 5, 9 and 10 and reported 16
gaps. What changed as a result:

- **Numbers that were wrong on screen.** The candidate graph showed routing shares where it meant
  spend shares (cheap is 23.4% of the money, not 34.5%); per-model token ratios are now fitted
  from the cassettes rather than assumed equal (`core/ratios.py`); the trace drawer's route
  decision now comes from the scorer's own score and threshold rather than being inferred; and
  `tokop prove --margin` is applied to the verdict rather than echoed.
- **Controls that were not real.** The per-run content deletion (non-negotiable 10) now calls the
  endpoint and reports what survived; the live-mode controls that are not built now name
  themselves one by one instead of being enabled in live mode.
- **Lint spans are drawn.** The engine has always returned the exact character ranges each
  finding fired on; nothing displayed them. Inspect now highlights them on the text the lint saw,
  and clicking a finding isolates it. Fixing this exposed a real bug: PL10's spans were offsets
  into the joined system text, so they pointed at the wrong characters in any prompt with more
  than one system block.
- **Claims corrected rather than quietly left standing.** `docs/ARCHITECTURE.md` said 425 engine
  tests and 53 Playwright tests; it is 429 and 38, at 91% line coverage rather than 95%. The
  README, the Makefile and this file no longer imply `make record` can record: the live path is
  wired and tested up to the request (`tests/test_recorder_wiring.py`) but has never made one, and
  user-supplied workloads are not built. Both are DECISIONS.md D24.
- **Two checks that were weaker than they looked.** `tokop report --check` now makes a real HTTP
  request through the route instead of calling the in-process cache, and `make verify` now fails
  when `docs/DEMO.md` quotes a figure the report has moved past.

## The CI gate (roadmap item 4)

`tokop prove` already exited nonzero unless the verdict was non-inferior; what shipped now is
everything around it.

- `.github/workflows/verify.yml` runs `make verify` — the same command a contributor runs, so CI
  is not a second source of truth — on every push and pull request, with no credentials set.
- `.github/workflows/proof-gate.yml` is the gate, written to be copied: set your workload and
  your margin, and a merge that makes the workload worse fails the build. It is `continue-on-error`
  here alone, because Tokop's own demo verdict is inconclusive at the 3-point margin; making it
  green by choosing a looser margin is the exact dishonesty the project exists to prevent (D25).
- `engine/tests/test_cli_gate.py` is what actually blocks: six tests that run the real CLI in a
  subprocess and pin the exit codes — 1 on inconclusive, 0 at a margin the result clears, 2 on an
  unsupported workload — because an exit code asserted in-process is not what CI observes.
- `playwright.config.ts` no longer hard-codes this image's Chromium path. It falls back to
  Playwright's own resolution when that path is absent, which is everywhere but here.

438 engine tests, 39 Playwright tests, 91% line coverage on `core/` and `optimize/`.
`make verify`: 12 checks, none skipped, green. (At M10: 599 engine tests, 43 Playwright tests,
91% coverage, 17 checks.)

## The tokenizer divergence (D26)

The CI gate's first run failed four tests that pass here, all with one cause: `o200k_base` is
downloaded on first use, this environment cannot reach the vocabulary host and GitHub's runners
can, so the same repository counted the handbook at 5,328 tokens locally and 4,436 on CI. The
approximation reads 20.1% high on this text.

The one that mattered: Haiku 4.5's cached prefix cleared its 4,096-token minimum by **8%** under
the real tokenizer, not the 30% measured here. The demo's whole caching argument rests on that
prefix being comfortably cacheable, and it was not.

Fixed by growing the handbook 2,918 characters (to roughly 25% headroom under `o200k_base`),
recording the counter in the fixture manifest so `fixtures-check` fails on a mismatch, and having
the report name the counter its fixtures were built with rather than the ambient one. Fixtures
were rebuilt from scratch; every downstream number moved. Details in DECISIONS.md D26.

## What CI found that this machine could not (D26)

The gate paid for itself on its first run. `o200k_base` is downloaded on first use; this
environment cannot reach the vocabulary host and silently falls back to an approximation that
reads 20.1% high on the demo handbook. GitHub's runners get the real tokenizer. Six runs:

| Run | Failures | What was actually wrong |
| --- | --- | --- |
| 1 | 4 | Haiku's cached prefix cleared its minimum by 8%, not 30%. The demo's caching argument rested on a number the approximation had flattered. |
| 2 | 2 | Rebuilding the ledger restamped the fixture manifest with the runner's tokenizer. |
| 3 | 1 | The mismatch check reported a property of the machine as a defect in the repository. |
| 4 | 1 | `docs/DEMO.md` quotes lint projections, and the lint counted with the ambient counter. |
| 5 | 1 | An e2e test asserted a rule that fires on one side of a 500-token threshold. |
| 6 | 0 | Green. |

The handbook grew 2,918 characters, the fixtures record the counter that built them, and the
report counts with that counter rather than with whichever one the machine has. Every headline
number moved as a result, and every generated document now reproduces on any machine.

## A second scorer, and a negative result about the fixtures (D27)

The cascade scorer is now pluggable. `CascadeSpec` names it; `optimize/scorers.py` holds a
`Scorer` protocol and a registry; `logistic-v1` is the scorer that always shipped, registered
unchanged and verified byte-identical — a full payload diff across both objectives moved 0 of
19,473 leaf values.

`self-consistency-v1` is the second: k samples per task, grouped by the workload's
answer-equivalence relation, routed on the normalized discrete entropy of the group proportions,
returning the tier's majority answer. It refuses rather than degrades — no equivalence function,
k below 2, or a matrix recorded shallower than asked for. It is an exact-match approximation of
semantic entropy, not semantic entropy, and says so everywhere it appears.

The sampling money is charged to the scorer that spends it. k generations at a tier is k times
that tier's generation cost, through the same per-task accounting, waterfall, proof cost and
repayment figure as everything else. `tests/test_pluggable_scorers.py` fails if a sampled
configuration is charged nothing — checked by breaking the accounting on purpose.

**The comparison, on the demo's 200-task test split:**

| Configuration | Calls | Accuracy | 95% CI | $/success | Scarce | Sampling $ | Repays | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `logistic-v1` (shipped) | 1 | 96.0% | [92.3, 98.0] | $0.004253 | 36.1% | $0 | 334 | inconclusive |
| `self-consistency-v1` k=3 | 3 | 96.0% | [92.3, 98.0] | $0.003577 | 0% | $5.98 | 459 | inconclusive |
| `self-consistency-v1` k=5 | 5 | 97.0% | [93.6, 98.6] | $0.004987 | 0% | $11.82 | 605 | non-inferior |

**This is not a finding, and Tokop does not ship it.** The simulated provider draws repeated
samples independently, so majority voting gets the full benefit of Condorcet's jury theorem: it
lifts the cheap tier from 0.848 to 0.939 expected accuracy at k=5 on this question mix, computed
from the accuracy table that was already committed. Real sampled generations are strongly
correlated — a model that misreads a rule misreads it every draw — so the benefit on a recording
would be far smaller, and how much smaller cannot be known from here. The same defect inflates
the scorer's AUROC to 0.87–0.97 against the logistic scorer's 0.72–0.89.

So the demo measures both and adopts one: `scorer_search_grid: [logistic-v1]`, with the reason
written beside it in the workload. Every headline number is unchanged. Deleting that one line
turns the comparison back on, and should be deleted once the matrix is a recording.

The deliverable is the pluggable interface, a second scorer that runs end to end from fixtures
with its sampling charged honestly, and a documented reason not to believe its result yet.

Fixtures rebuilt at sample depth 5: 1,314 cassettes became 4,914, with all 1,314 originals
byte-identical. 466 engine tests, 91% line coverage, `make verify` green on all 12 checks.

## Where the build stands

599 engine tests, 43 Playwright tests, 91% line coverage on `core/` and `optimize/`.
`make verify`: 17 checks, none skipped, green.
