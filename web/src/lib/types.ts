/**
 * The shape of `/api/report`.
 *
 * `api-types.ts` is generated from FastAPI's OpenAPI schema and covers the route surface. The
 * report body is assembled as a plain object by `optimize/report.py`, so FastAPI advertises it
 * as `object` and the generated types stop at the door. These interfaces describe what is
 * actually inside it. They are hand-written on purpose and `tokop report --check` is what keeps
 * them honest: every field below is asserted by the engine tests and the e2e suite.
 */

/** How a number came to be. Defined once here and rendered once in the legend. */
export type Provenance = "provider" | "exact" | "estimated" | "projected" | "simulated";

export interface Interval {
  point: number;
  low: number;
  high: number;
  level: number;
  n: number;
  method: string;
  dropped_resamples: number;
}

export interface ArmView {
  pipeline: string;
  label: string;
  n: number;
  accuracy: Interval;
  cost_per_successful_task: Interval;
  total_cost_usd: string;
  cost_per_task_usd: string;
  scarce_share: number;
  model_ids: string[];
  origin: string;
  tier_shares?: Record<string, number>;
}

export interface VerdictView {
  label: "non_inferior" | "inconclusive" | "worse";
  display: string;
  margin: number;
  additional_tasks_needed: number | null;
  note: string;
  sentence: string;
}

export interface ProofCostView {
  baseline_run_usd: string;
  calibration_runs_usd: string;
  candidate_run_usd: string;
  other_pipelines_usd: string;
  scorer_and_judge_usd: string;
  prewarming_usd: string;
  total_usd: string;
}

export interface TypeBreakdownView {
  question_type: string;
  n: number;
  baseline_accuracy: number;
  candidate_accuracy: number;
  candidate_tier_shares: Record<string, number>;
  baseline_cost_per_success_usd: string | null;
  candidate_cost_per_success_usd: string | null;
}

export interface DisagreementView {
  task_id: string;
  question: string;
  gold: string;
  question_type: string;
  baseline_correct: boolean;
  candidate_correct: boolean;
  candidate_tier: string;
  baseline_cost_usd: string;
  candidate_cost_usd: string;
}

export interface ProofView {
  baseline: ArmView;
  candidate: ArmView;
  delta_accuracy: Interval;
  cost_ratio: Interval;
  cost_reduction: number | null;
  verdict: VerdictView;
  mcnemar: {
    baseline_only: number;
    candidate_only: number;
    concordant: number;
    discordant: number;
    p_value: number;
  };
  proof_cost: ProofCostView;
  repayment_tasks: number | null;
  savings_per_task_usd: string;
  split_sizes: { calibration: number; test: number };
  by_type: TypeBreakdownView[];
  disagreements: DisagreementView[];
  scorer_auroc: Record<string, number | null>;
  operating_point_note: string;
}

export interface FindingView {
  id: string;
  group: string;
  title: string;
  detail: string;
  evidence: string;
  projected_usd_per_1k: string;
  weighted_usd_per_1k: string;
  formula: string;
  confidence: "measured" | "projected" | "heuristic";
  transform: string | null;
  spans: { section: string; block: number; start: number; end: number; text: string }[];
}

export interface WaterfallStepView {
  pipeline: string;
  label: string;
  n: number;
  accuracy: Interval;
  cost_per_successful_task: Interval;
  total_cost_usd: string;
  cost_per_task_usd: string;
  scarce_share: number;
  verdict: { label: string; display: string } | null;
}

export interface CascadeTierView {
  tier: string;
  model_id: string;
  scarce: boolean;
  threshold: number;
  /** Share of tasks that *stopped* here. */
  share_of_tasks: number;
  /** Share of tasks that *reached* here at all — every task reaches the first tier. */
  share_of_attempts: number;
  /** Share of the candidate's spend. This is what a cost-weighted graph node needs. */
  share_of_cost: number;
  cost_usd: string;
  attempts: number;
  auroc: number | null;
  p50_latency_ms: number | null;
  latency_is_recorded: boolean;
}

export interface FrontierPointView {
  thresholds: number[];
  accuracy: number;
  cost_per_task_usd: string;
  scarce_share: number;
  is_operating_point: boolean;
}

export interface CascadeView {
  thresholds: number[];
  objective: string;
  accuracy_floor: number;
  evaluated: number;
  feasible: number;
  runtime_seconds: number;
  accuracy: number;
  total_cost_usd: string;
  cost_per_task_usd: string | null;
  scarce_share: number;
  note: string;
  tiers: CascadeTierView[];
  reached_frontier_share: number;
  scorers: {
    tiers: Record<
      string,
      {
        tier: string;
        features: string[];
        fitted_on: number;
        auroc: number | null;
        degenerate: boolean;
        coefficients: Record<string, number>;
      }
    >;
    warnings: { tier: string; message: string }[];
  };
  frontier_chart: FrontierPointView[];
  pareto: { thresholds: number[]; accuracy: number; cost_per_task_usd: string }[];
}

export interface RunView {
  pipeline: string;
  split: string;
  tier: string;
  model_id: string;
  n: number;
  accuracy: number;
  total_cost_usd: string;
  prewarm_cost_usd: string;
  cache_read_tokens: number;
  cache_write_tokens: number;
  input_tokens: number;
  output_tokens: number;
  calls: number;
  prewarm_calls: number;
  p50_latency_ms: number | null;
  recorded_latency_p50_ms: number | null;
  origin: string;
}

export interface PipelineView {
  name: string;
  description: string;
  model_role: string;
  max_tokens: number;
  output_contract: string;
  notes: string[];
  known_antipatterns: Record<string, string>;
}

export interface ProvenanceView {
  fixture_source: "demo" | "test";
  is_test_data: boolean;
  recording_state: string;
  recording_reason: string;
  origin: string;
  recorded_at: string | null;
  note: string;
  model_ids: Record<string, string>;
  price_snapshot_id: string;
  prices_verified: boolean;
  unverified_models: string[];
  base_token_counter: string;
  git_sha: string;
}

export interface LiveControls {
  enabled: boolean;
  disabled_reasons: Record<string, string>;
  implemented?: string[];
}

export interface Report {
  generated_by: string;
  workload: {
    id: string;
    name: string;
    description: string;
    margin: number;
    latency_sensitive: boolean;
    dataset: { size: number; calibration: number; test: number; seed: number };
  };
  provenance: ProvenanceView;
  proof: ProofView;
  waterfall: WaterfallStepView[];
  cascade: CascadeView;
  findings: FindingView[];
  lint: Record<string, { findings: FindingView[]; clear: Record<string, number>; clear_total: number }>;
  pipelines: Record<string, PipelineView>;
  runs: RunView[];
  live_controls: LiveControls;
}

export interface TraceCall {
  pipeline: string;
  tier: string;
  model_id: string;
  prompt_hash: string;
  cassette_key: string;
  reused: boolean;
  system_preview: string;
  user_preview: string;
  static_prefix_chars: number;
  max_tokens: number;
  response: string;
  usage: Record<string, { tokens: number; source: string }>;
  raw_usage: Record<string, unknown>;
  total_input: number;
  total_output: number;
  cost_usd: string;
  cost_formula: string;
  price_snapshot_id: string;
  latency_ms: number;
  ttft_ms: number | null;
  scorer_features: Record<string, number>;
  scorer_score: number | null;
  scorer_threshold: number | null;
  route_decision: string;
  grade: { correct: boolean; parsed: string | null; reason: string };
  origin: string;
}

export interface Trace {
  task: {
    id: string;
    question: string;
    gold: string;
    answer_type: string;
    question_type: string;
    sections: string[];
    split: string;
  };
  calls: TraceCall[];
  note: string;
  thresholds: Record<string, number>;
}

export interface Health {
  version: string;
  mode: "live" | "replay";
  recording: {
    state: string;
    reason: string;
    fixture_source: string;
    is_test_data: boolean;
  };
  live_controls: LiveControls;
}


// --------------------------------------------------------------------------- Inspect

export interface ModelCostRow {
  model_id: string;
  display_name: string;
  provider: string;
  scarce: boolean;
  input_tokens: number;
  token_source: "exact" | "estimated";
  token_method: string;
  tool_definition_tokens: number;
  tool_use_system_prompt_tokens: number;
  fixed_overhead_tokens: number;
  cacheable_prefix_tokens: number;
  min_cacheable_tokens: number;
  clears_minimum: boolean;
  cost_per_call_usd: string;
  cost_per_1k_usd: string;
  price_verified: boolean;
  price_source: string;
  price_retrieved: string;
}

export interface FixChange {
  kind: string;
  section: string;
  block: number;
  before: string;
  after: string;
  note: string;
}

export interface FixesView {
  applied: { transform: string; title: string; input_delta: number; changes: FixChange[] }[];
  skipped: { transform: string; title: string; reason: string }[];
  input_tokens_before: number;
  input_tokens_after: number;
  input_delta: number;
  cacheable_before: number;
  cacheable_after: number;
  max_tokens_before: number;
  max_tokens_after: number;
  savings_per_1k: Record<string, string>;
  system_after: string;
  user_after: string;
  clear_after: Record<string, number>;
  clear_total_after: number;
  findings_after: string[];
  note: string;
}

export interface InspectResult {
  models: ModelCostRow[];
  findings: FindingView[];
  clear: Record<string, number>;
  clear_total: number;
  cacheability: { static_prefix_chars: number; has_breakpoint: boolean; note: string };
  token_counter: string;
  live_controls: LiveControls;
  fixes?: FixesView;
}

export interface PipelineTemplate {
  id: string;
  name: string;
  system: string;
  user: string;
  max_tokens: number;
  cache_after_system: boolean;
  variables: string[];
}

// --------------------------------------------------------------------------- Compare

export interface CompareColumn {
  model_id: string;
  display_name: string;
  scarce: boolean;
  text: string;
  usage: Record<string, { tokens: number; source: string }>;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string;
  cost_formula: string;
  ttft_ms: number | null;
  latency_ms: number;
  origin: string;
}

export interface CompareExample {
  id: string;
  title: string;
  prompt: string;
  system: string;
  columns: CompareColumn[];
  synthesis: {
    agreement: string[];
    disagreement: string[];
    unique: string[];
    likely_errors: string[];
    final: string;
  } | null;
  synthesis_model: string | null;
  manual_column: { note: string; chars_per_token: number; token_counter: string };
}

export interface CompareExamples {
  examples: CompareExample[];
  is_test_data: boolean;
  live_controls: LiveControls;
  note: string;
}

export interface BriefResult {
  model_id: string;
  display_name: string;
  source_sections: string[];
  source_preview: string;
  source_chars: number;
  brief: string;
  tokens_before: number;
  tokens_after: number;
  reduction: number;
  cost_usd: string;
  cost_formula: string;
  token_counter: string;
  lossy_note: string;
  purpose_note: string;
  origin: string;
  is_test_data: boolean;
  live_controls: LiveControls;
}

// --------------------------------------------------------------------------- Spend / Settings

export interface SpendView {
  total_usd: string;
  scarce_usd: string;
  scarce_share: number;
  by_model: { model_id: string; usd: string; scarce: boolean; share: number }[];
  by_provider: { provider: string; usd: string; share: number }[];
  by_pipeline: { pipeline: string; usd: string }[];
  by_workload: { workload: string; usd: string; runs: number }[];
  by_day: { day: string; usd: string }[];
  runs: RunView[];
  budgets: {
    daily_cap_usd: string | null;
    daily_spent_usd: string;
    daily_remaining_usd: string | null;
    fraction: number | null;
    state: string;
    warning_at: number;
    note: string;
  };
  cost_per_successful_task: {
    pipeline: string;
    label: string;
    usd: number;
    low: number;
    high: number;
    day: string;
  }[];
  provenance: ProvenanceView;
  note: string;
  live_controls: LiveControls;
}

export interface SettingsView {
  mode: string;
  providers: {
    name: string;
    configured: boolean;
    enabled: boolean;
    adapter: string;
    base_url: string | null;
    usage_mapping: string;
    unconfirmed_reason: string | null;
    test_only: boolean;
    docs: string | null;
  }[];
  models: {
    model_id: string;
    display_name: string;
    provider: string;
    input_per_mtok: string;
    output_per_mtok: string;
    cache_read_per_mtok: string;
    cache_write_5m_per_mtok: string;
    batch_discount: string;
    min_cacheable_tokens: number | null;
    context_tokens: number | null;
    tokenizer_generation: string | null;
    scarce: boolean;
    roles: string[];
    provenance: {
      source_url: string;
      retrieved: string;
      verified: boolean;
      note: string | null;
    };
  }[];
  scarce_models: string[];
  budgets: { record_budget_usd: string | null; daily_budget_usd: string | null };
  allowed_providers: Record<string, string[]>;
  model_listing: { source: string | null; taken: string | null; age_days: number | null };
  token_counter: string;
  recording: { state: string; reason: string; fixture_source: string; is_test_data: boolean };
  live_controls: LiveControls;
}

export interface LedgerRun {
  run_id: string;
  pipeline: string;
  split: string;
  model_ids: string[];
  calls: number;
  total_cost_usd: string;
  content_deleted: boolean;
  stored_prompts: number;
  stored_responses: number;
}

export interface RunsView {
  runs: LedgerRun[];
  ledger: string | null;
}

export interface DeleteResult {
  run_id: string;
  calls_cleared: number;
  prompts_remaining: number;
  responses_remaining: number;
  cost_usd_unchanged: boolean;
  tokens_unchanged: boolean;
  note: string;
}
