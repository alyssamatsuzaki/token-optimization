# Tokop, as one container. Defaults to replay mode and needs no API keys.
#
# Two stages: the SPA is built with Node and copied into a Python image that serves both it and
# the API. Nothing is fetched at runtime — fonts are self-hosted from the bundle, and the app
# runs entirely from the committed fixtures — so the container works on a host with no egress.

# ---------------------------------------------------------------- 1. build the SPA
FROM node:22-slim AS web

WORKDIR /build
RUN corepack enable

# Dependencies first, so a source change does not re-resolve the lockfile.
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml web/.npmrc ./
RUN pnpm install --frozen-lockfile

COPY web/ ./
RUN pnpm build


# ---------------------------------------------------------------- 2. serve
FROM python:3.12-slim AS app

# uv, for the same resolution the repo was built with.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    TOKOP_MODE=replay \
    TOKOP_STATE_DIR=/tmp/tokop

COPY engine/pyproject.toml engine/uv.lock engine/.python-version ./engine/
RUN cd engine && uv sync --frozen --no-dev --no-install-project

COPY engine/ ./engine/
RUN cd engine && uv sync --frozen --no-dev

# Everything the engine reads at runtime. The fixtures are the demo: without them the app has
# nothing to compute over, so they ship in the image rather than being mounted.
COPY config/ ./config/
COPY data/ ./data/
COPY fixtures/ ./fixtures/
COPY docs/ ./docs/
COPY README.md SPEC.md DECISIONS.md ./
COPY --from=web /build/dist ./web/dist

# Non-root. The only thing the app writes is the SQLite ledger under TOKOP_STATE_DIR.
RUN useradd --create-home --uid 10001 tokop \
    && mkdir -p /tmp/tokop \
    && chown -R tokop:tokop /app /tmp/tokop
USER tokop

EXPOSE 8000

# The report takes a few seconds to build on first request, so it is computed at image build
# time into the module cache and again on startup — see the deployment guide about cold starts.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "tokop.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--app-dir", "engine"]
