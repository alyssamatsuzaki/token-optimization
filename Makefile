.PHONY: setup dev verify record demo build clean fmt

ENGINE := engine
WEB    := web
PY     := $(ENGINE)/.venv/bin/python
TOKOP  := $(ENGINE)/.venv/bin/tokop

setup:
	cd $(ENGINE) && uv venv --python 3.12 && uv sync --all-groups
	cd $(WEB) && pnpm install

## Runs uvicorn and Vite together. Works with no API keys (replay mode).
dev:
	@bash scripts/dev.sh

## Every check in SPEC.md section 10, in order.
verify:
	@bash scripts/verify.sh

## Live recording. Stops before spending: the live path has never been exercised
## against a real provider in this build (DECISIONS.md D24). Use build-test-fixtures.
record:
	$(TOKOP) record --workload data/demo/workload.yaml

## Production build served in replay mode on port 8000, no keys needed.
demo: build
	TOKOP_MODE=replay $(PY) -m uvicorn tokop.api.app:app --host 0.0.0.0 --port 8000

build:
	cd $(WEB) && pnpm build

fmt:
	cd $(ENGINE) && .venv/bin/ruff format tokop tests && .venv/bin/ruff check --fix tokop tests

clean:
	rm -rf $(WEB)/dist $(ENGINE)/.pytest_cache $(ENGINE)/htmlcov $(ENGINE)/.coverage
