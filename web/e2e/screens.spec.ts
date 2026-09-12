import { expect, test, type Page } from "@playwright/test";

/**
 * Inspect, Compare, Spend and Settings (SPEC.md section 10, check 6).
 *
 * As with the Optimize suite, these compare what the screen displays against what the API
 * returns, rather than against numbers written into the test.
 */

async function inspectWithB0(page: Page) {
  await page.goto("/inspect");
  await expect(page.getByTestId("system-input")).not.toHaveValue("");
  await page.getByTestId("inspect-run").click();
  await expect(page.getByTestId("inspect-findings")).toBeVisible();
}

test.describe("Inspect", () => {
  test("the findings list is exactly what the engine returned", async ({ page, request }) => {
    // SPEC.md non-negotiable 1: the screen's job is to show the engine's findings, so that is
    // what this asserts — not a hard-coded list of rule IDs. An earlier version named PL02,
    // which fires on B0's template under one token counter and not under another because the
    // template's static block lands within a few tokens of the rule's threshold. PL02's own
    // behaviour is pinned in tests/test_lint.py with a document far clear of that threshold,
    // which is where a rule's behaviour belongs (DECISIONS.md D26).
    const template = await (await request.get("/api/inspect/pipeline/B0")).json();
    const result = await (
      await request.post("/api/inspect", {
        data: {
          system: template.system,
          user: template.user,
          tools: "",
          max_tokens: template.max_tokens,
          cache_after_system: template.cache_after_system,
          apply_fixes: false,
        },
      })
    ).json();
    const expected = result.findings.map((f: { id: string }) => f.id);
    expect(expected.length).toBeGreaterThan(3);

    await inspectWithB0(page);
    const findings = page.getByTestId("inspect-findings");
    // The id and the title are adjacent spans, so innerText runs them together: "PL06No output
    // contract…". Take the leading rule id.
    const shown = (await findings.locator("li").allInnerTexts()).map(
      (text) => /^[A-Z]+\d+/.exec(text.trim())?.[0] ?? text.trim(),
    );
    expect(shown).toEqual(expected);

    // B0 exists to demonstrate these two, and they fire under any token counter: a volatile
    // block inside the cacheable prefix, and no cap on the output.
    for (const id of ["PL01", "PL06"]) {
      expect(expected).toContain(id);
    }
  });

  test("cost per call and per 1,000 calls are cheapest first and mark unverified prices", async ({
    page,
    request,
  }) => {
    await inspectWithB0(page);
    const rows = page.getByTestId("inspect-models").locator("tbody tr");
    const count = await rows.count();
    expect(count).toBeGreaterThan(3);
    const perCall: number[] = [];
    const perThousand: number[] = [];
    for (let i = 0; i < count; i++) {
      const cells = rows.nth(i).locator("td");
      perCall.push(Number((await cells.nth(3).innerText()).replace(/[$,]/g, "")));
      perThousand.push(Number((await cells.nth(4).innerText()).replace(/[$,]/g, "")));
    }
    expect(perThousand).toEqual([...perThousand].sort((a, b) => a - b));
    expect(perCall).toEqual([...perCall].sort((a, b) => a - b));

    // The per-call column is the engine's number, not the per-1k column divided in the browser.
    const template = await (await request.get("/api/inspect/pipeline/B0")).json();
    const result = await (
      await request.post("/api/inspect", {
        data: {
          system: template.system,
          user: template.user,
          tools: "",
          max_tokens: template.max_tokens,
          cache_after_system: template.cache_after_system,
          apply_fixes: false,
        },
      })
    ).json();
    const cheapest = Number(result.models[0].cost_per_call_usd);
    expect(perCall[0]).toBeCloseTo(cheapest, 5);
    await expect(page.getByTestId("inspect-models")).toContainText("price unverified");
  });

  test("lint spans are drawn on the prompt the lint saw", async ({ page, request }) => {
    const template = await (await request.get("/api/inspect/pipeline/B0")).json();
    const result = await (
      await request.post("/api/inspect", {
        data: {
          system: template.system,
          user: template.user,
          tools: "",
          max_tokens: template.max_tokens,
          cache_after_system: template.cache_after_system,
          apply_fixes: false,
        },
      })
    ).json();
    const withSpans = result.findings.filter(
      (f: { spans: unknown[] }) => f.spans.length > 0,
    );
    expect(withSpans.length).toBeGreaterThan(0);

    await inspectWithB0(page);
    const panel = page.getByTestId("prompt-highlights");
    await expect(panel).toBeVisible();
    await expect(panel.getByTestId("prompt-span").first()).toBeVisible();

    // Isolating one finding draws that finding's own span text, character for character.
    const first = withSpans[0];
    await page.getByTestId(`finding-${first.id}`).click();
    await expect(panel).toContainText(`from ${first.id}`);
    const marks = panel.getByTestId("prompt-span");
    await expect(marks.first()).toBeVisible();
    const drawn = await marks.allInnerTexts();
    const expected = first.spans[0].text as string;
    expect(drawn.join("\n")).toContain(expected.trim().split("\n")[0]);
  });

  test("token counts name their method rather than implying an exact count", async ({ page }) => {
    await inspectWithB0(page);
    const body = await page.getByTestId("inspect-models").innerText();
    expect(body).toBeTruthy();
    await expect(page.getByText(/Token counts are estimated/)).toBeVisible();
  });

  test("apply safe fixes shows the net token delta in the diff", async ({ page }) => {
    await page.goto("/inspect");
    await expect(page.getByTestId("system-input")).not.toHaveValue("");
    await page.getByTestId("apply-fixes").click();

    const diff = page.getByTestId("fixes-diff");
    await expect(diff).toBeVisible();
    // The net delta is shown, and for B0 it is positive: adding an output contract costs input
    // tokens and buys back more on the output side.
    const delta = page.getByTestId("net-input-delta");
    await expect(delta).toBeVisible();
    const text = await delta.innerText();
    expect(text).toMatch(/^[+-]?[\d,]+ tok$/);
    await expect(diff).toContainText("Now cacheable");
    await expect(diff).toContainText("Output cap");
    await expect(diff).toContainText("make the prompt longer");
  });

  test("Rewrite with a model is disabled with a reason", async ({ page }) => {
    await page.goto("/inspect");
    await expect(page.getByTestId("rewrite-with-model")).toBeDisabled();
    await expect(page.getByTestId("rewrite-with-model-reason")).toContainText("replay mode");
  });

  test("Brief mode shows a recorded brief with tokens before and after", async ({
    page,
    request,
  }) => {
    const brief = await (await request.get("/api/brief")).json();
    await page.goto("/inspect");
    await page.getByTestId("tab-brief").click();

    const panel = page.getByTestId("brief-panel");
    await expect(panel).toBeVisible();
    await expect(page.getByTestId("brief-before")).toContainText(
      brief.tokens_before.toLocaleString(),
    );
    await expect(page.getByTestId("brief-after")).toContainText(
      brief.tokens_after.toLocaleString(),
    );
    await expect(panel).toContainText("Lossy");
    await expect(panel).toContainText("never automates a chat app");
    await expect(page.getByTestId("brief-text")).toContainText("Returns brief");
  });
});

test.describe("Compare", () => {
  test("a recorded example renders with tokens and cost", async ({ page, request }) => {
    const compare = await (await request.get("/api/compare")).json();
    await page.goto("/compare");

    const example = compare.examples[0];
    await expect(page.getByTestId("compare-prompt")).toContainText(example.prompt.slice(0, 60));
    for (const column of example.columns) {
      const card = page.getByTestId(`compare-column-${column.model_id}`);
      await expect(card).toBeVisible();
      await expect(card).toContainText(`${column.output_tokens.toLocaleString()} tok`);
      await expect(card).toContainText(`$${Number(column.cost_usd).toFixed(5)}`);
      await expect(card).toContainText("first token");
    }
  });

  test("the manual column explains why subscription apps are manual", async ({ page }) => {
    await page.goto("/compare");
    const manual = page.getByTestId("manual-column");
    await expect(manual).toContainText("no public API");
    await expect(manual).toContainText("subscription");
    // Pasting an answer produces an estimated token count.
    await page.getByTestId("manual-paste").fill("A pasted answer from a chat app.");
    await expect(manual).toContainText("tok");
  });

  test("synthesis returns the fixed structure", async ({ page }) => {
    await page.goto("/compare");
    const synthesis = page.getByTestId("synthesis");
    await expect(synthesis).toContainText("Points of agreement");
    await expect(synthesis).toContainText("Disagreements");
    await expect(synthesis).toContainText("Claims only one model made");
    await expect(synthesis).toContainText("Likely errors");
    await expect(page.getByText("Final answer")).toBeVisible();
  });

  test("the second recorded example is reachable", async ({ page, request }) => {
    const compare = await (await request.get("/api/compare")).json();
    await page.goto("/compare");
    const second = compare.examples[1];
    await page.getByTestId(`compare-tab-${second.id}`).click();
    await expect(page.getByTestId("compare-prompt")).toContainText(second.prompt.slice(0, 50));
  });

  test("running a live comparison is disabled with a reason", async ({ page }) => {
    await page.goto("/compare");
    await expect(page.getByTestId("run-comparison")).toBeDisabled();
    await expect(page.getByTestId("run-comparison-reason")).toContainText("replay mode");
  });
});

test.describe("Spend", () => {
  test("the recorded runs appear with their tokens and cost", async ({ page, request }) => {
    const spend = await (await request.get("/api/spend")).json();
    await page.goto("/spend");

    const rows = page.getByTestId("spend-runs").locator("tbody tr");
    await expect(rows).toHaveCount(spend.runs.length);
    await expect(page.getByTestId("spend-headline")).toContainText(
      `$${Number(spend.total_usd).toFixed(4)}`,
    );
    for (const model of spend.by_model) {
      await expect(page.getByTestId("spend-by-model")).toContainText(model.model_id);
    }
  });

  test("the scarce-model share is reported and never exceeds 100%", async ({ page, request }) => {
    const spend = await (await request.get("/api/spend")).json();
    expect(spend.scarce_share).toBeLessThanOrEqual(1);
    await page.goto("/spend");
    await expect(page.getByTestId("spend-headline")).toContainText("Scarce-model spend");
  });

  test("the copy says what this ledger cannot see", async ({ page }) => {
    await page.goto("/spend");
    await expect(
      page.getByText(/cannot see Claude\.ai or Claude Code plan usage/),
    ).toBeVisible();
  });

  test("cost per successful task is shown with its interval", async ({ page, request }) => {
    const spend = await (await request.get("/api/spend")).json();
    await page.goto("/spend");
    for (const row of spend.cost_per_successful_task) {
      await expect(page.getByTestId("spend-cps")).toContainText(`$${row.usd.toFixed(5)}`);
    }
  });
});

test.describe("Settings", () => {
  test("providers are reported as configured or not, and no key is ever sent", async ({
    page,
    request,
  }) => {
    const raw = await (await request.get("/api/settings")).text();
    expect(raw).not.toMatch(/sk-[A-Za-z0-9]/);
    await page.goto("/settings");
    await expect(page.getByTestId("settings-providers")).toContainText("not configured");
    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(/sk-[A-Za-z0-9]/);
  });

  test("every price shows its source and unverified ones are marked", async ({
    page,
    request,
  }) => {
    const settings = await (await request.get("/api/settings")).json();
    await page.goto("/settings");
    const table = page.getByTestId("settings-models");
    await expect(table).toContainText("platform.claude.com");
    const unverified = settings.models.filter(
      (m: { provenance: { verified: boolean } }) => !m.provenance.verified,
    );
    for (const model of unverified.slice(0, 2)) {
      await expect(page.getByTestId(`unverified-${model.model_id}`)).toBeVisible();
    }
  });

  test("unconfirmed providers are disabled with their reason", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByTestId("settings-providers")).toContainText(
      "not confirmed against vendor documentation",
    );
  });

  test("the recording state is explained", async ({ page, request }) => {
    const settings = await (await request.get("/api/settings")).json();
    await page.goto("/settings");
    await expect(page.getByTestId("recording-reason")).toContainText(
      settings.recording.reason.slice(0, 50),
    );
  });

  test("Sync models is disabled with a reason", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByTestId("sync-models")).toBeDisabled();
  });

  test("deleting stored content really deletes it and keeps every metric", async ({
    page,
    request,
  }) => {
    // SPEC.md non-negotiable 10, and non-negotiable 9: this control is not a label, it calls
    // the endpoint. The run picked is a calibration run, whose stored text no screen displays,
    // so the deletion does not blank a trace the other tests read. It is also idempotent: a
    // second run of this suite finds the content already gone and still passes.
    const runId = "returns-support-B2-calibration-claude-haiku-4-5-20251001";
    const before = await (await request.get("/api/runs")).json();
    const target = before.runs.find((r: { run_id: string }) => r.run_id === runId);
    expect(target).toBeTruthy();

    await page.goto("/settings");
    await page.getByTestId("delete-run-select").selectOption(runId);
    await expect(page.getByText(/Deleting\s+them keeps every metric/)).toBeVisible();
    await page.getByTestId("delete-content").click();

    const result = page.getByTestId("delete-result");
    await expect(result).toBeVisible();
    await expect(result).toContainText("Prompts remaining");
    await expect(result).not.toContainText("this is a bug");

    // The ledger agrees, and the cost the run reports has not moved.
    const after = await (await request.get("/api/runs")).json();
    const now = after.runs.find((r: { run_id: string }) => r.run_id === runId);
    expect(now.content_deleted).toBe(true);
    expect(now.stored_prompts).toBe(0);
    expect(now.stored_responses).toBe(0);
    expect(now.total_cost_usd).toBe(target.total_cost_usd);
    expect(now.calls).toBe(target.calls);
  });
});

test.describe("New experiment", () => {
  test("the form opens read-only with its preflight and defaults", async ({ page, request }) => {
    const report = await (await request.get("/api/report?protect_scarce=false")).json();
    await page.goto("/optimize");
    await page.getByTestId("open-new-experiment").click();

    const form = page.getByTestId("new-experiment-form");
    await expect(form).toBeVisible();
    await expect(page.getByTestId("new-experiment-disabled")).toContainText("replay mode");
    // The defaults reproduce the demo.
    await expect(form).toContainText(report.workload.name);
    for (const tier of report.cascade.tiers) {
      await expect(page.getByTestId(`tier-${tier.tier}`)).toBeChecked();
    }
    await expect(form).toContainText("Projected spend");
    await expect(page.getByTestId("start-experiment")).toBeDisabled();

    await page.getByTestId("advanced-toggle").click();
    await expect(page.getByTestId("advanced-panel")).toBeVisible();
    await page.getByTestId("new-experiment-close").click();
    await expect(form).toHaveCount(0);
  });
});

test("screenshots of the remaining screens", async ({ page }) => {
  for (const [route, name] of [
    ["/inspect", "inspect"],
    ["/compare", "compare"],
    ["/spend", "spend"],
    ["/settings", "settings"],
  ] as const) {
    await page.goto(route);
    await page.waitForTimeout(600);
    await page.screenshot({ path: `../docs/screenshots/${name}.png`, fullPage: true });
  }
});
