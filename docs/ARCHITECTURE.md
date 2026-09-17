# Architecture

Tokop runs as one FastAPI process in production. FastAPI serves the API and built React
application; SQLite stores the local ledger. There is no queue or worker.

```text
Browser / CLI
    │
    ▼
FastAPI ── /api/* and the built SPA
    │
    ▼
Report builder ── lint, cascade search, proof, evidence, export
    │
    ▼
Workload runner ── render, call, normalize, price, grade, record
    │
    ▼
Adapters ── Anthropic, OpenAI-compatible, cassette, simulated
    │
    ▼
Core ── usage, pricing, tokens, statistics, budgets, registry, sessions
```

## Metric source

`optimize/report.py:build_report()` produces every headline metric. All public surfaces consume the
same result:

- the API serves it;
- the CLI prints it;
- `tokop report --write-readme` updates the generated README and method blocks;
- `scripts/write_demo.py` updates the demo script; and
- Playwright compares displayed values with `/api/report`.

`tokop report --check` verifies that these surfaces agree. Aggregate values should not be copied
into UI components, tests, or prose.

## Core

The modules under `core/` perform calculations and do not make network or filesystem calls.

### Usage and pricing

`usage.py` maps provider payloads to a common set of token buckets. Provider semantics differ:
Anthropic reports input after the final cache breakpoint separately, while OpenAI includes cached
tokens in `prompt_tokens`. The mapping functions include the source documentation and verification
date, and tests cover each mapping with hand-calculated examples.

`pricing.py` uses `Decimal` throughout. Each price includes its source URL, retrieval date, and
verification status. A run stores a content-addressed price snapshot, so later registry changes do
not alter historical results.

### Statistics and token estimates

`stats.py` implements Wilson intervals, paired bootstrap intervals, the exact McNemar test,
non-inferiority decisions, power estimates, and the active-evaluation estimator. Simulation tests
cover properties such as long-run interval coverage and estimator bias.

`tokenize.py` includes the counting method in every estimate. If the preferred tokenizer is not
available, the fallback method is reported to the caller.

### Sessions and budgets

`session.py` prices a sequence of turns. It models cache expiry, time between turns, growing history,
and periodic compaction. Assumptions that providers do not document, such as whether a cache read
refreshes its lifetime, remain explicit in the result.

`budget.py` rejects a provider call before it would exceed a run or daily cap. The guard runs only on
cassette misses. Replayed calls cost nothing and do not consume budget.

## Adapters and cassettes

Tokop uses direct Anthropic and OpenAI-compatible adapters so usage normalization remains visible and
testable. `RecordingAdapter` adds exact-request reuse; `ReplayAdapter` reads recorded cassettes; the
simulated adapter generates deterministic fixture data without opening a socket.

A cassette key is the SHA-256 hash of the provider, model, and canonical request. This provides:

- **Resume support:** a restarted recording reuses completed requests.
- **Safe replay:** a missing cassette raises `CassetteMiss` and never triggers a live request.
- **Auditability:** the stored response is tied to the exact request that produced it.

Long block text is stored once in `cassettes/blobs/` and referenced by hash to avoid repeating large
grounding documents in every cassette.

## Workloads

`workloads/spec.py` loads YAML workload definitions. A workload defines its dataset, pipelines,
grading policy, provider restrictions, cascade configuration, and optional graph steps.

`workloads/graph.py` compiles both single-call and multi-step pipelines. A pipeline without `steps:`
becomes one `generate` node. Tests compare the rendered cassette keys of the graph and legacy paths,
which protects existing recordings from request-shape changes.

`workloads/ingest.py` accepts JSONL, CSV, and OpenTelemetry GenAI exports. OpenTelemetry spans contain
model completions but usually have no reference answer; ingestion leaves `gold` empty instead of
treating a completion as its own label.

`workloads/item.py` defines the neutral item protocol and `bundle.py` loads task data relative to the
workload file. Neutral modules do not import `tokop.workloads.demo`; `test_no_demo_imports.py` checks
this in fresh processes and at runtime.

### Runner

`workloads/runner.py` renders each task, calls the adapter, normalizes usage, applies the run's price
snapshot, grades the answer, and writes a row. It pre-warms each distinct cacheable prefix before
parallel task execution. Without this step, the initial fan-out would create multiple cache writes.

Errors are stored and graded as unsuccessful tasks. A pipeline that fails on part of the dataset is
measured against the full dataset.

### Graders, judges, and scorers

These components have separate interfaces because they receive different information:

- A **grader** in `workloads/grading.py` may use the reference answer.
- A **verifier** in `workloads/verification.py` sees the question, grounding, and proposed answer,
  but not the reference answer.
- A **cascade scorer** in `optimize/scorers.py` estimates whether to accept or escalate an answer and
  cannot access the reference answer.

Isolation tests check the types, imports, source, and rendered judge variables. Allocation policies
that select tasks for strong grading follow the same restriction.

The scorer registry includes deterministic logistic scoring, exact-match self-consistency, and
semantic entropy based on bidirectional entailment. `sep-v1` is registered as unsupported because
provider APIs do not expose the hidden states it requires. Sampling and entailment calls are charged
to the scorer's per-task cost.

## Optimization and proof

The modules under `optimize/` derive recommendations from recorded task rows:

- `lint.py` applies local prompt rules and ranks findings by confidence-weighted savings.
- `cascade.py` evaluates routing thresholds from the recorded response matrix without provider calls.
- `proof.py` compares matching baseline and candidate tasks and refuses mismatched task sets.
- `annotation.py` allocates a strong-grading budget using judge uncertainty and arm disagreement.
- `pseudolabels.py` supports majority-vote and penalized calibration when gold labels are unavailable.
- `entailment.py` groups sampled answers by bidirectional semantic entailment and reports effective
  sample size.
- `ties.py` retains configurations whose cost intervals overlap instead of forcing a single winner.
- `graph_findings.py` detects unused outputs, repeated calls, and duplicated context across steps.
- `certificate.py` binds a result to its data and model snapshot and controls repeated canary looks.
- `fingerprint.py` describes task mix, input lengths, and the configured difficulty proxy.
- `evidence.py` assigns the report's evidence grade from the weakest required evidence link.
- `export.py` writes the prompt diff, operating point, and Python routing rule.

Graph and session reports use the deterministic in-process provider and are reported separately from
the committed single-call demo recording.

## Runtime modes

| Behavior | Replay | Live |
| --- | --- | --- |
| Adapter | `ReplayAdapter` over cassettes | Provider adapter wrapped by `RecordingAdapter` |
| Missing cassette | Raises `CassetteMiss` | Calls the provider and records the response |
| Identical request | Reads the cassette | Reuses the cassette |
| Provider spend | None | Enforced by the spend guard |
| Browser controls | Disabled with a reason | Enabled when credentials and budget are present |

`make demo` and the test suite use replay mode. Live recording requires a provider key and
`RECORD_BUDGET_USD`.

### Recording gates

`tokop record` performs four checks before the full run:

1. **Consent:** require live mode, a key, and an explicit recording budget.
2. **Power:** estimate whether the test split can produce a conclusive McNemar result. The
   `--underpowered` override is stored in the run record.
3. **Projection:** render and price the planned requests as a floor-to-ceiling range.
4. **Pilot:** run a small sample and update the projection using measured output lengths.

Pilot responses become normal cassettes and are reused by the full run.

## Storage and deletion

SQLite stores runs, calls, normalized usage, raw provider usage, costs, grades, scores, and optional
prompt and response content. `delete_run_content` removes stored text while retaining the derived
metrics.

## Web application

The frontend uses React 18, strict TypeScript, Vite, Tailwind, React Flow, and TanStack Query.
FastAPI serves the production bundle and falls back to `index.html` for client routes.

Shared components encode presentation rules: `Figure` distinguishes measured and estimated values,
`Metric` accepts uncertainty information, and disabled `Button` instances require a reason.

## Verification

`make verify` runs formatting, static analysis, engine tests, fixture checks, report consistency,
frontend lint and type checks, a production build, and Playwright flows. Important behavioral tests
cover:

- provider usage normalization and price snapshots;
- confidence-interval coverage and active-estimator bias;
- recording resume behavior and per-call budget enforcement;
- grader, verifier, scorer, and allocation isolation;
- adversarial judges and label-free calibration;
- graph request compatibility and demo-module isolation;
- generated report and export consistency; and
- deletion of stored content without changes to derived metrics.

## Simulated fixtures

`fixtures/test/` was produced by the deterministic in-process provider. It emits provider-shaped
usage, models cache writes and reads, and runs through the normal usage and pricing code. Separate
responders simulate answer, judge, and entailment errors.

The simulation does not establish real model quality or production traffic coverage. Reports label
all derived values as simulated. `DECISIONS.md` D1 documents the fixture model, and D29 documents the
injected-judge tests used for label-free evaluation.
