# Contributing to Tokop

Thank you for improving Tokop. This guide covers the supported local workflow and the checks a
change must pass before review.

## Set up the repository

Install Python 3.12, `uv`, Node.js, and `pnpm`, then run:

```bash
make setup
```

The Python environment is created under `engine/.venv`. Run engine commands through that environment
or use the installed `tokop` command. Frontend commands run from `web/`.

## Run the application

```bash
make dev
```

This starts FastAPI on port 8000 and Vite on port 5173. Replay mode is the default and requires no
credentials. To test the production build locally, run `make demo` and open port 8000.

## Repository conventions

### Python

- Target Python 3.12.
- Use type annotations for public and internal interfaces.
- Keep `tokop/core` and `tokop/optimize` compatible with strict mypy settings.
- Format with Ruff; the configured line length is 100 characters.
- Raise explicit errors instead of silently falling back or swallowing exceptions.

### TypeScript and React

- Keep TypeScript strict.
- Reuse the design tokens in `web/tailwind.config.js` and `docs/DESIGN.md`.
- Controls must perform a real action or display a concise disabled reason.
- Preserve keyboard navigation, visible focus, reduced-motion support, and WCAG AA contrast.

### Product data

- Do not type aggregate metrics into components or prose. Generate them from engine traces.
- Keep provenance with every measured or estimated value.
- Do not modify generated content between `metrics:start` and `metrics:end` markers by hand.
- Never add credentials, live responses, or unreviewed personal data to fixtures.
- Do not change a budget value on behalf of a user.

## Make a change

1. Read the relevant module and its tests before editing it.
2. Keep the change focused; avoid unrelated formatting or refactoring.
3. Add or update tests for changed behavior.
4. Update user-facing documentation when an interface changes.
5. Run the narrowest relevant test while iterating.
6. Run the complete verification suite before committing.

Useful commands:

```bash
make fmt
make verify
engine/.venv/bin/pytest engine/tests/test_file.py -q
pnpm -C web lint
pnpm -C web typecheck
pnpm -C web e2e
```

`make verify` is the release check. It validates formatting, types, tests, fixture integrity,
generated reports, the production frontend build, and Playwright flows in replay mode.

## Commit and review

- Use a short imperative subject that describes the change.
- Explain user-visible behavior and important tradeoffs in the pull request.
- List every test or check run, including environmental limitations or failures.
- Keep generated fixture churn out of unrelated changes.

## Live-provider policy

Tests must not call live APIs. Live calls are allowed only through explicit user actions protected
by `DAILY_BUDGET_USD`, or through `tokop record` with `RECORD_BUDGET_USD`. Tokop uses official APIs
only; browser automation, shared consumer credentials, account pooling, and rate-limit evasion are
out of scope.

## Documentation map

Start with [`docs/README.md`](docs/README.md). `SPEC.md` is the original product contract,
`DECISIONS.md` is the append-only decision record, and `PROGRESS.md` is the historical build log.
