/**
 * Settings (SPEC.md 5.5).
 *
 * Providers are reported as configured or not. **Keys are never sent here** — the server
 * reports a boolean and nothing else (non-negotiable 7). Every price shows where it came from
 * and whether anyone checked it, and unverified prices are marked.
 */
import { useState } from "react";
import { deleteRunContent, useRuns, useSettings } from "../lib/api";
import { count, tokens, usd } from "../lib/format";
import type { DeleteResult } from "../lib/types";
import { Button, ErrorRow, Legend, LoadingRow, Metric, Pill, Section } from "../components/Primitives";

export default function Settings() {
  const { data, isLoading, error } = useSettings();
  if (isLoading) return <div className="px-8"><LoadingRow what="settings" /></div>;
  if (error || !data) return <div className="px-8"><ErrorRow error={error} what="settings" /></div>;

  // Its own reason, not new_experiment's: the blocker is that openrouter.ai is unreachable,
  // and the listing endpoint needs no Anthropic key.
  const syncReason =
    data.live_controls.disabled_reasons.sync_models ??
    "Syncing the model listing needs network access.";
  const deleteReason = data.live_controls.disabled_reasons.delete_content;

  return (
    <div className="px-8 pb-16 max-w-[1180px]">
      <header className="py-5">
        <h1 className="text-title font-medium tracking-tight">Settings</h1>
        <p className="text-base text-graphite mt-1 max-w-prose">
          What Tokop is configured with. API keys stay on the server and are never sent to this
          page; a provider is reported as configured or not, and nothing more.
        </p>
      </header>

      <div className="rule-t rule-b py-5 grid grid-cols-2 md:grid-cols-4 gap-6">
        <Metric label="Mode" value={data.mode} interval={data.recording.state} kind="none" />
        <Metric
          label="Record budget"
          value={data.budgets.record_budget_usd ? usd(data.budgets.record_budget_usd, 2) : "not set"}
          interval="RECORD_BUDGET_USD"
          kind="none"
        />
        <Metric
          label="Daily budget"
          value={data.budgets.daily_budget_usd ? usd(data.budgets.daily_budget_usd, 2) : "not set"}
          interval="DAILY_BUDGET_USD"
          kind="none"
        />
        <Metric
          label="Token counter"
          value={data.token_counter}
          interval="used for every estimate"
          kind="none"
        />
      </div>

      <Section title="Recording" subtitle="Which fixtures this build is running on, and why.">
        <p className="text-base max-w-prose" data-testid="recording-reason">
          {data.recording.reason}
        </p>
      </Section>

      <Section title="Providers" subtitle="Configured means a key is present in the environment.">
        <table className="w-full text-small" data-testid="settings-providers">
          <thead>
            <tr className="text-graphite text-micro">
              <th className="text-left font-medium py-1">Provider</th>
              <th className="text-left font-medium py-1">Key</th>
              <th className="text-left font-medium py-1">Enabled</th>
              <th className="text-left font-medium py-1">Usage mapping</th>
              <th className="text-left font-medium py-1">Base URL</th>
            </tr>
          </thead>
          <tbody>
            {data.providers.map((p) => (
              <tr key={p.name} className="rule-t align-baseline">
                <td className="py-1.5">
                  {p.name}
                  {p.test_only && <span className="ml-2"><Pill>test only</Pill></span>}
                </td>
                <td className="py-1.5">{p.configured ? "configured" : "not configured"}</td>
                <td className="py-1.5">
                  {p.enabled ? "yes" : "no"}
                  {p.unconfirmed_reason && (
                    <div className="text-micro text-graphite max-w-[36ch]">
                      {p.unconfirmed_reason}
                    </div>
                  )}
                </td>
                <td className="py-1.5 font-mono text-micro">{p.usage_mapping}</td>
                <td className="py-1.5 font-mono text-micro text-graphite">{p.base_url ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section
        title="Model registry"
        subtitle="Prices in dollars per million tokens, each with the page it was read from and the date."
        right={
          <span className="inline-flex flex-col items-end gap-1">
            <button
              type="button"
              disabled
              className="px-3 py-1.5 text-base border border-rule text-graphite cursor-not-allowed"
              data-testid="sync-models"
            >
              Sync models
            </button>
            <span className="text-micro text-graphite max-w-[34ch] text-right">{syncReason}</span>
          </span>
        }
      >
        <table className="w-full text-small" data-testid="settings-models">
          <thead>
            <tr className="text-graphite text-micro">
              <th className="text-left font-medium py-1">Model</th>
              <th className="text-right font-medium py-1">Input</th>
              <th className="text-right font-medium py-1">Output</th>
              <th className="text-right font-medium py-1">Cache read</th>
              <th className="text-right font-medium py-1">Min cacheable</th>
              <th className="text-left font-medium py-1 pl-3">Provenance</th>
            </tr>
          </thead>
          <tbody>
            {data.models.map((m) => (
              <tr key={m.model_id} className="rule-t align-baseline">
                <td className="py-1.5">
                  <span className="font-mono text-micro">{m.model_id}</span>
                  {m.scarce && <span className="ml-2"><Pill tone="scarce">scarce</Pill></span>}
                  {m.roles.length > 0 && (
                    <span className="ml-2 text-micro text-graphite">{m.roles.join(", ")}</span>
                  )}
                </td>
                <td className="py-1.5 text-right tabular-nums">${m.input_per_mtok}</td>
                <td className="py-1.5 text-right tabular-nums">${m.output_per_mtok}</td>
                <td className="py-1.5 text-right tabular-nums">${m.cache_read_per_mtok}</td>
                <td className="py-1.5 text-right tabular-nums">
                  {m.min_cacheable_tokens ? tokens(m.min_cacheable_tokens) : "—"}
                </td>
                <td className="py-1.5 pl-3">
                  {m.provenance.verified ? (
                    <span className="text-ink">verified {m.provenance.retrieved}</span>
                  ) : (
                    <span className="text-vermilion" data-testid={`unverified-${m.model_id}`}>
                      unverified
                    </span>
                  )}
                  <div className="text-micro text-graphite break-all max-w-[38ch]">
                    {m.provenance.source_url}
                  </div>
                  {m.provenance.note && (
                    <div className="text-micro text-graphite max-w-[38ch]">{m.provenance.note}</div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.model_listing.source && (
          <p className="text-small text-graphite mt-3">
            Model listing snapshot: {data.model_listing.source}, taken{" "}
            {data.model_listing.taken}
            {data.model_listing.age_days !== null && ` (${count(data.model_listing.age_days)} days old)`}.
            Everything from the listing loads unverified; config/prices.yaml always wins.
          </p>
        )}
      </Section>

      <Section title="Scarce models" subtitle="Spend on these is tracked separately.">
        <ul className="text-base">
          {data.scarce_models.map((m) => (
            <li key={m} className="rule-t py-2 font-mono text-small">
              {m}
            </li>
          ))}
        </ul>
      </Section>

      <Section
        title="Allowed providers per workload"
        subtitle="Enforced by the router: a run against a provider not on the list is refused."
      >
        <ul className="text-base">
          {Object.entries(data.allowed_providers).map(([workload, providers]) => (
            <li key={workload} className="rule-t py-2">
              <span className="font-mono text-small">{workload}</span>
              <span className="text-graphite ml-3">{providers.join(", ")}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section
        title="Stored prompts and outputs"
        subtitle="Deleting content for a run keeps every metric: costs, token counts, grades and scores are unaffected."
      >
        <DeleteRunContent reason={deleteReason} />
      </Section>

      <footer className="rule-t pt-4 mt-2">
        <Legend kinds={["exact", "estimated", "simulated"]} />
      </footer>
    </div>
  );
}

/**
 * Delete stored prompts and outputs for one run (SPEC.md non-negotiable 10).
 *
 * The ledger really does hold them — 1,300 calls with their full request and response — so a
 * disabled button claiming "there is nothing here to delete" was false. This one calls the
 * endpoint and reports what happened, including that every metric survived.
 */
function DeleteRunContent({ reason }: { reason?: string }) {
  const runs = useRuns();
  const [selected, setSelected] = useState("");
  const [result, setResult] = useState<DeleteResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (runs.isLoading) return <LoadingRow what="the ledger" />;
  if (!runs.data || runs.data.runs.length === 0) {
    return (
      <p className="text-small text-graphite max-w-prose">
        The ledger has no runs yet. It is rebuilt by{" "}
        <code className="font-mono">tokop build-test-fixtures</code>; there is nothing stored to
        delete until then.
      </p>
    );
  }

  const runId = selected || runs.data.runs[0].run_id;
  const run = runs.data.runs.find((r) => r.run_id === runId);

  return (
    <div data-testid="delete-content-panel">
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-small text-graphite">Run</span>
          <select
            value={runId}
            onChange={(e) => {
              setSelected(e.target.value);
              setResult(null);
            }}
            className="block w-[32rem] max-w-full border border-rule-strong px-2 py-1 bg-paper mt-0.5 font-mono text-small"
            data-testid="delete-run-select"
          >
            {runs.data.runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} — {r.stored_prompts} prompts, {r.stored_responses} responses
                {r.content_deleted ? " (already deleted)" : ""}
              </option>
            ))}
          </select>
        </label>
        <Button
          disabledReason={reason}
          testId="delete-content"
          onClick={() => {
            setBusy(true);
            setError(null);
            deleteRunContent(runId)
              .then((r) => {
                setResult(r);
                void runs.refetch();
              })
              .catch(setError)
              .finally(() => setBusy(false));
          }}
        >
          {busy ? "Deleting…" : "Delete stored content for this run"}
        </Button>
      </div>

      {run && !result && (
        <p className="text-small text-graphite mt-3">
          This run stores {count(run.stored_prompts)} prompts and {count(run.stored_responses)}{" "}
          responses across {count(run.calls)} calls, costing {usd(run.total_cost_usd, 4)}. Deleting
          them keeps every metric.
        </p>
      )}

      {error !== null && <ErrorRow error={error} what="the deletion" />}

      {result && (
        <dl
          className="text-small grid grid-cols-[16rem_1fr] gap-x-4 gap-y-1 mt-3"
          data-testid="delete-result"
        >
          <dt className="text-graphite">Calls cleared</dt>
          <dd className="tabular-nums">{count(result.calls_cleared)}</dd>
          <dt className="text-graphite">Prompts remaining</dt>
          <dd className="tabular-nums">{count(result.prompts_remaining)}</dd>
          <dt className="text-graphite">Responses remaining</dt>
          <dd className="tabular-nums">{count(result.responses_remaining)}</dd>
          <dt className="text-graphite">Cost unchanged</dt>
          <dd>{result.cost_usd_unchanged ? "yes" : "NO — this is a bug"}</dd>
          <dt className="text-graphite">Token counts unchanged</dt>
          <dd>{result.tokens_unchanged ? "yes" : "NO — this is a bug"}</dd>
        </dl>
      )}
    </div>
  );
}
