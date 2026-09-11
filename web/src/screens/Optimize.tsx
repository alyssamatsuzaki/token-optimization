/**
 * Optimize: what's the cheapest way to run this workload without getting worse?
 *
 * The screen is a sequence, not a dashboard: the current pipeline, what is wrong with it ranked
 * by dollars, the candidate that fixes it, and the proof that the candidate is not worse. Every
 * number comes from `/api/report`, which is the same computation the CLI runs and the README is
 * generated from.
 */
import { useMemo, useState } from "react";
import { useHealth, useReport } from "../lib/api";
import {
  count,
  intervalPct,
  intervalPoints,
  intervalUsd,
  measuredMark,
  pct,
  points,
  tierTint,
  titleCase,
  usd,
  usdShort,
} from "../lib/format";
import type { FindingView, Provenance, Report } from "../lib/types";
import { FrontierChart } from "../components/FrontierChart";
import { PipelineGraph, type StepNode } from "../components/PipelineGraph";
import {
  Button,
  ErrorRow,
  Figure,
  IntervalPlot,
  Legend,
  LoadingRow,
  Metric,
  Pill,
  Section,
} from "../components/Primitives";
import { TraceDrawer } from "../components/TraceDrawer";

type Stage = "baseline" | "candidate" | "proof";

/**
 * A finding's projected saving, styled by its own confidence rather than uniformly: a measured
 * finding must not look like a heuristic one (docs/DESIGN.md section 3). A quality finding with
 * no token saving says so instead of printing "$0.00000", which reads as a measurement of zero.
 */
function FindingAmount({ finding, mark }: { finding: FindingView; mark: Provenance }) {
  const amount = Number(finding.projected_usd_per_1k);
  if (!(amount > 0)) {
    return (
      <span className="text-small text-graphite whitespace-nowrap" title={finding.formula}>
        quality finding
      </span>
    );
  }
  const kind: Provenance = finding.confidence === "measured" ? mark : "projected";
  return (
    <span className="whitespace-nowrap tabular-nums" title={finding.formula}>
      <Figure value={usdShort(amount)} kind={kind} />
      <span className="text-micro text-graphite ml-1">/1k</span>
    </span>
  );
}

function baselineSteps(report: Report): StepNode[] {
  const run = report.runs.find((r) => r.pipeline === "B0" && r.split === "test");
  if (!run) return [];
  return [
    {
      id: "prompt",
      label: "Prompt",
      model: "—",
      calls: run.n,
      costUsd: 0,
      costShare: 0,
      inputTokens: Math.round(run.input_tokens / Math.max(1, run.calls)),
      outputTokens: 0,
      cacheReadTokens: 0,
      p50LatencyMs: null,
      scarce: false,
      tierIndex: 0,
      tierCount: 2,
    },
    {
      id: "frontier",
      label: report.pipelines.B0?.name ?? "B0",
      model: run.model_id,
      calls: run.calls,
      costUsd: Number(run.total_cost_usd),
      costShare: 1,
      inputTokens: run.input_tokens,
      outputTokens: run.output_tokens,
      cacheReadTokens: run.cache_read_tokens,
      p50LatencyMs: null,
      scarce: true,
      tierIndex: 1,
      tierCount: 2,
    },
  ];
}

function candidateSteps(report: Report): StepNode[] {
  const tiers = report.cascade.tiers;
  const total = Number(report.proof.candidate.total_cost_usd) || 1;
  const steps: StepNode[] = [
    {
      id: "prompt",
      label: "Prompt (B2, cached)",
      model: "—",
      calls: report.proof.candidate.n,
      costUsd: 0,
      costShare: 0,
      inputTokens: 0,
      outputTokens: 0,
      cacheReadTokens: 0,
      p50LatencyMs: null,
      scarce: false,
      tierIndex: 0,
      tierCount: tiers.length + 1,
    },
  ];
  tiers.forEach((tier, index) => {
    const run = report.runs.find(
      (r) => r.pipeline === "B2" && r.split === "test" && r.tier === tier.tier,
    );
    const share = tier.share_of_tasks;
    steps.push({
      id: tier.tier,
      label: `${titleCase(tier.tier)} tier`,
      model: tier.model_id,
      calls: run?.calls ?? 0,
      costUsd: total * share,
      costShare: share,
      inputTokens: run?.input_tokens ?? 0,
      outputTokens: run?.output_tokens ?? 0,
      cacheReadTokens: run?.cache_read_tokens ?? 0,
      p50LatencyMs: null,
      scarce: tier.scarce,
      tierIndex: index + 1,
      tierCount: tiers.length + 1,
    });
  });
  return steps;
}

export default function Optimize() {
  const health = useHealth();
  const [protectScarce, setProtectScarce] = useState(false);
  const { data: report, isLoading, error } = useReport(protectScarce);
  const [stage, setStage] = useState<Stage>("baseline");
  const [traceTask, setTraceTask] = useState<string | null>(null);

  const steps = useMemo(
    () => (report ? (stage === "baseline" ? baselineSteps(report) : candidateSteps(report)) : []),
    [report, stage],
  );

  if (isLoading) return <div className="px-8"><LoadingRow what="the proof" /></div>;
  if (error || !report) return <div className="px-8"><ErrorRow error={error} what="the report" /></div>;

  const mark = measuredMark(report.provenance.is_test_data);
  const proof = report.proof;
  const cascade = report.cascade;
  const liveReason = report.live_controls.disabled_reasons.new_experiment;
  const verdictTone =
    proof.verdict.label === "non_inferior"
      ? "better"
      : proof.verdict.label === "worse"
        ? "worse"
        : "neutral";

  const edges =
    stage === "baseline"
      ? [{ from: "prompt", to: "frontier", label: "100% of tasks" }]
      : [
          { from: "prompt", to: cascade.tiers[0].tier, label: "100%" },
          ...cascade.tiers.slice(0, -1).map((tier, i) => ({
            from: tier.tier,
            to: cascade.tiers[i + 1].tier,
            label: pct(
              cascade.tiers.slice(i + 1).reduce((sum, t) => sum + t.share_of_tasks, 0),
              0,
            ),
          })),
        ];

  return (
    <div className="px-8 pb-16 max-w-[1180px]">
      <header className="py-5">
        <h1 className="text-title font-medium tracking-tight">{report.workload.name}</h1>
        <p className="text-base text-graphite mt-1 max-w-prose">{report.workload.description}</p>
        <p className="text-small text-graphite mt-2" data-testid="provenance-line">
          {count(report.workload.dataset.size)} tasks ·{" "}
          {count(report.workload.dataset.calibration)} calibration and{" "}
          {count(report.workload.dataset.test)} test · seed {report.workload.dataset.seed} ·{" "}
          {Object.values(report.provenance.model_ids).join(", ")}
        </p>
        {report.provenance.is_test_data && (
          <p
            className="mt-3 border-l-2 border-vermilion pl-3 text-small text-graphite max-w-prose"
            data-testid="test-data-banner"
          >
            <strong className="text-vermilion font-medium">Simulated test data.</strong>{" "}
            {report.provenance.note}
          </p>
        )}
      </header>

      {/* ---------------------------------------------------------------- headline */}
      <div className="rule-t rule-b py-5 grid grid-cols-2 md:grid-cols-4 gap-6" data-testid="headline-row">
        <Metric
          label="Cost per successful task, now"
          value={usd(proof.baseline.cost_per_successful_task.point)}
          interval={intervalUsd(proof.baseline.cost_per_successful_task)}
          kind={mark}
          size="figure"
          hint={proof.baseline.cost_per_successful_task.method}
        />
        <Metric
          label="Cost per successful task, candidate"
          value={usd(proof.candidate.cost_per_successful_task.point)}
          interval={intervalUsd(proof.candidate.cost_per_successful_task)}
          kind="simulated"
          size="figure"
          hint={proof.candidate.cost_per_successful_task.method}
        />
        <Metric
          label="Accuracy difference"
          value={`${points(proof.delta_accuracy.point)} pt`}
          interval={`95% CI ${intervalPoints(proof.delta_accuracy)}`}
          kind="simulated"
          n={proof.candidate.n}
          hint={proof.delta_accuracy.method}
        />
        <Metric
          label="Reached the frontier model"
          value={pct(cascade.reached_frontier_share, 0)}
          interval={`${count(proof.split_sizes.calibration)} calibration / ${count(proof.split_sizes.test)} test`}
          kind="simulated"
        />
      </div>

      <div className="rule-b py-4 flex flex-wrap items-baseline gap-x-8 gap-y-2 text-small">
        <span className="text-graphite">
          The proof cost{" "}
          <span className="text-ink font-medium">{usd(proof.proof_cost.total_usd, 4)}</span>
          {proof.repayment_tasks
            ? ` and repays after ${count(proof.repayment_tasks)} tasks.`
            : " and does not repay: the candidate is not cheaper."}
        </span>
        <span className="text-graphite">
          Scarce-model share: {pct(proof.baseline.scarce_share, 0)} now,{" "}
          {pct(proof.candidate.scarce_share, 0)} with the candidate.
        </span>
      </div>

      {/* ---------------------------------------------------------------- pipeline + findings */}
      <Section
        title={stage === "baseline" ? "Current pipeline" : "Candidate pipeline"}
        subtitle={
          stage === "baseline"
            ? report.pipelines.B0?.description
            : "Cache-friendly order, a CLEAR rewrite with an output contract, and a cascade that sends each task to the cheapest tier that can be trusted with it."
        }
        right={
          <label className="flex items-center gap-2 text-small text-graphite cursor-pointer">
            <input
              type="checkbox"
              checked={protectScarce}
              onChange={(e) => setProtectScarce(e.target.checked)}
              className="accent-vermilion"
              data-testid="protect-scarce"
            />
            Protect scarce models
          </label>
        }
      >
        <div className="grid grid-cols-1 lg:grid-cols-[1.15fr_1fr] gap-8">
          <div>
            <div
              className="transition-opacity duration-[420ms] ease-morph"
              key={stage}
              data-testid={`graph-stage-${stage}`}
            >
              <PipelineGraph steps={steps} edges={edges} mark={mark} testId="pipeline-graph" />
            </div>
            <div className="mt-3 flex flex-wrap items-start gap-4">
              {stage === "baseline" ? (
                <Button variant="primary" onClick={() => setStage("candidate")} testId="build-candidate">
                  Build candidate
                </Button>
              ) : (
                <>
                  <Button variant="primary" onClick={() => setStage("proof")} testId="run-proof">
                    Run proof
                  </Button>
                  <Button onClick={() => setStage("baseline")} testId="show-baseline">
                    Show the current pipeline
                  </Button>
                </>
              )}
              <Button disabledReason={liveReason} testId="new-experiment">
                New experiment
              </Button>
            </div>
            {stage !== "baseline" && (
              <dl className="mt-4 text-small grid grid-cols-[9rem_1fr] gap-x-4 gap-y-1" data-testid="cascade-summary">
                <dt className="text-graphite">Thresholds</dt>
                <dd className="tabular-nums">
                  {cascade.tiers
                    .slice(0, -1)
                    .map((t) => `${titleCase(t.tier)} ${t.threshold.toFixed(2)}`)
                    .join(" · ")}
                </dd>
                <dt className="text-graphite">Search</dt>
                <dd>
                  {count(cascade.evaluated)} settings evaluated in{" "}
                  {cascade.runtime_seconds.toFixed(2)} s, {count(cascade.feasible)} met the{" "}
                  {pct(cascade.accuracy_floor, 1)} calibration floor
                </dd>
                <dt className="text-graphite">Objective</dt>
                <dd>
                  {cascade.objective === "scarce_model_spend"
                    ? "minimise scarce-model spend"
                    : "minimise cost per task"}
                </dd>
                <dt className="text-graphite">Scorer AUROC</dt>
                <dd className="tabular-nums">
                  {cascade.tiers
                    .map((t) => `${titleCase(t.tier)} ${t.auroc === null ? "—" : t.auroc.toFixed(2)}`)
                    .join(" · ")}
                </dd>
              </dl>
            )}
          </div>

          <div>
            <h3 className="text-base font-medium mb-1">Findings</h3>
            <p className="text-small text-graphite mb-3">
              Ranked by projected dollars per 1,000 tasks, weighted by confidence.
            </p>
            <ol data-testid="findings-list">
              {report.findings.map((finding) => (
                <li key={finding.id} className="rule-t py-2.5">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="text-base">
                      <span className="font-mono text-small text-graphite mr-2">{finding.id}</span>
                      {finding.title}
                    </span>
                    <FindingAmount finding={finding} mark={mark} />
                  </div>
                  <p className="text-small text-graphite mt-0.5">{finding.evidence}</p>
                  <p className="text-micro text-graphite mt-1">
                    <span className="uppercase tracking-wide">{finding.confidence}</span>
                    {finding.transform && <span className="ml-2">→ {finding.transform}</span>}
                  </p>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </Section>

      {/* ---------------------------------------------------------------- proof */}
      {stage === "proof" && (
        <>
          <Section
            title="Verdict"
            subtitle={`Held-out test split, ${count(proof.split_sizes.test)} tasks. ${proof.operating_point_note}`}
            right={<Pill tone={verdictTone}>{proof.verdict.display}</Pill>}
            id="verdict"
          >
            <p className="text-body max-w-prose" data-testid="verdict-sentence">
              {proof.verdict.sentence}
            </p>
            <IntervalPlot interval={proof.delta_accuracy} margin={proof.verdict.margin} />
            <dl className="mt-4 text-small grid grid-cols-[12rem_1fr] gap-x-4 gap-y-1">
              <dt className="text-graphite">McNemar exact</dt>
              <dd className="tabular-nums">
                p = {proof.mcnemar.p_value.toFixed(3)} on {count(proof.mcnemar.discordant)}{" "}
                discordant pairs ({count(proof.mcnemar.baseline_only)} where only the baseline was
                right, {count(proof.mcnemar.candidate_only)} where only the candidate was)
              </dd>
              <dt className="text-graphite">Proof cost</dt>
              <dd className="tabular-nums">
                {usd(proof.proof_cost.total_usd, 4)} — baseline{" "}
                {usd(proof.proof_cost.baseline_run_usd, 4)}, calibration{" "}
                {usd(proof.proof_cost.calibration_runs_usd, 4)}, response matrix{" "}
                {usd(proof.proof_cost.candidate_run_usd, 4)}, other pipelines{" "}
                {usd(proof.proof_cost.other_pipelines_usd, 4)}, pre-warming{" "}
                {usd(proof.proof_cost.prewarming_usd, 4)}
              </dd>
              <dt className="text-graphite">Accuracy</dt>
              <dd className="tabular-nums">
                baseline {pct(proof.baseline.accuracy.point)} ({intervalPct(proof.baseline.accuracy)}) ·
                candidate {pct(proof.candidate.accuracy.point)} ({intervalPct(proof.candidate.accuracy)})
              </dd>
            </dl>
          </Section>

          <Section
            title="Cost-quality frontier"
            subtitle="Every threshold setting the search evaluated, scored on the test split."
          >
            <FrontierChart
              points={cascade.frontier_chart}
              markers={report.waterfall}
              operatingNote={proof.operating_point_note}
            />
          </Section>

          <Section title="Savings waterfall" subtitle="Cost per successful task at each step.">
            <table className="w-full text-base" data-testid="waterfall">
              <thead>
                <tr className="text-small text-graphite">
                  <th className="text-left font-medium py-2">Pipeline</th>
                  <th className="text-right font-medium py-2">Cost per successful task</th>
                  <th className="text-right font-medium py-2">Accuracy</th>
                  <th className="text-right font-medium py-2">Scarce share</th>
                  <th className="text-left font-medium py-2 pl-6">Against the baseline</th>
                </tr>
              </thead>
              <tbody>
                {report.waterfall.map((step) => {
                  const width =
                    (step.cost_per_successful_task.point /
                      Math.max(...report.waterfall.map((s) => s.cost_per_successful_task.point))) *
                    100;
                  return (
                    <tr key={step.pipeline} className="rule-t align-baseline">
                      <td className="py-2.5">
                        <span className="font-mono text-small text-graphite mr-2">{step.pipeline}</span>
                        {step.label}
                        <div
                          className="h-1 mt-1.5"
                          style={{
                            width: `${width}%`,
                            background: tierTint(
                              report.waterfall.indexOf(step),
                              report.waterfall.length,
                            ),
                          }}
                        />
                      </td>
                      <td className="py-2.5 text-right tabular-nums">
                        {usd(step.cost_per_successful_task.point)}
                        <div className="text-micro text-graphite">
                          {intervalUsd(step.cost_per_successful_task)}
                        </div>
                      </td>
                      <td className="py-2.5 text-right tabular-nums">
                        {pct(step.accuracy.point)}
                        <div className="text-micro text-graphite">{intervalPct(step.accuracy)}</div>
                      </td>
                      <td className="py-2.5 text-right tabular-nums">{pct(step.scarce_share, 0)}</td>
                      <td className="py-2.5 pl-6 text-small">
                        {step.verdict ? step.verdict.display : "— it is the baseline"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Section>

          <Section
            title="Breakdown by question type"
            subtitle="Analysis after the fact. The router never sees the question type."
          >
            <table className="w-full text-base" data-testid="by-type">
              <thead>
                <tr className="text-small text-graphite">
                  <th className="text-left font-medium py-2">Type</th>
                  <th className="text-right font-medium py-2">n</th>
                  <th className="text-right font-medium py-2">Baseline accuracy</th>
                  <th className="text-right font-medium py-2">Candidate accuracy</th>
                  <th className="text-left font-medium py-2 pl-6">Resolved at</th>
                  <th className="text-right font-medium py-2">Cost per successful task</th>
                </tr>
              </thead>
              <tbody>
                {proof.by_type.map((row) => (
                  <tr key={row.question_type} className="rule-t">
                    <td className="py-2">{titleCase(row.question_type)}</td>
                    <td className="py-2 text-right tabular-nums">{count(row.n)}</td>
                    <td className="py-2 text-right tabular-nums">{pct(row.baseline_accuracy)}</td>
                    <td className="py-2 text-right tabular-nums">{pct(row.candidate_accuracy)}</td>
                    <td className="py-2 pl-6 text-small">
                      {Object.entries(row.candidate_tier_shares)
                        .map(([tier, share]) => `${titleCase(tier)} ${pct(share, 0)}`)
                        .join(" · ")}
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {row.candidate_cost_per_success_usd
                        ? usd(row.candidate_cost_per_success_usd)
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <Section
            title="Disagreements"
            subtitle={`${count(proof.disagreements.length)} tasks the two pipelines graded differently. Open one to see every call behind it.`}
          >
            <table className="w-full text-base" data-testid="disagreements">
              <thead>
                <tr className="text-small text-graphite">
                  <th className="text-left font-medium py-2">Question</th>
                  <th className="text-left font-medium py-2">Gold</th>
                  <th className="text-left font-medium py-2">Baseline</th>
                  <th className="text-left font-medium py-2">Candidate</th>
                  <th className="text-left font-medium py-2">Resolved at</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody>
                {proof.disagreements.map((row) => (
                  <tr key={row.task_id} className="rule-t align-baseline">
                    <td className="py-2 pr-4 max-w-[34rem]">{row.question}</td>
                    <td className="py-2 font-mono text-small">{row.gold}</td>
                    <td className="py-2">
                      <Pill tone={row.baseline_correct ? "better" : "worse"}>
                        {row.baseline_correct ? "right" : "wrong"}
                      </Pill>
                    </td>
                    <td className="py-2">
                      <Pill tone={row.candidate_correct ? "better" : "worse"}>
                        {row.candidate_correct ? "right" : "wrong"}
                      </Pill>
                    </td>
                    <td className="py-2 text-small">{titleCase(row.candidate_tier)}</td>
                    <td className="py-2 text-right">
                      <button
                        type="button"
                        className="text-small text-prussian hover:text-ink underline underline-offset-2"
                        onClick={() => setTraceTask(row.task_id)}
                        data-testid={`open-trace-${row.task_id}`}
                      >
                        Open trace
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        </>
      )}

      <footer className="rule-t pt-4 mt-2 flex flex-wrap items-baseline justify-between gap-4">
        <Legend kinds={["provider", "exact", "estimated", "projected", "simulated"]} />
        <p className="text-micro text-graphite">
          Prices: snapshot {report.provenance.price_snapshot_id},{" "}
          {report.provenance.prices_verified
            ? "all verified against the provider's own page"
            : `unverified for ${report.provenance.unverified_models.join(", ")}`}
          . Token estimates: {report.provenance.base_token_counter}.
          {health.data?.mode === "replay" && " Replay mode: no API calls are made."}
        </p>
      </footer>

      <TraceDrawer taskId={traceTask} onClose={() => setTraceTask(null)} mark={mark} />
    </div>
  );
}
