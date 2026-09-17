# Progress

> **Project record.** This append-only milestone log explains how the current implementation was
> built. It is not a setup guide. Start with [README.md](README.md), or use
> [docs/README.md](docs/README.md) to find technical documentation.

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
| M11 Expiry and provenance | **done** | certificates with a canary and alpha spending, dataset provenance that refuses |
| M12 Meaning and ties | **done** | `semantic-entropy-v1` with effective-k, `sep-v1` refuses, the report names every tie |
| M13 One real recording | **blocked on a key** | the four gates are built and tested; no generation request has been made |
| M14 The first screen | **done** | README reordered into `docs/METHOD.md`, an evidence grade, a summary block, Deploy exports |
| M15a The engine without the demo | **done** | `Item` protocol, datasets loaded not generated, no neutral module imports the demo |
| M15b A second workload | **done** | `incident-triage` proves clean, `tokop ingest` for JSONL/CSV/OTel; `tokop label` not built |
| M16a Pipelines as graphs | **done** | `steps:` compiles to a graph; a single call is still byte-identical, pinned by cassette key |
| M16b Graphs execute | **done** | the runner runs a graph per task; `TaskCall` carries the step; a graph warms its own cache |
| M16c Graph findings and proof | **done** | G01–G05 over traces; deleting the idle verifier cuts 51.8% with delta zero by construction |
| M17 Session and cache accounting | **done** | `core/session.py` with expiry; PL15–PL17; holding the prefix beats compacting at three paces |
| M18 Workload identity and drift | **done** | `optimize/fingerprint.py`; a certificate binds to the mix and expires on coverage, named apart from the canary's outcome drift |

## Next

**M13b: the recording itself.** Everything that decides whether it should happen is built,
tested and green. What is missing is consent: `ANTHROPIC_API_KEY` and `RECORD_BUDGET_USD` are
both unset here, there is no `.env`, and Tokop never sets either. `api.anthropic.com` is
reachable from this environment — an unauthenticated POST to `/v1/messages` returns 401 — so the
blocker is a decision, not a network.

One command changes it:

    ANTHROPIC_API_KEY=... RECORD_BUDGET_USD=25 TOKOP_MODE=live make record

It will refuse before spending if the test split cannot produce a verdict, print what it expects
to cost against the cap, ask, run ten tasks per step, re-project from their real answer lengths,
refuse again if that lands over the cap, and only then record. Until it runs, every number in
this repository is simulated and labelled so.

**M14 is done and did not wait for it.** The evidence grade reads `insufficient` today; a
recording moves two of its eight links and the verdict moves a third.

**`tokop label` is the one piece of M15 not built.** Without it the grader-agreement link in the
evidence chain reads "not measured" for any workload with no judged arm, which is the honest
state. The 50 hand labels UPGRADE_V4.md M15.3 asks for are a human's to write.

**M16, M17 and M18 are done and are described below.** What each of them still cannot do is
written beside it rather than at the end, because every one of the three has a half that waits on
a recording: the graph workload has no committed cassettes by choice, the session comparison
refuses its quality claim, and a fingerprint over a program-generated split is not a fingerprint
of anybody's traffic.

**`tokop label` is still the one piece of M15 not built**, for the same reason as before.

**U9, the serving-cost basis**, is deferred rather than renumbered (D41). It prices a hosted tier
in GPU-hours over achieved throughput so a cascade can mix an API tier with a hosted one. Nothing
in this build has a self-hosted tier, so it would be a fake column.

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

## Certificates expire, and this task set cannot back one (M11, UPGRADE_V3.md U5 and U8)

**U5.** `tokop certificate` issues what was proven, what it rests on — model snapshot identifiers,
the price hash, the grading mode, the calibration mode, the dataset's provenance — and when it
stops being true. Thirty days, four weekly looks, and `tokop canary` takes them.

Looking repeatedly is the part that needs care, so the correction is measured rather than argued:
four unadjusted looks at the 5% level raise on more than 15% of workloads where nothing happened,
and the spent schedule holds the family-wise rate at 0.05 over 40,000 simulated certificate
lifetimes. The looks are independent draws, not accumulating data, so the exact correction is
`alpha_k = 1 - (1 - S_k)/(1 - S_{k-1})` rather than a group-sequential boundary — D38.2 says why
the difference matters.

A swapped snapshot identifier raises immediately and is not a statistical question: the
certificate is about a model that is no longer there. `tokop canary` exits 1 on that, on an
expired certificate, and on a moved interval, so a scheduled canary gates a deploy the way
`tokop prove` gates a merge.

And a canary in replay has nothing to find: re-scoring the cassettes the certificate was issued
from cannot produce a different answer, so the drift test reports *not tested* with that reason
rather than a reassuring pass.

**U8.** Every certificate now carries a `dataset_provenance` block, and the demo's own dataset is
**refused**:

    items:      300
    origins:    0 real traffic, 0 model-generated, 300 program-generated (100% synthetic)
    tail:       28 of 28 templates present (100%), concentration 0.21

    CERTIFIABLE: no
      refused:  the set contains no real recorded traffic, so it cannot certify behaviour on a
                distribution nobody has observed.

That is the right answer, not an inconvenience. Everything measured on this set is true about the
set; none of it is yet evidence about anybody's production traffic. The certificate is still
issued and carries the refusal, and `tokop provenance` reports without gating — a set that cannot
certify is a fact about the set, not a defect in the build.

Program-generated and model-generated items are counted apart, because the collapse literature is
about a model sampling from its own output and a program templating from a policy file is not in
that loop. The acceptance fixture is: the same number of items piled onto 4 of 20 templates,
refused with the 16 that are missing named. The failure it guards against is specific — a cascade
earns its savings on easy tasks and its risk lives in the tail, so a thinned tail certifies a
router that fails in production while every number above it looks fine.

The machine-generated-text detector is **not built and refuses**: it needs a model, this build has
none, and one that guessed would importance-resample a set towards its own guess. The resampling
mathematics is implemented and tested for a caller who has a real detector.

## Meaning, effective k, and the tie (M12, UPGRADE_V3.md U6 and U7)

**U6.** `semantic-entropy-v1` clusters k samples by **bidirectional entailment** judged by a cheap
model — two directed calls per candidate pair, because entailment is not symmetric — and routes
on the entropy of the meaning clusters. The calls are recorded, replayed from cassettes, and
charged to the scorer through a new `extra_cost` on the `Scorer` protocol: a scorer that spends
money the cascade is not charged for is how a comparison stops meaning anything, and D27 learned
that once already. Identical answers are never sent, which is why 830 judgements cover the whole
matrix at depth 5.

**And on this workload it costs money and buys nothing.** Same accuracy, same AUROC, same routing
as the exact-match scorer at every k, and $0.000223 more per successful task at k=3:

| Configuration | calls/task | accuracy | $/success | effective k |
| --- | --- | --- | --- | --- |
| `logistic-v1` (operating point) | 1 | 96.0% | $0.004253 | — |
| `self-consistency-v1` k=3 | 3 | 96.0% | $0.003577 | 2.94 |
| `self-consistency-v1` k=5 | 5 | 97.0% | $0.004987 | 4.27 |
| `semantic-entropy-v1` k=3 | 9 | 96.0% | $0.003800 | 2.94 |
| `semantic-entropy-v1` k=5 | 25 | 97.0% | $0.005442 | 4.27 |

The demo's answers are numbers, enums and yes/no, and the grader's exact-match relation already
merges a currency symbol, a trailing percent and a hedge before a yes or no. What it misses is a
unit word after a number — "60" against "60 days" — and the fixtures contain none of those. That
is a property of this question mix, not of the method, and a test pins it so it cannot quietly
stop being reported. The paraphrase merging the method exists for is tested against explicit
pairs; three of the four I first wrote down turned out to be cases the grader already handled,
and claiming them would have overstated what entailment adds.

**Effective k turns D27's caveat into a number.** The intraclass correlation of within-task
agreement and the design effect `k / (1 + (k-1) rho)`: rho runs 0.000 to 0.043 here, so k samples
really are worth about k. That is a fact about the simulator's independent draws, and it is
exactly why the scorer comparison is still measured and not adopted. A real recording would show
a much lower effective k, and the column would say so without anyone writing a paragraph.

**`sep-v1` is registered and refuses.** It reads hidden states, no provider API returns them, and
the registry now carries a `hidden_states` flag so the refusal points at something real. D27.4
unchanged: not a "not yet", a consequence of non-negotiable 6.

**U7.** Three configurations' cost intervals overlap, so the report lists all three ordered by
dollars and `single_winner` **raises** rather than returning one. The cheapest of them is one this
workload does not let the search adopt, so the tie says that too — and there is consequently no
fallback, which the report states rather than designating the operating point as its own. A
fallback that shares the constraint it is meant to survive is not a fallback.

Both land in the README's generated block — the comparison with its effective-sample column, and
the tie with its note and its missing fallback — so no M12 number in the README is typed by hand
(non-negotiable 1), and `tokop report --check` fails the build when one moves.

## A cap that was a cap, and a price before the first call (M13a, UPGRADE_V4.md M13)

M13 is the milestone that turns every number here from a statement about a simulator into
evidence. It splits at the money: everything that decides whether a recording should happen
spends nothing and is finished; the recording itself needs a key and a budget nobody has set.

**The finding that came first.** `SpendGuard` is written for per-call use — "Every adapter call
passes through here twice", says its docstring — and nothing called it. A grep for `preflight(`
over the engine returned its own definition, one call in `Recorder._step` passing a projection of
zero under a comment claiming the live adapter checked each call, and nothing else. Measured
before the fix, on the plan a real recording would run:

    cap $0.01, step cost $0.0309, guard spent $0.0309, 25 calls paid for, no refusal

At full size that is B0 on the test split as a single uninterruptible $9.88 purchase.
`RECORD_BUDGET_USD` could be observed to have been passed; it could not stop anything. The same
probe after the fix refuses after one call. A second defect sat behind it: all three of the
runner's gathers use `return_exceptions=True`, which is right for a provider failure and wrong
for a budget refusal — it would have spent the whole cap and then recorded every remaining task
as unsuccessful, handing the report an accuracy figure that describes a budget. D42 has both.

**What `tokop record` does now.** Four gates, then it asks:

| Gate | What it refuses |
| --- | --- |
| Consent | No key, or no `RECORD_BUDGET_USD`. Tokop never sets either |
| Power | A test split too small to produce a verdict. `--underpowered` overrides, and the override is stored in the run record and printed in the report |
| Projection | A plan whose counted input alone costs more than the cap |
| Pilot | Ten tasks per step, whose measured answer lengths re-project the total. Over the cap, it stops — and the pilot's calls are cassettes the next run replays, so they are not lost |

**`tokop power`, on the demo's own fixtures:**

    split as recorded            200 tasks
    the arms disagree on         13 of them (6.5%)
    accuracy difference          +0.5 points (candidate ahead on 7, behind on 6)
    tasks needed                 204
    204 tasks at the observed discordance, 4 more than the 200 in the split.

Which is the same 4 the README's verdict line has been quoting all along, now available before a
recording rather than after one. It prints its own caveat: a discordance rate measured on
simulated arms whose errors are drawn independently is the optimistic case, and two real models
that fail on the same hard tasks will be more concordant and need more tasks.

**The projection is a band.** Input is counted — exactly, through the provider's counting
endpoint, which M13 is the first caller of — with the cache modelled as the recorder runs it.
Output cannot be counted before the call, so the ceiling prices every call at `max_tokens` and
the floor at zero. The acceptance check is that the committed recording lands inside it, step by
step:

| Step | floor | recorded | ceiling |
| --- | --- | --- | --- |
| B2 calibration cheap | $0.35 | $0.50 | $0.85 |
| B2 test frontier | $4.47 | $6.37 | $9.47 |
| B0 test frontier | $8.72 | $9.88 | $18.72 |
| B1 test frontier | $0.98 | $2.13 | $10.98 |
| **whole plan** | **$21.15** | **$28.43** | **$54.75** |

Getting there needed one correction worth remembering: subtracting a base-counter count of the
cacheable prefix from an exact count of the whole request books the ~30% tokenizer-generation gap
(D3, D26) as text that changes every call, and prices thousands of cached tokens at the full
input rate. The first projection read $42.72 against a recorded $28.43. The base counter now
supplies the cacheable *share* and the exact count supplies the magnitude; the projected prefix
then reproduces the recording's own cache-write figures to within one token on every step.

**What is checkable now that was not.** `fixtures/*/manifest.json` carries a `cassette_digest`
over every cassette and blob, and `tokop fixtures-check` compares it to the bytes on disk —
cassette keys hash the request, so a key set alone cannot notice an edited answer. And the resume
path is demonstrated on the money rather than assumed: a recording stopped at its cap, resumed
under a larger one, pays for exactly the remainder, with the two runs together paying for the
step once.

## The promise first, the qualifications one click in (M14, UPGRADE_V4.md M14)

The README opened with the promise and then spent five screens qualifying it. Ten sections moved
verbatim to `docs/METHOD.md` — the proof method, the gold-free estimate, label-free calibration,
the checkability price, certificates, dataset provenance, semantic entropy, ties, the CI gate and
the citations. Nothing was softened and nothing was cut: **the method document is generated and
checked exactly as the README is**, because a qualification that moves out of sight and out of
the build is a deleted one. `metrics_block` split into `readme_block` and `method_block`;
`tokop report --write-readme` writes both and `tokop report --check` fails if either moves.

**The evidence grade, and what it says about this build.** `optimize/evidence.py` grades eight
links and takes the *lowest* rung any of them forces:

| Link | Standing | Reading |
| --- | --- | --- |
| Verdict | **blocking** | Inconclusive: about 4 more tasks would settle it |
| Test split | **blocking** | 200 tasks, 204 needed |
| Tasks the two pipelines disagree on | ok | 13 of 200, exact p = 1.000 |
| Tasks from real traffic | simulated | 0 of 300 |
| Answers from a provider | simulated | simulated |
| Cheap judge agrees with the strong grader | ok | 83.7% on B2, its worst arm |
| Prices | ok | all verified against the provider's page |
| Token counts | simulated | `bytes-bpe-approx-v1` |

So the grade is **insufficient**, not `simulated`. That was the interesting decision of the
milestone: `simulated` was available and true as far as it goes, and it would have implied the
result holds about the traces, which it does not. One word at the top of Optimize, the whole
table one click away, and a test that fails if any weak link stops saying what would change it.

**Deploy exports, and never proxies.** SPEC.md section 3 rules out any gateway carrying other
applications' traffic, so "deploy" ends at handing over artifacts: the prompt as a unified diff
(B0 to B2, with the cache breakpoint visible, since that breakpoint is most of the saving), the
operating point as JSON, and the routing rule as Python. A test executes the generated rule and
checks it escalates cheap → mid → frontier at the thresholds the cascade proved. Each artifact
carries an `evidence` field, so the config file says `insufficient` about itself rather than
looking authoritative.

**The summary block is engine-computed.** Non-negotiable 1 bans metric literals in components,
and a component that divides one number by another to get a saving has written a metric in
TypeScript. `report["summary"]` carries all six figures ready to print, and the Playwright test
compares the rendered screen against `/api/report` rather than against itself.

## The engine stops being the demo's engine (M15a, UPGRADE_V4.md M15)

"Ships one workload" undersold the problem. It was not a missing ingestion command: `DemoItem`,
a type from one workload's *generator*, sat in the signature of the runner, the recorder, the
annotator and the report; `DatasetBundle` sat beside it; and `build_dataset()` was called
directly in eight places wherever tasks were needed. Nothing else could run because nothing else
could be represented.

**`Item` is a protocol**, so `DemoItem` satisfies it without changing and an ingested task
satisfies it without inheriting anything. Its fields split three ways and the split is the
interesting part: grading fields every workload has, a description field every workload needs
something for, and provenance fields — `template_id`, `sections` — that only a *generated*
workload has. Real traffic has neither, so both default to empty rather than being invented.

**Loading replaced generating.** Every workload, the demo included, is now read from the dataset
file committed beside it rather than re-run through a generator. `fixtures-check` already
asserted the two agree, so no number moved — `tokop report --check` passing unchanged is the
evidence — and the last reason for a neutral module to import a workload-specific one is gone.

**One rename with a real edge.** `handbook` became `grounding` in the engine; the demo still
calls its own document a handbook, in its own prose, because that is what it is. Only the five
`{{handbook}}` placeholders changed, and a placeholder rename does not change rendered text —
confirmed by all 6,856 cassettes still matching.

**A set nothing templated is not a set with zero coverage.** Tail coverage measures how much of
a generator's space a set reaches. Ingested traffic has no generator, so coverage does not
exist. It now reports `None`, where `0.0` would have refused the set for a thinned tail and
`1.0` would have said the tail was checked when nothing checked it.

**The check has to run twice.** `test_scorer_isolation.py` scans source for imports, which is
right for what it checks and would have seen none of this: half the demo imports are lazy and
sit inside functions. So `test_no_demo_imports.py` imports each neutral module in a subprocess
and reads `sys.modules`, then loads a workload nobody generated and reads `sys.modules` again.
Fourteen checks, and two of them failed until `TierProfile` moved out of the demo's responder.

**What this does not do yet.** `tokop prove --workload` still refuses anything but the demo by
name, because `report.py` hardcodes its path in six places and `build_report()` takes no
workload. The engine can represent and load another workload; it cannot report on one. M15b.

## A second workload, and the refusal ingestion exists to make (M15b, UPGRADE_V4.md M15)

`tokop prove --workload data/incident-triage/workload.yaml` runs, and the import test fails if
the demo is reached for while it does. That is M15's acceptance check, and getting there took
threading a workload through six entry points in `report.py` that had the demo's path spelled
out — five more spellings than a default needs.

**The second workload is an on-call triage bot** over a runbook mapping alert signatures to
escalation codes, owning teams and paging rules. Its answer mix is codes, team names and yes/no
rather than the demo's money and day counts, and two of its five question shapes turn on
overrides that beat the signature's own rule. On its own 99-task test split:

| Pipeline | Accuracy | Cost per successful task |
| --- | --- | --- |
| B0 current, on the frontier model | 89.9% | $0.00719 |
| B1 cache-friendly order | 90.9% | $0.00259 |
| B2 tightened, with an output contract | 90.9% | $0.00348 |
| B3 cascade on B2 | 85.9% | $0.00600 |

The tiers on B2 alone score 68.7% cheap, 82.8% mid, 90.9% frontier on the test split, and the
cascade routes 31% of tasks to cheap and 69% straight to frontier — the mid tier earns nothing
here.

**Verdict: inconclusive, and unlikely to be settled by more tasks.** The cascade cuts cost per
successful task 16.6% (interval 7.1 to 26.4%) and the accuracy difference is −4.0 points, 95% CI
[−10.1, +2.0], against a 3-point margin. The point estimate is already past the margin, so more
tasks would tighten an interval around a point on the wrong side of it. Reported as it came out —
and note that B1, a pure reordering, beats the cascade on this workload at no accuracy cost.

**And it showed something the demo cannot.** Every pseudo-label on the demo's calibration split
agrees with its answer key under *both* kinds, which D34 recorded as a fact about the noise model
rather than evidence that label-free calibration works. Here they come apart: **majority-vote
agrees with gold on 86%** of the calibration split, while **`penalized-v1` excludes the 21 tasks
it cannot settle and agrees perfectly on what remains.** That is the mechanism U3 exists for,
finally exercised by a workload rather than by a constructed test — because a cheap tier
answering an override question from the signature makes the *same* mistake every time, which is
exactly what a plurality vote reads as correctness.

**`tokop ingest` reads JSONL, CSV and OpenTelemetry GenAI spans**, and the interesting part is a
refusal. A span records what was asked and what the model *said*; it does not record what the
right answer was. Writing `gen_ai.completion` into `gold` would produce a workload on which every
pipeline scores 100% against itself, and nothing about the result would look wrong. Tokop reads
the tasks, leaves the answers empty, says how many, and exits 3.

**The invented numbers moved into the workload that owns them.** `simulation:` in the second
workload's YAML declares what each tier gets right, by task type, beside the provenance block
saying the whole set is program-generated. The neutral responder reads them from there. It is
deliberately not a better simulator — section 5 of the upgrade rules that out, and a more
convincing one would only produce more convincing numbers about nothing.

## A pipeline becomes a graph, and stays exactly as cheap (M16a, UPGRADE_V4.md M16)

A pipeline was one model call, which can express two optimizations — run a cheaper model, write
a cheaper prompt — and no others. The workloads where the money actually goes are shaped
differently: an agent making fourteen calls and retrying twice is a larger problem than an
8,000-token prompt, and the useful proposal there is usually *delete a step*, which a
single-call spec cannot represent at all.

`PipelineSpec` now takes an optional `steps:` list. Empty — every pipeline in both shipped
workloads — compiles to a graph of one `generate` step **whose spec is the pipeline itself**, so
there is no second rendering path to drift from the first.

**The guarantee, and a correction to the plan.** PLAN.md proposed protecting single-call
workloads by diffing the report JSON against a committed snapshot. That is the wrong instrument:
M13, M14 and M15 each legitimately changed the payload, so the snapshot would need rewriting
every milestone and would stop meaning anything the moment it did. The guarantee is asserted
where it actually lives instead — **the compiled one-step graph renders a request with the same
cassette key**, for every pipeline of both workloads. A cassette key is a hash of provider, model
and the canonicalized request, so if the keys match, every committed cassette replays and every
number is identical by construction. `make verify` runs it by name.

Two details that would have broken it quietly. Run order is Kahn's algorithm with ties broken by
*declaration* order, because an order that depended on dict iteration would hash differently on a
different day. And `CallRow` gained a `step`: the graph findings coming in M16c are all questions
about which step made a call, and a call row that cannot say is one none of them can be computed
from.

**Nothing executes a graph yet.** A workload that declared steps would compile, validate, and
then be run as though it had not. That is M16b, and the spec landing first is deliberate: it is
what every later commit depends on, and the part that could have forced the fallback to a
parallel `GraphSpec`.

## A graph that runs, and a step that buys nothing (M16b and M16c, UPGRADE_V4.md M16)

M16a made a pipeline compilable into a graph and could not execute one. This is the other half.

**What a step had to become.** Tokop executes no tools, so a graph has two kinds of step.
`generate`, `verify` and `retry` are model calls. `tool` and `retrieve` are *local*: their `user:`
blocks are the arguments they were called with and a new `emits:` block is the result the
**workload declares** they return. A local step with nothing to emit is refused at compile time,
and a generation step that declares one is refused too — its output is the model's reply, and a
second declared output would be a number nobody could trace back to a call. That makes a graph a
model of an agent's *shape* rather than an agent, which is what every surface reporting on one
says. `loop` compiles and refuses to run: nothing bounds the iterations, so nothing can price it.

**A graph warms its own cache, because it cannot warm it any other way.** A step's request cannot
be rendered before the steps it consumes have run, so there is no `max_tokens: 0` copy to send in
advance. The first task runs to completion on its own and every task after it reads what that task
wrote — one task's worth of misses rather than a synthetic call per step, and `prewarm_calls`
reports 0 rather than a number nobody made.

**`data/incident-agent/` is the same 150 tasks as `incident-triage`, in a shape a single call
cannot express.** Holding the tasks fixed is the point: a graph workload whose tasks also differed
would be comparing two things at once. `G0` is the agent as shipped — fetch the runbook, look the
signature up twice, answer, then have a frontier model verify — and `G1` is `G0` without the
verifier. It has no committed recording and is not meant to: `tokop graph` runs it against the
deterministic in-process provider, spends nothing, opens no socket, and nothing it produces
reaches the README or a screen. The recording plan in `tokop/recorder.py` is left exactly as it
is, because changing it is how committed numbers move by accident.

**The five findings, and where two of them refuse to claim anything.**

| | | |
| --- | --- | --- |
| G03 | $3.16/1k | the verifier `check` has never changed an outcome |
| G01 | $1.73/1k | the output of `fetch` is sent to 2 steps |
| G05 | $1.19/1k | `check` runs at the dearest model on text an earlier step produced |
| G02 | $0.00/1k | `signature_lookup` is called twice with identical arguments |

G01 prices the redundant copies at each step's **blended** input rate — its own input dollars over
its own input tokens — because a block read from cache in one step and sent fresh in another costs
different amounts in each. G02 reports **$0 and says that is what it means**: Tokop prices provider
tokens and has no price for a tool, so it cannot say what the duplicate costs; it can say the call
is made twice. G03 answers *structurally* here — the graph has no path from `check` to the step
the pipeline answers with, so no number of further tasks would make it change one. G05 is labelled
a **ceiling**, because it prices compressing that text to nothing. G04 fires on `G2`, a third
pipeline with an unconditional retry, and names what its share depends on: this provider is
deterministic, so the retry returns byte-identical text on every task, which is the most
favourable case the finding can be measured in.

**The acceptance check.** `G1` cuts cost per successful task by **51.8%** (interval 50.9 to 53.0%)
with the accuracy difference at **+0.0 points, 95% CI [+0.0, +0.0]**, n = 99, non-inferior. The
zero is *by construction* and the report says so in those words: nothing read the deleted step's
verdict, so all 99 answers are byte-identical between the arms. Reporting a bootstrap where an
argument belongs would be the wrong kind of rigour. Both arms see the simulator seeded on the
**workload** rather than the pipeline, which is a correction — seeded per pipeline, two pipelines
sharing an answering step would get different answers and the deletion's measured effect would be
the simulator's noise (D47).

That is the point of the milestone: an optimizer that can propose *deleting a step*, not only
swapping a model.

## A conversation is not a request repeated (M17, UPGRADE_V4.md M17)

Every price in this build was a price per request. `core/session.py` adds the other unit: writes
at the write premium, reads at the read rate, and **an entry that expires between turns**, which is
the thing a per-request price cannot show at all.

**The bill is counted, not provider-reported, and the payload says so.** The in-process provider
has no clock and so cannot expire an entry. The turns are priced from the rendered requests with
the base counter — an estimate that names its counter — and the provider supplies only the
answers. It also has to be **scaled into the model's units**: the incident runbook is 658 tokens
locally and about 855 as these models count, which is cacheable on Opus 5's 512-token minimum and
not on Sonnet 5's 1,024. Priced unscaled, a perfectly cacheable session reads as entirely uncached
(D48.2).

**The result, at three paces.**

| Pace between turns | Compacting costs, measured | At the summary budget | Winner | Entries expired (immutable / compact) |
| --- | --- | --- | --- | --- |
| 40s | 1.27–1.33x | 1.41–1.48x | immutable | 0 / 0 |
| 70s | 1.09–1.15x | 1.21–1.28x | immutable | 16 / 0 |
| 90s | 1.11–1.16x | 1.23–1.29x | immutable | 16 / 1 |

Two columns, not one, because the one bias left in the comparison is **priced rather than
mentioned**: the summary the compacting arm carries is the simulated provider's short reply rather
than the budget the workload set aside, and a shorter summary is a cheaper prefix to write and to
read. Every turn carrying a summary is therefore also priced at that budget. It matters — at the
first geometry tried, the measured ratio favoured compacting at [0.95, 0.97] and the bias-closed
one favoured holding at [1.04, 1.06], and the honest report was "not established".

Three paces, not one, because the gap decides the answer. Six turns seventy seconds apart is 350
seconds, so the five-minute entry expires before the last turn of every session — and rewriting
the prefix, which is what compaction is charged for, *also refreshes the entry*, so compacting can
win by destroying something just before it would have died anyway. The expiry counts are in the
table so that mechanism is visible rather than inferred from a cost that moved. The interval
resamples **sessions**, not turns: turns inside one session share a cache entry, so bootstrapping
turns would report an interval several times too narrow.

**PL15 to PL17** read a sequence of requests rather than one. PL15 fires when the prefix moves on
every turn; PL16 when text identical on every turn sits *after* the breakpoint, which is what a
conversation that re-renders itself rather than appending tends to produce; PL17 recognises a
compaction by what it does — a prefix that changed and a tail that got shorter. Both shipped arms
raise PL16 on `S0`'s contract block, and only the compacting one raises PL17.

**The quality side is refused, and that is a logged conflict with the plan.** PLAN.md asks for the
change in task success with an interval. This build's provider answers from the task and the model
alone: its replies do not depend on the conversation carried with them, so a compacted arm cannot
lose accuracy here, and "no accuracy difference" would report a property of the simulator as a
property of compaction. It is displayed as not measured, with that reason, everywhere the
comparison appears (D48.5).

**And B1's claim is restated rather than softened.** The README's generated metrics block now says
that every figure in it is per request, and that a conversation is a different accounting with a
different answer. A refusal moving one click away, which is what UPGRADE_V4.md M17 asks for.

## A certificate that knows what it was measured on (M18, UPGRADE_V4.md M18)

A certificate bound to models, prices, grading mode and dataset provenance, and nothing in it
noticed when the traffic it was quoted about stopped resembling the split it was measured on.

`optimize/fingerprint.py` is that shape: the task-type mix, input-length quantiles at p10/p50/p90/
p99, and a difficulty **proxy** that says it is one — nothing here carries a difficulty label, so
what is computed is the concentration of the mix and the share sitting in its smallest type, which
is the region a claim covers least. The demo's is `computation 20%, exception 10%, lookup 45%,
two_hop 25%` over 200 tasks, and the Optimize screen shows it under "What this was measured on".

**Distribution drift is not the canary's drift, and they never share a word.**
`CanaryResult.drift_tested` means a re-scored subset whose accuracy difference could have moved;
`distribution_tested` means recent traffic compared, as a distribution, to the certified split. A
certificate can pass every outcome look while being quoted about tasks it never saw — the failure
that looks most like success — so a test class enforces that neither implies the other, and a look
with nothing to compare reports that it *could not* check rather than passing.

Divergence is total variation distance over the mix, which reads as a share of traffic: 0.2 means
a fifth of the queue is in a different type from the one the certificate would predict. Beside it,
the **uncovered region** is named individually, because "the distribution moved" is not something
anybody can act on and "`two_hop` is 43% of recent traffic and was 1% of the certified split, 2 of
200 tasks" is. `tokop canary --drift <recent.jsonl>` is the command; a certificate issued before
M18 still reads, and says it cannot be checked for coverage rather than passing the check.

## Demo result (simulated test fixtures, 200-task test split)

Generated into README.md by `tokop report --write-readme`. Headline: B0 $0.05172 per successful
task, B3 $0.00425 — a 91.8% reduction, accuracy +0.5 points with 95% CI [-3.0, +4.0], verdict
**inconclusive** because the lower bound sits exactly on the 3-point margin; about 4 more tasks
would settle it. The cascade answers 34.5% of tasks at the cheap tier, 42.5% at the mid tier and
23.0% at the frontier — but those are not its spend: because an escalated task pays for every
attempt it made, the cheap tier is 23.4% of the money and the frontier 36.1%. The proof itself
cost $16.61 and repays after 367 tasks.

These are simulated, not recorded (DECISIONS.md D1).

## Known issues

- `make verify` runs all 31 checks with none skipped.
- **The graph and session workloads have no committed recording, by choice.** `tokop graph` and
  `tokop session` run `data/incident-agent/` against the deterministic in-process provider. The
  recording plan in `tokop/recorder.py` is SPEC.md section 6's single-call plan and is left
  exactly as it is; changing it is how committed numbers move by accident (D47).
- **A graph's `tool` and `retrieve` steps return text the workload declares.** Tokop executes no
  tools. That makes `incident-agent` a model of an agent's shape rather than an agent, and every
  surface reporting on it says so.
- **The session comparison refuses its quality half.** This build's provider answers from the task
  and the model alone, so a compacted arm cannot lose accuracy here and any figure would describe
  the simulator (D48.5). The cost half is measured, at three paces, under two pricings.
- **A session's bill is counted, not provider-reported.** The in-process provider has no clock and
  cannot expire a cache entry, which is half of what a session is. The counter and the ratio it
  was scaled by are in the payload.
- **A workload fingerprint over a program-generated split is not a fingerprint of traffic.** M18
  makes a certificate expire on coverage; what it is covering is still a set nobody observed.
- **Two latency bugs the M17 screen work surfaced, both fixed.** Fitting token ratios walks every
  cassette — nineteen seconds here — and `lru_cache` memoizes a *result* without stopping two
  threads both missing and both doing the work, so several tabs opening Inspect together each paid
  for it. It is behind a lock now, with a test that four threads missing together produce one fit.
  And the session comparison is a button rather than something Inspect does on arrival: a screen
  people open to lint a prompt should not run six hundred simulated turns first.
- **The ledger is generated and now has a schema that can go stale.** M16b added `calls.step`;
  `create_all` will not add a column to an existing table, so a ledger built before it is refused
  by name with the one command that fixes it (`tokop build-test-fixtures --ledger-only`).
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
`make verify`: 12 checks, none skipped, green. (At M18: 880 engine tests, 51 Playwright tests,
31 checks.)

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

805 engine tests, 49 Playwright tests, 91% line coverage on `core/` and `optimize/`.
`make verify`: 21 checks, none skipped, green.
