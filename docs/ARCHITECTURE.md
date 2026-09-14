# Architecture

One process in production: FastAPI serves `/api/*` and the built SPA. SQLite holds the local
ledger. No queue, no worker, no external service.

```
                    ┌─────────────────────────────────────────┐
  browser ─────────▶│  FastAPI  ·  tokop.api.app              │
                    │    /api/*        routes.py              │
                    │    /*            the built SPA          │
                    └───────────────┬─────────────────────────┘
                                    │  every number comes from
                                    ▼
                    ┌─────────────────────────────────────────┐
                    │  optimize/report.py  build_report()     │
                    │  the single computation                 │
                    └──┬──────────┬──────────┬──────────┬─────┘
                       │          │          │          │
              ┌────────▼──┐ ┌─────▼────┐ ┌───▼─────┐ ┌──▼──────┐
              │  lint     │ │ findings │ │ cascade │ │  proof  │
              │ PL01-14   │ │ W01-W06  │ │ + search│ │ + stats │
              └────────┬──┘ └─────┬────┘ └───┬─────┘ └──┬──────┘
                       └──────────┴──────────┴──────────┘
                                    │  computes over
                                    ▼
                    ┌─────────────────────────────────────────┐
                    │  workloads/runner.py                    │
                    │  renders · calls · normalizes · grades  │
                    └───────────────┬─────────────────────────┘
                                    │
              ┌─────────────────────▼───────────────────────┐
              │  adapters/                                  │
              │    anthropic · openai_compatible            │
              │    recording (exact-request reuse)          │
              │    cassette  (replay; CassetteMiss)         │
              │    simulated (deterministic, no socket)     │
              └─────────────────────┬───────────────────────┘
                                    │
                    ┌───────────────▼─────────────────────────┐
                    │  core/  usage · pricing · stats         │
                    │         tokenize · budget · registry    │
                    └─────────────────────────────────────────┘
```

## The one rule that shapes everything

**Every number in the UI and the README comes from engine code computing over traces.** The way
to achieve that rather than intend it is to have exactly one function that produces every
headline number — `optimize/report.py:build_report()` — and to let everything else read its
output.

- The API serves it.
- The CLI prints it.
- `tokop report --write-readme` generates the README's metrics block from it.
- `scripts/write_demo.py` generates the demo script from it.
- The Playwright tests compare what the screen *displays* against what `/api/report` *returns*,
  rather than against numbers written into the test.
- `tokop report --check` asserts all of that still agrees, and `make verify` runs it.

There is no second path by which a number can reach a screen.

## Layers

### `core/` — the maths, with no I/O

`usage.py` normalizes every provider's usage payload into one set of buckets. This is the most
consequential file in the repo, because a cost is only as trustworthy as this mapping, and every
provider gets it subtly differently: Anthropic's `input_tokens` counts only *past the last cache
breakpoint*, while OpenAI's `prompt_tokens` *already includes* its cached tokens. One of those
needs adding and the other needs subtracting, and getting it backwards is a 10x error nobody
would see. Every mapping function carries the documentation URL and the date it was checked, and
every trap has a hand-computed test.

`pricing.py` is Decimal end to end, and every price carries the URL it came from, the date, and
whether a human verified it. Runs are costed from a **content-addressed price snapshot**, so a
price change tomorrow cannot move a result recorded today.

`stats.py` holds every statistic the product claims: Wilson intervals, a paired bootstrap, an
exact McNemar test, the non-inferiority verdict, and the active-evaluation estimator from
Angelopoulos et al. Two of its tests are simulations rather than assertions, because a confidence
interval is a claim about long-run coverage and an estimator's unbiasedness is a claim about a
long-run mean — neither can be checked by inspecting one call.

`tokenize.py` counts tokens and **names its method in every estimate it produces**. When
`o200k_base` cannot be loaded, the fallback says so in the string that reaches the UI rather than
producing a plausible number silently.

`budget.py` refuses a call that would breach a cap, and never raises one.

### `adapters/` — the wire, written by hand

Both provider adapters are hand-written rather than routed through a gateway library. Usage
normalization *is* the product; putting the most important code in the build behind someone
else's abstraction would defeat it.

The interesting piece is `cassette.py`. A cassette is keyed by the SHA-256 of provider, model and
the canonicalized request, and that one decision buys three things:

- **Resumability.** An interrupted `make record` reuses every response it already paid for, so
  recording a $20 dataset is not a $20 gamble. A test hard-kills a recorder after 4 of 10 calls
  and asserts the restart reaches the provider exactly 10 times in total.
- **Replay.** The whole app runs from cassettes with no API keys. A missing key raises
  `CassetteMiss` naming the request; replay **never** falls back to a live call, because a demo
  that quietly spends money is worse than one that fails loudly.
- **Auditability.** Every cassette holds the request that produced it.

Block texts over 512 characters are stored once under `cassettes/blobs/<sha256>.txt` and
referenced by hash. The demo handbook is 18 kB and appears in all ~1,300 requests; inline it made
the fixture set 34 MB of the same paragraph, and 6.2 MB deduplicated.

### `workloads/` — running and grading

`runner.py` renders each task's request, sends it, normalizes the usage, costs it against the
run's own snapshot, grades it, and writes a row. It is deliberately boring: everything
interesting is computed later from those rows.

Two behaviours worth knowing. It **pre-warms** each distinct cacheable prefix with a
`max_tokens: 0` request and awaits it before fanning out, because a cache entry only exists once
the first response begins — a parallel fan-out with no pre-warm misses on every request and pays
the 1.25x write price for the privilege. And a failed call is recorded with its error and graded
as unsuccessful, never dropped: a pipeline that crashes on 5% of tasks is 5% worse, not 5%
smaller.

**Checkers live in `workloads/grading.py` and scorers live in `optimize/scorers.py`, and the
separation is load-bearing.** A checker sees the gold answer and decides whether a task
succeeded. A scorer never sees gold and *guesses*, because at run time on an ungraded task a
guess is all there is. A scorer that could reach a gold answer would make every measurement
circular, so `TaskView` has no field that could carry one and
`tests/test_scorer_isolation.py` checks that structurally, behaviourally and at the source level.

**Which scorer runs is named in the pipeline spec.** `optimize/scorers.py` holds a `Scorer`
protocol and a registry; `CascadeSpec` names a kind and a sample count, and the calibration
search chooses between the kinds a workload allows alongside the thresholds — all before any
test result is computed. Two are registered: `logistic-v1`, the per-tier logistic regression over
deterministic features, and `self-consistency-v1`, which draws k samples and routes on how much
a tier agrees with itself.

The isolation survives the second one by injection rather than import. A sampling scorer needs
to know when two answers mean the same thing, and the obvious source for that is the grader —
which is exactly what `scorers.py` may not import. So the *workload* supplies the relation
(`grading.answers_equivalent`, a function of two candidate answers, neither of them a reference)
and the scorer receives it through `ScorerContext`. The source-level isolation check keeps
holding, and a workload that supplies no such relation gets a refusal rather than a silent
fallback to string equality.

A scorer that samples is charged for every sample it draws, through the same per-task
accounting as everything else, and the answer graded is the one it returned rather than the
first one drawn. `DECISIONS.md` D27 records why the demo measures the sampling scorer but does
not adopt it.

**`provenance.py` asks where the task set came from**, and refuses to certify on the answer. The
declared origins are a claim the workload makes; the tail coverage, the share of items in
rarely-seen templates and the concentration across templates are computed from the items. The
detector that would resample a set towards human-written text is deliberately absent — it needs a
model, and one that guessed would resample towards its own guess.

**`verification.py` is the gold-free half of `grading.py`,** and the same isolation applies for a
stronger reason. A *verifier* reads the question, the grounding document and the answer, and
returns a verdict; it never sees gold, because a judge that could reach the answer key would make
the entire label-free proof circular. `AnswerView` has no field that could carry one, and
`tests/test_judge_isolation.py` checks it structurally, at the source level, and through the
prompt itself — the four variables a judge prompt may render are asserted, so a workload cannot
smuggle an answer key in through a template. The allocation policy in `optimize/annotation.py` is
held to the same rule, because it decides *which* items get a strong label and would otherwise be
choosing the ones it already knew the answer to.

A judge is a model call like any other: the same `Runner`, the same pre-warming, the same
cassettes, the same price snapshot. That is what makes a judge verdict a number computed over a
trace rather than a number a function made up.

### `optimize/` — the analysis

`lint.py` is local and deterministic and makes **no model calls**. Its ranking rule is the whole
point: findings are ordered by projected dollars per 1,000 tasks, weighted by confidence, so
hygiene sinks. One consequence surprises people: cutting words from a *cached* prefix saves about
a tenth of what the same cut saves in uncached input, because cache reads cost 0.1x. The lint
prices them at the read rate and says so.

`cascade.py` evaluates any threshold setting exactly from the recorded response matrix, so a
2,601-setting grid search is arithmetic rather than an API bill. It ships with a second,
deliberately naive `brute_force` implementation used only to check the first — a simulator that
agrees only with itself proves nothing.

`proof.py` pairs the two arms over the same tasks and refuses to compare arms that answered
different task sets rather than approximating. It carries two comparisons: the gold one, and —
when an annotation set exists — the same comparison with the answer key withheld, built on
`core/stats.py`'s active-evaluation estimator.

`annotation.py` decides where the annotation budget goes (UPGRADE_V3.md U2). The sampling rate is
proportional to the judge's uncertainty on both arms plus a boost for pairs the arms disagree
about, which is a proxy for how large the correction on that item will be; the budget fixes the
mean rate and a floor keeps every item samplable, because at a rate of zero the inverse weight is
infinite and the estimator's unbiasedness is gone. It also owns `annotations.json`, the committed
record of what was drawn, what was spent and what every verdict was —
`tokop fixtures-check` replays each of those verdicts through the parser and fails if one
disagrees with the call it came from.

`../annotate.py` is the driver, a sibling of `recorder.py`, and a separate command for a reason
that is not organisational: the allocation reads disagreement between the two arms, and the
candidate arm is a cascade whose per-task tier is only known once calibration has chosen an
operating point. `DECISIONS.md` D29 records the rest.

`certificate.py` turns a report into something with an expiry, and takes the looks that keep it
honest. Alpha is spent across the looks a certificate plans — the looks are independent samples,
not accumulating data, so the exact correction is a product over `(1 - alpha_k)` rather than a
group-sequential boundary — and a changed model snapshot raises before any statistics are
computed, because a certificate about a model that is no longer there is not a certificate.

`pseudolabels.py` stands in for an answer key on the **calibration** split, for the other half of
the same problem: thresholds have to be fitted somehow, and a workload with no labels has none
there either. Two kinds are registered so the comparison is always available — a plain majority
vote over pooled generations, and `penalized-v1`, which will not let a tier vote on its own
label, excludes and counts distributions with no clear winner, and weights confident disagreement
up rather than down. A tier's own repetition is the failure it is built for: a cheap model whose
mistakes repeat looks, to a majority vote, exactly like a cheap model that is right.

## Modes

| | replay | live |
|---|---|---|
| Adapter stack | `ReplayAdapter` over cassettes | provider adapter wrapped in `RecordingAdapter` |
| A missing cassette | raises `CassetteMiss` | recorded |
| An identical request | served from disk | reuses its cassette, pays nothing |
| Spend | zero, structurally | capped by the spend guard |
| Live controls | disabled, each with its computed reason | enabled |

`make demo` and the whole test suite run in replay. Live mode needs both a key and an explicit
`RECORD_BUDGET_USD` — setting that variable *is* the consent to spend, and Tokop never sets it.

## The ledger

SQLite, one file, single user. Raw usage is stored next to the normalized buckets on every call,
so an auditor can redo the mapping rather than take Tokop's word for it. `delete_run_content`
drops stored prompts and outputs for a run and **leaves every metric intact**, so honouring a
deletion request does not cost the user the result they paid for.

## The web app

Vite, React 18, TypeScript strict, Tailwind, React Flow for the pipeline graph, TanStack Query
for fetching. Types for the route surface are generated from FastAPI's OpenAPI schema with
openapi-typescript; the report body is a plain dict server-side, so its interfaces are
hand-written and kept honest by `tokop report --check` and the e2e suite.

The design rules in `docs/DESIGN.md` are enforced in code, not remembered at each call site:
`Figure` cannot render a measured number and an estimated one the same way, `Metric` takes an
interval, and `Button`'s type requires a reason whenever it is disabled.

## Testing

- **653 engine tests**, 91% line coverage on `core/` and `optimize/`
  (`pytest --cov=tokop/core --cov=tokop/optimize`).
- **44 Playwright tests** against the production build in replay mode.
- **Exit-code tests for `tokop prove`** run through a subprocess, because an exit code asserted
  in-process is not the thing CI observes. They pin the case that matters: an *inconclusive*
  verdict fails the build.
- Two **simulations** rather than assertions: Wilson coverage over 4,000 trials, and estimator
  unbiasedness over 2,000 trials against known ground truth with a deliberately biased cheap
  rater, to prove the correction is doing the work.
- A **resume test** that hard-kills a recorder mid-run in a subprocess.
- A **scorer-isolation test** that fails if a scorer can reach a gold answer, and a
  **judge-isolation test** that does the same for the verifier and the allocation policy.
- An **adversarial judge test**, which is a release blocker: a judge that marks 20% of one class
  of correct answers wrong must break the naive estimate and not the corrected one.
- A **label-free calibration test** over a constructed set where a cheap tier's mistakes repeat,
  and a **negative-delta disclosure test** that fails if the checkable-contract row ever reports
  the money it saved without the accuracy it cost.
- An **alpha-spending simulation** over 40,000 certificate lifetimes, which also measures what
  *uncorrected* weekly looking would have cost, and a **collapse-refusal test** over a set piled
  onto a fifth of its template space.
- A **variance-reduction test** that compares the allocation policy against uniform sampling at
  the same budget by evaluating the estimator's variance exactly, rather than by drawing once
  and eyeballing the interval.
- `make verify` runs all nineteen checks in SPEC.md section 10 plus the UPGRADE_V3.md acceptance
  checks, in order, and prints `VERIFY PASSED (19 checks)`.

## What is simulated in this build

This build had no API credentials, so `fixtures/test/` was generated by a deterministic
in-process provider (`adapters/simulated.py`) that opens no socket and is refused in live mode.
It simulates prompt-caching economics honestly — writes, reads, and silently declining below a
model's minimum — and emits Anthropic-shaped usage payloads so the real normalizer runs over
them.

What it cannot simulate is whether a model is actually right; answer quality comes from a
responder with a per-tier, per-difficulty accuracy model, plus a legibility tax when a pipeline
asks for a checkable answer. A second responder invents how often a *judge* is wrong, with sensitivity and specificity given separately because the characteristic
LLM-judge failure is leniency rather than error. Everything downstream is real code over those
traces, and every surface that displays a number derived from them labels it simulated.
`DECISIONS.md` D1 records the whole arrangement, and D29 records why the guarantee U1 rests on is
tested against an *injected* judge rather than against that table.
