# Tokop implementation plan

Written before M0, from SPEC.md. Deviations are logged in DECISIONS.md as they happen.

## Milestones to files

| M | Delivers | Files |
|---|---|---|
| M0 | Scaffold | `Makefile`, `scripts/verify.sh`, `scripts/dev.sh`, `engine/pyproject.toml`, `web/*` config, `config/providers.yaml`, `tokop/{paths,settings,recording_state,cli}.py`, `tokop/api/app.py`, `docs/DESIGN.md`, `CLAUDE.md`, `PROGRESS.md`, `DECISIONS.md` |
| M1 | Core math | `core/usage.py`, `core/pricing.py`, `core/stats.py`, `core/tokenize.py`, `core/budget.py` + tests |
| M2 | Registry and adapters | `core/registry.py`, `config/models.yaml`, `config/prices.yaml`, `adapters/{base,cassette,anthropic,openai_compatible,simulated}.py`, CLI skeleton, `fixtures/test/` |
| M3 | Workloads | `workloads/{spec,runner,grading}.py`, `workloads/demo/{policy,handbook,generator}.py`, `db.py`, `data/demo/*`, `docs/DATASET.md` |
| M4 | Optimization | `optimize/{lint,findings,transforms,scorers,cascade,proof,report}.py`, `recorder.py`, full CLI |
| M5 | Optimize screen | `web/src/screens/Optimize.tsx` + components, `api/routes.py`, `web/e2e/optimize.spec.ts` |
| M6 | Remaining screens | `web/src/screens/{Inspect,Compare,Spend,Settings}.tsx`, their e2e specs |
| M7 | Finish | `README.md`, `docs/{DEMO,ARCHITECTURE}.md`, `Dockerfile`, screenshot critique, spec review |

## Key interfaces

```python
# core/usage.py
class TokenUsage(BaseModel):
    input_uncached: int; cache_write_5m: int; cache_write_1h: int; cache_read: int
    output_visible: int; output_reasoning: int
    image_in: int; audio_in: int; image_out: int          # reserved, always 0 in v1
    source: Literal["provider", "estimated"]
    raw: dict[str, Any]

def normalize_anthropic(raw: dict) -> TokenUsage
def normalize_openai(raw: dict) -> TokenUsage
def normalize_deepseek(raw: dict) -> TokenUsage
def normalize_openrouter(raw: dict) -> tuple[TokenUsage, Decimal | None]   # provider cost

# core/pricing.py
class ModelPrice(BaseModel):     # $/Mtok + provenance
    input, output, cache_write_5m, cache_write_1h, cache_read, batch_discount
    source_url: str; retrieved: date; verified: bool
def compute_cost(usage: TokenUsage, price: ModelPrice) -> Decimal

# core/stats.py
wilson(k, n) -> Interval
paired_bootstrap_delta_accuracy(a, b, seed) -> Interval
paired_bootstrap_cost_per_success(...) -> Interval
mcnemar_exact(b, c) -> float
verdict(delta_interval, margin) -> Verdict
required_n(p10, p01, delta_hat, margin) -> int | None
active_eval_estimate(g, h, xi, pi) -> Estimate      # A2
optimal_fixed_rate(mse, var_h, c_g, c_h) -> float

# adapters/base.py
class LLMRequest(BaseModel):  provider, model, system, messages, max_tokens, ...
class LLMResponse(BaseModel): text, usage, latency_ms, ttft_ms, raw, reused: bool
class Adapter(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResponse
    async def count_tokens(self, req: LLMRequest) -> int | None
```

## Risks

1. **No API keys and no `RECORD_BUDGET_USD` in this environment.** Nothing can be recorded.
   The whole product needs a response matrix to compute over, so `fixtures/test/` is built by a
   deterministic in-process simulated provider and the UI labels it as simulated test data
   everywhere it is displayed. See DECISIONS.md D1.
2. **Egress policy blocks `openrouter.ai` and `openaipublic.blob.core.windows.net`.**
   `sync-models` is written and unit-tested against a committed listing snapshot but cannot run
   live here; `tiktoken`'s `o200k_base` vocabulary cannot be fetched, so the base token counter
   names itself in every estimate's provenance string. D2, D3.
3. **Coverage floor of 85% on `core` and `optimize`** across a lot of code. Tests go in
   alongside each module, not at the end.
4. **The cascade simulator and the live run must agree.** Without a live run, the simulator is
   checked against brute-force per-task evaluation over the same recorded matrix instead, and
   the report says which check ran.
5. **`report --check` must match UI, API and README exactly.** One function computes the report;
   the API serves it, the e2e tests compare against it, and the README block is written from it.

## Ambiguities and the reversible choice taken

| Ambiguity | Choice |
|---|---|
| Where the ledger lives | `.tokop/tokop.db`, gitignored, rebuilt from fixtures by `tokop report` |
| "Scarce model share" denominator | share of **spend**, not of calls; stated in the hover |
| Judge features in the demo scorer | off, as the spec says; the feature exists and is tested |
| Splits | seeded `random.Random(20260911)`, stratified by answer type |
| Bootstrap resamples | 5,000, seed 20260911, as specified |
| Cost per successful task when successes = 0 | undefined, rendered as "—", excluded from a bootstrap resample rather than made infinite |

---

# Upgrade plan: Tokop without gold labels

Written before the first upgrade milestone, from UPGRADE_V3.md. `SPEC.md` stays the source of
truth for everything the upgrade does not touch; where UPGRADE_V3.md and `SPEC.md` disagree,
UPGRADE_V3.md wins and the supersession is logged in `DECISIONS.md` with the section it
overrides.

## Milestone numbering

UPGRADE_V3.md section 5 numbers its milestones M8 to M12. **M8 is already taken** by the
pluggable-scorer work in `PROGRESS.md` and `DECISIONS.md` D27, so the upgrade's milestones are
shifted by one here. Reality wins over the document (CLAUDE.md); logged as D28.

| Upgrade doc | Here | Delivers |
|---|---|---|
| M8  | **M9**  | U1 prove without gold answers, U2 cost-optimal annotation allocation |
| M9  | **M10** | U3 calibrate without labels, U4 price a checkable output contract |
| M10 | **M11** | U5 certificates expire, U8 dataset provenance |
| M11 | **M12** | U6 semantic entropy, U7 report the tie |
| M12 | **M13** | U9 serving-cost basis, only if a self-hosted tier is in scope |

## M9 to files

| Delivers | Files |
|---|---|
| Gold-free verification | `workloads/verification.py` (new), `tests/test_verification.py`, `tests/test_judge_isolation.py` |
| Judge behaviour for the simulator | `workloads/demo/judge_responder.py` (new) |
| Schema | `workloads/spec.py`: `grading`, `JudgeSpec`, `AnnotationSpec`; `data/demo/workload.yaml` |
| Running a judge | `workloads/runner.py`: `run_judge` |
| Allocation policy (U2) | `optimize/annotation.py` (new), `tests/test_annotation.py` |
| The estimate | `optimize/proof.py`: `JudgedDelta`; `core/stats.py`: `verdict_from_interval` |
| Wiring | `optimize/report.py`, `cli.py` (`annotate`, `prove --grading/--annotation-budget`) |
| Screens | `web/src/screens/Inspect.tsx` annotation row, `web/e2e/` |
| Checks | `scripts/verify.sh`: adversarial judge, variance reduction |

## Design decisions M9 takes

1. **The judge is a recorded model call, not a function.** Non-negotiable 1 says every number
   comes from engine code computing over traces, and a judge verdict is a number on screen. The
   cheap judge and the strong grader are pipelines with cassettes, priced like any other call.
   Their simulated behaviour lives in `workloads/demo/`, the only place invented behaviour is
   allowed.
2. **Annotation is its own command, run after the operating point exists.** The allocation reads
   disagreement between the two arms, and the candidate arm is the cascade, whose resolved tier
   is only known once calibration has chosen a configuration. So `tokop annotate` runs the cheap
   judge, computes pi, draws, spends the budget on the strong grader, and commits
   `fixtures/test/annotations.json`. `tokop prove` reads that set rather than re-drawing, and
   refuses if it was drawn against a different operating point.
3. **The draw is nested in the budget.** Each item carries a fixed uniform `u_t` keyed by task
   id and seed; `xi_t = 1[u_t < pi_t]`. Lowering the budget lowers every `pi_t`, so a smaller
   budget replays as a subset of a larger one's annotations. A budget *above* what was recorded
   is refused with the command that would fix it, never approximated.
4. **The judged path never sees gold, structurally.** `workloads/verification.py` may import the
   contract parsers from `grading.py` and nothing gold-aware, enforced by
   `tests/test_judge_isolation.py` the way `test_scorer_isolation.py` does it for scorers.
5. **The demo keeps `grading: gold` and gains a judge.** Existing headline numbers do not move.
   The judged estimate is computed beside the gold one so the demo can show the thing that
   matters: the gold-free interval covering the gold-graded delta. `tokop prove --grading judged`
   takes the verdict from the judged estimate with golds withheld.
6. **The operating point is still calibrated on gold in M9,** because that is U3's job. The
   judged block names that dependency rather than implying the whole pipeline is label-free.

---

# Upgrade plan: Tokop from laboratory to product

Written before M13, from UPGRADE_V4.md. `SPEC.md` stays the source of truth for everything the
upgrade does not touch. Where UPGRADE_V4.md and `SPEC.md` disagree the conflict is named below
and the conservative option is taken, per CLAUDE.md.

Decisions log continues from **D41**. Milestones are M13 to M18, one at a time, `make verify`
green before each commit.

## Milestone numbering, again

**M13 was already spoken for.** The UPGRADE_V3.md plan above maps its last milestone to repo M13:
U9, a serving-cost basis for a self-hosted tier. It was never started — nothing in this build has
a self-hosted tier, and PROGRESS.md says so under "Next". UPGRADE_V4.md does not list U9 at all,
and its own ranking rule in section 2 ("does it move Tokop from proving things about a simulator
to proving things about someone's traffic?") puts a GPU-hour cost model behind everything here.

So **M13 is the recording**, U9 is deferred rather than renumbered, and PROGRESS.md's "Next"
section is rewritten to say that rather than left pointing at a milestone that now means
something else. Logged as D41 alongside the critique corrections. The same reasoning as D28: the
numbering follows what was built, not what a document expected.

## 0. What the code actually says, checked before planning

UPGRADE_V4.md section 1 asks for each critique claim to be verified against the code first.
Here is that pass. Two of the six need correcting in the upgrade's own favour, and one in the
opposite direction: a thing the upgrade asks for is already written.

| Claim | What is in the repo | Verdict |
|---|---|---|
| "Uses `bytes-bpe-approx-v1`, not real tokenizers" | `O200kCounter` wrapping tiktoken `o200k_base` at `core/tokenize.py:61`; `ApproxCounter` at `:94` is the labelled fallback and names itself in every estimate | **Wrong**, as the upgrade says |
| "…so Anthropic exact counts are missing" | `AnthropicAdapter.count_tokens` at `adapters/anthropic.py:185` posts to `/v1/messages/count_tokens` and is tested at `tests/test_adapters.py:229`. **Nothing else calls it.** The only call site in the whole engine is that test | **Half wrong.** The endpoint is written; it is not wired. M13's job is the wiring, not the adapter |
| "Rewriting context invalidates prefix caches" | `BlockSpec.cache` at `spec.py:48`; `reorder_static_first`, `move_volatile_after_breakpoint`, `add_breakpoint_after_static`, `move_breakpoint_to_static_end` at `transforms.py:185–346` | **Right about what is missing.** Single-request ordering is solved, multi-turn is not modelled at all. M17 |
| "Caps files at 150 lines, MCPs at 10, tools at 80" | No such constant exists anywhere in the engine | **Wrong.** Nothing to do, and nothing to defend against |
| "One workload, no ingestion" | Worse than stated. It is not a missing command but a coupling: `DatasetSpec` names a *generator* (`spec.py:264`), and `runner.py:36`, `report.py:91–93`, `recorder.py:43–45` and `annotate.py:55–57` import `workloads.demo.*` directly | **Right, and larger than described.** M15 |
| "No live recording has ever run" | `tokop record` prints its refusal and exits 3 (`cli.py:97–139`). `require_live_consent` at `recorder.py:519` refuses without both a key and a budget | **Right.** M13 |
| "Prompt lint is purely lexical" | PL01–PL14 in `optimize/lint.py` | **Right** |

Two environment facts that decide what is possible here, both checked rather than assumed:

- `ANTHROPIC_API_KEY`, `RECORD_BUDGET_USD` and `DAILY_BUDGET_USD` are all unset and there is no
  `.env`. **M13 cannot spend anything in this environment as it stands.**
- `api.anthropic.com` is reachable through the proxy: an unauthenticated POST to `/v1/messages`
  returns **401**, which is the API answering. So the blocker is consent and credentials, not
  egress. (arxiv.org and nature.com stay blocked at 403; the README already says so.)

## 1. Where UPGRADE_V4.md and SPEC.md disagree

| Conflict | Conservative option taken |
|---|---|
| CLAUDE.md closes a milestone when "its acceptance check in SPEC.md section 11 passes". SPEC.md section 11 stops at M7 and has no entry for M13–M18 | Each new milestone's acceptance check is added to `scripts/verify.sh` **by name**, citing the milestone, exactly as M9–M12 did. The convention is already de facto; D41 records it once so it stops being implicit |
| SPEC.md section 12 requires the README to carry "the method, with citations… the limitations". M14 moves methodology to `docs/METHOD.md` | Nothing is deleted. The README keeps Limitations and the citations line and links to `METHOD.md` above the fold. Section 12 is satisfied by the pair, and `report --check` gates both files |
| SPEC.md section 13 lists the upload wizard (S3) and trace import (S4) as **stretch goals, only after section 10 passes** | Section 10 does pass — `make verify` is green on 21 checks — so pulling them forward is allowed by SPEC's own ordering, not against it. M15 and M16 are S3 and S4 arriving early |
| SPEC.md section 3 ships "the tooling to record any YAML-defined workload whose JSONL or CSV dataset has gold answers". That tooling was never built | M15 is **owed v1 scope**, not new scope. It is planned as completing section 3 rather than extending it |
| SPEC.md sections 7.3 and 7.5 price a single call per task. M16 and M17 price a graph and a session | Additive only. A pipeline with no `steps:` compiles to a one-step graph and a one-turn session, and a byte-identical-output test is the gate on both milestones |
| SPEC.md section 3 excludes "any proxy or gateway for other applications' traffic" | Agrees with UPGRADE_V4.md M14.4. Deploy exports an artifact and never carries traffic |

## 2. Milestones to files

| M | Delivers | Files |
|---|---|---|
| M13 | One real recording | `core/stats.py` (`power_for_split`), `recorder.py` (projection, confirmation, live path), `core/tokenize.py` + `adapters/*` (exact counts wired), `cli.py` (`power`, `record`), `optimize/report.py` (counter provenance, settling n), `scripts/verify.sh` (manifest-hash check), `fixtures/demo/`, `tests/test_power.py`, `tests/test_recording_projection.py` |
| M14 | The first screen | `optimize/evidence.py` (new), `optimize/report.py` (`readme_block`, `method_block`, `evidence`), `optimize/export.py` (new), `cli.py` (`export`, `report --write-readme`), `api/routes.py`, `web/src/screens/Optimize.tsx`, `web/src/components/EvidencePanel.tsx` + `SummaryBlock.tsx` (new), `README.md`, `docs/METHOD.md` (new), `web/e2e/optimize.spec.ts` |
| M15 | A second workload | `workloads/item.py` + `workloads/bundle.py` + `workloads/ingest.py` (new), `workloads/spec.py` (`DatasetSpec.source`), `runner.py`, `recorder.py`, `annotate.py`, `optimize/report.py`, `optimize/scorers.py`, `cli.py` (`ingest`, `label`), `data/<second>/`, `tests/test_ingest.py`, `tests/test_no_demo_imports.py` |
| M16 | Multi-call workloads | `workloads/spec.py` (`StepSpec`), `optimize/graph.py` (new), `workloads/runner.py`, `optimize/findings.py` (G01–G05), `optimize/proof.py`, `optimize/cascade.py`, `web/src/components/PipelineGraph.tsx`, `data/<graph>/`, `tests/test_graph*.py` |
| M17 | Session and cache accounting | `core/session.py` (new), `core/pricing.py`, `optimize/lint.py` (PL15–PL17), `optimize/report.py`, `workloads/spec.py` (`SessionSpec`), `web/src/screens/Inspect.tsx`, `tests/test_session.py` |
| M18 | Workload identity and drift | `optimize/fingerprint.py` (new), `optimize/certificate.py`, `optimize/report.py`, `cli.py` (`certificate --drift`), `web/src/screens/Optimize.tsx`, `tests/test_fingerprint.py` |

## 3. Interfaces added

```python
# core/stats.py — required_n() already exists at :358; this is the split-sizing wrapper
@dataclass(frozen=True)
class PowerEstimate:
    required_test_n: int | None       # None when the observed deficit already clears the margin
    observed_discordance: float       # p10 + p01 on the run it was read from
    delta_hat: float; margin: float; alpha: float
    source: str                       # which run supplied the discordance
    caveat: str                       # why a simulated discordance is the optimistic case
def power_for_split(payload, *, margin: float, alpha: float) -> PowerEstimate

# recorder.py
@dataclass(frozen=True)
class StepProjection:
    pipeline: str; split: str; tier: str; model_id: str
    calls: int; input_tokens: int; output_tokens: int
    counter: str                      # exact | estimated, and which one
    cost_usd: Decimal
@dataclass(frozen=True)
class RecordingProjection:
    steps: tuple[StepProjection, ...]
    total_usd: Decimal; cap_usd: Decimal; exceeds_cap: bool
    power: PowerEstimate; underpowered: bool
def project(plan, registry, snapshot, counter) -> RecordingProjection
def confirm(projection) -> bool       # prints the table, then asks; --yes skips the question

# core/tokenize.py
class ExactCounter(Protocol):
    name: str
    async def count_request(self, request: LLMRequest) -> TokenCount
async def exact_count(request, adapter) -> TokenCount | None   # None -> labelled estimate

# optimize/evidence.py                                                            (M14)
Grade = Literal["recording", "simulated", "insufficient"]
@dataclass(frozen=True)
class EvidenceInput:
    name: str; value: Any; source: str; blocking: bool; note: str
@dataclass(frozen=True)
class Evidence:
    grade: Grade; reason: str; inputs: tuple[EvidenceInput, ...]
def assess(payload: ReportPayload) -> Evidence

# optimize/export.py                                                              (M14)
Artifact = Literal["prompt-diff", "cascade-config", "routing-rule"]
def export(payload, artifact: Artifact, out: Path) -> ExportResult

# workloads/item.py                                                               (M15)
class Item(Protocol):                 # what runner, report, recorder and annotate depend on
    id: str; question: str; gold: str; answer_type: AnswerType; aliases: tuple[str, ...]
    def router_view(self) -> dict[str, Any]: ...
@dataclass(frozen=True)
class IngestedItem: ...               # DemoItem becomes one implementation among two

# workloads/ingest.py                                                             (M15)
def read_jsonl(path, mapping) -> list[IngestedItem]
def read_csv(path, mapping) -> list[IngestedItem]
def read_otel(path, mapping) -> list[IngestedItem]     # gen_ai.* conventions, version pinned
def ingest(source: Path, fmt: str, out: Path) -> IngestReport   # fills DatasetProvenanceSpec

# workloads/spec.py                                                               (M16)
class StepSpec(BaseModel):
    id: str
    kind: Literal["generate", "tool", "retrieve", "verify", "retry", "loop"]
    model_role: str | None = None
    inputs: list[str] = []            # step ids whose output this step consumes
    system: list[BlockSpec] = []; user: list[BlockSpec] = []
class PipelineSpec(BaseModel):
    steps: list[StepSpec] = []        # empty means the existing single call, unchanged

# core/session.py                                                                 (M17)
@dataclass(frozen=True)
class Turn: blocks: tuple[Block, ...]; at: datetime
@dataclass(frozen=True)
class SessionCost:
    writes_5m: int; writes_1h: int; reads: int; misses: int
    cost_usd: Decimal; prefix_stability: float; expired_entries: int
def session_cost(turns, price, *, ttl: Literal["5m", "1h"]) -> SessionCost

# optimize/fingerprint.py                                                         (M18)
@dataclass(frozen=True)
class Fingerprint:
    task_type_mix: dict[str, float]; length_quantiles: dict[str, float]
    difficulty: dict[str, float]; counter: str; n: int
def fingerprint(items: Sequence[Item], counter: BaseCounter) -> Fingerprint
def divergence(certified: Fingerprint, recent: Fingerprint) -> Divergence  # names the gap
```

## 4. M13. One real recording

The only milestone here that turns a statement about a simulator into evidence, and the only one
that cannot be finished in this environment as it stands. It splits at the money.

**M13a, spends nothing, commits on its own:**

1. `tokop power --workload <path> --margin <pts> --alpha 0.05`. Reads the discordance the
   simulated run observed, calls the existing `required_n`, prints the test-split size a
   conclusive McNemar verdict needs — and prints, in the same breath, that a discordance rate
   read off independently-drawn simulated errors is the optimistic case.
2. `tokop record` gains a **power refusal** before it gains a live call: a split smaller than
   `power` demands is refused, `--underpowered` overrides, and the override is written into the
   run record and printed in the report. Recording an underpowered set is the one mistake that
   spends the whole budget and still returns "inconclusive".
3. **The cost projection.** Tokens in, tokens out, per-tier price, per-step and total, against
   the cap, with the counter that produced each figure named. Then the confirmation.
4. **Exact Anthropic token counts wired.** The endpoint exists; nothing calls it. `exact_count`
   goes in front of the estimator on the projection path and the counter name travels with every
   metric into `provenance`. `ApproxCounter` stays as the labelled fallback.
5. **Resume, demonstrated rather than assumed.** `RecordingAdapter` + `CassetteStore` already do
   exact-request reuse and a kill-and-restart test exists from M2; M13a adds the check that a
   *partially spent* recording resumes without re-paying, asserted on the ledger.
6. `make verify` gains **"recording manifest matches fixtures"**: every cassette hash in the
   manifest resolves, and the manifest's counter matches the one the report computes with.

Then **stop and ask**, per UPGRADE_V4.md section 0.4 and section 3.5.

**M13b, spends money, only on an explicit yes:** the recording, then `prove`, `report`,
`--write-readme`, the provenance block regenerated from whatever real-traffic share the recording
actually produces.

**What it would cost.** Computed from the committed manifest, which prices simulated calls at the
real verified prices, so it is a projection and not a quote — real answers have different lengths:

| Recording | Cost |
|---|---|
| The committed fixture set replicated exactly (sample depth 5, for the scorer comparison) | **$28.43** |
| The same matrix at sample depth 1 | **$16.61** |
| …of which B0 on the frontier model alone | $9.88 |
| Entailment judgements (830 calls) | $0.25 |
| The annotation run (cheap judge on 400, strong grader on the drawn subset) | ~$1.24 |

*Corrected during M13:* the depth-1 figure read $16.50 when this plan was approved, from dividing
each run's recorded cost by its sample depth. The engine's own `single_sample_cost_usd` sums to
**$16.6071**; the difference is the pre-warm call, which is not divided. Nothing else moves.

**Acceptance:** `tokop report` shows a recording-backed result; the provenance gate stops refusing
on the real-traffic ground; the manifest-hash check is in `make verify`; and if the verdict is
still inconclusive the report names the exact n that would settle it.

## 5. M14. The first screen

Cheap, fast, and it changes the first thirty seconds. Restructure the README to promise, flow, one
headline result, How it works, Limitations, link. Everything cut moves to `docs/METHOD.md`, which
is generated by the same code path so the scorer comparison and the effective-sample table stay
regenerated rather than copied.

`metrics_block()` splits into `readme_block()` (the headline table, the verdict, the repayment
line, the simulated mark) and `method_block()` (the scorer comparison, the ties, the contract
table, the pseudo-label diagnosis, the provenance detail). `tokop report --write-readme` writes
both files; `--check` fails if either has moved. Two markers, two files, one check.

The **evidence grade** is the answer to "why should I trust the strong grader": one word at the
top of Optimize, and a click shows the chain that produced it — real-traffic share, n, discordant
pairs, grader agreement, price verification, certificate age — each with its value and its source,
each able to be the blocking one. The grade is `simulated` today and says so; it becomes
`recording` only when M13b has run. **Deploy exports an artifact** — the prompt diff, the cascade
thresholds as config, the routing rule as code — and never carries traffic. If the export slips
the control ships disabled with its one-line reason, per non-negotiable 9.

**Acceptance:** nothing above the fold in the README is a statistical qualification; every number
still regenerates through `report --check`; a Playwright test asserts the grade and the summary
block render from `/api/report` values, and a source test fails on a numeric literal in either
component.

## 6. M15. A second workload

The plumbing, not the command, is the work. `Item` becomes a protocol in `workloads/item.py`,
`DatasetBundle` moves out of `workloads/demo/`, and `runner`, `recorder`, `annotate`, `report` and
`scorers` depend on the protocol. `DemoItem` stays, as one implementation.

`tokop ingest --source <path> --format {jsonl,csv,otel} --out data/<name>/` maps fields into that
protocol and fills `DatasetProvenanceSpec` **from the source** — including the real-traffic count,
which is the number the provenance gate refuses on. OTel GenAI attribute names are pinned to the
convention version read and labelled with it, because that convention is still moving.

**Grader agreement is where this milestone can go wrong quietly.** The chain from production task
to certificate has an unmeasured link and the fix is 50 hand labels. I cannot supply them: a label
I write is a model's label, and presenting it as a human's would be the exact failure the proof
layer exists to prevent. So `tokop label` opens a small labelling loop for the repo owner, the
labels carry a `labelled_by` field, and until a human has run it the evidence panel shows that
link as **unmeasured** rather than showing a number. Agreement is reported when it has been
measured and refused when it has not.

**Acceptance:** `tokop prove --workload data/<second>/workload.yaml` runs clean, and
`tests/test_no_demo_imports.py` fails if any `tokop.workloads.demo` module is imported during it.
Checked on `sys.modules` after a subprocess run, because half the demo imports are lazy and sit
inside functions (`annotate.py:550`, `cli.py:73`, `report.py:717`) — the AST source scan
`test_scorer_isolation.py` uses would miss every one of them, so it runs as the second layer and
not the only one.

## 7. M16. Multi-call workloads

The risk. Split into three commits, each green on its own: **(a)** spec and compilation, **(b)**
the graph findings, **(c)** proof on graph-level outcomes.

A pipeline gains an optional `steps:` list. Absent, it compiles to a one-step graph and the
existing path runs untouched — enforced by a test that recomputes the whole report and diffs the
JSON against a committed snapshot byte for byte. Cost per successful task sums over the graph, so
the optimizer can propose deleting a step rather than only swapping a model.

Five findings, from UPGRADE_V4.md: the same context sent to more than one step; a tool called
twice with identical arguments; a verifier that has never changed an outcome in the traces; a
retry that returns what it retried; a frontier step whose input an earlier step could compress.

**Acceptance:** a graph workload whose winning plan deletes a step, proven at the graph-level
outcome, with single-call output byte-identical.

## 8. M17. Session and cache accounting

Cost over a sequence of turns: writes at the write premium, reads at the read rate, entries
expiring at their TTL — the price fields already exist and carry provenance. Prefix stability
across turns becomes measured rather than assumed. PL15 (a block whose text changes between turns
sits above a breakpoint), PL16 (instructions rewritten rather than appended after the breakpoint),
PL17 (a compaction step rewrites the stable prefix).

Compaction is the interesting one and gets reported from both sides: tokens saved by compacting,
tokens spent re-establishing what compaction dropped, and the change in task success — with an
interval, not a point.

**This milestone also has to restate an existing claim.** B1's "79% on a single call" is true per
request and unknown over a session. Once turns are modelled the README says which claim it is
making. That is a refusal moving one click away, not a number being softened.

**Acceptance:** a multi-turn workload where holding the prefix immutable beats compacting, or the
reverse, reported with an interval.

## 9. M18. Workload identity and drift

The certificate binds to a fingerprint as well as a dataset: the distribution over task types,
input-length quantiles, and a difficulty proxy. A drift check compares recent traffic to the
certified distribution and expires on coverage, not only on time, naming the divergence and the
uncovered region.

`CanaryResult.drift_tested` already exists and means *outcome* drift at a canary look. Distribution
drift is a different thing and gets a different name, so the two never read as one check.

**Acceptance:** a test that shifts the task mix and shows the certificate expiring for
distribution reasons, with the divergence and the uncovered region named.

## 10. Risks

1. **M13 is blocked on a person, not on code.** No key, no budget, and Tokop must never set
   either. Everything in M13a lands without spending; the recording waits. If the answer is no,
   M13 commits as *ready, unspent* and PROGRESS.md names the one command that would change it —
   it does not get marked done.
2. **A recording can come back inconclusive anyway.** The power estimate is only as good as the
   discordance rate it is fed, and the simulator's independently-drawn errors are the optimistic
   case. The mitigation is `tokop power` refusing before the money moves, and the report naming
   the settling n after it.
3. **M16 is the context risk** and is split into three commits for that reason. If a session runs
   short mid-milestone, commit what `make verify` passes and write the rest into PROGRESS.md.
4. **The byte-identical guarantee could fail** under the superset spec. The fallback is a separate
   `GraphSpec` alongside `PipelineSpec`; the guarantee decides, not preference.
5. **Coverage stays at 85% on `core` and `optimize`** across six milestones of new code. Tests
   land beside each module, not at the end.
6. **`report --check` now gates two generated documents.** One code path writes both or they drift.
7. **M15 can quietly invent a human.** Handled above: unmeasured is displayed as unmeasured.

## 11. Ambiguities and the reversible choice taken

| Ambiguity | Choice |
|---|---|
| How much of the 300-item set to record if the budget cannot cover it (UPGRADE_V4 M13) | Record the **test split at the size `tokop power` demands** and calibration with what is left, floor 60. The verdict rests on the test split; calibration only picks thresholds and degrades gracefully |
| Whether to record calibration and test at different depths (same) | **Yes, and different sample depths too:** depth 1 everywhere, which drops the k=3/k=5 scorer grid from the recording and takes the matrix from $28.43 to $16.61. UPGRADE_V4 section 5 says two scorers are enough until a recording can tell them apart; a 5x matrix to compare scorers is exactly the spend that buys the least |
| What to cut first if even that does not fit | **Not B0.** It is 60% of the bill and it is also the thing every saving is measured against. Scale n down instead, and let `power` say whether what is left can still conclude |
| Graph as a new spec version or a superset (UPGRADE_V4 M16) | **Superset**, conditional on the byte-identical test passing. `PipelineSpec.steps` empty means today's single call. If byte-identical fails, fall back to a parallel `GraphSpec` and log it |
| Which second workload to ship (M15) | A **real, permissively-licensed, gold-labelled set** if one can be fetched and its licence checked at M15 time; otherwise a second *program-generated* workload in a different shape (structured extraction, JSON-field answers), shipped with its provenance saying exactly that. The acceptance check is about the plumbing either way, and a second simulated workload must not be described as a second piece of evidence |
| Who supplies the 50 hand labels (M15) | Not me. `tokop label` for the owner, `labelled_by` on every label, and the agreement figure refused until a human has run it |
| Where the evidence grade lives (M14) | Engine-side in `optimize/evidence.py`, served in the report payload, rendered with no literals. A grade computed in a component is a metric literal wearing a hat |
| Whether `--underpowered` should exist at all (M13) | Yes, because the alternative is someone editing the split to get past the refusal. It prints, it is stored in the run record, and it shows in the report |
| What "Deploy" does when the export is not ready (M14) | Disabled with its reason, per non-negotiable 9. Never a control that looks live |
| Distribution drift vs. the existing canary drift (M18) | Separate names, separate fields, both reported. Collapsing them would let a passing outcome check imply a covered distribution |
| U9, the serving-cost basis the V3 plan mapped to M13 | **Deferred, not renumbered.** It is reversible: nothing is deleted and the V3 plan's row stands as written. A GPU-hour column with no self-hosted tier behind it would be a fake column, which is the one thing this repo has consistently refused to ship |
