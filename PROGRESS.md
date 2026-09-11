# Progress

## Milestones

| M | State | Notes |
|---|---|---|
| M0 Scaffold | **done** | layout, uv + pnpm, Makefile, verify, design |
| M1 Core math | **done** | usage, pricing, stats, tokenize, budget; 138 tests, 98% cover |
| M2 Registry and adapters | not started | |
| M3 Workloads | not started | |
| M4 Optimization | not started | |
| M5 Optimize screen | not started | |
| M6 Remaining screens | not started | |
| M7 Finish | not started | |

## Next

M2 Registry and adapters: model sync and price overrides verified against Anthropic's own docs,
both adapters, cassette record/replay with exact-request reuse, the CLI skeleton, fixtures/test/.

## Known issues

- `make verify` currently skips 4 checks (pytest, fixtures-check, report --check, playwright).
  Each is skipped with a stated reason and must be unskipped by the milestone that builds it.
- `tiktoken` cannot load `o200k_base` in this environment (D3), so token estimates will name
  `bytes-bpe-approx-v1` as their method until the vocabulary is reachable.

## Open decisions

See DECISIONS.md. D1 (no credentials, simulated fixtures) shapes every later milestone.
