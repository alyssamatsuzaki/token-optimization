# Deploying the demo

One container, no keys, no external services. The whole app runs from the committed fixtures.

```bash
docker build -t tokop .
docker run --rm -p 8000:8000 tokop
# http://localhost:8000
```

The image sets `TOKOP_MODE=replay`. In replay mode a missing cassette raises rather than falling
back to a live call, so a deployed demo **cannot** spend money even if someone adds a key to the
environment by mistake.

## Do not let it cold-start during a live demo

This is the one operational thing that matters. `build_report()` replays eight recorded runs,
fits three scorers, searches 2,601 threshold settings and runs three paired bootstraps of 5,000
resamples each. That takes about **nine seconds**. It is cached for the life of the process, so
only the first request pays — but on a scale-to-zero host, the first request is the one your
audience is watching.

**Pick a host that keeps one instance warm**, and warm it yourself before the demo:

| Host | What to set | Warm-up |
|---|---|---|
| Fly.io | `min_machines_running = 1` under `[http_service]`, and **do not** set `auto_stop_machines` | `fly deploy` then `curl https://<app>.fly.dev/api/report` |
| Google Cloud Run | `--min-instances=1 --cpu-boost` | `curl https://<url>/api/report` after deploy |
| Render | a paid instance type (the free tier sleeps) | `curl https://<url>/api/report` |
| Railway / a plain VM | nothing special; the process stays up | `curl http://<host>:8000/api/report` |

`min-instances=1` is not optional for a demo. A scale-to-zero container adds container start plus
those nine seconds to the first click, and there is no way to make that look intentional.

A minimal `fly.toml`:

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
  auto_stop_machines = false     # the point of the whole file
  min_machines_running = 1

[[vm]]
  memory = "1gb"                 # numpy, scipy and scikit-learn are not small
  cpu_kind = "shared"
  cpus = 1
```

Then, every time, before anyone is watching:

```bash
curl -s https://tokop-demo.fly.dev/api/report > /dev/null   # pays the nine seconds
curl -s https://tokop-demo.fly.dev/api/health               # should return instantly
```

## Resources

- **Memory**: 1 GB. numpy, scipy and scikit-learn dominate; the fixtures are 6 MB on disk and the
  report is a few MB in memory.
- **CPU**: one shared core is enough once the report is cached. The first build is CPU-bound, so
  a burst credit (`--cpu-boost` on Cloud Run) shortens it.
- **Disk**: the image is roughly 700 MB, mostly the scientific Python stack. The only writable
  path needed is `TOKOP_STATE_DIR` (`/tmp/tokop`), and replay mode never writes to it.
- **Egress**: none. Fonts are bundled, there is no CDN, and no provider is contacted.

## Running it live instead

Only if you mean to spend money:

```bash
docker run --rm -p 8000:8000 \
  -e TOKOP_MODE=live \
  -e ANTHROPIC_API_KEY=sk-... \
  -e DAILY_BUDGET_USD=5 \
  tokop
```

Without `DAILY_BUDGET_USD` every live control stays disabled and says so. `make record` needs
`RECORD_BUDGET_USD` on top; setting it is the consent to spend up to that amount, and Tokop will
not set it for you.

Keys stay on the server. The API reports a provider as configured or not and never returns a key;
an end-to-end test greps both the rendered page and the raw API response for a key-shaped string.
