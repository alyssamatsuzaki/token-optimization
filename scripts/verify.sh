#!/usr/bin/env bash
# SPEC.md section 10. Checks run in order; the first failure stops the run.
# A check that cannot apply yet is SKIPPED with a reason and is not counted.
# At M7 nothing may be skipped.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PASSED=0
SKIPPED=0
SKIP_NAMES=()

run() {           # run <name> <command...>
  local name="$1"; shift
  printf '  %-34s ' "$name"
  local out
  if out=$("$@" 2>&1); then
    printf 'ok\n'
    PASSED=$((PASSED + 1))
  else
    printf 'FAILED\n\n'
    printf '%s\n\n' "$out" | tail -60
    printf 'VERIFY FAILED: %s\n' "$name"
    exit 1
  fi
}

skip() {          # skip <name> <reason>
  printf '  %-34s skipped (%s)\n' "$1" "$2"
  SKIPPED=$((SKIPPED + 1))
  SKIP_NAMES+=("$1")
}

have() { [ -e "$1" ]; }

echo "tokop verify"
echo

# ---------------------------------------------------------------- 1. static checks
if have engine/.venv/bin/ruff; then
  run "ruff"            bash -c "cd '$ROOT/engine' && .venv/bin/ruff check tokop tests"
  run "ruff format"     bash -c "cd '$ROOT/engine' && .venv/bin/ruff format --check tokop tests"
  run "mypy"            bash -c "cd '$ROOT/engine' && .venv/bin/mypy tokop"
else
  skip "ruff" "engine venv missing; run make setup"
  skip "ruff format" "engine venv missing"
  skip "mypy" "engine venv missing"
fi

if have web/node_modules; then
  run "eslint"          bash -c "cd '$ROOT/web' && pnpm -s lint"
  run "tsc"             bash -c "cd '$ROOT/web' && pnpm -s typecheck"
else
  skip "eslint" "web node_modules missing; run make setup"
  skip "tsc" "web node_modules missing"
fi

# ---------------------------------------------------------------- 2. tests
if have engine/.venv/bin/pytest && [ -n "$(find engine/tests -name 'test_*.py' 2>/dev/null)" ]; then
  run "pytest + coverage" bash -c "cd '$ROOT/engine' && .venv/bin/pytest -q \
      --cov=tokop/core --cov=tokop/optimize --cov-report=term-missing --cov-fail-under=85"
else
  skip "pytest + coverage" "no tests yet"
fi

# ---------------------------------------------------------------- 3. web build
if have web/node_modules; then
  run "web production build" bash -c "cd '$ROOT/web' && pnpm -s build"
else
  skip "web production build" "web node_modules missing"
fi

# ---------------------------------------------------------------- 4-5. fixtures, report
if have engine/.venv/bin/tokop; then
  if have data/demo/workload.yaml; then
    run "tokop fixtures-check" bash -c "cd '$ROOT' && engine/.venv/bin/tokop fixtures-check"
  else
    skip "tokop fixtures-check" "demo workload not generated yet"
  fi
  if have fixtures/test/manifest.json || have fixtures/demo/manifest.json; then
    run "tokop report --check" bash -c "cd '$ROOT' && engine/.venv/bin/tokop report --check"
  else
    skip "tokop report --check" "no recorded fixtures yet"
  fi
else
  skip "tokop fixtures-check" "tokop CLI not installed"
  skip "tokop report --check" "tokop CLI not installed"
fi

# ---------------------------------------------------------------- 6. end to end
if have web/e2e && [ -n "$(find web/e2e -name '*.spec.ts' 2>/dev/null)" ]; then
  if [ -d "$HOME/.cache/ms-playwright" ] || [ -d /opt/pw-browsers ]; then
    run "playwright e2e (replay)" bash -c "cd '$ROOT/web' && pnpm -s e2e"
  else
    skip "playwright e2e (replay)" "no browser installed; run pnpm -C web e2e:install"
  fi
else
  skip "playwright e2e (replay)" "no e2e specs yet"
fi

# ---------------------------------------------------------------- 7. recording state
if have engine/.venv/bin/tokop; then
  run "recording state" bash -c "cd '$ROOT' && engine/.venv/bin/tokop recording-state --check"
else
  skip "recording state" "tokop CLI not installed"
fi

echo
if [ "$SKIPPED" -gt 0 ]; then
  printf 'VERIFY PASSED (%d checks, %d skipped: %s)\n' \
    "$PASSED" "$SKIPPED" "$(IFS=', '; echo "${SKIP_NAMES[*]}")"
else
  printf 'VERIFY PASSED (%d checks)\n' "$PASSED"
fi
