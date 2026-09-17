/**
 * Optimize: what's the cheapest way to run this workload without getting worse?
 *
 * The screen is a sequence, not a dashboard: the current pipeline, what is wrong with it ranked
 * by dollars, the candidate that fixes it, and the proof that the candidate is not worse. Every
 * number comes from `/api/report`, which is the same computation the CLI runs and the README is
 * generated from.
 */
import { Fragment, useMemo, useState } from "react";
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
import type {
  CalibrationView,
  ContractView,
  DatasetProvenanceView,
  TiesView,
  FindingView,
  JudgedView,
  Provenance,
  Report,
  WorkloadFingerprintView,
} from "../lib/types";
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
import { NewExperiment } from "../components/NewExperiment";
import { SummaryBlock } from "../components/Summary";
import { TraceDrawer } from "../components/TraceDrawer";

type Stage = "baseline" | "candidate" | "proof";

/**
 * Style projected savings by confidence (docs/DESIGN.md section 3). Quality-only findings use a
 * text label because "$0.00000" would imply a measured monetary value.
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
      p50LatencyMs: run.p50_latency_ms ?? run.recorded_latency_p50_ms,
      scarce: true,
      tierIndex: 1,
      tierCount: 2,
    },
  ];
}

function candidateSteps(report: Report): StepNode[] {
  const tiers = report.cascade.tiers;
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
    // Cost and cost share come from the cascade's own per-tier spend, not from the share of
    // tasks resolved here. Every task pays the cheap tier and an escalated task pays twice, so
    // the two quantities differ by a lot — sizing the node by the wrong one would mislead about
    // exactly the thing the graph exists to show.
    steps.push({
      id: tier.tier,
      label: `${titleCase(tier.tier)} tier`,
      model: tier.model_id,
      calls: tier.attempts,
      costUsd: Number(tier.cost_usd),
      costShare: tier.share_of_cost,
      inputTokens: run?.input_tokens ?? 0,
      outputTokens: run?.output_tokens ?? 0,
      cacheReadTokens: run?.cache_read_tokens ?? 0,
      p50LatencyMs: tier.p50_latency_ms,
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
  const [showExperiment, setShowExperiment] = useState(false);

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
            // The share of tasks that escalate past this tier, which is the next tier's
            // attempt share — not the share resolved somewhere further down.
            label: pct(cascade.tiers[i + 1].share_of_attempts, 0),
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
          {report.provenance.is_test_data ? "generated" : "recorded"}{" "}
          {report.provenance.recorded_at?.slice(0, 10) ?? "date unknown"} ·{" "}
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

      {/* ------------------------------------------------- the recommendation, first */}
      <SummaryBlock summary={report.summary} evidence={report.evidence} />

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
              {/* The button is disabled with its reason; the form itself opens read-only so a
                  reader can see what the experiment would cost before enabling live mode. */}
              <Button disabledReason={liveReason} testId="new-experiment">
                New experiment
              </Button>
              <button
                type="button"
                className="text-small text-prussian hover:text-ink underline underline-offset-2 self-start mt-2"
                onClick={() => setShowExperiment(true)}
                data-testid="open-new-experiment"
              >
                See the form and its preflight
              </button>
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

          <TiesPanel ties={cascade.ties} />

          <ProvenancePanel provenance={report.dataset_provenance} />

          <FingerprintPanel fingerprint={report.workload_fingerprint} />

          <CalibrationPanel calibration={report.calibration} />

          <ContractPanel contract={report.contract} />

          <JudgedPanel judged={report.judged} margin={proof.verdict.margin} />

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
          . Token estimates: {report.provenance.base_token_counter}
          {!report.provenance.token_counter_matches_fixtures &&
            ` (recorded as ${report.provenance.recorded_token_counter}, which this server cannot load)`}
          .
          {health.data?.mode === "replay" && " Replay mode: no API calls are made."}
        </p>
      </footer>

      <TraceDrawer taskId={traceTask} onClose={() => setTraceTask(null)} mark={mark} />
      {showExperiment && (
        <NewExperiment report={report} onClose={() => setShowExperiment(false)} />
      )}
    </div>
  );
}

/**
 * What the same comparison says with the answer key withheld (UPGRADE_V3.md U1).
 *
 * Show the raw cheap-judge estimate beside the corrected estimate. Their difference is the
 * measured judge bias.
 */
function JudgedPanel({ judged, margin }: { judged: JudgedView; margin: number }) {
  if (!judged?.available) {
    return (
      <Section
        title="Without the answer key"
        subtitle="The estimate a workload that arrives unlabelled would get."
        id="judged"
      >
        <p className="text-small text-graphite max-w-prose" data-testid="judged-unavailable">
          {judged?.reason ?? "No judged estimate was computed for this report."}
        </p>
      </Section>
    );
  }

  const annotation = judged.annotation;
  const coverage = judged.coverage_check;
  const tone =
    judged.verdict.label === "non_inferior"
      ? "better"
      : judged.verdict.label === "worse"
        ? "worse"
        : "neutral";

  return (
    <Section
      title="Without the answer key"
      subtitle={`A cheap judge graded all ${count(annotation.n)} tasks; ${count(
        annotation.n_annotated,
      )} were re-graded by ${judged.strong_grader.source === "human" ? "a human reviewer" : judged.strong_grader.model_id}.`}
      right={<Pill tone={tone}>{judged.verdict.display}</Pill>}
      id="judged"
    >
      <p className="text-body max-w-prose" data-testid="judged-sentence">
        {judged.verdict.sentence}
      </p>
      <IntervalPlot
        interval={judged.delta_accuracy}
        margin={margin}
        testId="judged-interval-plot"
      />
      <dl className="mt-4 text-small grid grid-cols-[12rem_1fr] gap-x-4 gap-y-1">
        <dt className="text-graphite">The judge alone</dt>
        <dd className="tabular-nums" data-testid="judge-bias">
          {points(judged.judge_only.delta_accuracy.point)} pt (
          {intervalPoints(judged.judge_only.delta_accuracy)}) — {points(judged.judge_only.bias_vs_corrected)} pt
          of judge bias, measured against the strong grader
        </dd>
        <dt className="text-graphite">Annotation</dt>
        <dd className="tabular-nums" data-testid="annotation-row">
          {count(annotation.n_annotated)} of {count(annotation.n)} items ({pct(annotation.annotated_share, 0)})
          at {usd(annotation.annotation_cost_usd, 4)}, judge {usd(annotation.judge_cost_usd, 4)}
          {annotation.strong_only_items_for_same_width !== null &&
            ` · strong-only grading needs ${count(annotation.strong_only_items_for_same_width)} items at ${usdShort(annotation.strong_only_cost_usd ?? "0")} for the same interval width`}
        </dd>
        <dt className="text-graphite">Cost-optimal rate</dt>
        <dd className="tabular-nums">{pct(annotation.cost_optimal_rate, 0)}</dd>
        {coverage && (
          <>
            <dt className="text-graphite">Against the answer key</dt>
            <dd className="tabular-nums" data-testid="judged-coverage">
              gold {points(coverage.gold_delta_accuracy)} pt —{" "}
              {coverage.judged_interval_covers_gold ? "inside" : "outside"} the gold-free interval
            </dd>
          </>
        )}
      </dl>
      <p className="mt-3 text-small text-graphite max-w-prose">
        {annotation.cost_optimal_rate_note}
      </p>
      <ul className="mt-3 text-small text-graphite max-w-prose list-disc pl-5 space-y-1">
        {judged.caveats.map((caveat) => (
          <li key={caveat}>{caveat}</li>
        ))}
      </ul>
      <p className="mt-3 text-micro text-graphite max-w-prose">{annotation.method}</p>
      <Legend kinds={["estimated", "simulated"]} />
    </Section>
  );
}

/**
 * Where the cascade's thresholds came from, and what a calibration with no answer key would
 * have chosen instead (UPGRADE_V3.md U3).
 */
function CalibrationPanel({ calibration }: { calibration: CalibrationView }) {
  if (!calibration) return null;
  const exercise = calibration.fixtures_can_exercise_this;
  return (
    <Section
      title="Where the thresholds came from"
      subtitle={calibration.description}
      right={<Pill tone="neutral">{calibration.mode}</Pill>}
      id="calibration"
    >
      <dl className="text-small grid grid-cols-[12rem_1fr] gap-x-4 gap-y-1">
        <dt className="text-graphite">Operating point</dt>
        <dd className="tabular-nums" data-testid="calibration-thresholds">
          {calibration.thresholds.slice(0, -1).map((t) => t.toFixed(2)).join(", ")} at a{" "}
          {pct(calibration.accuracy_floor)} accuracy floor
          {calibration.excluded > 0 &&
            `, ${count(calibration.excluded)} of ${count(calibration.n)} tasks excluded`}
        </dd>
        {calibration.alternatives.map((alternative) => {
          const agreements = Object.values(alternative.label_agreement_with_gold).filter(
            (value): value is number => value !== null,
          );
          return (
            <Fragment key={alternative.kind}>
              <dt className="text-graphite">{alternative.kind}</dt>
              <dd className="tabular-nums">
                {alternative.thresholds.slice(0, -1).map((t) => t.toFixed(2)).join(", ")} — gap{" "}
                {alternative.max_threshold_gap.toFixed(2)}, {count(alternative.excluded)} excluded
                {agreements.length > 0 && `, labels agree ${pct(Math.min(...agreements), 0)}`}
              </dd>
            </Fragment>
          );
        })}
      </dl>
      <p className="mt-3 text-small text-graphite max-w-prose">{calibration.note}</p>
      {calibration.alternatives.length > 0 && !exercise.answer && (
        <p className="mt-2 text-small text-graphite max-w-prose" data-testid="calibration-limits">
          {exercise.reason}
        </p>
      )}
    </Section>
  );
}

/**
 * The legibility tax, priced (UPGRADE_V3.md U4).
 *
 * Always render the signed accuracy delta beside the saving. `test_contract.py` checks this
 * disclosure.
 */
function ContractPanel({ contract }: { contract: ContractView }) {
  if (!contract) return null;
  if (!contract.available) {
    return (
      <Section
        title="What checkability costs, and what it buys"
        subtitle="A contract that makes an answer easy to verify, priced against the annotation budget."
        id="contract"
      >
        <p className="text-small text-graphite max-w-prose" data-testid="contract-unavailable">
          {contract.reason ?? "Not priced for this report."}
        </p>
      </Section>
    );
  }
  return (
    <Section
      title="What checkability costs, and what it buys"
      subtitle={`${contract.candidate_pipeline} against ${contract.baseline_pipeline}, judged by ${contract.judge.model_id} and corrected by ${count(contract.annotated)} of ${count(contract.n)} strong-graded tasks.`}
      right={
        <Pill tone={contract.pays_for_itself ? "better" : "neutral"}>
          {contract.pays_for_itself ? "Pays for itself" : "Does not pay here"}
        </Pill>
      }
      id="contract"
    >
      <table className="w-full text-base" data-testid="contract-table">
        <thead>
          <tr className="text-small text-graphite">
            <th className="text-left font-medium py-2">Pipeline</th>
            <th className="text-right font-medium py-2">Judge agrees</th>
            <th className="text-right font-medium py-2">Sample rate</th>
            <th className="text-right font-medium py-2">Annotation</th>
            <th className="text-right font-medium py-2">Accuracy</th>
            <th className="text-right font-medium py-2">Generation</th>
          </tr>
        </thead>
        <tbody>
          {contract.arms.map((arm) => (
            <tr key={arm.pipeline} className="rule-t">
              <td className="py-2">
                {arm.pipeline} <span className="text-graphite text-small">{arm.label}</span>
              </td>
              <td className="text-right tabular-nums">
                {pct(arm.judge_agreement_with_strong_grader)}
              </td>
              <td className="text-right tabular-nums">{pct(arm.sampling_rate_for_target)}</td>
              <td className="text-right tabular-nums">
                {usd(arm.annotation_cost_for_target_usd, 4)}
              </td>
              <td className="text-right tabular-nums">{pct(arm.accuracy)}</td>
              <td className="text-right tabular-nums">{usd(arm.generation_cost_usd, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-3 text-body max-w-prose" data-testid="contract-verdict">
        The contract moves judge agreement {points(contract.agreement_delta)} points and accuracy{" "}
        <span data-testid="contract-accuracy-delta">{points(contract.accuracy_delta)}</span> points.
        Annotation saving {usd(contract.annotation_saving_usd, 4)} against a generation premium of{" "}
        {usd(contract.generation_premium_usd, 4)}: net{" "}
        {usd(contract.net_on_evaluation_split_usd, 4)} on the evaluation split, at a target
        standard error of {contract.target_standard_error.toFixed(2)}.
      </p>
      <p className="mt-2 text-small text-graphite max-w-prose">{contract.note}</p>
    </Section>
  );
}

/**
 * Where the tasks came from, and whether that is enough to certify anything (UPGRADE_V3.md U8).
 *
 * Sits under the verdict because it qualifies it. A cascade earns its savings on easy tasks and
 * its risk lives in the tail, so an eval set whose tail has thinned will certify a router that
 * fails in production — and every number above will look fine while it does.
 */
function ProvenancePanel({ provenance }: { provenance: DatasetProvenanceView }) {
  if (!provenance) return null;
  const shape = provenance.shape;
  return (
    <Section
      title="Where these tasks came from"
      subtitle={`${count(provenance.n)} items: ${count(provenance.real_traffic_items)} real traffic, ${count(provenance.model_generated_items)} model-generated, ${count(provenance.program_generated_items)} program-generated.`}
      right={
        <Pill tone={provenance.certifiable ? "better" : "worse"}>
          {provenance.certifiable ? "Can certify" : "Cannot certify"}
        </Pill>
      }
      id="provenance"
    >
      <dl className="text-small grid grid-cols-[12rem_1fr] gap-x-4 gap-y-1">
        <dt className="text-graphite">Tail coverage</dt>
        <dd className="tabular-nums" data-testid="provenance-tail">
          {count(shape.templates_present)} of {count(shape.template_space)} question templates
          present ({pct(shape.tail_coverage, 0)}), {pct(shape.tail_share, 1)} of items in
          rarely-seen templates, concentration {shape.concentration.toFixed(2)}
        </dd>
        <dt className="text-graphite">Generators</dt>
        <dd>
          {provenance.generators.join(", ") || "none declared"}
          {provenance.decoding_budget
            ? `, decoding budget ${provenance.decoding_budget}`
            : ", no decoding budget (no model wrote any of it)"}
        </dd>
      </dl>
      {provenance.refusals.length > 0 && (
        <ul
          className="mt-3 text-small text-vermilion max-w-prose list-disc pl-5 space-y-1"
          data-testid="provenance-refusals"
        >
          {provenance.refusals.map((refusal) => (
            <li key={refusal}>{refusal}</li>
          ))}
        </ul>
      )}
      {provenance.warnings.length > 0 && (
        <ul className="mt-2 text-small text-graphite max-w-prose list-disc pl-5 space-y-1">
          {provenance.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/**
 * What the claim was measured on, as a distribution (UPGRADE_V4.md M18).
 *
 * "Proven on 200 tasks" says nothing about which 200, and the reader deciding whether this
 * result applies to their own queue is the one person who needs to know. The same numbers are
 * what a certificate binds to, so a canary can expire it on coverage — traffic that has moved
 * into a region this split barely held — and not only on time.
 */
function FingerprintPanel({ fingerprint }: { fingerprint: WorkloadFingerprintView }) {
  if (!fingerprint) return null;
  const mix = Object.entries(fingerprint.task_type_mix).sort((a, b) => b[1] - a[1]);
  const quantiles = Object.entries(fingerprint.length_quantiles).sort((a, b) =>
    a[0].localeCompare(b[0], undefined, { numeric: true }),
  );
  return (
    <Section
      title="What this was measured on"
      subtitle="A certificate binds to this distribution, and a canary expires it when recent traffic stops matching."
      right={<Pill tone="neutral">{count(fingerprint.n)} tasks</Pill>}
      id="fingerprint"
    >
      <dl className="text-small grid grid-cols-[12rem_1fr] gap-x-4 gap-y-1">
        <dt className="text-graphite">Task mix</dt>
        <dd className="tabular-nums" data-testid="fingerprint-mix">
          {mix.map(([name, share]) => `${name} ${pct(share, 0)}`).join(", ")}
        </dd>
        <dt className="text-graphite">Question length</dt>
        <dd className="tabular-nums" data-testid="fingerprint-lengths">
          {quantiles.map(([name, value]) => `${name} ${count(Math.round(value))}`).join(", ")}{" "}
          tokens, counted with {fingerprint.counter}
        </dd>
        <dt className="text-graphite">Concentration</dt>
        <dd className="tabular-nums">
          {fingerprint.difficulty.type_concentration.toFixed(2)} across {mix.length} types, the
          rarest {pct(fingerprint.difficulty.rarest_type_share, 1)} of the split
        </dd>
      </dl>
      <p className="mt-3 text-small text-graphite max-w-prose">
        This is a coverage proxy, not a labelled difficulty measure. It reports how much of the
        split lies in its smallest task-type region.
      </p>
    </Section>
  );
}

/**
 * Every configuration the data cannot tell apart on cost (UPGRADE_V3.md U7).
 *
 * List every configuration with an overlapping cost interval, ordered by cost, and include an
 * eligible fallback when one exists.
 */
function TiesPanel({ ties }: { ties: TiesView | null }) {
  if (!ties) return null;
  return (
    <Section
      title={ties.is_tie ? "Tied for cheapest" : "One cheapest configuration"}
      subtitle="Configurations whose cost-per-successful-task intervals overlap cannot be ranked by this data."
      right={
        <Pill tone={ties.is_tie ? "neutral" : "better"}>
          {ties.is_tie ? `${ties.tied.length} tied` : "no tie"}
        </Pill>
      }
      id="ties"
    >
      <table className="w-full text-base" data-testid="ties-table">
        <thead>
          <tr className="text-small text-graphite">
            <th className="text-left font-medium py-2">Configuration</th>
            <th className="text-right font-medium py-2">Cost per successful task</th>
            <th className="text-right font-medium py-2">Accuracy</th>
            <th className="text-right font-medium py-2">Scarce share</th>
            <th className="text-left font-medium py-2 pl-6">Notes</th>
          </tr>
        </thead>
        <tbody>
          {ties.tied.map((row) => (
            <tr key={row.label} className="rule-t">
              <td className="py-2">{row.label}</td>
              <td className="text-right tabular-nums">
                {usd(row.cost_per_successful_task, 6)}
                <span className="text-graphite text-small ml-2">
                  ({usd(row.cost_low, 6)}–{usd(row.cost_high, 6)})
                </span>
              </td>
              <td className="text-right tabular-nums">{pct(row.accuracy)}</td>
              <td className="text-right tabular-nums">{pct(row.scarce_share, 0)}</td>
              <td className="pl-6 text-small text-graphite">
                {[
                  row.is_operating_point ? "operating point" : null,
                  row.is_fallback ? "fallback" : null,
                  row.adoptable ? null : "not adoptable here",
                ]
                  .filter(Boolean)
                  .join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-3 text-small text-graphite max-w-prose" data-testid="ties-note">
        {ties.note}
      </p>
      <p className="mt-2 text-small text-graphite max-w-prose" data-testid="ties-fallback">
        {ties.fallback_reason}
      </p>
    </Section>
  );
}
