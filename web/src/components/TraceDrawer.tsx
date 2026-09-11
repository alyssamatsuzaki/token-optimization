/**
 * The trace drawer: every call behind one task (SPEC.md 5.1, point 5).
 *
 * This is where a sceptic lands after clicking a number, so it holds everything needed to
 * check it: the prompt hash, tokens by bucket with their provenance, reuse status, the cost
 * with its formula, latency, the scorer's features, the route decision and the grade.
 */
import { useTrace } from "../lib/api";
import { count, ms, tokens, usd } from "../lib/format";
import type { Provenance } from "../lib/types";
import { ErrorRow, Figure, LoadingRow, Pill } from "./Primitives";

export function TraceDrawer({
  taskId,
  onClose,
  mark,
}: {
  taskId: string | null;
  onClose: () => void;
  mark: Provenance;
}) {
  const { data, isLoading, error } = useTrace(taskId);
  if (taskId === null) return null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label="Task trace">
      <button
        type="button"
        className="flex-1 bg-ink/20"
        onClick={onClose}
        aria-label="Close the trace"
      />
      <div className="w-[min(760px,92vw)] bg-chalk overflow-y-auto border-l border-rule-strong" data-testid="trace-drawer">
        <header className="sticky top-0 bg-chalk rule-b px-5 py-3 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="text-head font-medium">Trace</h2>
            <p className="text-micro text-graphite font-mono truncate">{taskId}</p>
          </div>
          <button type="button" onClick={onClose} className="text-base text-graphite hover:text-ink" data-testid="trace-close">
            Close
          </button>
        </header>

        {isLoading && <div className="px-5"><LoadingRow what="this trace" /></div>}
        {error && <div className="px-5"><ErrorRow error={error} what="the trace" /></div>}

        {data && (
          <div className="px-5 py-4">
            <dl className="text-base grid grid-cols-[9rem_1fr] gap-x-4 gap-y-1 mb-5">
              <dt className="text-graphite">Question</dt>
              <dd>{data.task.question}</dd>
              <dt className="text-graphite">Gold answer</dt>
              <dd className="font-mono">{data.task.gold}</dd>
              <dt className="text-graphite">Answer type</dt>
              <dd>{data.task.answer_type}</dd>
              <dt className="text-graphite">Question type</dt>
              <dd>
                {data.task.question_type}
                <span className="text-graphite text-micro ml-2">analysis only</span>
              </dd>
              <dt className="text-graphite">Supporting sections</dt>
              <dd className="font-mono text-small">{data.task.sections.join(", ")}</dd>
              <dt className="text-graphite">Split</dt>
              <dd>{data.task.split}</dd>
            </dl>
            <p className="text-micro text-graphite mb-5 max-w-prose">{data.note}</p>

            {data.calls.map((call, index) => (
              <article key={index} className="rule-t py-4" data-testid={`trace-call-${call.pipeline}-${call.tier}`}>
                <header className="flex items-baseline justify-between gap-3 flex-wrap">
                  <h3 className="text-base font-medium">
                    {call.pipeline} · {call.tier}
                    <span className="text-graphite font-normal ml-2 text-small">{call.model_id}</span>
                  </h3>
                  <div className="flex items-center gap-2">
                    {call.reused && <Pill>replayed from cassette</Pill>}
                    <Pill tone={call.grade.correct ? "better" : "worse"}>
                      {call.grade.correct ? "correct" : "wrong"}
                    </Pill>
                  </div>
                </header>

                <dl className="mt-2 text-small grid grid-cols-[10rem_1fr] gap-x-4 gap-y-1">
                  <dt className="text-graphite">Prompt hash</dt>
                  <dd className="font-mono text-micro">{call.prompt_hash}</dd>
                  <dt className="text-graphite">Cassette</dt>
                  <dd className="font-mono text-micro">{call.cassette_key}</dd>
                  <dt className="text-graphite">Cacheable prefix</dt>
                  <dd>
                    {call.static_prefix_chars
                      ? `${count(call.static_prefix_chars)} characters`
                      : "none — nothing in this request can be cached"}
                  </dd>
                  <dt className="text-graphite">max_tokens</dt>
                  <dd>{count(call.max_tokens)}</dd>
                  <dt className="text-graphite">Cost</dt>
                  <dd title={call.cost_formula}>
                    <Figure value={usd(call.cost_usd, 6)} kind={mark} />
                    <span className="text-graphite text-micro ml-2">
                      snapshot {call.price_snapshot_id}
                    </span>
                  </dd>
                  <dt className="text-graphite">Latency</dt>
                  <dd>
                    {ms(call.latency_ms)}
                    {call.reused && (
                      <span className="text-graphite text-micro ml-2">
                        excluded from latency statistics: this is a disk read
                      </span>
                    )}
                  </dd>
                  <dt className="text-graphite">Scorer score</dt>
                  <dd className="tabular-nums">
                    {call.scorer_score === null ? "—" : call.scorer_score.toFixed(3)}
                    {call.scorer_threshold !== null && (
                      <span className="text-graphite text-micro ml-2">
                        threshold {call.scorer_threshold.toFixed(2)}
                      </span>
                    )}
                  </dd>
                  <dt className="text-graphite">Route decision</dt>
                  <dd data-testid={`route-decision-${call.tier}`}>
                    {call.route_decision}
                    <div className="text-micro text-graphite">
                      A function of the scorer alone. It does not know the grade below.
                    </div>
                  </dd>
                </dl>

                <table className="mt-3 w-full text-small">
                  <caption className="text-left text-micro text-graphite mb-1">
                    Tokens by bucket, with the source each came from
                  </caption>
                  <thead>
                    <tr className="text-graphite text-micro">
                      <th className="text-left font-medium py-1">Bucket</th>
                      <th className="text-right font-medium py-1">Tokens</th>
                      <th className="text-left font-medium py-1 pl-4">Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(call.usage).map(([bucket, entry]) => (
                      <tr key={bucket} className="rule-t">
                        <td className="py-1 font-mono text-micro">{bucket}</td>
                        <td className="py-1 text-right tabular-nums">{tokens(entry.tokens)}</td>
                        <td className="py-1 pl-4 text-graphite text-micro">{entry.source}</td>
                      </tr>
                    ))}
                    <tr className="rule-t font-medium">
                      <td className="py-1">total</td>
                      <td className="py-1 text-right tabular-nums">
                        {tokens(call.total_input + call.total_output)}
                      </td>
                      <td className="py-1 pl-4 text-graphite text-micro">
                        {tokens(call.total_input)} in, {tokens(call.total_output)} out
                      </td>
                    </tr>
                  </tbody>
                </table>

                {Object.keys(call.scorer_features).length > 0 && (
                  <table className="mt-3 w-full text-small">
                    <caption className="text-left text-micro text-graphite mb-1">
                      Scorer features. The scorer never sees the gold answer.
                    </caption>
                    <tbody>
                      {Object.entries(call.scorer_features).map(([name, value]) => (
                        <tr key={name} className="rule-t">
                          <td className="py-1 font-mono text-micro">{name}</td>
                          <td className="py-1 text-right tabular-nums">{value}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                <details className="mt-3">
                  <summary className="text-small text-graphite cursor-pointer hover:text-ink">
                    Messages and response
                  </summary>
                  <div className="mt-2 space-y-2">
                    <div>
                      <p className="text-micro text-graphite">system (first 600 characters)</p>
                      <pre className="font-mono text-micro whitespace-pre-wrap bg-paper p-2 border border-rule max-h-48 overflow-y-auto">
                        {call.system_preview}
                      </pre>
                    </div>
                    <div>
                      <p className="text-micro text-graphite">user (first 600 characters)</p>
                      <pre className="font-mono text-micro whitespace-pre-wrap bg-paper p-2 border border-rule max-h-48 overflow-y-auto">
                        {call.user_preview}
                      </pre>
                    </div>
                    <div>
                      <p className="text-micro text-graphite">response</p>
                      <pre className="font-mono text-micro whitespace-pre-wrap bg-paper p-2 border border-rule max-h-64 overflow-y-auto">
                        {call.response}
                      </pre>
                    </div>
                    <div>
                      <p className="text-micro text-graphite">
                        raw provider usage, as returned
                      </p>
                      <pre className="font-mono text-micro whitespace-pre-wrap bg-paper p-2 border border-rule">
                        {JSON.stringify(call.raw_usage, null, 2)}
                      </pre>
                    </div>
                  </div>
                </details>

                <p className="text-micro text-graphite mt-2">
                  Graded: {call.grade.reason}
                </p>
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
