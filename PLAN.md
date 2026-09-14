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
