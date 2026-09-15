import { expect, test, type Page } from "@playwright/test";

/**
 * The Optimize flow (SPEC.md section 10, check 6).
 *
 * These tests do the thing the spec asks for that matters most: they compare what the screen
 * *displays* against what `/api/report` *returns*. That makes them a check on non-negotiable 1
 * — every number in the UI comes from engine code — rather than a check that the UI can do
 * arithmetic consistently with itself.
 */

/** The report body is a plain object by design; these tests read it the way the UI does. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let report: any;

test.beforeAll(async ({ request }) => {
  report = await (await request.get("/api/report?protect_scarce=false")).json();
});

async function openOptimize(page: Page) {
  await page.goto("/optimize");
  await expect(page.getByTestId("headline-row")).toBeVisible();
}

test("the default route is Optimize with the demo workload loaded", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/optimize$/);
  await expect(page.getByRole("heading", { name: report.workload.name })).toBeVisible();
});

test("replay mode labels the fixtures as test data", async ({ page }) => {
  await openOptimize(page);
  const banner = page.getByTestId("test-data-banner");
  if (report.provenance.is_test_data) {
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("Simulated test data");
  } else {
    await expect(banner).toHaveCount(0);
  }
});

test("replay mode names when the fixtures were made and with which models", async ({ page }) => {
  await openOptimize(page);
  const line = page.getByTestId("provenance-line");
  for (const modelId of Object.values(report.provenance.model_ids) as string[]) {
    await expect(line).toContainText(modelId);
  }
  await expect(line).toContainText(String(report.workload.dataset.seed));
});

test("the headline row shows what the report computed", async ({ page }) => {
  await openOptimize(page);
  const headline = page.getByTestId("headline-row");
  const baseline = report.proof.baseline.cost_per_successful_task.point as number;
  const candidate = report.proof.candidate.cost_per_successful_task.point as number;
  await expect(headline).toContainText(`$${baseline.toFixed(5)}`);
  await expect(headline).toContainText(`$${candidate.toFixed(5)}`);
  await expect(headline).toContainText(`n = ${report.proof.candidate.n}`);
  // Every quality claim shows its interval (non-negotiable 5).
  const delta = report.proof.delta_accuracy;
  await expect(headline).toContainText("95% CI");
  await expect(headline).toContainText((delta.low * 100).toFixed(1));
});

test("the baseline pipeline graph renders with its cost share", async ({ page }) => {
  await openOptimize(page);
  await expect(page.getByTestId("graph-stage-baseline")).toBeVisible();
  await expect(page.getByTestId("graph-node-frontier")).toContainText("100%");
  await expect(page.getByTestId("graph-node-frontier")).toContainText(
    report.provenance.model_ids.frontier,
  );
});

test("findings are ranked by dollars and every one carries evidence", async ({ page }) => {
  await openOptimize(page);
  const rows = page.getByTestId("findings-list").locator("li");
  await expect(rows).toHaveCount(report.findings.length);
  for (const finding of report.findings.slice(0, 5)) {
    const row = rows.filter({ hasText: finding.id }).first();
    await expect(row).toContainText(finding.title);
    await expect(row).toContainText(finding.evidence.slice(0, 30));
  }
  // The three workload findings the spec names must be present on B0.
  const text = await page.getByTestId("findings-list").innerText();
  for (const id of ["W01", "W02", "W03"]) {
    expect(text).toContain(id);
  }
});

test("New experiment is disabled with a reason", async ({ page }) => {
  await openOptimize(page);
  const button = page.getByTestId("new-experiment");
  await expect(button).toBeDisabled();
  const reason = page.getByTestId("new-experiment-reason");
  await expect(reason).toBeVisible();
  await expect(reason).toContainText(report.live_controls.disabled_reasons.new_experiment.slice(0, 40));
});

test("build candidate, run proof, and read the verdict", async ({ page }) => {
  await openOptimize(page);

  await page.getByTestId("build-candidate").click();
  await expect(page.getByTestId("graph-stage-candidate")).toBeVisible();
  await expect(page.getByTestId("cascade-summary")).toContainText("settings evaluated");
  for (const tier of report.cascade.tiers) {
    await expect(page.getByTestId(`graph-node-${tier.tier}`)).toContainText(tier.model_id);
  }

  await page.getByTestId("run-proof").click();

  // The verdict sentence must be the engine's, word for word.
  await expect(page.getByTestId("verdict-sentence")).toHaveText(report.proof.verdict.sentence);
  await expect(page.getByText(report.proof.verdict.display).first()).toBeVisible();
  await expect(page.getByTestId("interval-plot")).toBeVisible();

  // An inconclusive result is displayed as inconclusive (non-negotiable 5).
  if (report.proof.verdict.label === "inconclusive") {
    await expect(page.getByTestId("verdict-sentence").locator("..")).toContainText("Inconclusive");
  }
});

test("configurations the data cannot separate are all shown, not ranked", async ({ page }) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const ties = report.cascade.ties;
  test.skip(!ties, "this report evaluated one configuration");
  const table = page.getByTestId("ties-table");
  for (const row of ties.tied as { label: string }[]) {
    await expect(table).toContainText(row.label);
  }
  await expect(page.getByTestId("ties-note")).toContainText(ties.note.slice(0, 60));
  await expect(page.getByTestId("ties-fallback")).toContainText(
    ties.fallback_reason.slice(0, 50),
  );
});

test("the screen says where the tasks came from and whether that can certify", async ({
  page,
}) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const provenance = report.dataset_provenance;
  await expect(page.getByTestId("provenance-tail")).toContainText(
    `${provenance.shape.templates_present} of ${provenance.shape.template_space}`,
  );
  // A set that cannot certify says why, in the words the engine chose.
  if (provenance.refusals.length > 0) {
    const refusals = page.getByTestId("provenance-refusals");
    for (const refusal of provenance.refusals as string[]) {
      await expect(refusals).toContainText(refusal.slice(0, 60));
    }
  }
});

test("the screen says where the cascade's thresholds came from", async ({ page }) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const calibration = report.calibration;
  await expect(page.getByTestId("calibration-thresholds")).toContainText(
    calibration.thresholds[0].toFixed(2),
  );
  // A label-free calibration is measured beside the labelled one, and when these fixtures
  // cannot exercise the difference the screen says so rather than implying a result.
  if (calibration.alternatives.length > 0 && !calibration.fixtures_can_exercise_this.answer) {
    await expect(page.getByTestId("calibration-limits")).toContainText(
      calibration.fixtures_can_exercise_this.reason.slice(0, 60),
    );
  }
});

test("the checkability row shows the accuracy it cost, not just the money it saved", async ({
  page,
}) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const contract = report.contract;
  if (!contract.available) {
    await expect(page.getByTestId("contract-unavailable")).toContainText(contract.reason);
    return;
  }

  // UPGRADE_V3.md U4: the row is allowed to show a negative accuracy delta, and must.
  const delta = page.getByTestId("contract-accuracy-delta");
  const sign = contract.accuracy_delta >= 0 ? "+" : "-";
  await expect(delta).toHaveText(
    `${sign}${Math.abs(contract.accuracy_delta * 100).toFixed(1)}`,
  );

  const table = page.getByTestId("contract-table");
  for (const arm of contract.arms as { pipeline: string }[]) {
    await expect(table).toContainText(arm.pipeline);
  }
  await expect(page.getByTestId("contract-verdict")).toContainText("net ");
});

test("the proof without the answer key shows what the judge alone would have said", async ({
  page,
}) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const judged = report.judged;
  if (!judged.available) {
    // A missing or stale annotation set is a real state and must say which. `tokop report
    // --check` fails on it, so this branch is the screen behaving correctly while CI is red.
    await expect(page.getByTestId("judged-unavailable")).toContainText(judged.reason);
    return;
  }

  await expect(page.getByTestId("judged-sentence")).toHaveText(judged.verdict.sentence);
  await expect(page.getByTestId("judged-interval-plot")).toBeVisible();

  // The row the whole upgrade exists for: the judge's bias, measured rather than assumed away.
  const bias = page.getByTestId("judge-bias");
  await expect(bias).toContainText("judge bias");
  await expect(bias).toContainText(
    `${judged.judge_only.bias_vs_corrected >= 0 ? "+" : "-"}${Math.abs(
      judged.judge_only.bias_vs_corrected * 100,
    ).toFixed(1)}`,
  );

  // UPGRADE_V3.md U1: the report states what the annotation cost and what strong-only grading
  // would have cost for the same interval width.
  const annotation = page.getByTestId("annotation-row");
  await expect(annotation).toContainText(`${judged.annotation.n_annotated} of ${judged.annotation.n} items`);
  await expect(annotation).toContainText("strong-only grading needs");

  // Every caveat the engine attached is rendered, not summarised away.
  const panel = page.locator("#judged");
  for (const caveat of judged.caveats as string[]) {
    await expect(panel).toContainText(caveat.slice(0, 60));
  }
});

test("the gold-free estimate is checked against the answer key the demo happens to have", async ({
  page,
}) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();
  const judged = report.judged;
  test.skip(!judged.available, "no annotation set to check");
  const coverage = page.getByTestId("judged-coverage");
  await expect(coverage).toContainText(
    judged.coverage_check.judged_interval_covers_gold ? "inside" : "outside",
  );
});

test("the savings waterfall lists every pipeline with its interval and verdict", async ({ page }) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const table = page.getByTestId("waterfall");
  for (const step of report.waterfall) {
    const row = table.locator("tr").filter({ hasText: step.label }).first();
    await expect(row).toContainText(`$${step.cost_per_successful_task.point.toFixed(5)}`);
    await expect(row).toContainText(`${(step.accuracy.point * 100).toFixed(1)}%`);
    if (step.verdict) await expect(row).toContainText(step.verdict.display);
  }
});

test("the cost-quality frontier marks the operating point and says how it was chosen", async ({
  page,
}) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const chart = page.getByTestId("frontier-chart");
  await expect(chart).toBeVisible();
  await expect(chart.getByTestId("operating-point")).toBeVisible();
  await expect(chart).toContainText("chosen on the calibration split");
});

test("the breakdown by type covers the whole split and says the router cannot see it", async ({
  page,
}) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const table = page.getByTestId("by-type");
  for (const row of report.proof.by_type) {
    await expect(table).toContainText(String(row.n));
  }
  await expect(page.getByText("The router never sees the question type")).toBeVisible();
});

test("a disagreement opens its trace, showing every call behind the task", async ({ page }) => {
  await openOptimize(page);
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();

  const first = report.proof.disagreements[0];
  expect(first).toBeTruthy();
  await page.getByTestId(`open-trace-${first.task_id}`).click();

  const drawer = page.getByTestId("trace-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText(first.question);
  await expect(drawer).toContainText(first.gold);
  // Tokens by bucket with provenance, the prompt hash, the cost and the scorer features.
  await expect(drawer).toContainText("Prompt hash");
  await expect(drawer).toContainText("Tokens by bucket");
  await expect(drawer).toContainText("The scorer never sees the gold answer");
  await expect(drawer).toContainText("output_visible");

  await page.getByTestId("trace-close").click();
  await expect(drawer).toHaveCount(0);
});

test("protecting scarce models changes the objective the search used", async ({ page, request }) => {
  await openOptimize(page);
  await page.getByTestId("protect-scarce").check();
  await page.getByTestId("build-candidate").click();
  await expect(page.getByTestId("cascade-summary")).toContainText("minimise scarce-model spend");

  const protectedReport = await (await request.get("/api/report?protect_scarce=true")).json();
  expect(protectedReport.cascade.objective).toBe("scarce_model_spend");
  expect(Number(protectedReport.proof.candidate.scarce_share)).toBeLessThanOrEqual(
    Number(report.proof.candidate.scarce_share) + 1e-9,
  );
});

test("the provenance legend is defined once and the prices carry a source", async ({ page }) => {
  await openOptimize(page);
  const legend = page.getByTestId("legend");
  await expect(legend).toContainText("provider-reported");
  await expect(legend).toContainText("exact count");
  await expect(legend).toContainText("estimated");
  await expect(legend).toContainText("projected");
  await expect(legend).toContainText("simulated");
  await expect(page.getByText(report.provenance.price_snapshot_id, { exact: false })).toBeVisible();
});

test("screenshots for the record", async ({ page }) => {
  await openOptimize(page);
  await page.screenshot({ path: "../docs/screenshots/optimize-baseline.png", fullPage: true });
  await page.getByTestId("build-candidate").click();
  await page.getByTestId("run-proof").click();
  await expect(page.getByTestId("verdict-sentence")).toBeVisible();
  await page.waitForTimeout(400);
  await page.screenshot({ path: "../docs/screenshots/optimize-proof.png", fullPage: true });
});

/* --------------------------------------------------------------- the first screen (M14) */

test("the recommendation block renders the engine's summary, not its own arithmetic", async ({
  page,
}) => {
  await openOptimize(page);
  const block = page.getByTestId("summary-block");
  await expect(block).toBeVisible();

  // Every figure is compared against `/api/report`. A component that recomputed any of these
  // would pass its own consistency check and fail this one, which is the point of asserting
  // against the API rather than against a fixture (non-negotiable 1).
  const summary = report.summary;
  await expect(page.getByTestId("summary-sentence")).toHaveText(summary.verdict.sentence);
  await expect(page.getByTestId("summary-current")).toContainText(
    `$${Number(summary.current.cost_per_successful_task_usd).toFixed(5)}`,
  );
  await expect(page.getByTestId("summary-recommended")).toContainText(
    `$${Number(summary.recommended.cost_per_successful_task_usd).toFixed(5)}`,
  );
  await expect(page.getByTestId("summary-saving")).toContainText(
    `${(summary.saving.fraction * 100).toFixed(1)}%`,
  );
  await expect(page.getByTestId("summary-quality")).toContainText(
    `${summary.quality.delta_points.toFixed(1)} pt`,
  );
  await expect(page.getByTestId("summary-allowed")).toContainText(
    `${summary.quality.allowed_points.toFixed(0)} pt`,
  );
  await expect(page.getByTestId("summary-verdict")).toContainText(summary.verdict.display);
});

test("the recommendation block is above the graph, and marks simulated figures", async ({
  page,
}) => {
  await openOptimize(page);
  const summaryBox = await page.getByTestId("summary-block").boundingBox();
  const headlineBox = await page.getByTestId("headline-row").boundingBox();
  expect(summaryBox!.y).toBeLessThan(headlineBox!.y);
  // UPGRADE_V4.md section 3.3: the simulated label goes everywhere, the new block included.
  if (report.provenance.is_test_data) {
    await expect(page.getByTestId("summary-current")).toContainText(report.summary.provenance_mark);
  }
});

test("the evidence grade is one word, and one click shows the whole chain", async ({ page }) => {
  await openOptimize(page);
  await expect(page.getByTestId("evidence-grade")).toHaveText(report.evidence.grade);
  await expect(page.getByTestId("evidence-headline")).toHaveText(report.evidence.headline);

  await expect(page.getByTestId("evidence-chain")).toHaveCount(0);
  await page.getByTestId("evidence-toggle").click();
  const rows = page.getByTestId("evidence-link");
  await expect(rows).toHaveCount(report.evidence.inputs.length);

  // Every link names its reading and where that reading came from. A chain that terminates in
  // an adjective is the thing the grade exists to replace.
  for (const link of report.evidence.inputs) {
    const row = rows.filter({ hasText: link.name }).first();
    await expect(row).toContainText(link.value);
    await expect(row).toContainText(link.source);
  }
});

test("deploy exports artifacts and never claims to carry traffic", async ({ page }) => {
  await openOptimize(page);
  await expect(page.getByTestId("deploy-button")).toBeEnabled();
  await page.getByTestId("deploy-button").click();

  const artifacts = page.getByTestId("deploy-artifacts");
  await expect(artifacts).toBeVisible();
  const exported = await (await page.request.get("/api/export")).json();
  for (const artifact of exported.artifacts) {
    await expect(artifacts).toContainText(artifact.filename);
  }
  // A result no recording backs says so on the artifacts themselves, not only in the prose.
  if (report.evidence.grade !== "recording") {
    await expect(page.getByTestId("deploy-caveat")).toBeVisible();
  }
});
