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
  share_of_tasks: number;
  auroc: number | null;
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
