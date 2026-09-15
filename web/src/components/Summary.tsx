/**
 * The first thing on the Optimize screen: what this costs now, what it could cost, whether the
 * evidence supports the swap, and the artifacts that would apply it (UPGRADE_V4.md M14).
 *
 * Every number here arrives formatted or pre-computed from `/api/report`. Non-negotiable 1 bans
 * metric literals in components, and a component that divides one figure by another to get a
 * saving has written a metric — it just wrote it in TypeScript. The engine's `summary` block
 * exists so this file can be arithmetic-free.
 */
import { useState } from "react";

import type { EvidenceView, SummaryView } from "../lib/types";
import { count, pct, points, usd } from "../lib/format";
import { Button, Pill, Section } from "./Primitives";

const GRADE_TONE: Record<EvidenceView["grade"], "better" | "worse" | "neutral"> = {
  recording: "better",
  simulated: "neutral",
  insufficient: "worse",
};

const STANDING_TONE: Record<string, "better" | "worse" | "neutral"> = {
  ok: "better",
  simulated: "neutral",
  blocking: "worse",
};

/** One word, and the whole chain behind it one click away. */
export function EvidencePanel({ evidence }: { evidence: EvidenceView }) {
  const [open, setOpen] = useState(false);
  return (
    <div data-testid="evidence-panel">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-baseline gap-2 text-left group"
        data-testid="evidence-toggle"
        aria-expanded={open}
      >
        <span className="text-micro uppercase tracking-wide text-graphite">Evidence</span>
        <span
          className="text-base font-medium underline decoration-rule-strong underline-offset-4 group-hover:decoration-ink"
          data-testid="evidence-grade"
        >
          {evidence.grade}
        </span>
        <span className="text-micro text-graphite">{open ? "hide the chain" : "show the chain"}</span>
      </button>
      <p className="text-small text-graphite mt-1 max-w-prose" data-testid="evidence-headline">
        {evidence.headline}
      </p>
      {open && (
        <table className="w-full mt-3 text-small" data-testid="evidence-chain">
          <thead>
            <tr className="text-micro uppercase tracking-wide text-graphite text-left">
              <th className="font-normal py-1 pr-3">Link</th>
              <th className="font-normal py-1 pr-3">Reading</th>
              <th className="font-normal py-1 pr-3">Source</th>
              <th className="font-normal py-1">What would change it</th>
            </tr>
          </thead>
          <tbody>
            {evidence.inputs.map((link) => (
              <tr key={link.name} className="rule-t align-top" data-testid="evidence-link">
                <td className="py-1.5 pr-3">
                  <Pill tone={STANDING_TONE[link.standing] ?? "neutral"}>{link.standing}</Pill>{" "}
                  {link.name}
                </td>
                <td className="py-1.5 pr-3 tabular-nums">{link.value}</td>
                <td className="py-1.5 pr-3 text-graphite">{link.source}</td>
                <td className="py-1.5 text-graphite">{link.what_would_change_it || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

interface Artifact {
  artifact: string;
  filename: string;
  note: string;
  text: string;
}

/** Deploy hands over artifacts. It never carries traffic — SPEC.md section 3 excludes that. */
function DeployPanel({ grade }: { grade: EvidenceView["grade"] }) {
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  async function load() {
    try {
      const response = await fetch("/api/export");
      if (!response.ok) throw new Error(`/api/export returned ${response.status}`);
      const body = (await response.json()) as { artifacts: Artifact[] };
      setArtifacts(body.artifacts);
    } catch (cause) {
      setFailed(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <div data-testid="deploy">
      <Button onClick={load} variant="primary" testId="deploy-button">
        Deploy
      </Button>
      <p className="text-micro text-graphite mt-1 max-w-[42ch] leading-snug">
        Exports a diff, a config file and a routing rule. Tokop never sits in your request path.
      </p>
      {failed && (
        <p className="text-small text-vermilion mt-2" data-testid="deploy-error">
          {failed}
        </p>
      )}
      {artifacts && (
        <div className="mt-3 space-y-3" data-testid="deploy-artifacts">
          {grade !== "recording" && (
            <p className="text-small text-vermilion max-w-prose" data-testid="deploy-caveat">
              These describe a result no recording backs yet. They are correct about the traces
              they were computed over; applying them is a decision about how far that generalises.
            </p>
          )}
          {artifacts.map((artifact) => (
            <details key={artifact.artifact} className="rule-t pt-2">
              <summary className="cursor-pointer text-base">
                {artifact.filename}{" "}
                <span className="text-small text-graphite">— {artifact.note}</span>
              </summary>
              <pre className="mt-2 overflow-x-auto text-micro leading-relaxed text-graphite">
                {artifact.text}
              </pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

export function SummaryBlock({
  summary,
  evidence,
}: {
  summary: SummaryView;
  evidence: EvidenceView;
}) {
  const quality = summary.quality;
  return (
    <Section title="The recommendation" testId="summary-block">
      <div className="grid gap-6 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div>
          <p className="text-base max-w-prose" data-testid="summary-sentence">
            {summary.verdict.sentence}
          </p>
          <dl className="mt-4 grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-4">
            <div>
              <dt className="text-micro uppercase tracking-wide text-graphite">Running today</dt>
              <dd className="text-base tabular-nums" data-testid="summary-current">
                {usd(summary.current.cost_per_successful_task_usd)}
                {summary.provenance_mark}
              </dd>
              <dd className="text-micro text-graphite">{summary.current.pipeline}</dd>
            </div>
            <div>
              <dt className="text-micro uppercase tracking-wide text-graphite">Recommended</dt>
              <dd className="text-base tabular-nums" data-testid="summary-recommended">
                {usd(summary.recommended.cost_per_successful_task_usd)}
                {summary.provenance_mark}
              </dd>
              <dd className="text-micro text-graphite">{summary.recommended.pipeline}</dd>
            </div>
            <div>
              <dt className="text-micro uppercase tracking-wide text-graphite">Saving</dt>
              <dd className="text-base tabular-nums" data-testid="summary-saving">
                {summary.saving.fraction === null ? "—" : pct(summary.saving.fraction, 1)}
              </dd>
              <dd className="text-micro text-graphite">
                {summary.saving.repayment_tasks === null
                  ? "does not repay the proof"
                  : `repays after ${count(summary.saving.repayment_tasks)} tasks`}
              </dd>
            </div>
            <div>
              <dt className="text-micro uppercase tracking-wide text-graphite">Quality change</dt>
              <dd className="text-base tabular-nums" data-testid="summary-quality">
                {points(quality.delta_points / 100)} pt
              </dd>
              <dd className="text-micro text-graphite">
                95% CI {points(quality.low_points / 100)} to {points(quality.high_points / 100)}, n ={" "}
                {count(quality.n)}
              </dd>
            </div>
            <div>
              <dt className="text-micro uppercase tracking-wide text-graphite">Allowed change</dt>
              <dd className="text-base tabular-nums" data-testid="summary-allowed">
                −{quality.allowed_points.toFixed(0)} pt
              </dd>
              <dd className="text-micro text-graphite">the margin this workload set</dd>
            </div>
            <div>
              <dt className="text-micro uppercase tracking-wide text-graphite">Verdict</dt>
              <dd className="mt-0.5" data-testid="summary-verdict">
                <Pill tone={GRADE_TONE[evidence.grade]}>{summary.verdict.display}</Pill>
              </dd>
            </div>
          </dl>
        </div>
        <div className="space-y-5">
          <EvidencePanel evidence={evidence} />
          <DeployPanel grade={evidence.grade} />
        </div>
      </div>
    </Section>
  );
}
