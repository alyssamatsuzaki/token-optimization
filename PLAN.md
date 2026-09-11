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
