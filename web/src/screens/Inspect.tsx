/**
 * Inspect: what will this prompt cost, and what in it is waste? (SPEC.md 5.2)
 *
 * Two things the screen is careful about. Token counts here are **estimates** and say which
 * method produced them — an exact count needs a provider counting endpoint, and replay mode
 * makes no calls. And "Apply safe fixes" shows the net input delta alongside what each fix buys
 * back, because two of the fixes make the prompt longer on purpose.
 */
import { useEffect, useState } from "react";
import { inspectPrompt, useBrief, useHealth, usePipelineTemplate } from "../lib/api";
import { count, pct, tokens, usd, usdShort } from "../lib/format";
import type { InspectResult } from "../lib/types";
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

type Tab = "prompt" | "brief";

export default function Inspect() {
  const health = useHealth();
  const template = usePipelineTemplate("B0");
  const brief = useBrief();
  const [tab, setTab] = useState<Tab>("prompt");
  const [system, setSystem] = useState("");
  const [user, setUser] = useState("");
  const [toolsJson, setToolsJson] = useState("");
  const [maxTokens, setMaxTokens] = useState(2000);
  const [cacheAfterSystem, setCacheAfterSystem] = useState(false);
  const [result, setResult] = useState<InspectResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function run(applyFixes: boolean) {
    setBusy(true);
    setError(null);
    try {
      setResult(
        await inspectPrompt({
          system,
          user,
          tools: toolsJson,
          max_tokens: maxTokens,
          cache_after_system: cacheAfterSystem,
          apply_fixes: applyFixes,
        }),
      );
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  function loadB0() {
    if (!template.data) return;
    setSystem(template.data.system);
    setUser(template.data.user);
    setMaxTokens(template.data.max_tokens);
    setCacheAfterSystem(template.data.cache_after_system);
  }

  // Load B0 and inspect it as soon as the template arrives. Opening on "paste a prompt and
  // press Inspect" when a prompt is already loaded is a dead end.
  useEffect(() => {
    if (!template.data || system) return;
    setSystem(template.data.system);
    setUser(template.data.user);
    setMaxTokens(template.data.max_tokens);
    setCacheAfterSystem(template.data.cache_after_system);
    void inspectPrompt({
      system: template.data.system,
      user: template.data.user,
      tools: "",
      max_tokens: template.data.max_tokens,
      cache_after_system: template.data.cache_after_system,
      apply_fixes: false,
    })
      .then(setResult)
      .catch(setError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [template.data]);

  const liveReason = result?.live_controls.disabled_reasons.rewrite ?? health.data?.live_controls.disabled_reasons.rewrite;
  const fixes = result?.fixes;

  return (
    <div className="px-8 pb-16 max-w-[1180px]">
      <header className="py-5">
        <h1 className="text-title font-medium tracking-tight">Inspect</h1>
        <p className="text-base text-graphite mt-1 max-w-prose">
          What a prompt costs before you send it, and what in it is waste. Variables stay as{" "}
          <code className="font-mono text-small">{"{{name}}"}</code>: the lint has to see the
          template, because a rendered timestamp and a variable are different findings.
        </p>
        <nav className="mt-4 flex gap-5 text-base" role="tablist">
          {(["prompt", "brief"] as Tab[]).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={tab === t ? "text-ink font-medium" : "text-graphite hover:text-ink"}
              data-testid={`tab-${t}`}
            >
              {t === "prompt" ? "Prompt" : "Brief"}
            </button>
          ))}
        </nav>
      </header>

      {tab === "prompt" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 rule-t pt-6">
          <div>
            <label className="block text-small text-graphite mb-1" htmlFor="system">
              System prompt — separate blocks with a line containing only <code>---</code>
            </label>
            <textarea
              id="system"
              value={system}
              onChange={(e) => setSystem(e.target.value)}
              rows={16}
              className="w-full font-mono text-micro border border-rule-strong p-2 bg-chalk"
              data-testid="system-input"
            />
            <label className="block text-small text-graphite mb-1 mt-4" htmlFor="user">
              User message template
            </label>
            <textarea
              id="user"
              value={user}
              onChange={(e) => setUser(e.target.value)}
              rows={5}
              className="w-full font-mono text-micro border border-rule-strong p-2 bg-chalk"
              data-testid="user-input"
            />
            <label className="block text-small text-graphite mb-1 mt-4" htmlFor="tools">
              Tool definitions (JSON array, optional)
            </label>
            <textarea
              id="tools"
              value={toolsJson}
              onChange={(e) => setToolsJson(e.target.value)}
              rows={3}
              className="w-full font-mono text-micro border border-rule-strong p-2 bg-chalk"
              data-testid="tools-input"
            />
            <div className="flex flex-wrap items-center gap-5 mt-3 text-small">
              <label className="flex items-center gap-2">
                max_tokens
                <input
                  type="number"
                  value={maxTokens}
                  min={1}
                  onChange={(e) => setMaxTokens(Number(e.target.value) || 1)}
                  className="w-24 border border-rule-strong px-1.5 py-0.5 bg-chalk tabular-nums"
                  data-testid="max-tokens-input"
                />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={cacheAfterSystem}
                  onChange={(e) => setCacheAfterSystem(e.target.checked)}
                  className="accent-prussian"
                  data-testid="cache-breakpoint-input"
                />
                Cache breakpoint after the last system block
              </label>
            </div>
            <div className="flex flex-wrap items-start gap-3 mt-4">
              <Button variant="primary" onClick={() => void run(false)} testId="inspect-run">
                {busy ? "Inspecting…" : "Inspect"}
              </Button>
              <Button onClick={() => void run(true)} testId="apply-fixes">
                Apply safe fixes
              </Button>
              <Button onClick={loadB0} testId="load-b0">
                Load the demo's B0 prompt
              </Button>
              <Button disabledReason={liveReason} testId="rewrite-with-model">
                Rewrite with a model
              </Button>
            </div>
          </div>

          <div>
            {error !== null && <ErrorRow error={error} what="the inspection" />}
            {!result && error === null && (
              <LoadingRow what="the demo's B0 prompt" />
            )}
            {result && (
              <>
                <div className="grid grid-cols-3 gap-5 mb-5">
                  <Metric
                    label="CLEAR score"
                    value={`${result.clear_total}/25`}
                    interval={Object.entries(result.clear)
                      .map(([letter, score]) => `${letter} ${score}`)
                      .join(" · ")}
                    kind="estimated"
                  />
                  <Metric
                    label="Cacheable prefix"
                    value={
                      result.cacheability.has_breakpoint
                        ? count(result.cacheability.static_prefix_chars) + " ch"
                        : "none"
                    }
                    interval={
                      result.cacheability.has_breakpoint
                        ? "up to the breakpoint"
                        : "no breakpoint is set"
                    }
                    kind="estimated"
                  />
                  <Metric
                    label="Findings"
                    value={count(result.findings.length)}
                    interval={result.findings
                      .slice(0, 4)
                      .map((f) => f.id)
                      .join(" ")}
                    kind="estimated"
                  />
                </div>

                <h3 className="text-base font-medium mb-1">Findings</h3>
                <ol data-testid="inspect-findings" className="mb-6">
                  {result.findings.map((f) => (
                    <li key={f.id} className="rule-t py-2">
                      <div className="flex items-baseline justify-between gap-3">
                        <span>
                          <span className="font-mono text-small text-graphite mr-2">{f.id}</span>
                          {f.title}
                        </span>
                        <span className="text-small text-graphite tabular-nums whitespace-nowrap" title={f.formula}>
                          {Number(f.projected_usd_per_1k) > 0
                            ? `${usdShort(f.projected_usd_per_1k)}/1k`
                            : "quality"}
                        </span>
                      </div>
                      <p className="text-small text-graphite mt-0.5">{f.evidence}</p>
                    </li>
                  ))}
                </ol>

                <h3 className="text-base font-medium mb-1">Cost per 1,000 calls</h3>
                <p className="text-small text-graphite mb-2">
                  Input plus the full <code className="font-mono">max_tokens</code>, cheapest
                  first. Token counts are estimated: {result.token_counter}.
                </p>
                <table className="w-full text-small" data-testid="inspect-models">
                  <thead>
                    <tr className="text-graphite text-micro">
                      <th className="text-left font-medium py-1">Model</th>
                      <th className="text-right font-medium py-1">Input</th>
                      <th className="text-right font-medium py-1">Overhead</th>
                      <th className="text-right font-medium py-1">Per 1,000 calls</th>
                      <th className="text-left font-medium py-1 pl-3">Cacheable?</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.models.map((m) => (
                      <tr key={m.model_id} className="rule-t">
                        <td className="py-1.5">
                          <span className="font-mono text-micro">{m.model_id}</span>
                          {m.scarce && <span className="ml-2"><Pill tone="scarce">scarce</Pill></span>}
                          {!m.price_verified && (
                            <span className="ml-2 text-micro text-graphite" title={m.price_source}>
                              price unverified
                            </span>
                          )}
                        </td>
                        <td className="py-1.5 text-right tabular-nums" title={m.token_method}>
                          <Figure value={tokens(m.input_tokens)} kind="estimated" />
                        </td>
                        <td className="py-1.5 text-right tabular-nums text-graphite">
                          {tokens(m.fixed_overhead_tokens)}
                        </td>
                        <td className="py-1.5 text-right tabular-nums">
                          {usd(m.cost_per_1k_usd, 2)}
                        </td>
                        <td className="py-1.5 pl-3 text-micro text-graphite">
                          {m.cacheable_prefix_tokens === 0
                            ? "no prefix"
                            : m.clears_minimum
                              ? `yes — ${tokens(m.cacheable_prefix_tokens)} clears ${tokens(m.min_cacheable_tokens)}`
                              : `no — ${tokens(m.cacheable_prefix_tokens)} is under ${tokens(m.min_cacheable_tokens)}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                {fixes && (
                  <div className="mt-6" data-testid="fixes-diff">
                    <h3 className="text-base font-medium mb-1">Safe fixes applied</h3>
                    <p className="text-small text-graphite mb-3 max-w-prose">{fixes.note}</p>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                      <Metric
                        label="Net input delta"
                        value={
                          <span data-testid="net-input-delta">
                            {fixes.input_delta >= 0 ? "+" : ""}
                            {count(fixes.input_delta)} tok
                          </span>
                        }
                        interval={`${count(fixes.input_tokens_before)} → ${count(fixes.input_tokens_after)}`}
                        kind="estimated"
                      />
                      <Metric
                        label="Now cacheable"
                        value={tokens(fixes.cacheable_after)}
                        interval={`was ${tokens(fixes.cacheable_before)}`}
                        kind="estimated"
                      />
                      <Metric
                        label="Output cap"
                        value={count(fixes.max_tokens_after)}
                        interval={`was ${count(fixes.max_tokens_before)}`}
                        kind="estimated"
                      />
                      <Metric
                        label="Net saving per 1,000 calls"
                        value={usd(fixes.savings_per_1k.net_usd, 2)}
                        interval={`cache ${usd(fixes.savings_per_1k.cache_usd, 2)} · output up to ${usd(fixes.savings_per_1k.output_usd_upper_bound, 2)}`}
                        kind="projected"
                      />
                    </div>
                    <p className="text-small text-graphite mb-3">
                      CLEAR {result.clear_total}/25 → {fixes.clear_total_after}/25. Remaining
                      findings: {fixes.findings_after.join(", ") || "none"}.
                    </p>
                    <ol>
                      {fixes.applied.map((step) => (
                        <li key={step.transform} className="rule-t py-2">
                          <div className="flex items-baseline justify-between gap-3">
                            <span>{step.title}</span>
                            <span className="text-small tabular-nums text-graphite">
                              {step.input_delta >= 0 ? "+" : ""}
                              {count(step.input_delta)} tok
                            </span>
                          </div>
                          {step.changes.map((c, i) => (
                            <div key={i} className="mt-1 font-mono text-micro">
                              {c.before && (
                                <div className="text-vermilion">− {c.before.slice(0, 150)}</div>
                              )}
                              {c.after && (
                                <div className="text-verdigris">+ {c.after.slice(0, 150)}</div>
                              )}
                              <div className="text-graphite">{c.note}</div>
                            </div>
                          ))}
                        </li>
                      ))}
                    </ol>
                    {fixes.skipped.length > 0 && (
                      <details className="mt-3">
                        <summary className="text-small text-graphite cursor-pointer">
                          {fixes.skipped.length} fix(es) did not apply
                        </summary>
                        <ul className="mt-1">
                          {fixes.skipped.map((s) => (
                            <li key={s.transform} className="text-small text-graphite py-0.5">
                              {s.title}: {s.reason}
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {tab === "brief" && (
        <div className="rule-t pt-6">
          {brief.isLoading && <LoadingRow what="the recorded brief" />}
          {brief.error && <ErrorRow error={brief.error} what="the brief" />}
          {brief.data && (
            <div data-testid="brief-panel">
              <Section
                title="Brief"
                subtitle={brief.data.purpose_note}
                right={<Pill>{brief.data.is_test_data ? "recorded, simulated" : "recorded"}</Pill>}
              >
                <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-5">
                  <Metric
                    label="Tokens before"
                    value={<span data-testid="brief-before">{tokens(brief.data.tokens_before)}</span>}
                    interval={brief.data.token_counter}
                    kind="estimated"
                  />
                  <Metric
                    label="Tokens after"
                    value={<span data-testid="brief-after">{tokens(brief.data.tokens_after)}</span>}
                    interval={`${pct(brief.data.reduction, 0)} smaller`}
                    kind="estimated"
                  />
                  <Metric
                    label="Cost of the brief"
                    value={usd(brief.data.cost_usd, 5)}
                    interval={brief.data.display_name}
                    kind={brief.data.is_test_data ? "simulated" : "provider"}
                  />
                  <Metric
                    label="Source"
                    value={`${brief.data.source_sections.length} sections`}
                    interval={`${count(brief.data.source_chars)} characters`}
                    kind="estimated"
                  />
                </div>
                <p className="text-small text-graphite border-l-2 border-vermilion pl-3 mb-5 max-w-prose">
                  <strong className="text-vermilion font-medium">Lossy.</strong>{" "}
                  {brief.data.lossy_note}
                </p>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  <div>
                    <h3 className="text-base font-medium mb-1">Source</h3>
                    <pre className="font-mono text-micro whitespace-pre-wrap bg-chalk border border-rule p-3 max-h-[420px] overflow-y-auto">
                      {brief.data.source_preview}
                    </pre>
                  </div>
                  <div>
                    <h3 className="text-base font-medium mb-1">Brief</h3>
                    <pre className="font-mono text-micro whitespace-pre-wrap bg-chalk border border-rule p-3 max-h-[420px] overflow-y-auto" data-testid="brief-text">
                      {brief.data.brief}
                    </pre>
                  </div>
                </div>
              </Section>
            </div>
          )}
        </div>
      )}

      <footer className="rule-t pt-4 mt-6">
        <Legend kinds={["exact", "estimated", "projected", "simulated"]} />
      </footer>
    </div>
  );
}
