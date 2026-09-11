/**
 * Settings (SPEC.md 5.5).
 *
 * Providers are reported as configured or not. **Keys are never sent here** — the server
 * reports a boolean and nothing else (non-negotiable 7). Every price shows where it came from
 * and whether anyone checked it, and unverified prices are marked.
 */
import { useSettings } from "../lib/api";
import { count, tokens, usd } from "../lib/format";
import { ErrorRow, Legend, LoadingRow, Metric, Pill, Section } from "../components/Primitives";

export default function Settings() {
  const { data, isLoading, error } = useSettings();
  if (isLoading) return <div className="px-8"><LoadingRow what="settings" /></div>;
  if (error || !data) return <div className="px-8"><ErrorRow error={error} what="settings" /></div>;

  const syncReason =
    data.live_controls.disabled_reasons.new_experiment ??
    "Syncing the model listing needs network access.";

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
        <Metric label="Mode" value={data.mode} interval={data.recording.state} kind="estimated" />
        <Metric
          label="Record budget"
          value={data.budgets.record_budget_usd ? usd(data.budgets.record_budget_usd, 2) : "not set"}
          interval="RECORD_BUDGET_USD"
          kind="estimated"
        />
        <Metric
          label="Daily budget"
          value={data.budgets.daily_budget_usd ? usd(data.budgets.daily_budget_usd, 2) : "not set"}
          interval="DAILY_BUDGET_USD"
          kind="estimated"
        />
        <Metric
          label="Token counter"
          value={data.token_counter}
          interval="used for every estimate"
          kind="estimated"
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
        <span className="inline-flex flex-col items-start gap-1">
          <button
            type="button"
            disabled
            className="px-3 py-1.5 text-base border border-rule text-graphite cursor-not-allowed"
            data-testid="delete-content"
          >
            Delete stored content for a run
          </button>
          <span className="text-micro text-graphite max-w-[48ch]">
            This build runs on committed fixtures rather than a writable ledger, so there is
            nothing here to delete. The engine supports it: `delete_run_content` drops the stored
            request and response for a run and leaves the numbers intact.
          </span>
        </span>
      </Section>

      <footer className="rule-t pt-4 mt-2">
        <Legend kinds={["exact", "estimated", "simulated"]} />
      </footer>
    </div>
  );
}
