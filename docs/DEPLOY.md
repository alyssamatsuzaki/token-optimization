# Deploying the demo

The demo runs in one container with no API keys or external services. It reads the committed
fixtures and does not make provider requests.

```bash
docker build -t tokop .
docker run --rm -p 8000:8000 tokop
# Open http://localhost:8000
```

The image defaults to `TOKOP_MODE=replay`. If a request is missing from the cassette set, replay
mode returns an error. It never falls back to a live provider, even when a key is present in the
environment.

## Keep the demo warm

The first call to `build_report()` replays eight runs, fits three scorers, evaluates 2,601 threshold
settings, and performs three paired bootstraps with 5,000 resamples. On the reference environment,
this takes about nine seconds. The result is cached until the process restarts.

Use at least one warm instance for a presentation and request `/api/report` after each deployment:

| Host | Minimum configuration | Warm-up command |
| --- | --- | --- |
| Fly.io | Set `min_machines_running = 1` and `auto_stop_machines = false` under `[http_service]`. | `curl https://<app>.fly.dev/api/report` |
| Google Cloud Run | Use `--min-instances=1`; `--cpu-boost` reduces startup time. | `curl https://<url>/api/report` |
| Render | Use an instance type that does not sleep. | `curl https://<url>/api/report` |
| Railway or a VM | Keep the process running. | `curl http://<host>:8000/api/report` |

A minimal Fly.io configuration:

```toml
app = "tokop-demo"
primary_region = "lhr"

[build]
  dockerfile = "Dockerfile"

[env]
  TOKOP_MODE = "replay"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false
  min_machines_running = 1

[[vm]]
  memory = "1gb"
  cpu_kind = "shared"
  cpus = 1
```

Warm the report and check the service before the presentation:

```bash
curl -fsS https://tokop-demo.fly.dev/api/report > /dev/null
curl -fsS https://tokop-demo.fly.dev/api/health
```

## Resource requirements

- **Memory:** Allocate 1 GB. NumPy, SciPy, and scikit-learn account for most of it.
- **CPU:** One shared core is sufficient after warm-up. The initial report build is CPU-bound.
- **Disk:** The image is about 700 MB. `TOKOP_STATE_DIR` defaults to `/tmp/tokop`; replay mode does
  not write application state there.
- **Egress:** Replay mode needs none. Fonts are bundled and provider adapters are not called.

## Live mode

Live mode can create provider charges. Supply the mode, key, and daily cap explicitly:

```bash
docker run --rm -p 8000:8000 \
  -e TOKOP_MODE=live \
  -e ANTHROPIC_API_KEY=sk-... \
  -e DAILY_BUDGET_USD=5 \
  tokop
```

Without `DAILY_BUDGET_USD`, live controls remain disabled. Recording also requires
`RECORD_BUDGET_USD`; its value is the maximum amount the operator authorizes `tokop record` to
spend.

Keys stay on the server. API responses expose only whether a provider is configured. The browser
and API responses are checked in the end-to-end suite for key-shaped strings.
