/**
 * Spend: where did the money and the scarce-model budget go? (SPEC.md 5.4)
 *
 * Built from the same replayed runs the Optimize screen reads, so a figure here and a figure
 * there cannot disagree. The copy says plainly what this ledger can and cannot see.
 */
import { useSpend } from "../lib/api";
import { count, pct, tokens, usd } from "../lib/format";
import { ErrorRow, Figure, Legend, LoadingRow, Metric, Pill, Section } from "../components/Primitives";

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
        <Metric
          label="Daily budget"
          value={data.budgets.daily_cap_usd ? usd(data.budgets.daily_cap_usd, 2) : "not set"}
          interval={
            data.budgets.daily_cap_usd
              ? `${usd(data.budgets.daily_spent_usd, 2)} used today`
              : "no live action may spend without DAILY_BUDGET_USD"
          }
          kind="estimated"
        />
        <Metric
          label="Mode"
          value={data.provenance.is_test_data ? "replay, test data" : "replay, recorded"}
          interval={data.provenance.recorded_at ?? "—"}
          kind="estimated"
        />
      </div>

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
        subtitle="The metric that matters, with its interval."
      >
        <table className="w-full text-base" data-testid="spend-cps">
          <thead>
            <tr className="text-small text-graphite">
              <th className="text-left font-medium py-2">Pipeline</th>
              <th className="text-right font-medium py-2">Cost per successful task</th>
              <th className="text-right font-medium py-2">95% interval</th>
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
