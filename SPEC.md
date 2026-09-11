# Tokop: build spec

Working name: **Tokop**. Rename freely; the name appears only in the UI header, the CLI, and the README.

Tokop finds the cheapest way to run an AI workload that stays within a stated quality margin of what you run today, and it attaches the statistical proof.

This file is the source of truth for the build. It is written to be executed by Claude Code in one long autonomous session.

## 0. Launch sequence (you run this; Claude Code skips it)

1. Put this file at the root of an empty git repository.
2. Create `.env` in the same folder:

```
ANTHROPIC_API_KEY=            # required for a real demo recording
OPENROUTER_API_KEY=           # optional: adds GPT, Gemini, DeepSeek, Qwen, Kimi, GLM and Sonar to Compare
RECORD_BUDGET_USD=30          # hard cap for `make record`; setting it is your consent to spend up to this amount
DAILY_BUDGET_USD=5            # hard cap for live actions inside the app
TOKOP_MODE=live              # live or replay
```

   To audit the demo dataset before any money is spent, leave `RECORD_BUDGET_USD` empty. The build then stops short of recording, and you run `make record` yourself after reading docs/DATASET.md.

3. Run `claude --permission-mode plan`, choose Opus 5 with `/model`, and send:
   `Read SPEC.md end to end and propose your implementation plan: milestones M0 to M7 mapped to files and interfaces, the risks you see, and every ambiguity in the spec with the reversible choice you would make.`
4. Read the plan (Ctrl+G opens it in your editor). Approving it exits plan mode. Check that the mode indicator shows auto mode (Shift+Tab cycles modes).
5. Send:
   `/goal Tokop is built as specified in SPEC.md: in this conversation, the most recent run of make verify printed VERIFY PASSED after the last code change, the most recent printout of PROGRESS.md shows M0 through M7 done, and the most recent spec-review subagent report lists no open requirement gaps. Stop after 60 turns.`
6. If you hit a usage limit, run `claude --continue` later. If the goal is no longer active, send the same `/goal` line again. The repository carries the state.

Expected API spend for the demo recording: roughly $15 to $25 at September 2026 Anthropic prices. The recorder prints its projection before spending and stops if the projection exceeds your cap.

## 1. How to work

You will build a demo-ready MVP in one long session, and context compaction will drop details along the way. Treat the repository as your memory.

- Your first actions after the plan is approved: save it as PLAN.md, then write CLAUDE.md in under 60 lines containing the commands, the conventions, the non-negotiables from section 9, and this compaction note: "When compacting, preserve the current milestone, failing checks, files changed since the last commit, and open decisions."
- Work one milestone at a time. A milestone closes when its acceptance check in section 11 passes, `make verify` is green for everything built so far, the work is committed as `M<n>: <summary>`, and PROGRESS.md is updated (done, next, known issues, open decisions). Print PROGRESS.md in the conversation after each update.
- At the start of each milestone, re-read that milestone's sections of this file. After a compaction or restart, read CLAUDE.md, PROGRESS.md, `git log --oneline -15`, and the current milestone's sections before editing code.
- When this spec is silent, choose the option that is easiest to reverse, record it in DECISIONS.md with one line of rationale, and keep going. Ask me only if you are blocked by missing credentials.
- When the spec conflicts with what you find (an API behaves differently, a library or SDK method no longer exists, a fact in Appendix B has changed), reality wins. Record the conflict in DECISIONS.md and take the conservative option. If a feature would need anything section 9 forbids, drop the feature and log it.
- Delegate side quests to subagents when you need the conclusion and not the details: reading provider pricing pages; confirming API field names, SDK method names, and current library versions; reviewing a milestone's diff against this spec. Implementation stays in the main session.
- Engineering rules. Make the smallest change that does the job. Raise errors explicitly: no silent fallbacks, no swallowed exceptions. Never describe something as working unless you ran it, and show the command and its output. End every milestone by listing what changed and anything you are unsure about.
- Money. Live API calls happen only inside `make record` (capped by RECORD_BUDGET_USD) and in user-initiated live actions (capped by DAILY_BUDGET_USD). Tests never call live APIs. Never change a budget value yourself.

## 2. The product

Teams and heavy individual users overspend on frontier models because cutting cost feels risky: nobody can say whether the cheaper setup is as good. Tokop turns that question into a measurement. Given a workload (tasks plus a way to grade them) and the pipeline in use today, it:

1. records what the current pipeline really costs, per task and per pipeline step;
2. finds waste and ranks it by projected dollars;
3. builds a candidate pipeline: cache-friendly prompt order, a tightened prompt with an output contract, and a model cascade that sends each task to the cheapest model that can be trusted with it;
4. proves the candidate on held-out tasks with cost per successful task, confidence intervals, and a non-inferiority verdict against the stated margin.

Cost per successful task is the headline metric. Token counts are supporting evidence. The proof has a price of its own (the baseline run, the calibration runs, any judge calls), so Tokop reports it next to the savings, along with the task volume at which the savings repay it.

Origin story for the README: the author used to run one prompt through several AI apps by hand, paste the answers back into Claude to compare them, and hit Claude usage limits doing it. That habit is a model cascade with a human as the scorer. Tokop automates the scorer and adds statistics.

## 3. Scope

Build in v1:
- The engine (Python): provider adapters with record, replay, and exact-request reuse; usage normalization; a model and price registry; a spend guard; the workload runner; findings; transforms; scorers; the cascade; proof statistics, including the active-evaluation estimator; and a CLI.
- Four screens (Optimize, Inspect, Compare, Spend) plus Settings. Inspect includes a Brief mode.
- One recorded demo workload (section 6), and the tooling to record any YAML-defined workload whose JSONL or CSV dataset has gold answers.
- Replay mode, which runs the entire app from recordings with no API keys. The public demo and the test suite both use it.

Leave out of v1 entirely: authentication and multiple users; billing; a visual graph editor (graphs are views); browser automation of consumer AI apps; reuse of subscription credentials or session cookies; account pooling; rate-limit evasion of any kind; any proxy or gateway for other applications' traffic; SDK packages; an MCP client or gateway; accounting for image, video, or audio generation (reserve schema fields only); fine-tuning; semantic caching; marketing pages beyond the README.

The integration limits have a reason. Anthropic's terms restrict OAuth tokens from Free, Pro, and Max plans to Claude Code and Claude.ai, and third-party products must use API keys. Other vendors' consumer terms restrict automated access in similar ways. Official APIs only.

Consumer apps without a public API (NotebookLM, Liner, Poe's web app, Lovable, Base44, Replit and others) appear only as manual columns in Compare (section 5.3). Agent runtimes such as Goose and OpenClaw, and frameworks such as LangGraph, are candidates for trace import later (section 13).

## 4. Architecture and stack

```
tokop/
  engine/                 Python package `tokop`, managed with uv
    adapters/             anthropic.py, openai_compatible.py, cassette.py
    core/                 usage.py, pricing.py, stats.py, tokenize.py, budget.py
    workloads/            spec.py (YAML schema), runner.py, grading.py (checkers), demo/ (generator)
    optimize/             lint.py, findings.py, transforms.py, scorers.py, cascade.py, proof.py, report.py
    api/                  FastAPI app: /api/* plus the built SPA
    cli.py                Typer CLI: record, prove, report, lint, fixtures-check, sync-models
  web/                    Vite + React + TypeScript SPA
  config/                 providers.yaml, models.yaml, prices.yaml (overrides)
  data/demo/              policy.yaml and question templates
  fixtures/demo/          recorded cassettes and run manifests (committed)
  fixtures/test/          small hand-built cassettes and a model-listing snapshot, used only by tests
  docs/                   ARCHITECTURE.md, DATASET.md, DEMO.md, DESIGN.md, screenshots/
  Makefile  Dockerfile  .env.example  README.md
```

- Engine: Python 3.12, uv, FastAPI, Pydantic v2, SQLAlchemy 2 with SQLite for the local ledger, httpx, the official `anthropic` and `openai` SDKs, tiktoken, numpy, scipy, scikit-learn, Typer, pytest, ruff, mypy (strict on `core/` and `optimize/`).
- Web: Vite, React, TypeScript in strict mode, Tailwind, shadcn/ui, @xyflow/react, Recharts, TanStack Query, pnpm. Generate API types from FastAPI's OpenAPI schema with openapi-typescript. Playwright for end-to-end tests, ESLint.
- Production is one process: FastAPI serves the API and the built SPA. In development, `make dev` runs uvicorn and Vite, with Vite proxying `/api`. `make demo` builds the SPA and serves the app in replay mode on port 8000.
- Long runs execute as in-process asyncio tasks with a semaphore per provider. Run status lives in SQLite; the UI polls it.
- SQLite tables (refine them in PLAN.md): workloads, runs (one per pipeline and split), calls (raw usage JSON next to the normalized buckets, provenance, cost, price snapshot ID, prompt hash, reuse status, tier, decision), grades, scores (rater and sampling probability), price_snapshots, compare_sessions, preferences, briefs.
- Why this stack: the owner is a Python and statistics researcher who will extend the proof engine and write up results from the same code; a static SPA keeps production to one container; SQLite fits a single-user local ledger.
- Write both adapters yourself. Don't route calls through LiteLLM or another gateway library. Usage normalization is the product, and every line of it must be auditable.

## 5. Screens

Each screen answers one question. Navigation: Optimize, Inspect, Compare, Spend, then Settings. The default route is Optimize with the demo workload loaded. Every aggregate number opens the tasks and calls behind it, and every metric has a hover that shows how it was computed and where the number came from. From a fresh launch, the demo's verdict should be five clicks away or fewer.

### 5.1 Optimize: what's the cheapest way to run this workload without getting worse?

1. **Current pipeline.** A React Flow graph of the baseline. Each node shows calls, cost, share of run cost, tokens by bucket, and p50 latency; a node's visual weight scales with its cost share. The header shows cost per successful task and accuracy (each with a 95% interval) and the scarce-model share of spend.
2. **Findings.** Ranked by projected dollars saved per 1,000 tasks, weighted by confidence. Each row: rule ID, a plain-language description, the evidence (for example, "The handbook is identical across 200 calls but sits after the question, so it can't be cached"), the projected saving with its formula on hover, and the transform that addresses it.
3. **Build candidate.** Produces the candidate pipeline. The graph animates from baseline to candidate; this is the product's one orchestrated motion. New nodes appear (cheaper tiers, verifiers, routes), and each edge is labeled with the share of tasks that flow along it. A toggle, "Protect scarce models", switches the cascade objective to minimizing scarce-model spend (section 7.5).
4. **Run proof.** Results on the held-out split:
   - Headline row, above the fold: cost per successful task for B0 and B3; the accuracy difference with its 95% interval and n; the share of tasks that reached the frontier model; the split sizes; and what the proof cost (pipeline calls, scorer or judge calls, pre-warming), with the monthly task volume at which the savings repay it.
   - A plain-language verdict with its numbers filled in, in this shape: "B3 cuts cost per successful task by X% (interval a to b); accuracy difference -Y points, 95% CI [c, d], n = N, inside the 3-point margin." The verdict label is one of "Non-inferior at a 3-point margin", "Inconclusive: about N more tasks would settle it", or "Worse". Draw the D-accuracy interval against the margin as one horizontal interval plot.
   - Savings waterfall: cost per successful task for B0, B1, B2 and B3 (section 6). Each bar shows its interval, its accuracy with an interval, its scarce-model share, and its verdict against B0.
   - Cost-quality frontier: every cascade threshold setting evaluated on the test split from recorded responses, with B0, B1 and B2 marked and the operating point highlighted. A note under the chart says the operating point was chosen on the calibration split before test results were computed.
   - Breakdown by question type: accuracy per pipeline, the share of tasks resolved at each tier, and cost per successful task. The router never sees question types; this table is analysis after the fact.
   - Disagreements: tasks where B0 and B3 got different grades, each opening its trace.
5. **Trace drawer**, available for any task: every call with collapsed messages, prompt hash, tokens by bucket with provenance, reuse status, cost, latency, scorer features and score, the route decision, and the grade against gold.

In replay mode, all of this runs from fixtures, and live controls are disabled with a one-line explanation. In live mode, **New experiment** opens a form: workload, baseline model, 2 to 4 cascade tiers chosen from the registry, margin, and spend cap, with split sizes, threshold step, and scorer features behind an "Advanced" disclosure. The defaults reproduce the demo. The preflight projection appears before the run starts, the run refuses to start above the cap, and a progress view follows it.

### 5.2 Inspect: what will this prompt cost, and what in it is waste?

Inputs: a system prompt, a user message template, optional tool definitions as JSON, and variables marked as `{{name}}`.

Outputs:
- token counts per selected model, exact where a counting endpoint was used and estimated otherwise (with the method shown);
- cost per call and per 1,000 calls across the registry, sorted;
- fixed overhead per call: system prompt, tool definitions, and the provider's tool-use system prompt (Anthropic documents this per model on its pricing page);
- cacheability: static prefix length against each provider's minimum, volatile content inside the prefix, and dynamic content placed before static content;
- lint findings (section 7.4) with highlighted spans, ranked by dollars;
- **Apply safe fixes**: deterministic rewrites only, shown as a before/after diff. A fix may lengthen a prompt when it adds an output contract; the diff shows the net input delta next to the expected output-side saving;
- **Rewrite with a model**: a cheap model applies the CLEAR rubric. Before it runs, show its own cost and the number of calls after which the rewrite pays for itself.

**Brief** mode, for people whose scarce resource is a chat subscription: paste long material, and a cheap model extracts, deduplicates, and structures it into a compact brief with pointers back to the source, sized for pasting into a chat app. Show tokens before and after, the cost of making the brief, and a label saying the brief is lossy. It spends cheap API tokens to save subscription capacity and never automates a chat app. Replay mode shows one recorded brief.

### 5.3 Compare: which model is good enough for this prompt?

One prompt goes to the selected models in parallel, after a preflight that shows the projected total cost. Responses stream into side-by-side columns, each showing tokens by bucket, cost, time to first token, and total latency. This is the daily-use screen, so keep it fast.

Manual columns cover subscription apps: the column shows the exact prompt with a copy button and a link to open the app, and the user pastes the response back (tokens estimated, cost shown as "subscription").

**Synthesize** sends all responses to a chosen model, which returns a fixed structure: points of agreement, disagreements, claims only one model made, likely errors, and a final answer.

**Prefer this answer** stores a preference row for a future learned router. Nothing reads it in v1.

Replay mode shows two recorded comparisons.

### 5.4 Spend: where did the money and the scarce-model budget go?

A ledger of every call made through Tokop: spend by day, model, provider and workload; scarce-model share; cost per successful task per workload over time; and budget status (warning at 80%, hard stop at 100%). The copy says plainly that this tracks API usage made through Tokop and cannot see Claude.ai or Claude Code plan limits.

### 5.5 Settings

Which providers are configured (yes or no; keys are never displayed or sent to the browser), the model registry with price provenance (source URL, retrieval date, verified flag) and a "Sync models" action, scarce models, budgets, allowed providers per workload, the mode, and deletion of stored prompts and outputs per run.

## 6. The demo workload

Build a generator instead of writing a dataset by hand, so every gold answer is correct by construction.

- `data/demo/policy.yaml` holds the structured facts of an obviously fictional outdoor-gear retailer: return windows by product category and loyalty tier, restocking fees, a shipping fee table (zones by weight bands), free-shipping thresholds per tier, warranty terms, exceptions (final-sale items, opened electronics), escalation rules, and a seasonal return extension.
- The handbook is a deterministic Markdown rendering of that YAML (prose and tables) with stable section IDs. Size it so the cached prefix (system prompt plus handbook) clears every tier's minimum cacheable length by at least 20%, measured with count_tokens on each model. Haiku 4.5's minimum is 4,096 tokens and it counts with Anthropic's older tokenizer, so expect roughly 6,500 to 7,500 tokens on Opus 5's count.
- 300 questions come from templates with at least four phrasings each; code computes the gold answers from the YAML. Types: lookup, two-hop, computation (fees and refunds with arithmetic), and exception handling, in roughly a 45/25/20/10 mix. Each item stores its gold answer, answer type, and supporting section IDs. The router never sees the type or the section IDs.
- Split: 100 calibration and 200 test, stratified by type, with a fixed seed.
- Grading: numbers are parsed and compared with a tolerance of 0.01; enums and yes/no answers use normalized exact match; short strings use normalized exact match plus an alias list. Output that can't be parsed is a failure.
- `tokop fixtures-check` regenerates gold answers from the YAML and asserts they match the stored dataset.
- docs/DATASET.md shows ten sample items per question type (question, gold answer, supporting quote) so a person can audit the set before any money is spent.

Pipelines, defined in YAML in the repo:

- **B0**, the way many first-version support bots ship. Frontier model. A system prompt of about 700 tokens that opens with a current-timestamp line, carries courtesy filler, states one rule twice in different words, contains a conflicting pair ("Be concise." and "Explain your reasoning in full detail."), gives no explicit role, task or success criteria, and specifies no output format beyond "End with a line: Final answer: <answer>". The user message puts the question first and the handbook after it. max_tokens is 2,000. Comment each anti-pattern in the YAML and explain it in the UI's description of B0. Keep them realistic: each should be recognizable from real production prompts, and none should be exaggerated for effect.
- **B1** = B0 with a cache-friendly order: the handbook moves into the system prompt, the cache breakpoint sits at the end of the static prefix, the timestamp moves after the breakpoint, and the question goes last.
- **B2** = B1 with a CLEAR rewrite and an output contract: explicit role, task and success criteria; duplicates and the conflict removed; JSON output `{"answer": "...", "evidence": "<verbatim quote from the handbook>", "section": "<section id>"}` requested through instructions and validated by the parser; max_tokens 200. Don't use provider structured-output features in v1. That keeps requests comparable across providers and keeps cache pre-warming possible.
- **B3** = the B2 prompt inside a cascade: cheap tier, then mid tier, then frontier (section 7.5).

Default model roles live in config and nowhere else: frontier `claude-opus-5`, mid `claude-sonnet-5`, cheap `claude-haiku-4-5-20251001`. Scarce models default to the frontier model. Confirm the IDs against Anthropic's models page at M2. When an OpenRouter key is present, the cheap tier may be any model that beats Haiku on the calibration split at lower cost; record which model won and why.

`make record` starts with a 10-task pilot per pipeline and projects total spend from the pilot's p95 output length. It aborts if the projection exceeds the remaining RECORD_BUDGET_USD. Then it spends in order of cost, cheapest first:

1. B2 on the calibration split with each of the three tiers.
2. A sanity and difficulty check. Stop and report before spending more if the frontier scores below 90% on calibration (almost always a generator or grader bug, since every answer is in the handbook) or if the cheap tier lands within 3 points of the frontier (the set is too easy to show escalation: make the computation and exception templates harder, log the change, and regenerate).
3. B2 on the test split with each tier. With step 1, this completes the response matrix the cascade simulator needs.
4. B0 and B1 on the test split with the frontier model.
5. The chosen B3 configuration, live, on the test split, with exact-request reuse turned off so every call is fresh. Compare it with the simulation (decision agreement and metric differences).
6. Two Compare examples (one short reasoning prompt, one extraction prompt) across the available models, and one Brief example.

Pre-warm each cached prefix before fanning out: send a `max_tokens: 0` request with an explicit breakpoint on the last static block and wait for it to finish. A cache entry only becomes available once the first response begins, so parallel first requests all miss. Pre-warm calls are costed like any other call.

An interrupted `make record` resumes where it stopped: identical requests reuse their recorded response (section 7.1), so nothing is paid for twice.

Each run writes a manifest: git SHA, model IDs, the prices used (with source and date), seed, split hashes, timestamps, and total spend from provider-reported usage.

## 7. Engine

### 7.1 Adapters and recording

- The `anthropic` adapter uses the Messages API with explicit `cache_control` breakpoints (automatic caching is supported for multi-turn use) and the count_tokens endpoint. The `openai_compatible` adapter takes a base URL, a key, and a model, and uses Chat Completions.
- providers.yaml holds base URLs. Enable OpenAI (`https://api.openai.com/v1`), OpenRouter (`https://openrouter.ai/api/v1`), Gemini's OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`), and DeepSeek (`https://api.deepseek.com`). Add Qwen, Kimi, Z.ai and Perplexity entries with `enabled: false` until a subagent confirms their current base URLs and usage field names from their documentation.
- Cassettes. In record mode the key is the SHA-256 of provider, model, and the canonicalized request (JSON with sorted keys; unstable key order also breaks provider caches). Store the request, raw response, raw usage, latency, time to first token, and timestamp. An identical request in record mode reuses its cassette instead of calling the provider; the trace marks it as reused, and reused calls are excluded from latency statistics. This exact-request reuse is what makes recording resumable. In replay mode a missing key raises `CassetteMiss` naming the request; replay never falls back to a live call.
- Retries: exponential backoff with jitter on 429, 5xx, and overloaded errors, at most 3 attempts. Every retry is its own traced span with its own cost.

### 7.2 Usage normalization

Get this exactly right; a technical founder will check it. Normalize every response into these buckets, each with `source` set to `provider` or `estimated`, and store the raw usage JSON next to them for every call:

`input_uncached, cache_write_5m, cache_write_1h, cache_read, output_visible, output_reasoning`, plus reserved `image_in, audio_in, image_out`.

Known traps:
- Anthropic's `input_tokens` counts only the tokens after the last cache breakpoint. Total input is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. The `cache_creation` object splits writes into `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`. Thinking tokens bill as output.
- OpenAI Chat Completions: `prompt_tokens` already includes `prompt_tokens_details.cached_tokens`, and `completion_tokens` already includes `completion_tokens_details.reasoning_tokens`. Subtract to get the uncached and visible parts; adding them double-counts.
- DeepSeek reports `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` instead of OpenAI's fields.
- Gateways such as OpenRouter may return an authoritative cost. When one does, store it with `cost_source=provider` next to the registry-computed cost and flag disagreements above 2%.

Each mapping function has a docstring with the documentation URL and the date you checked it. Tests use real recorded payloads where they exist and documentation examples otherwise.

### 7.3 Tokens, prices, cost

- Before a call: exact counts through Anthropic's count_tokens when a key is present, and tiktoken `o200k_base` for OpenAI models. Everything else is estimated as o200k times a ratio fitted per model on recorded provider-reported usage, and the UI shows the method (for example, "~ o200k x 1.07, fitted on 312 calls"). Fit per model: Anthropic's models from Claude 4.7 onward use a newer tokenizer that produces about 30% more tokens for the same text than earlier models such as Haiku 4.5.
- After a call: provider-reported usage is authoritative.
- Registry. `tokop sync-models` pulls model metadata (pricing and context length) from OpenRouter's public model listing into SQLite; confirm the endpoint and whether it needs a key in OpenRouter's docs. Tests and replay use a committed snapshot of the listing. prices.yaml overrides the listing with official direct-provider prices (Anthropic's pricing page for the demo models, filled at M2 by a subagent) and supplies fields the listing lacks: input, output, 5-minute cache write, 1-hour cache write, cache read, batch discount. Every price keeps its source URL, retrieval date, and verified flag; anything unconfirmed stays `verified: false`, and the UI marks it.
- Each run stores a price snapshot, and costs are always computed from the run's own snapshot, so historical results don't move when prices change.
- The spend guard wraps every adapter call: a preflight estimate before the call (input plus max_tokens, or the historical p95 output), a hard stop when a cap would be exceeded, and the actual cost recorded afterward.
- Every model call Tokop makes on its own behalf (scorer features, judges, rewrites, briefs, pre-warming) is costed and counted in the cost of the run or experiment that caused it.

### 7.4 Findings and lint

These are deterministic and make no model calls. Rank everything by projected dollars per 1,000 tasks, weighted by confidence. Low-dollar hygiene findings sink to the bottom, which is where they belong.

Prompt lint (used by Inspect and on every pipeline template), mapped to CLEAR plus a Cache group:

| ID | Detects | Group |
|---|---|---|
| PL01 | Volatile content inside the cacheable prefix: timestamps, dates, UUIDs, counters | Cache |
| PL02 | Dynamic content placed before static content | Cache |
| PL03 | Static prefix below a provider's minimum cacheable length, where caching silently does nothing | Cache |
| PL04 | Cache breakpoint on a block that changes every request, so every request writes and none reads | Cache |
| PL05 | Large fixed overhead per call from the system prompt and tool definitions | Cache |
| PL06 | No output contract with a length cap | C |
| PL07 | Exact or near-duplicate instructions (normalized word 3-gram Jaccard >= 0.8) | L |
| PL08 | Courtesy, hedges, filler | L |
| PL09 | Oversized or redundant few-shot examples | L |
| PL10 | No explicit role, task, or success criteria; vague task verbs ("help with", "look at", "handle") | E |
| PL11 | Conflicting instruction pairs from a curated list (labeled heuristic) | E |
| PL12 | Unstructured wall of text above a length threshold | A |
| PL13 | Hard-coded specifics that should be variables | R |
| PL14 | Compose instead: over about 500 tokens with three or more distinct jobs, counted from separate imperative task clauses (labeled heuristic) | A |

Derive a CLEAR score of 1 to 5 per letter from its rules (25 total). Display it; never use it for ranking.

Workload findings, computed from traces and the calibration split:

| ID | Detects |
|---|---|
| W01 | An identical block repeated across calls outside the cached prefix |
| W02 | A prefix that changes on every call |
| W03 | Output tokens far above what the grader needs |
| W04 | The frontier model answering tasks a cheaper tier gets right (projected from the calibration split, showing its n) |
| W05 | A workload with no latency requirement, where the Batch API would halve cost (projected only; v1 doesn't execute batches) |
| W06 | Retries, with their count and cost |

Every finding stores its evidence, its projected saving with the formula, a confidence label (measured, projected, or heuristic), and the transform that addresses it.

### 7.5 Cascade (FrugalGPT, adapted)

- Tiers are ordered by cost. After each tier answers, a scorer estimates the probability that the answer is correct.
- Scorers live in `optimize/scorers.py` and receive a task view with no gold fields. Checkers live in `workloads/grading.py`. They get separate modules and separate tests, including one that fails if a scorer can reach a gold answer.
- The scorer is an L2-regularized logistic regression per tier (scikit-learn), fitted on the calibration split only, over deterministic features from a feature registry that each workload's YAML selects from. Generic features: output parses, answer type matches, output length, question length, count of numerals in the question. Demo-specific feature: the evidence quote appears verbatim (whitespace-normalized) inside the handbook section that the answer itself cites. The check compares the quote with the handbook only; it never looks at gold. Optional feature, off for the demo: a cheap judge model's 0 to 1 confidence with a short reason, costed and shown in the trace. Report each scorer's AUROC on the test split.
- Thresholds come from a grid search on the calibration split (step 0.02, exhaustive for up to four tiers; log the runtime) that minimizes cost per task subject to calibration accuracy of at least the B2-frontier accuracy minus 1 point. With "Protect scarce models" on, the search minimizes scarce-model spend under the same accuracy constraint.
- For user workloads, warn when the calibration split has fewer than 30 tasks: a cascade needs labeled examples from the same distribution it will serve.
- An escalated task pays for every attempt it made. A task that goes from cheap to mid pays for both calls.
- The simulator evaluates any threshold setting from the recorded response matrix. The live confirmation run checks the simulator.

### 7.6 Proof statistics (core/stats.py, fully unit-tested)

- Accuracy: Wilson 95% interval.
- Paired bootstrap over tasks (5,000 resamples, fixed seed) for D accuracy, for cost per successful task (total cost divided by successes within each resample), and for the cost ratio.
- McNemar exact test on the discordant pairs.
- Verdict with margin d (default 3 points, configurable): non-inferior when the lower bound of the D-accuracy interval is above -d, worse when the upper bound is below -d, inconclusive otherwise.
- For an inconclusive verdict, estimate the tasks needed from the per-task variance of the paired difference, v = p10 + p01 - (p10 - p01)^2, as n >= 1.96^2 * v / (Dhat + d)^2, and show n minus the current count. When Dhat + d <= 0, say that more tasks are unlikely to change the verdict.
- The active-evaluation estimator from Appendix A2, built now as core math even though judges arrive in S1: per-item terms; the paired version for differences between pipelines (d = score_candidate - score_baseline); an interval from the empirical variance of the per-item terms; the optimal fixed rate p*; and an uncertainty-proportional rate clipped to [0.05, 1]. Label the uncertainty-proportional rate as a simplification of the paper's active policy.
- Tests include coverage simulations (Wilson intervals cover about 95% of simulated binomials), hand-computed McNemar values, and an unbiasedness simulation for the estimator (known ground truth, 2,000 trials, mean error within tolerance).

### 7.7 Budgets and policy

A hard cap per run; a daily cap with a warning at 80% and a stop at 100%; a preflight estimate on every run; allowed providers per workload, enforced by the router.

## 8. Design direction

Brief: a measuring instrument for AI spend. The audience is founders and engineers deciding whether to trust a cheaper setup. Its job is to make a cost-quality tradeoff legible and trustworthy in under a minute.

- Process. Write docs/DESIGN.md first: 4 to 6 named hex values, typefaces and their roles, ASCII wireframes of the four screens, and principles. Review it against the defaults listed below and revise anything that reads as generic, noting what you changed. Then build. After each screen, take Playwright screenshots, critique them against DESIGN.md, and fix what you find. Before finishing, remove one decorative element from every screen.
- Spend the boldness in one place: the cost-quality frontier chart. Keep everything around it quiet.
- Numbers use tabular figures and always show units. Intervals appear as ranges or whiskers. Every metric carries a provenance mark (provider-reported, exact count, estimated, projected, simulated), defined once in a legend. Estimated, projected, and simulated values render visibly different from measured ones (muted, with a label).
- Color encodes role (scarce model, cheap tier, verifier) and state (better, worse, inconclusive). It never encodes vendor brand.
- Avoid the defaults of generated UI: warm cream with a serif and a terracotta accent; near-black with a single acid-green accent; identical rounded cards with the same soft shadow; gradient washes; tracked-out all-caps labels above headings; meta strings joined with middle dots; monospace for small labels; arrows appended to button text; indigo on Inter.
- Copy uses sentence case and plain verbs, and names things the way users think of them (write "Cost per successful task" in full). Buttons say what they do ("Build candidate", "Run proof"), and the resulting states reuse the same words. Empty states name the next action. Errors say what happened and how to fix it.
- Quality floor: keyboard navigation with visible focus, reduced motion respected, WCAG AA contrast, designed for 1280 px and wider (smaller screens can be view-only).

## 9. Non-negotiables (copy these into CLAUDE.md)

1. Every number in the UI and the README comes from engine code computing over traces. No metric literals in UI components or docs.
2. Every metric carries provenance, and every estimate names its method.
3. Replay mode shows when the recording was made and with which model IDs.
4. Prices carry a source URL and retrieval date; unverified prices are marked.
5. Quality claims show the estimate, the interval, and n; results state split sizes; inconclusive results are displayed as inconclusive.
6. Official APIs only: no browser automation, no cookie reuse, no credential sharing or pooling, no rate-limit evasion. Drop any feature that would need them and log it in DECISIONS.md.
7. API keys stay on the server and never appear in logs, fixtures, or the browser.
8. Failures stay visible: a failed call counts as an unsuccessful task and shows in the trace.
9. No fake buttons: every control does something real or is disabled with a one-line reason.
10. Stored prompts and outputs can be deleted per run.

## 10. Definition of done

`make verify` runs these checks in order and ends by printing `VERIFY PASSED (<n> checks)` or `VERIFY FAILED: <check>` with a nonzero exit code:

1. ruff, mypy (strict on core/ and optimize/), ESLint, tsc.
2. pytest, unit and simulation tests (including the resume test, the scorer-isolation test, and the estimator simulation), with coverage of at least 85% on engine/core and engine/optimize.
3. The production web build.
4. `tokop fixtures-check`: gold answers regenerated from YAML match the dataset; every cassette has provider-reported usage; every manifest has model IDs, prices with provenance, seed, and date.
5. `tokop report --check`: recompute every headline metric from fixtures, assert the API returns identical values, and assert that the README's metrics block (between `<!-- metrics:start -->` and `<!-- metrics:end -->`, written by `tokop report --write-readme`) is current.
6. Playwright end-to-end tests in replay mode against the production build. They use fixtures/demo/ when it exists and fixtures/test/ otherwise, and the UI labels test fixtures as test data. Optimize: baseline, findings, build candidate, run proof, headline row and verdict visible, open one disagreement trace; New experiment is disabled with a reason. Inspect: paste B0's prompt, see PL01, PL02 and PL06 fire, apply safe fixes, see the net token delta in the diff; Brief mode shows a recorded brief with tokens before and after. Compare: a recorded example renders with tokens and cost. Spend: the recorded runs appear. The tests compare displayed headline numbers with `/api/report` and save screenshots to docs/screenshots/.
7. If ANTHROPIC_API_KEY and RECORD_BUDGET_USD are both set, fixtures/demo/ holds a complete recording. If RECORD_BUDGET_USD was left empty, docs/DATASET.md exists and the final report says the recording is waiting for review. In any other case, the final report states that the demo is unrecorded and why.

Before declaring done, run a spec review in a fresh subagent with this instruction: "Review the repository against SPEC.md sections 3, 5, 9 and 10. Report only gaps that violate a stated requirement or would make a displayed number wrong. Ignore style." Fix what it reports, rerun `make verify`, and keep the reviewer's final report in the conversation.

Then report back: what was built; the exact commands to run it; which numbers are recorded, estimated, projected, or simulated; total API spend from the ledger, including what the proof itself cost; the demo's actual headline results; known limitations; and what you would build next, in priority order.

## 11. Milestones

Each milestone ends with its acceptance check passing.

- **M0 Scaffold.** Layout, uv and pnpm, Makefile targets (`setup`, `dev`, `verify`, `record`, `demo`, `build`), CLAUDE.md, PLAN.md, PROGRESS.md, DECISIONS.md, and a `make verify` that runs with whatever checks exist. *Accept:* `make dev` starts with no keys, and `make verify` runs.
- **M1 Core math, tests first.** Usage normalization, cost, statistics (including the estimator), token estimation, the spend guard. *Accept:* hand-computed fixtures pass for every trap in 7.2; the Wilson coverage and estimator unbiasedness simulations pass.
- **M2 Registry and adapters.** Model sync and price overrides verified by a subagent, both adapters, cassette record and replay with exact-request reuse, the CLI skeleton, fixtures/test/. *Accept:* a record and replay round trip reproduces identical metrics; a recording against a fake provider that is killed halfway and restarted sends no request twice.
- **M3 Workloads.** Generator, DATASET.md and fixtures-check, pipeline YAML, the runner with traces, grading, the SQLite ledger. *Accept:* fixtures-check passes; B0 metrics on test fixtures match a hand-computed fixture.
- **M4 Optimization.** Lint, findings, transforms, scorers, the cascade and simulator, proof, report JSON, and `make record` end to end (run it when the key and budget are set). *Accept:* threshold search on a synthetic fixture returns the known optimum; the simulator agrees with brute-force per-task evaluation; W01, W02 and W03 fire on B0.
- **M5 Optimize screen,** complete, on recorded or test fixtures. *Accept:* the Playwright Optimize flow passes.
- **M6 Remaining screens.** Inspect with Brief mode, Compare, Spend, Settings, and the New experiment form. *Accept:* their Playwright checks pass.
- **M7 Finish.** README with generated metrics; DEMO.md, a 90-second demo script built from the real numbers; ARCHITECTURE.md; a Dockerfile that defaults to replay mode and needs no keys, with a deployment guide for one container host (make sure the instance won't cold-start during a live demo); the screenshot critique pass; the spec review. *Accept:* the final `make verify` prints VERIFY PASSED.

The flagship path (M3 to M5) comes before breadth (M6) on purpose. If the session ends early, the screen that matters most already exists.

## 12. README and positioning

- One line: "Tokop finds the cheapest way to run your AI workload without getting worse, and proves it."
- Why now: more AI usage is billed per token. GitHub Copilot moved to usage-based billing in June 2026, and Anthropic requires API keys for third-party tools. Teams that want to cut cost need evidence that quality held.
- Describe alternatives only by category, without stating their features or prices: observability and evaluation tools (Langfuse, Helicone, LangSmith, Braintrust) report spend and quality; gateways and routers (LiteLLM, Portkey, OpenRouter, Martian, Not Diamond, RouteLLM) move traffic. Tokop's claim is narrower and testable: for one workload, the cheapest pipeline that is statistically non-inferior to the current one, with the proof attached.
- A model vendor has little reason to recommend a competitor's cheaper model. A neutral optimizer can.
- Business hypothesis, labeled as a hypothesis: charge a share of verified savings. The proof engine is what makes savings billable.
- Include the demo's real numbers (the generated block) and what the proof cost; the method, with citations to FrugalGPT and the cost-optimal evaluation paper; the limitations (a cascade needs around 100 labeled tasks per workload, and savings depend on the price spreads at recording time); and a roadmap built from section 3's exclusions and section 13.

## 13. Stretch goals (only after section 10 passes, in this order)

- **S1 Judges and human rating for unlabeled workloads.** Wire the core estimator to a cheap judge on every task and a strong rater on a sampled subset. The strong rater can be a frontier judge or the owner, through a small rating screen. Show the judging cost next to the cost of having the strong rater grade everything.
- **S2 CI gate.** A GitHub Action around `tokop prove --workload w.yaml --baseline a --candidate b --margin 0.03` that fails unless the verdict is non-inferior.
- **S3 Upload wizard** for YAML, JSONL, or CSV workloads in the UI.
- **S4 LangGraph trace import.** The pipeline spec already uses nodes, edges, conditional edges, and shared state, so the mapping is direct.
- **S5 Experiments:** semantic caching with its own false-hit evaluation, and TRIM-style output compression.

## Appendix A. Research, distilled

The source files are optional. This is what the build needs from them.

**A1. FrugalGPT** (Chen, Zaharia, Zou; arXiv:2305.05176). Three families of cost reduction: prompt adaptation, LLM approximation (caching, fine-tuning), and the LLM cascade. A cascade is an ordered list of models, a scoring function g(q, a) in [0, 1], and a threshold per tier; the first answer that scores above its tier's threshold is returned. Their scorer was a small regression model (DistilBERT) trained on labeled examples, and the model list and thresholds are chosen to maximize quality under a cost budget. Limitations they state: labeled examples from the same distribution are required; learning the cascade costs money up front; a scorer that is often unsure sends tasks through every tier. Their headline (matching GPT-4 at up to 98% lower cost) used March 2023 prices, so never quote it as a Tokop claim.

**A2. Cost-Optimal Active AI Model Evaluation** (Angelopoulos, Eisenstein, Berant, Agarwal, Fisch; arXiv:2506.07949). A cheap rater G scores every item; an expensive rater H scores items sampled with probability pi(x). The estimator theta-hat = (1/T) Sum [G_t + (H_t - G_t) * xi_t / pi(X_t)], with xi_t ~ Bernoulli(pi(X_t)), is unbiased for E[H]. The optimal fixed sampling rate is p* = sqrt((c_g / c_h) * MSE / (Var(H) - MSE)) when MSE < (c_h / (c_h + c_g)) * Var(H), and 1 otherwise, where MSE = E[(H - G)^2]. Gains shrink as the cheap rater's error grows or as its cost approaches the expensive rater's, and they grow when its error varies strongly across items (easy versus hard). Input-dependent (active) policies need good uncertainty estimates; the fixed-rate policy improved on baselines in all of their experiments. Tokop also applies the estimator to paired differences between pipelines and clips sampling probabilities at 0.05 to bound the inverse weights. Used by 7.6 and S1.

**A3. TRIM** (Garrachon Ruiz, de la Rosa, Borrajo; arXiv:2412.07682). The large model omits a set of easily inferable function words and a small fine-tuned model restores them, saving about 19% of output tokens on narrative tasks with small metric losses. It needs a trained reconstruction model, so v1 uses output contracts, which cut far more on short-answer tasks. Stretch only.

**A4. Microsoft Reactor session on GitHub Copilot token optimization** (June 2026, shortly after Copilot's June 1 move to usage-based billing). CLEAR: Constraint (cap the shape of the output), Lean (cut courtesy, hedges, filler and duplicated rules), Explicit (name the role, the task, and what good looks like), Architected (visible structure), Reusable (parameterize and share what works). Rubric: 1 to 5 per letter. Their analyzer, Kates, uses local heuristics, makes no model calls, and can gate CI. Split a prompt into composed steps only when it exceeds about 500 tokens, does three or more distinct jobs, and stays brittle after a CLEAR pass; composition doesn't automatically use fewer tokens. The presenters' figures (always-loaded instructions cut from about 14,000 to 5,500 tokens, first tokens 41% sooner, about 30% fewer re-asks) are anecdotes from a live session and must not be cited as results.

**A5. Embeddings** (AWS explainer). Background for semantic caching and similarity-based routing. Not used in v1.

## Appendix B. Platform facts checked in September 2026 (re-verify at M2)

- Anthropic prices per million tokens (input / output / cache read): Opus 5 $5 / $25 / $0.50; Sonnet 5 $2 / $10 / $0.20; Haiku 4.5 $1 / $5 / $0.10. Cache writes cost 1.25x base input for the 5-minute lifetime and 2x for 1 hour; reads cost 0.1x (0.025x on Fable 5.1 and Mythos 5.1). Batch API: 50% off input and output.
- Minimum cacheable prefix: 512 tokens on Opus 5, 1,024 on Sonnet 5, 4,096 on Haiku 4.5. Below the minimum the request runs uncached and returns no error; both cache usage fields read 0.
- The cache key is an exact prefix over tools, then system, then messages. Writes happen only at breakpoints, so a breakpoint on a block that changes every request never produces a read. Up to 4 breakpoints, with a 20-block lookback.
- A cache entry becomes available only after the first response begins, so wait before sending parallel requests. A `max_tokens: 0` request with an explicit breakpoint pre-warms the cache and bills no output; it is rejected with streaming, extended thinking, structured outputs, or forced tool choice.
- The cache lifetime counts from the start of the request, so long generations use up the 5-minute window.
- Sources: https://platform.claude.com/docs/en/about-claude/pricing and https://platform.claude.com/docs/en/build-with-claude/prompt-caching
