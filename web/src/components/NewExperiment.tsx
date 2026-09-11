/**
 * The New experiment form (SPEC.md 5.1).
 *
 * The defaults reproduce the demo. In replay mode the whole form is read-only with the reason
 * stated once, and the preflight projection is shown from the recorded run so a reader can see
 * what the experiment *would* cost before enabling live mode — which is the number that
 * actually governs whether they run it.
 */
import { useState } from "react";
import { count, usd } from "../lib/format";
import type { Report } from "../lib/types";
import { Button, Section } from "./Primitives";

export function NewExperiment({
  report,
  onClose,
}: {
  report: Report;
  onClose: () => void;
}) {
  const tiers = report.cascade.tiers.map((t) => t.tier);
  const [margin, setMargin] = useState(report.workload.margin);
  const [cap, setCap] = useState(30);
  const [advanced, setAdvanced] = useState(false);
  const reason = report.live_controls.disabled_reasons.new_experiment;
  const disabled = Boolean(reason);
  const projection = Number(report.proof.proof_cost.total_usd);

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label="New experiment">
      <button type="button" className="flex-1 bg-ink/20" onClick={onClose} aria-label="Close" />
      <div className="w-[min(560px,92vw)] bg-chalk overflow-y-auto border-l border-rule-strong px-5 py-4" data-testid="new-experiment-form">
        <header className="flex items-start justify-between gap-4 mb-3">
          <h2 className="text-head font-medium">New experiment</h2>
          <button type="button" onClick={onClose} className="text-base text-graphite hover:text-ink" data-testid="new-experiment-close">
            Close
          </button>
        </header>

        {disabled && (
          <p className="border-l-2 border-vermilion pl-3 text-small text-graphite mb-4" data-testid="new-experiment-disabled">
            {reason}
          </p>
        )}

        <fieldset disabled={disabled} className="space-y-4">
          <label className="block">
            <span className="text-small text-graphite">Workload</span>
            <select className="w-full border border-rule-strong px-2 py-1 bg-paper mt-0.5" defaultValue={report.workload.id}>
              <option value={report.workload.id}>{report.workload.name}</option>
            </select>
          </label>

          <label className="block">
            <span className="text-small text-graphite">Baseline model</span>
            <select className="w-full border border-rule-strong px-2 py-1 bg-paper mt-0.5" defaultValue={report.provenance.model_ids.frontier}>
              {Object.values(report.provenance.model_ids).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </label>

          <div>
            <span className="text-small text-graphite">Cascade tiers, cheapest first (2 to 4)</span>
            <ul className="mt-1">
              {tiers.map((tier, i) => (
                <li key={tier} className="flex items-center gap-2 py-1">
                  <span className="text-micro text-graphite w-4 tabular-nums">{i + 1}</span>
                  <input
                    type="checkbox"
                    defaultChecked
                    className="accent-prussian"
                    data-testid={`tier-${tier}`}
                  />
                  <span className="font-mono text-small">{report.provenance.model_ids[tier]}</span>
                </li>
              ))}
            </ul>
          </div>

          <label className="block">
            <span className="text-small text-graphite">
              Non-inferiority margin — {(margin * 100).toFixed(0)} points
            </span>
            <input
              type="range"
              min={0.01}
              max={0.1}
              step={0.01}
              value={margin}
              onChange={(e) => setMargin(Number(e.target.value))}
              className="w-full accent-prussian"
              data-testid="margin-input"
            />
          </label>

          <label className="block">
            <span className="text-small text-graphite">Spend cap for this experiment</span>
            <input
              type="number"
              value={cap}
              min={1}
              onChange={(e) => setCap(Number(e.target.value) || 1)}
              className="w-32 border border-rule-strong px-2 py-1 bg-paper mt-0.5 tabular-nums"
              data-testid="cap-input"
            />
          </label>

          {advanced && (
            <div className="space-y-3 rule-t pt-3" data-testid="advanced-panel">
              <label className="block">
                <span className="text-small text-graphite">Split sizes</span>
                <div className="flex gap-3 mt-0.5">
                  <input
                    type="number"
                    defaultValue={report.workload.dataset.calibration}
                    className="w-24 border border-rule-strong px-2 py-1 bg-paper tabular-nums"
                  />
                  <input
                    type="number"
                    defaultValue={report.workload.dataset.test}
                    className="w-24 border border-rule-strong px-2 py-1 bg-paper tabular-nums"
                  />
                </div>
              </label>
              <label className="block">
                <span className="text-small text-graphite">Threshold step</span>
                <input
                  type="number"
                  step={0.01}
                  defaultValue={0.02}
                  className="w-24 border border-rule-strong px-2 py-1 bg-paper mt-0.5 tabular-nums"
                />
              </label>
              <div>
                <span className="text-small text-graphite">Scorer features</span>
                <ul className="mt-1">
                  {Object.values(report.cascade.scorers.tiers)[0]?.features.map((f) => (
                    <li key={f} className="flex items-center gap-2 py-0.5">
                      <input type="checkbox" defaultChecked className="accent-prussian" />
                      <span className="font-mono text-micro">{f}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </fieldset>

        {/* A disclosure, not an input: it stays usable when the form is read-only, so a reader
            can see what the advanced options are before enabling live mode. */}
        <button
          type="button"
          onClick={() => setAdvanced(!advanced)}
          className="text-small text-prussian hover:text-ink mt-3"
          data-testid="advanced-toggle"
        >
          {advanced ? "Hide" : "Show"} advanced
        </button>

        <Section
          title="Preflight"
          subtitle="What this experiment would cost, projected from the recorded run."
        >
          <dl className="text-small grid grid-cols-[11rem_1fr] gap-x-4 gap-y-1">
            <dt className="text-graphite">Projected spend</dt>
            <dd className="tabular-nums">{usd(projection, 2)}</dd>
            <dt className="text-graphite">Against your cap</dt>
            <dd className={projection > cap ? "text-vermilion" : "text-verdigris"}>
              {projection > cap
                ? `over the $${cap} cap — the run would refuse to start`
                : `within the $${cap} cap`}
            </dd>
            <dt className="text-graphite">Task-runs</dt>
            <dd className="tabular-nums">
              {count(
                report.workload.dataset.calibration * tiers.length +
                  report.workload.dataset.test * (tiers.length + 2),
              )}
            </dd>
          </dl>
          <div className="mt-4">
            <Button
              variant="primary"
              disabledReason={reason ?? undefined}
              testId="start-experiment"
            >
              Start the experiment
            </Button>
          </div>
        </Section>
      </div>
    </div>
  );
}
