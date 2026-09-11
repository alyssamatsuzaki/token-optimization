/**
 * Spend: where did the money and the scarce-model budget go? (SPEC.md 5.4)
 *
 * Built from the same replayed runs the Optimize screen reads, so a figure here and a figure
 * there cannot disagree. The copy says plainly what this ledger can and cannot see.
 */
import { useSpend } from "../lib/api";
import { count, ms, pct, tokens, usd } from "../lib/format";
import { ErrorRow, Figure, Legend, LoadingRow, Metric, Pill, Section } from "../components/Primitives";
import type { SpendView } from "../lib/types";

/**
 * Budget status with the warning at 80% and the stop at 100% (SPEC.md 5.4, 7.7).
 *
 * The state comes from the engine's own SpendGuard rather than being recomputed here, so the
 * threshold the screen shows and the threshold that refuses a call are the same number.
 */
function BudgetStatus({ budgets }: { budgets: SpendView["budgets"] }) {
  const tone =
    budgets.state === "stopped" ? "worse" : budgets.state === "warning" ? "worse" : "neutral";
  return (
    <div data-testid="budget-status">
      <div className="text-small text-graphite">Daily budget</div>
      <div className="mt-0.5">
        <Figure
          value={budgets.daily_cap_usd ? usd(budgets.daily_cap_usd, 2) : "not set"}
          kind="none"
          size="figure"
        />
      </div>
      {budgets.daily_cap_usd ? (
        <>
          <div className="h-1.5 bg-rule mt-1.5 w-full">
            <div
              className={budgets.state === "ok" ? "h-1.5 bg-prussian" : "h-1.5 bg-vermilion"}
              style={{ width: `${Math.min(100, (budgets.fraction ?? 0) * 100)}%` }}
            />
          </div>
          <div className="text-small text-graphite mt-0.5 flex items-center gap-2">
            {usd(budgets.daily_spent_usd, 2)} used, {usd(budgets.daily_remaining_usd ?? "0", 2)}{" "}
            left
            {budgets.state !== "ok" && <Pill tone={tone}>{budgets.state}</Pill>}
          </div>
          <div className="text-micro text-graphite mt-0.5">
            Warning at {pct(budgets.warning_at, 0)}, hard stop at 100%.
          </div>
        </>
      ) : (
        <div className="text-small text-graphite mt-0.5">
          No live action may spend without DAILY_BUDGET_USD.
        </div>
      )}
      <div className="text-micro text-graphite mt-1 max-w-[36ch]">{budgets.note}</div>
    </div>
  );
}

export default function Spend() {
  const { data, isLoading, error } = useSpend();
  if (isLoading) return <div className="px-8"><LoadingRow what="the ledger" /></div>;
  if (error || !data) return <div className="px-8"><ErrorRow error={error} what="Spend" /></div>;

  const mark = data.provenance.is_test_data ? "simulated" : "provider";
  const total = Number(data.total_usd);
  const maxModel = Math.max(...data.by_model.map((m) => Number(m.usd)));

  return (
    <div className="px-8 pb-16 max-w-[1180px]">
      <header className="py-5">
        <h1 className="text-title font-medium tracking-tight">Spend</h1>
        <p className="text-base text-graphite mt-1 max-w-prose">{data.note}</p>
      </header>

      <div className="rule-t rule-b py-5 grid grid-cols-2 md:grid-cols-4 gap-6" data-testid="spend-headline">
        <Metric
          label="Total through Tokop"
          value={usd(total, 4)}
          interval={`${count(data.runs.length)} runs`}
          kind={mark}
          size="figure"
        />
        <Metric
          label="Scarce-model spend"
          value={usd(data.scarce_usd, 4)}
          interval={`${pct(data.scarce_share, 0)} of the total`}
          kind={mark}
          size="figure"
        />
        <BudgetStatus budgets={data.budgets} />
        <Metric
          label="Fixtures"
          value={data.provenance.is_test_data ? "simulated" : "recorded"}
          interval={data.provenance.recorded_at?.slice(0, 10) ?? "—"}
          kind="none"
        />
      </div>

      <Section
        title="By day"
        subtitle="Spend over time. A fixture set was recorded in one session, so it has one day; a live ledger fills this out."
      >
        <table className="w-full text-base" data-testid="spend-by-day">
          <thead>
            <tr className="text-small text-graphite">
              <th className="text-left font-medium py-2">Day</th>
              <th className="text-right font-medium py-2">Spend</th>
              <th className="py-2 pl-6 w-1/2" />
            </tr>
          </thead>
          <tbody>
            {data.by_day.map((row) => (
              <tr key={row.day} className="rule-t">
                <td className="py-2 tabular-nums">{row.day}</td>
                <td className="py-2 text-right tabular-nums">
                  <Figure value={usd(row.usd, 4)} kind={mark} />
                </td>
                <td className="py-2 pl-6">
                  <div
                    className="h-2 bg-prussian"
                    style={{
                      width: `${(Number(row.usd) / Math.max(...data.by_day.map((d) => Number(d.usd)))) * 100}%`,
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="By provider and workload" subtitle="Which vendor, and which workload.">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
          <table className="w-full text-base" data-testid="spend-by-provider">
            <thead>
              <tr className="text-small text-graphite">
                <th className="text-left font-medium py-2">Provider</th>
                <th className="text-right font-medium py-2">Spend</th>
                <th className="text-right font-medium py-2">Share</th>
              </tr>
            </thead>
            <tbody>
              {data.by_provider.map((row) => (
                <tr key={row.provider} className="rule-t">
                  <td className="py-2">{row.provider}</td>
                  <td className="py-2 text-right tabular-nums">{usd(row.usd, 4)}</td>
                  <td className="py-2 text-right tabular-nums">{pct(row.share, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="w-full text-base" data-testid="spend-by-workload">
            <thead>
              <tr className="text-small text-graphite">
                <th className="text-left font-medium py-2">Workload</th>
                <th className="text-right font-medium py-2">Runs</th>
                <th className="text-right font-medium py-2">Spend</th>
              </tr>
            </thead>
            <tbody>
              {data.by_workload.map((row) => (
                <tr key={row.workload} className="rule-t">
                  <td className="py-2 font-mono text-small">{row.workload}</td>
                  <td className="py-2 text-right tabular-nums">{count(row.runs)}</td>
                  <td className="py-2 text-right tabular-nums">{usd(row.usd, 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="By model" subtitle="Where the money went, and how much of it was scarce.">
        <table className="w-full text-base" data-testid="spend-by-model">
          <thead>
            <tr className="text-small text-graphite">
              <th className="text-left font-medium py-2">Model</th>
              <th className="text-right font-medium py-2">Spend</th>
              <th className="text-right font-medium py-2">Share</th>
              <th className="py-2 pl-6 w-1/3" />
            </tr>
          </thead>
          <tbody>
            {data.by_model.map((row) => (
              <tr key={row.model_id} className="rule-t">
                <td className="py-2">
                  <span className="font-mono text-small">{row.model_id}</span>
                  {row.scarce && <span className="ml-2"><Pill tone="scarce">scarce</Pill></span>}
                </td>
                <td className="py-2 text-right tabular-nums">
                  <Figure value={usd(row.usd, 4)} kind={mark} />
                </td>
                <td className="py-2 text-right tabular-nums">{pct(row.share, 0)}</td>
                <td className="py-2 pl-6">
                  <div
                    className={`h-2 ${row.scarce ? "hatch-scarce border border-vermilion" : "bg-prussian"}`}
                    style={{ width: `${(Number(row.usd) / maxModel) * 100}%` }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section
        title="Cost per successful task, by pipeline"
        subtitle="The metric that matters, with its interval, for each workload over time."
      >
        <table className="w-full text-base" data-testid="spend-cps">
          <thead>
            <tr className="text-small text-graphite">
              <th className="text-left font-medium py-2">Pipeline</th>
              <th className="text-right font-medium py-2">Cost per successful task</th>
              <th className="text-right font-medium py-2">95% interval</th>
              <th className="text-right font-medium py-2">Recorded</th>
            </tr>
          </thead>
          <tbody>
            {data.cost_per_successful_task.map((row) => (
              <tr key={row.pipeline} className="rule-t">
                <td className="py-2">
                  <span className="font-mono text-small text-graphite mr-2">{row.pipeline}</span>
                  {row.label}
                </td>
                <td className="py-2 text-right tabular-nums">
                  <Figure value={usd(row.usd)} kind={mark} />
                </td>
                <td className="py-2 text-right tabular-nums text-graphite text-small">
                  {usd(row.low)}–{usd(row.high)}
                </td>
                <td className="py-2 text-right tabular-nums text-graphite text-small">
                  {row.day}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Every run" subtitle="Each pipeline on each split, with the tokens it used.">
        <table className="w-full text-small" data-testid="spend-runs">
          <thead>
            <tr className="text-graphite text-micro">
              <th className="text-left font-medium py-1">Pipeline</th>
              <th className="text-left font-medium py-1">Split</th>
              <th className="text-left font-medium py-1">Tier</th>
              <th className="text-right font-medium py-1">Calls</th>
              <th className="text-right font-medium py-1">Input</th>
              <th className="text-right font-medium py-1">Cache read</th>
              <th className="text-right font-medium py-1">Output</th>
              <th className="text-right font-medium py-1">p50</th>
              <th className="text-right font-medium py-1">Accuracy</th>
              <th className="text-right font-medium py-1">Cost</th>
            </tr>
          </thead>
          <tbody>
            {data.runs.map((run) => (
              <tr key={`${run.pipeline}-${run.split}-${run.tier}`} className="rule-t">
                <td className="py-1.5 font-mono text-micro">{run.pipeline}</td>
                <td className="py-1.5">{run.split}</td>
                <td className="py-1.5">{run.tier}</td>
                <td className="py-1.5 text-right tabular-nums">
                  {count(run.calls)}
                  {run.prewarm_calls > 0 && (
                    <span className="text-graphite text-micro"> +{run.prewarm_calls} warm</span>
                  )}
                </td>
                <td className="py-1.5 text-right tabular-nums">{tokens(run.input_tokens)}</td>
                <td className="py-1.5 text-right tabular-nums">
                  {run.cache_read_tokens ? tokens(run.cache_read_tokens) : "—"}
                </td>
                <td className="py-1.5 text-right tabular-nums">{tokens(run.output_tokens)}</td>
                <td className="py-1.5 text-right tabular-nums">
                  {run.p50_latency_ms ?? run.recorded_latency_p50_ms
                    ? ms(run.p50_latency_ms ?? run.recorded_latency_p50_ms)
                    : "—"}
                </td>
                <td className="py-1.5 text-right tabular-nums">{pct(run.accuracy)}</td>
                <td className="py-1.5 text-right tabular-nums">{usd(run.total_cost_usd, 4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <footer className="rule-t pt-4 mt-2">
        <Legend kinds={["provider", "estimated", "simulated"]} />
      </footer>
    </div>
  );
}
