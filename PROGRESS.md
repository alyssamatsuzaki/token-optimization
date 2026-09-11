# Progress

## Milestones

| M | State | Notes |
|---|---|---|
| M0 Scaffold | **done** | layout, uv + pnpm, Makefile, verify, design |
| M1 Core math | **done** | usage, pricing, stats, tokenize, budget; 138 tests, 98% cover |
| M2 Registry and adapters | **done** | prices verified vs. Anthropic docs; both adapters; cassettes; resume test |
| M3 Workloads | **done** | generator, handbook, grading, runner, ledger, fixtures/test/ |
| M4 Optimization | **done** | lint, findings, scorers, cascade, proof, report; 406 tests, 95% cover |
| M5 Optimize screen | **done** | graph, findings, proof, frontier chart, trace drawer; 15 e2e pass |
| M6 Remaining screens | not started | |
| M7 Finish | not started | |

## Next

M6 Remaining screens: Inspect (with Brief mode), Compare, Spend, Settings, and the New
experiment form, each with its Playwright checks.

## Demo result (simulated test fixtures, 200-task test split)

Generated into README.md by `tokop report --write-readme`. Headline: B0 $0.04670 per successful
task, B3 $0.00393 — a 91.6% reduction, accuracy +0.5 points with 95% CI [-3.0, +4.0], verdict
**inconclusive** because the lower bound sits exactly on the 3-point margin; about 4 more tasks
would settle it. The cascade routes 34.5% cheap / 42.5% mid / 23.0% frontier. The proof itself
cost $13.80 and repays after 339 tasks.

These are simulated, not recorded (DECISIONS.md D1).

## Known issues

- `make verify` now runs all 11 checks with none skipped.
- `tiktoken` cannot load `o200k_base` in this environment (D3), so token estimates name
  `bytes-bpe-approx-v1` as their method until the vocabulary is reachable.
- `tokop sync-models` is implemented and unit-tested but cannot run live here: openrouter.ai is
  blocked by the egress policy (D2). The committed snapshot is a hand-built fixture and says so
  in its own `_tokop_source` field, which the registry surfaces as the price provenance note.

## Open decisions

See DECISIONS.md. D1 (no credentials, simulated fixtures) shapes every later milestone.
