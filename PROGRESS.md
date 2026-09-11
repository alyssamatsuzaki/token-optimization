# Progress

## Milestones

| M | State | Notes |
|---|---|---|
| M0 Scaffold | **done** | layout, uv + pnpm, Makefile, verify, design |
| M1 Core math | **done** | usage, pricing, stats, tokenize, budget; 138 tests, 98% cover |
| M2 Registry and adapters | **done** | prices verified vs. Anthropic docs; both adapters; cassettes; resume test |
| M3 Workloads | **done** | generator, handbook, grading, runner, ledger, fixtures/test/ |
| M4 Optimization | not started | |
| M5 Optimize screen | not started | |
| M6 Remaining screens | not started | |
| M7 Finish | not started | |

## Next

M4 Optimization: lint (PL01-PL14), workload findings (W01-W06), transforms, scorers, the
cascade and its simulator, proof, report JSON, and the live `make record` path.

## Demo numbers so far (simulated test fixtures, 200-task test split)

| Pipeline | Accuracy | Cost | Cost per successful task |
|---|---:|---:|---:|
| B0 frontier | 95.5% | $8.92 | $0.0467 |
| B1 frontier | 95.5% | $2.03 | $0.0106 |
| B2 frontier | 97.0% | $1.21 | $0.0062 |
| B2 mid | 97.0% | $0.48 | $0.0025 |
| B2 cheap | 78.5% | $0.18 | $0.0012 |

B3's cascade result arrives with M4. These are simulated, not recorded (DECISIONS.md D1).

## Known issues

- `make verify` currently skips 4 checks (pytest, fixtures-check, report --check, playwright).
  Each is skipped with a stated reason and must be unskipped by the milestone that builds it.
- `tiktoken` cannot load `o200k_base` in this environment (D3), so token estimates name
  `bytes-bpe-approx-v1` as their method until the vocabulary is reachable.
- `tokop sync-models` is implemented and unit-tested but cannot run live here: openrouter.ai is
  blocked by the egress policy (D2). The committed snapshot is a hand-built fixture and says so
  in its own `_tokop_source` field, which the registry surfaces as the price provenance note.

## Open decisions

See DECISIONS.md. D1 (no credentials, simulated fixtures) shapes every later milestone.
