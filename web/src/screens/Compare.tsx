/**
 * Compare: which model is good enough for this prompt? (SPEC.md 5.3)
 *
 * In replay mode this shows the two recorded comparisons. A manual column is included because
 * subscription apps have no public API: Tokop hands you the prompt and takes the answer back,
 * and never automates a chat app (SPEC.md non-negotiable 6).
 */
import { useState } from "react";
import { useCompare } from "../lib/api";
import { count, ms, tokens, usd } from "../lib/format";
import {
  Button,
  ErrorRow,
  Figure,
  Legend,
  LoadingRow,
  Metric,
  Pill,
  Section,
} from "../components/Primitives";

export default function Compare() {
  const { data, isLoading, error } = useCompare();
  const [selected, setSelected] = useState(0);
  const [manual, setManual] = useState("");
  const [copied, setCopied] = useState(false);
  const [preferred, setPreferred] = useState<string | null>(null);

  if (isLoading) return <div className="px-8"><LoadingRow what="the recorded comparisons" /></div>;
  if (error || !data) return <div className="px-8"><ErrorRow error={error} what="Compare" /></div>;

  const example = data.examples[selected];
  const mark = data.is_test_data ? "simulated" : "provider";
  const liveReason = data.live_controls.disabled_reasons.compare;

  return (
    <div className="px-8 pb-16 max-w-[1180px]">
      <header className="py-5">
        <h1 className="text-title font-medium tracking-tight">Compare</h1>
        <p className="text-base text-graphite mt-1 max-w-prose">{data.note}</p>
        <nav className="mt-4 flex gap-5 text-base" role="tablist">
          {data.examples.map((e, i) => (
            <button
              key={e.id}
              role="tab"
              aria-selected={selected === i}
              onClick={() => setSelected(i)}
              className={selected === i ? "text-ink font-medium" : "text-graphite hover:text-ink"}
              data-testid={`compare-tab-${e.id}`}
            >
              {e.title}
            </button>
          ))}
        </nav>
      </header>

      <Section title="Prompt" subtitle={`System: ${example.system}`}>
        <pre className="font-mono text-micro whitespace-pre-wrap bg-chalk border border-rule p-3" data-testid="compare-prompt">
          {example.prompt}
        </pre>
        <div className="mt-3 flex flex-wrap items-start gap-3">
          <Button
            onClick={() => {
              void navigator.clipboard?.writeText(example.prompt);
              setCopied(true);
            }}
            testId="copy-prompt"
          >
            {copied ? "Copied" : "Copy the prompt"}
          </Button>
          <Button disabledReason={liveReason} testId="run-comparison">
            Run this on the selected models
          </Button>
        </div>
      </Section>

      <Section
        title="Responses"
        subtitle="Tokens by bucket, cost, time to first token and total latency, per model."
      >
        <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${example.columns.length + 1}, minmax(0, 1fr))` }} data-testid="compare-columns">
          {example.columns.map((column) => (
            <article key={column.model_id} className="border border-rule bg-chalk" data-testid={`compare-column-${column.model_id}`}>
              <header className="px-3 py-2 rule-b flex items-baseline justify-between gap-2">
                <span className="text-base font-medium truncate">{column.display_name}</span>
                {column.scarce && <Pill tone="scarce">scarce</Pill>}
              </header>
              <dl className="px-3 py-2 text-small grid grid-cols-2 gap-x-3 gap-y-1">
                <dt className="text-graphite">output</dt>
                <dd className="text-right tabular-nums">{tokens(column.output_tokens)}</dd>
                <dt className="text-graphite">input</dt>
                <dd className="text-right tabular-nums">{tokens(column.input_tokens)}</dd>
                <dt className="text-graphite">cost</dt>
                <dd className="text-right tabular-nums" title={column.cost_formula}>
                  <Figure value={usd(column.cost_usd, 5)} kind={mark} />
                </dd>
                <dt className="text-graphite">first token</dt>
                <dd className="text-right tabular-nums">{ms(column.ttft_ms)}</dd>
                <dt className="text-graphite">total</dt>
                <dd className="text-right tabular-nums">{ms(column.latency_ms)}</dd>
              </dl>
              <div className="px-3 py-2 rule-t">
                <pre className="font-mono text-micro whitespace-pre-wrap max-h-72 overflow-y-auto">
                  {column.text}
                </pre>
              </div>
              <div className="px-3 py-2 rule-t">
                <button
                  type="button"
                  onClick={() => setPreferred(column.model_id)}
                  className={`text-small ${preferred === column.model_id ? "text-verdigris font-medium" : "text-prussian hover:text-ink"}`}
                  data-testid={`prefer-${column.model_id}`}
                >
                  {preferred === column.model_id ? "Preferred" : "Prefer this answer"}
                </button>
                {preferred === column.model_id && (
                  <p className="text-micro text-graphite mt-1">
                    Stored for a future learned router. Nothing reads it in v1.
                  </p>
                )}
              </div>
            </article>
          ))}

          <article className="border border-dashed border-rule-strong" data-testid="manual-column">
            <header className="px-3 py-2 rule-b">
              <span className="text-base font-medium">Manual column</span>
            </header>
            <p className="px-3 py-2 text-micro text-graphite">{example.manual_column.note}</p>
            <div className="px-3 py-2">
              <textarea
                value={manual}
                onChange={(e) => setManual(e.target.value)}
                rows={8}
                placeholder="Paste the answer from a subscription app here"
                className="w-full font-mono text-micro border border-rule p-2 bg-chalk"
                data-testid="manual-paste"
              />
              <dl className="text-small grid grid-cols-2 gap-x-3 gap-y-1 mt-2">
                <dt className="text-graphite">output</dt>
                <dd className="text-right tabular-nums">
                  <Figure
                    value={tokens(Math.round(manual.length / 4.2))}
                    kind="estimated"
                    title="Estimated from character count; a pasted answer has no provider usage."
                  />
                </dd>
                <dt className="text-graphite">cost</dt>
                <dd className="text-right text-graphite">subscription</dd>
              </dl>
            </div>
          </article>
        </div>
      </Section>

      {example.synthesis && (
        <Section
          title="Synthesis"
          subtitle={`All responses sent to ${example.synthesis_model}, which returns a fixed structure.`}
        >
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6" data-testid="synthesis">
            {(
              [
                ["Points of agreement", example.synthesis.agreement],
                ["Disagreements", example.synthesis.disagreement],
                ["Claims only one model made", example.synthesis.unique],
                ["Likely errors", example.synthesis.likely_errors],
              ] as const
            ).map(([title, items]) => (
              <div key={title}>
                <h3 className="text-base font-medium mb-1">{title}</h3>
                <ul className="text-small text-graphite">
                  {items.length === 0 && <li className="py-1">None.</li>}
                  {items.map((item, i) => (
                    <li key={i} className="py-1 rule-t">
                      {item}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <div className="mt-5 rule-t pt-4">
            <h3 className="text-base font-medium mb-1">Final answer</h3>
            <p className="text-base max-w-prose">{example.synthesis.final}</p>
          </div>
        </Section>
      )}

      <Section title="Totals" subtitle="What this comparison cost.">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-5">
          <Metric
            label="Models compared"
            value={count(example.columns.length)}
            interval="plus one manual column"
            kind="estimated"
          />
          <Metric
            label="Total cost"
            value={usd(
              example.columns.reduce((sum, c) => sum + Number(c.cost_usd), 0),
              5,
            )}
            interval="sum of every column"
            kind={mark}
          />
          <Metric
            label="Cheapest"
            value={
              [...example.columns].sort((a, b) => Number(a.cost_usd) - Number(b.cost_usd))[0]
                .display_name
            }
            interval={usd(
              [...example.columns].sort((a, b) => Number(a.cost_usd) - Number(b.cost_usd))[0]
                .cost_usd,
              5,
            )}
            kind={mark}
          />
          <Metric
            label="Fastest first token"
            value={
              [...example.columns].sort((a, b) => (a.ttft_ms ?? 0) - (b.ttft_ms ?? 0))[0]
                .display_name
            }
            interval={ms(
              [...example.columns].sort((a, b) => (a.ttft_ms ?? 0) - (b.ttft_ms ?? 0))[0].ttft_ms,
            )}
            kind={mark}
          />
        </div>
      </Section>

      <footer className="rule-t pt-4 mt-2">
        <Legend kinds={["provider", "estimated", "simulated"]} />
      </footer>
    </div>
  );
}
