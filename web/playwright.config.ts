import { existsSync } from "node:fs";

import { defineConfig, devices } from "@playwright/test";

/**
 * Which Chromium to launch.
 *
 * This image ships one at a pinned path and blocks the download host, so the browser is pointed
 * at rather than fetched. Anywhere else — a contributor's laptop, GitHub Actions after
 * `playwright install chromium` — that path does not exist and Playwright's own resolution is
 * correct. `PLAYWRIGHT_CHROMIUM` overrides both and is used verbatim, so a wrong value fails
 * loudly instead of being silently ignored.
 */
const PINNED = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
const executablePath =
  process.env.PLAYWRIGHT_CHROMIUM ?? (existsSync(PINNED) ? PINNED : undefined);

/**
 * End-to-end tests run in replay mode against the **production build**, served by the same
 * FastAPI process that serves the API in production. Nothing here mocks the engine: the tests
 * compare what the screen displays against what `/api/report` returns, which is what makes them
 * a check on SPEC.md non-negotiable 1 rather than a check on the UI's own arithmetic.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  // A unique output directory per run. Playwright wipes `test-results/` when it starts, so two
  // concurrent runs — CI running the suite while someone runs it locally, say — otherwise
  // delete each other's trace artifacts mid-test and fail with ENOENT.
  outputDir: `test-results/run-${process.env.PLAYWRIGHT_RUN_ID ?? process.pid}`,
  use: {
    baseURL: "http://127.0.0.1:8123",
    viewport: { width: 1280, height: 900 },
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: { executablePath },
      },
    },
  ],
  webServer: {
    command:
      "TOKOP_MODE=replay ../engine/.venv/bin/python -m uvicorn tokop.api.app:app " +
      "--host 127.0.0.1 --port 8123 --app-dir ../engine",
    url: "http://127.0.0.1:8123/api/health",
    // Another run may already have a server on this port; reuse it rather than failing to bind.

    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
