# Validation and Reliability Strategy

This document defines the release gates, failure containment, and recovery behavior for Tokop. Its
goal is not to claim that external providers or hosts can never fail. Instead, **100% passing** means
that every supported run reaches one of two deterministic, tested outcomes:

1. a complete result whose evidence and provenance satisfy all acceptance gates; or
2. a fail-closed, resumable result with no hidden spend, partial result presented as final, or
   contamination of calibration and test evidence.

The baseline scope is the local-first text workflow described in `ARCHITECTURE.md`: ingestion,
rendering, provider execution or replay, usage normalization, pricing, grading, optimization,
held-out proof, reporting, export, and certificate/canary checks. Production gateway behavior,
multimedia, and absolute correctness of model-generated answers are explicitly out of scope.

## 1. Validity & Acceptance Checks

### Functional sanity checks

Each row is a release gate. A run must not emit an adoptable export or valid certificate when any
gate fails.

| Stage | Inputs to validate | Processing invariant | Required output / acceptance criterion |
|---|---|---|---|
| Environment | Python/Node versions, locked dependencies, writable state path, SQLite availability, runtime mode | Configuration is parsed once; secrets never enter client-visible state; replay opens no provider socket | Startup succeeds with a configuration fingerprint, or exits non-zero with a stable error code and remediation |
| Workload | YAML schema, dataset encoding, unique task IDs, grounding references, split labels, provenance, grading mode, margin, providers/models | Paths resolve beneath approved roots; task count and split membership are stable; no task belongs to both calibration and test | A content-addressed workload manifest records counts, hashes, provenance, and validation result |
| Ingestion | JSONL/CSV/OTel records, required fields, types, timestamps, request/response association | Parsing is lossless for supported fields; source completions are never promoted to gold labels; duplicates follow a declared policy | Accepted/rejected counts reconcile to input count; each rejection has record location and reason |
| Prompt/graph rendering | Task, grounding, template, graph dependencies, model role | Rendering is deterministic; graph is acyclic; every dependency is available; scorer input excludes gold and grade | Prompt/graph hash is repeatable; single-call graph rendering preserves the expected cassette key |
| Provider/replay | Mode, credentials, consent, budget, canonical request, cassette | Replay misses fail closed and never call live; live calls have bounded connect/read/total timeouts; retry only safe transient failures; each attempt is traceable | One terminal trace per task/arm: success or explicit failure; no task silently disappears; spend never exceeds the enforced policy |
| Usage/pricing | Raw provider usage, normalized token buckets, price snapshot, currency/units | Bucket conversion is provider-specific and checked; money uses decimal arithmetic; immutable content-addressed prices are used | Raw and normalized usage coexist; components sum to total; non-negative cost recomputes exactly from snapshot |
| Grading | Answer, grading contract, optional gold/judge evidence | Graders and judges cannot influence routing; judge sampling probabilities are positive and logged; parse failures are unsuccessful | Every terminal trace has a grade state; denominators include failed calls; judged estimates expose correction and uncertainty |
| Candidate search | Calibration traces, candidate space, scorer features, deterministic seed | Thresholds and transforms use calibration only; test labels/outcomes are unavailable until selection is frozen | Search space count, seed, candidate scores, selected operating point, and selection hash are recorded |
| Held-out proof | Frozen candidate/baseline, paired test task IDs, predeclared margin and confidence level | Both arms use the identical held-out task set; missing pairs fail proof; statistical methods handle zero discordance and boundary cases | Report includes accuracy/CIs, paired cost CI, exact McNemar result, accuracy delta CI, cost per success, evaluation cost, and verdict |
| Report/API/UI | `build_report()` output | API, CLI, generated docs, and UI consume the same report; serialization preserves precision and evidence labels | Schema validation passes; rendered headline values equal `/api/report`; simulated/projected/measured marks remain visible |
| Export/certificate | Passing proof, evidence context, routing/prompt diff | Export is derived from the frozen winner, never from an unproven candidate; certificate binds all relevant hashes and expiry | Export round-trips and routes golden cases correctly; stale, drifted, inconclusive, or inferior evidence cannot yield a valid certificate |

#### Dataset and statistical acceptance thresholds

- Task IDs are non-empty and unique; dataset, grounding, workload, cassette manifest, price, prompt,
  scorer, and selected-candidate hashes are reproducible across two clean replay runs.
- Calibration and test ID sets have an empty intersection. Their union and counts match the manifest;
  the demo specifically remains 100 calibration plus 204 test tasks unless its manifest and generated
  evidence are deliberately regenerated together.
- Candidate selection completes before test outcomes are loaded. Enforce this with separate data
  capabilities, not merely a convention or UI label.
- Proof uses paired task IDs and includes failures as unsuccessful observations. Any missing or extra
  pair is a hard error, never listwise deletion.
- Non-inferiority is true only when the configured one-sided confidence bound clears the predeclared
  negative margin. Observed point estimates alone never pass the gate.
- Confidence level, bootstrap seed/replicates, alternative hypothesis, tie policy, multiplicity/alpha
  spending, and rounding rules are versioned. Tests cover all-success, all-failure, no-disagreement,
  maximal-disagreement, tiny-sample, and zero-success denominators.
- A reported saving must reconcile from trace-level decimal costs. Cost per successful task is
  undefined (not zero or infinity serialized as invalid JSON) when there are no successes.
- A demonstration based on simulated data stays labeled simulated in the report, API, UI, docs,
  export, and certificate. It cannot be upgraded to production evidence by a display-layer change.

### Integration and data integrity checks

Use a run state machine: `created -> validated -> executing -> recorded -> selected -> proved ->
reported -> exported`. Only forward transitions are allowed, except a failed `executing` run may
resume from its last durable task boundary. `failed`, `cancelled`, and `superseded` are terminal;
their artifacts are retained for audit but cannot be selected.

1. **Atomic boundaries.** Commit the trace, raw usage, normalized usage, price hash, grade state, and
   task terminal state in one SQLite transaction. Write cassettes to a temporary file, `fsync`, then
   atomically rename; never expose a truncated cassette.
2. **Referential integrity.** Enable SQLite foreign keys and uniqueness constraints for run/task/arm,
   task IDs, request hashes, and immutable snapshot hashes. Run `foreign_key_check`, `integrity_check`,
   schema-version, and migration tests before evidence generation.
3. **Reconciliation.** At every boundary assert `input = accepted + rejected`, `planned = completed +
   failed + pending`, and `baseline test IDs = candidate test IDs = declared test IDs`. Recompute
   aggregate counts and costs from traces rather than trusting cached totals.
4. **Content addressing.** Verify hashes on every cassette/blob read. Bind reports to workload,
   dataset, prompts, graph, model registry, provider snapshot, prices, grader, scorer, code version,
   and random seed. Refuse mixed snapshots.
5. **Schema contracts.** Version workload, trace, cassette, report, export, and certificate schemas.
   Maintain backward-compatible readers or an explicit atomic migrator; reject unknown major versions.
6. **Cross-surface contract.** Generate/check API types and compare UI values with `/api/report`.
   Snapshot only structure and semantic labels; compare decimal/statistical values numerically using
   the engine's declared rounding rules.
7. **Isolation.** Run scorers with a capability-limited view containing only declared features.
   Graders receive answers only after routing is frozen. Tests should inject sentinel gold values and
   fail if they can affect routing, provider requests, or sampling policy.
8. **Auditability.** Structured events carry run, task, arm, attempt, request, snapshot, and trace IDs.
   Log secret names/configuration state, never values or unredacted sensitive prompt content.

## 2. Failure Modes and Effects Analysis (FMEA)

Prioritize by severity (`S`), likelihood (`L`), and detectability difficulty (`D`) on 1–5 scales.
Address high risk-priority numbers (`RPN = S x L x D`) first, while treating any severity-5 evidence,
privacy, or spend defect as release-blocking regardless of RPN.

### Infrastructure / environment failures

| Failure mode / effect | S/L/D | Root cause | Mitigation strategy | Automatic recovery / fallback |
|---|---:|---|---|---|
| Provider timeout or disconnect; incomplete arm | 4/3/1 | Network, rate limiting, upstream overload | Explicit bounded timeouts; classify retryable errors; exponential backoff with jitter and provider `Retry-After`; per-provider concurrency limits | Retry idempotent request under the same request ID up to a deadline; then persist failed trace and continue other tasks; resume later |
| Provider accepts request but response is lost; double spend | 5/2/4 | Ambiguous network failure after submission | Provider idempotency key where supported; attempt ledger and conservative reserved budget; do not blindly retry non-idempotent calls | Query/reconcile by provider request ID when possible; otherwise mark `ambiguous_spend`, charge reservation, and require replay/resume—not a hidden retry |
| Budget race or overspend | 5/2/3 | Concurrent workers check stale remaining budget | Transactional reserve-before-call using worst-case price; single-writer/compare-and-swap guard; process lock | Refuse new calls, settle actual usage against reservation, release unused amount, and leave run resumable |
| SQLite busy, disk full, or corruption | 5/2/2 | Concurrent write, exhausted volume, abrupt termination | WAL/busy timeout, one writer, free-space preflight, transactions, backups, checksums | Bounded retry for `busy`; stop before provider calls on persistence failure; restore last verified backup/cassettes and rebuild derived report |
| Process killed mid-run | 4/3/1 | Host restart, OOM, deploy, signal | Durable per-task checkpoints; signal handler stops scheduling; atomic files | On restart, verify manifest and resume only non-terminal tasks; reuse valid cassettes so completed calls are not repurchased |
| Dependency/runtime drift | 4/2/1 | Unlocked install, mutable image, incompatible migration | Lockfiles, pinned runtime/image digest, clean-install CI, schema compatibility gate | Roll back to last verified artifact and open database read-only if migration compatibility is uncertain |
| Clock skew or expiry error | 3/2/3 | Host clock drift, local timezone, naive timestamps | UTC-aware timestamps; monotonic time for durations; bounded skew check | Reject certificate/canary decisions when clock is untrusted; retain replay/report access with an explicit stale status |
| Permission/path failure | 3/2/1 | Read-only mount, wrong ownership, traversal/symlink | Startup read/write/rename probe; canonical allowlisted paths; least privilege | Fall back only to configured ephemeral state for replay; live mode fails closed because durable spend evidence is unavailable |
| UI/API cold-start timeout | 2/3/1 | Expensive report construction | Warm-up/readiness separate from liveness; cache keyed by full evidence hash | Return `202 building` with retry hint; never serve stale report under a new hash; retain last report explicitly labeled stale |

### Data / input failures

| Failure mode / effect | S/L/D | Root cause | Mitigation strategy | Automatic recovery / fallback |
|---|---:|---|---|---|
| Malformed YAML/JSONL/CSV/OTel | 3/3/1 | Syntax, encoding, truncated export, unexpected delimiter | Streaming parser with line/field diagnostics, size limits, UTF-8 policy, versioned Pydantic schemas | Quarantine bad records and emit reject manifest only when partial ingestion was explicitly selected; strict mode aborts atomically |
| Missing/wrong fields or units | 5/3/2 | Provider/schema evolution, nulls, strings for counts, milliseconds vs seconds | Strict types, enums, unit-bearing names, bounded numeric validators, contract fixtures per provider | Preserve raw event and mark trace unpriceable/ungradable; exclude it from adoption proof by failing completeness, never coerce silently |
| Duplicate or colliding task/request IDs | 4/2/2 | Re-import, unstable ID generator, hash canonicalization bug | Unique constraints; namespace IDs by dataset; canonicalization test vectors | Exact duplicates become idempotent no-ops; conflicting duplicates quarantine the dataset and block proof |
| Split leakage or test contamination | 5/2/4 | Duplicate paraphrases, shared entities, selection reads test labels | ID and semantic-near-duplicate audits; capability isolation; freeze selection artifact before test load | Invalidate proof/certificate, regenerate splits by group, recalibrate, and rerun held-out evaluation |
| Missing/corrupt/wrong cassette | 5/3/1 | Fixture drift, partial write, request changed | Request and body checksums; manifest completeness; atomic writes; `fixtures-check` | Replay raises a typed miss/corruption error. Never fall through to live; rebuild fixtures only through explicit budgeted workflow |
| Price/model registry mismatch | 5/2/2 | Renamed model, changed tokenizer, stale price effective date | Immutable effective-dated snapshots; exact model snapshot IDs; dimensional checks | Mark cost unavailable and block optimization/export until a complete snapshot is supplied; never substitute “closest” model |
| Untrusted prompt/content injection into judge/scorer | 4/3/3 | Dataset text impersonates instructions or leaks gold | Role-separated templates, structured fields, declared feature projection, adversarial corpus | Treat parse/contract violation as failure and escalate according to the frozen policy; do not dynamically broaden permissions |
| PII/secret leakage in traces | 5/2/4 | Raw traffic ingestion, headers or keys recorded | Allowlist fields, pre-persistence redaction, secret scanner, retention controls, encrypted storage | Quarantine and redact before retry; revoke exposed credentials; invalidate affected exports and preserve only sanitized audit metadata |
| Distribution collapse or unsupported strata | 5/2/3 | Synthetic/biased sampling, rare tail absent | Provenance and coverage gates, minimum per-stratum counts, drift/fingerprint checks | Return inconclusive and request more data; never collapse strata or claim population validity automatically |

### Logic / execution failures

| Failure mode / effect | S/L/D | Root cause | Mitigation strategy | Automatic recovery / fallback |
|---|---:|---|---|---|
| Gold-label leakage into scorer/router | 5/2/5 | Shared object, broad function signature, cached joined table | Separate scorer DTO/module; static/source tests plus sentinel behavioral tests | Abort selection, invalidate all derived artifacts, rerun from clean calibration-only materialization |
| Unpaired or silently dropped failures inflate quality | 5/2/4 | Inner join/filtering exceptions, retry exhaustion omitted | Left-join against declared task manifest; terminal record required per arm; invariant checks | Synthesize explicit unsuccessful terminal state for known failed attempt; block proof if task state is unknown |
| Nondeterministic search/report | 4/3/3 | Unseeded RNG, unordered sets, parallel reduction, unstable float order | Central seed registry; stable sorting/tie policy; deterministic reductions; record library versions | Rerun twice in clean processes and compare canonical report hashes; quarantine differing result and emit diagnostic diff |
| Incorrect cache/session accounting | 4/3/3 | Warm-cache assumption, expiry boundary, history growth | State-machine tests around hit/miss/expiry; per-turn ledger; provider-specific bucket invariants | Recompute derived costs from raw turns under pinned rules; block savings claim if bucket mapping is unknown |
| Decimal/float/rounding error | 4/2/3 | Binary floats, early rounding, mixed currencies | Decimal internally; round only at presentation; currency and unit types | Recompute from immutable raw usage; flag report stale and regenerate all dependent exports |
| Statistical edge-case crash or false verdict | 5/2/4 | Zero denominator, zero discordance, tiny sample, invalid bootstrap | Property/metamorphic tests against hand-computed fixtures; explicit undefined states; numerical library pinning | Produce `inconclusive`/`not computable` with reason; never default to non-inferior or zero cost |
| Multiple looks inflate false-positive rate | 5/2/4 | Repeated canaries or candidate peeking without alpha control | Predeclare looks; alpha-spending ledger; immutable look index | Refuse look beyond budget; issue new evaluation/certificate with a fresh declared plan |
| Race creates duplicate traces or inconsistent totals | 4/2/3 | Parallel task completion, retry plus original returns | Unique run/task/arm terminal constraint and transactional compare-and-set | Keep first valid terminal result, retain later attempt as superseded audit record, recompute aggregates |
| Report/API/UI drift | 4/3/2 | Duplicated calculations, hand-entered values, serialization precision | `build_report()` as sole source; schema/type checks; E2E semantic comparisons | Hide adoption controls and display schema mismatch; serve downloadable canonical report for diagnosis |
| Export differs from evaluated candidate | 5/2/4 | Separate code paths, mutable config, template drift | Export from frozen selection artifact; round-trip and golden routing execution tests | Refuse export on hash mismatch; regenerate from proof-bound artifact, never patch exported output in place |
| Broad exception handling masks defects | 4/3/3 | Catch-all returns empty/default data | Typed error taxonomy; catch only at boundaries; causal chains and stable exit codes | Persist failure context, stop dependent stages, continue only independent tasks; never return a success-shaped default |

#### Error-handling framework

- Define typed categories: `ValidationError` (non-retryable input), `IntegrityError` (fail closed),
  `CassetteMiss` (replay-only hard failure), `TransientProviderError` (bounded retry),
  `PermanentProviderError` (terminal task failure), `BudgetExceeded` (stop scheduling), and
  `InternalInvariantError` (abort run and invalidate derivatives).
- Every error includes a stable code, stage, retryability, run/task/attempt correlation, safe operator
  message, causal detail for logs, and remediation. API responses never include secrets or raw stack
  traces; CLI exits distinguish invalid input, inconclusive proof, and internal failure.
- Retry only when the operation is idempotent or protected by an idempotency key. Bound by attempt
  count, elapsed deadline, and budget reservation. Use exponential backoff, jitter, and circuit
  breaking; never retry schema, authentication, permission, budget, or integrity failures.
- Graceful degradation may preserve inspection of prior evidence, but optimization, proof, export,
  and certification remain fail closed. A fallback must be explicit in evidence metadata and cannot
  silently change provider, model, price, grader, split, or statistical method.

## 3. "Green Build" Strategy (Ensuring All Runs Pass)

### Mocking and idempotency

1. Make replay the default in developer machines and CI, unset provider credentials, and install a
   network-deny guard. Any socket attempt is a test failure.
2. Keep provider contract fixtures for success, malformed response, timeout, 429 with `Retry-After`,
   5xx, authentication failure, missing usage, streaming interruption, and unknown usage buckets.
   Fake time and retry sleep so fault tests finish instantly and deterministically.
3. Use the deterministic in-process simulator for generative coverage and cassettes for exact request
   regression. Cassettes must contain canonical request, raw response/usage, adapter/schema version,
   and checksums; replay never updates them.
4. Derive idempotency keys from run + task + arm + canonical request hash. Persist attempts separately
   from the one terminal trace, and enforce uniqueness in the database.
5. Pin all RNGs and record seeds. Freeze time, timezone, locale, hash seed, concurrency, and stable sort
   keys in golden tests. Compare two clean replays byte-for-byte after canonicalizing timestamps.
6. Tests write only to a unique temporary state directory/database. Cleanup is safe after success or
   failure; no test depends on execution order or a developer's cache.
7. Make fixture regeneration an explicit reviewed command with a before/after manifest, expected spend,
   model snapshot, and report diff. CI checks fixtures but never records them.

### Test automation hooks

| CI point | Required tests | Gate |
|---|---|---|
| Pre-commit / fastest lane | Formatting, lint, type checks, schema validation, secret scan, unit tests for changed modules | No warning-only failures; generated files unchanged |
| Pull request: engine | Full unit suite with branch/line coverage; property tests for usage, pricing, stats, budgets, state transitions; mutation testing on proof and spend guards | Existing 85% floor remains minimum; critical proof/budget/isolation modules require explicit near-total branch coverage and surviving mutants are reviewed |
| Pull request: contracts | Adapter contract tests, cassette corruption/miss tests, ingestion matrix, database migration up/down/forward tests, API schema compatibility | No live network; all providers pass the same behavioral contract |
| Pull request: integration | End-to-end replay from workload to export, kill-and-resume, concurrent budget reservation, clean-run determinism, report/docs/fixture checks | Identical canonical hashes on repeat; zero skipped release blockers |
| Pull request: frontend | Typecheck/lint/build, Playwright against production build, API/UI metric equality, error/empty/loading/stale states, secret-shaped-string scan | All supported browser tests pass with deterministic fixtures |
| Main/nightly | Full `make verify`, randomized/property seeds retained on failure, fault injection (disk full, kill, timeout, 429/5xx), migration from last release, dependency/security/license scan | Quarantine only non-release performance probes; correctness tests never become flaky retries |
| Release candidate | Clean locked install, offline replay smoke, container read-only-filesystem smoke, artifact/SBOM/signature check, backup/restore, certificate expiry/drift, exported router execution | Same source commit and lock hashes; no skips; evidence artifacts archived |
| Post-deploy | Readiness, `/api/report` schema/hash check, replay smoke, no-key/no-network assertion, canary for drift | Roll back automatically on schema/hash/readiness mismatch; never enable live mode implicitly |

Treat flaky tests as product defects: capture seed, timing, logs, and artifacts; quarantine only with an
owner and expiry; and never pass a build by rerunning until green. Parallelize isolated tests, but run
race- and budget-sensitive tests both serially and under forced concurrency. `make verify` remains the
single local/CI entry point and must fail if any mandatory check is skipped in the release environment.

## 4. Actionable Verification Checklist

### Acceptance and integrity

- [ ] Validate and hash workload, dataset, grounding, prompts/graph, registry, prices, grading, scorer,
      seeds, and provenance before execution.
- [ ] Assert unique task IDs, disjoint calibration/test sets, declared counts, group/semantic leakage
      audit, and sufficient tail-stratum coverage.
- [ ] Enforce the run state machine and atomic trace/usage/price/grade/task transactions.
- [ ] Enable and test SQLite foreign keys, uniqueness constraints, integrity checks, migrations,
      backup, and restore.
- [ ] Reconcile records and money at every boundary; count provider failures as unsuccessful tasks.
- [ ] Verify all cassette/blob hashes and prove replay cannot open a socket or fall back to live.
- [ ] Prove scorer/judge isolation with source, capability, and adversarial sentinel tests.
- [ ] Freeze candidate selection before loading held-out outcomes and bind proof to that selection hash.
- [ ] Test paired proof edge cases and ensure undefined results become `inconclusive`, never pass.
- [ ] Verify report/API/UI/docs/export values derive from the same canonical report and evidence labels.

### Failure containment and recovery

- [ ] Implement the typed error taxonomy, stable error codes, redacted structured events, and distinct
      CLI/API outcomes.
- [ ] Set connect/read/total deadlines, bounded idempotent retries, jitter, circuit breakers, and
      provider concurrency limits.
- [ ] Reserve budget transactionally before each live call and test concurrency plus ambiguous spend.
- [ ] Inject timeout, 429, 5xx, malformed usage, disk-full, SQLite-busy, corruption, kill, and restart.
- [ ] Confirm every interrupted run resumes without duplicate calls, traces, grades, or spend.
- [ ] Confirm permission, persistence, schema, price, and integrity errors fail before billable work.
- [ ] Ensure stale or partial evidence disables proof/export/certification while preserving audit access.
- [ ] Scan ingested data, traces, logs, API responses, built assets, and test artifacts for secrets/PII.

### Deterministic green build

- [ ] Pin runtimes/dependencies; run clean locked and offline installs; record code and lock hashes.
- [ ] Freeze seeds/time/timezone/locale/hash order and compare two isolated canonical replay outputs.
- [ ] Cover every adapter with the shared contract/fault fixture suite; CI has no provider credentials.
- [ ] Run static checks, unit/property/mutation tests, integration/recovery tests, report/fixture checks,
      frontend production build, and Playwright from `make verify`.
- [ ] Require zero skipped release blockers and never use rerun-until-green as a pass mechanism.
- [ ] Test the deploy artifact with a read-only filesystem plus its configured state volume, readiness,
      backup/restore, and rollback.
- [ ] Round-trip the selected export and execute golden routing cases against the evaluated behavior.
- [ ] Exercise certificate expiry, model/price/workload drift, alpha exhaustion, and canary failure.
- [ ] Archive canonical report, manifests, checksums, logs, test results, coverage, SBOM, and exported
      configuration for every release.
