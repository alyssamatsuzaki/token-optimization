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
different task sets rather than approximating.

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

- **466 engine tests**, 91% line coverage on `core/` and `optimize/`
  (`pytest --cov=tokop/core --cov=tokop/optimize`).
- **39 Playwright tests** against the production build in replay mode.
- **Exit-code tests for `tokop prove`** run through a subprocess, because an exit code asserted
  in-process is not the thing CI observes. They pin the case that matters: an *inconclusive*
  verdict fails the build.
- Two **simulations** rather than assertions: Wilson coverage over 4,000 trials, and estimator
  unbiasedness over 2,000 trials against known ground truth with a deliberately biased cheap
  rater, to prove the correction is doing the work.
- A **resume test** that hard-kills a recorder mid-run in a subprocess.
- A **scorer-isolation test** that fails if a scorer can reach a gold answer.
- `make verify` runs all eleven checks in SPEC.md section 10, in order, and prints
  `VERIFY PASSED (11 checks)`.

## What is simulated in this build

This build had no API credentials, so `fixtures/test/` was generated by a deterministic
in-process provider (`adapters/simulated.py`) that opens no socket and is refused in live mode.
It simulates prompt-caching economics honestly — writes, reads, and silently declining below a
model's minimum — and emits Anthropic-shaped usage payloads so the real normalizer runs over
them.

What it cannot simulate is whether a model is actually right; answer quality comes from a
responder with a per-tier, per-difficulty accuracy model. Everything downstream is real code over
those traces, and every surface that displays a number derived from them labels it simulated.
`DECISIONS.md` D1 records the whole arrangement.
