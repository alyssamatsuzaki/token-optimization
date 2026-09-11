# Tokop — working notes for Claude Code

SPEC.md is the source of truth. PROGRESS.md is current state. DECISIONS.md is every choice
the spec left open. Read all three plus `git log --oneline -15` after a compaction or restart.

**When compacting, preserve the current milestone, failing checks, files changed since the
last commit, and open decisions.**

## Commands

    make setup     uv venv + uv sync, pnpm install
    make dev       uvicorn :8000 + vite :5173, works with no API keys
    make verify    every check in SPEC.md section 10, in order
    make demo      production build served in replay mode on :8000
    make record    stops before spending; the live path has never run (DECISIONS.md D24)
    make fmt       ruff format + ruff check --fix

Engine commands run through `engine/.venv/bin/`. Web commands run with `pnpm -C web`.

## Conventions

- Python 3.12, `uv`. mypy is strict on `tokop/core` and `tokop/optimize`; keep it that way.
- Line length 100. `ruff format` is the formatter; run `make fmt` before committing.
- TypeScript strict. Palette and type scale live in `docs/DESIGN.md` and `tailwind.config.js`;
  no colour ships that is not a named token there.
- One milestone at a time. A milestone closes when its acceptance check in SPEC.md section 11
  passes, `make verify` is green, the work is committed as `M<n>: <summary>`, and PROGRESS.md
  is updated and printed in the conversation.
- When the spec is silent, take the reversible option and log one line in DECISIONS.md.
- When reality contradicts the spec, reality wins; log the conflict and take the conservative
  option.
- Smallest change that does the job. Raise errors explicitly: no silent fallbacks, no swallowed
  exceptions. Never call something working without showing the command and its output.

## Non-negotiables (SPEC.md section 9)

1. Every number in the UI and the README comes from engine code computing over traces. No
   metric literals in UI components or docs.
2. Every metric carries provenance, and every estimate names its method.
3. Replay mode shows when the recording was made and with which model IDs.
4. Prices carry a source URL and retrieval date; unverified prices are marked.
5. Quality claims show the estimate, the interval, and n; results state split sizes;
   inconclusive results are displayed as inconclusive.
6. Official APIs only: no browser automation, no cookie reuse, no credential sharing or
   pooling, no rate-limit evasion. Drop any feature that would need them and log it.
7. API keys stay on the server and never appear in logs, fixtures, or the browser.
8. Failures stay visible: a failed call counts as an unsuccessful task and shows in the trace.
9. No fake buttons: every control does something real or is disabled with a one-line reason.
10. Stored prompts and outputs can be deleted per run.

## Money

Live API calls happen only inside `make record` (capped by `RECORD_BUDGET_USD`) and in
user-initiated live actions (capped by `DAILY_BUDGET_USD`). Neither has ever made one:
`make record` stops before spending and the live actions are not built (DECISIONS.md D24).
Tests never call live APIs.
**Never change a budget value.**
