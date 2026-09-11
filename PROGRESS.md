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
| M6 Remaining screens | **done** | Inspect+Brief, Compare, Spend, Settings, New experiment; 38 e2e pass |
| M7 Finish | **in progress** | README, DEMO, ARCHITECTURE, Dockerfile, critique done; spec review running |

## Next

Fix whatever the spec review reports, then a final `make verify`.

## Demo result (simulated test fixtures, 200-task test split)

Generated into README.md by `tokop report --write-readme`. Headline: B0 $0.04670 per successful
task, B3 $0.00393 — a 91.6% reduction, accuracy +0.5 points with 95% CI [-3.0, +4.0], verdict
**inconclusive** because the lower bound sits exactly on the 3-point margin; about 4 more tasks
would settle it. The cascade answers 34.5% of tasks at the cheap tier, 42.5% at the mid tier and
23.0% at the frontier — but those are not its spend: because an escalated task pays for every
attempt it made, the cheap tier is 23.4% of the money and the frontier 36.1%. The proof itself
cost $13.80 and repays after 339 tasks.

These are simulated, not recorded (DECISIONS.md D1).

## Known issues

- `make verify` runs all 12 checks with none skipped.
- The Dockerfile has never been built: Docker is unavailable in this environment (D22).
- One `make verify` run failed on a Playwright ENOENT after I ran the spec-review subagent
  concurrently — two runs of the suite wiped each other's `test-results/`. Playwright now gets a
  per-run output directory so concurrent runs cannot collide.
- `tiktoken` cannot load `o200k_base` in this environment (D3), so token estimates name
  `bytes-bpe-approx-v1` as their method until the vocabulary is reachable.
- `tokop sync-models` is implemented and unit-tested but cannot run live here: openrouter.ai is
  blocked by the egress policy (D2). The committed snapshot is a hand-built fixture and says so
  in its own `_tokop_source` field, which the registry surfaces as the price provenance note.

## Open decisions

See DECISIONS.md. D1 (no credentials, simulated fixtures) shapes every later milestone.

## Spec review (SPEC.md section 10, final gate)

A fresh subagent reviewed the repository against SPEC.md sections 3, 5, 9 and 10 and reported 16
gaps. What changed as a result:

- **Numbers that were wrong on screen.** The candidate graph showed routing shares where it meant
  spend shares (cheap is 23.4% of the money, not 34.5%); per-model token ratios are now fitted
  from the cassettes rather than assumed equal (`core/ratios.py`); the trace drawer's route
  decision now comes from the scorer's own score and threshold rather than being inferred; and
  `tokop prove --margin` is applied to the verdict rather than echoed.
- **Controls that were not real.** The per-run content deletion (non-negotiable 10) now calls the
  endpoint and reports what survived; the live-mode controls that are not built now name
  themselves one by one instead of being enabled in live mode.
- **Lint spans are drawn.** The engine has always returned the exact character ranges each
  finding fired on; nothing displayed them. Inspect now highlights them on the text the lint saw,
  and clicking a finding isolates it. Fixing this exposed a real bug: PL10's spans were offsets
  into the joined system text, so they pointed at the wrong characters in any prompt with more
  than one system block.
- **Claims corrected rather than quietly left standing.** `docs/ARCHITECTURE.md` said 425 engine
  tests and 53 Playwright tests; it is 429 and 38, at 91% line coverage rather than 95%. The
  README, the Makefile and this file no longer imply `make record` can record: the live path is
  wired and tested up to the request (`tests/test_recorder_wiring.py`) but has never made one, and
  user-supplied workloads are not built. Both are DECISIONS.md D24.
- **Two checks that were weaker than they looked.** `tokop report --check` now makes a real HTTP
  request through the route instead of calling the in-process cache, and `make verify` now fails
  when `docs/DEMO.md` quotes a figure the report has moved past.
