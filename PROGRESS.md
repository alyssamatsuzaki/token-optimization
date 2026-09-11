# Progress

## Milestones

| M | State | Notes |
|---|---|---|
| M0 Scaffold | **done** | layout, uv + pnpm, Makefile, verify, design |
| M1 Core math | **done** | usage, pricing, stats, tokenize, budget; 138 tests, 98% cover |
| M2 Registry and adapters | **done** | prices verified vs. Anthropic docs; both adapters; cassettes; resume test |
| M3 Workloads | not started | |
| M4 Optimization | not started | |
| M5 Optimize screen | not started | |
| M6 Remaining screens | not started | |
| M7 Finish | not started | |

## Next

M3 Workloads: the demo generator, DATASET.md and fixtures-check, pipeline YAML for B0/B1/B2,
the runner with traces, grading, and the SQLite ledger.

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
