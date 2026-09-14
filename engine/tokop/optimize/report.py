"""Build the report: one computation that the API, the UI, the tests and the README all read.

SPEC.md non-negotiable 1 says every number in the UI and the README comes from engine code
computing over traces. The way to actually achieve that — rather than intend it — is to have
exactly one function that produces every headline number, and to let everything else read its
output. That function is ``build_report``.

It works entirely from committed fixtures, replayed:

1. Replay each recorded run from its cassettes, grading as it goes.
2. Fit a scorer per tier on the **calibration** split.
3. Search thresholds on calibration, and fix the operating point there.
4. Simulate the cascade on the **test** split with that operating point.
5. Compare, and price the proof itself.

``tokop report --check`` recomputes all of it and asserts the API and the README still agree.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from tokop.adapters.cassette import CassetteStore, ReplayAdapter
from tokop.core.pricing import PriceSnapshot
from tokop.core.registry import Registry, load_registry
from tokop.core.stats import DEFAULT_SEED
from tokop.core.tokenize import base_counter_name, counter_named
from tokop.optimize.cascade import (
    OBJECTIVE_COST,
    OBJECTIVE_SCARCE,
    SearchResult,
    TierRun,
    pareto_frontier,
    search_thresholds,
    simulate,
    tier_run_from_arrays,
)
from tokop.optimize.findings import (
    CallRow,
    RunStats,
    batch_finding,
    cheaper_tier_finding,
    rank,
    workload_findings,
)
from tokop.optimize.lint import Finding, LintContext, clear_score, lint
from tokop.optimize.proof import ArmResult, ProofCost, build_proof, build_waterfall
from tokop.optimize.scorers import (
    FEATURE_REGISTRY,
    Scorer,
    ScorerBundle,
    ScorerContext,
    ScorerWarning,
    TaskView,
    evaluate_auroc,
    fit_scorer,
    scorer_kind,
)
from tokop.paths import fixtures_dir, repo_root
from tokop.recording_state import describe as describe_recording
from tokop.workloads.demo.dataset import DatasetBundle
from tokop.workloads.demo.dataset import build as build_dataset
from tokop.workloads.demo.generator import DemoItem
from tokop.workloads.grading import answers_equivalent, grade
from tokop.workloads.runner import Runner
from tokop.workloads.spec import CascadeSpec, WorkloadSpec, demo_timestamp, load_workload

#: What the grader actually reads out of an answer, in tokens. Used by W03 and PL06 to price
#: the gap between what is generated and what is consumed.
NEEDED_OUTPUT_TOKENS = 40

BASELINE_PIPELINE = "B0"
CANDIDATE_CASCADE = "B3"


class ReportError(RuntimeError):
    """The report could not be built from the fixtures on disk."""


@dataclass
class ReplayedRun:
    """One recorded run, replayed and graded."""

    pipeline_id: str
    split: str
    tier: str
    model_id: str
    task_ids: tuple[str, ...]
    correct: tuple[int, ...]
    cost_usd: tuple[Decimal, ...]
    outputs: tuple[str, ...]
    calls: tuple[CallRow, ...]
    total_cost: Decimal
    prewarm_cost: Decimal
    origin: str
    #: Every recorded generation for each task, in sample order. ``sample_outputs[i][0]`` is
    #: ``outputs[i]``. Length 1 unless the matrix was recorded at depth.
    sample_outputs: tuple[tuple[str, ...], ...] = ()
    #: What each of those generations cost. A scorer that draws k of them pays for k.
    sample_costs: tuple[tuple[Decimal, ...], ...] = ()
    #: Whether each generation would be graded correct, so that a scorer which returns a
    #: different sample than the first can be graded on the answer it actually returned.
    sample_correct: tuple[tuple[int, ...], ...] = ()

    def sample_depth(self) -> int:
        return min((len(s) for s in self.sample_outputs), default=1)

    @property
    def n(self) -> int:
        return len(self.task_ids)

    @property
    def accuracy(self) -> float:
        return sum(self.correct) / self.n if self.n else 0.0


def _p50(values: Sequence[float]) -> float | None:
    """Median, or ``None`` when there is nothing to take a median of."""
    usable = [v for v in values if v > 0]
    if not usable:
        return None
    return float(statistics.median(usable))


def _fixture_root() -> Path:
    state = describe_recording()
    return fixtures_dir() / state.fixture_source


def _load_manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.json"
    if not path.exists():
        raise ReportError(
            f"no fixture manifest at {path}. Run `tokop build-test-fixtures` (simulated, free) "
            "or `make record` (live, capped by RECORD_BUDGET_USD)."
        )
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ReportError(f"{path} must contain a JSON object")
    return data


def replay_run(
    workload: WorkloadSpec,
    registry: Registry,
    bundle: DatasetBundle,
    snapshot: PriceSnapshot,
    store: CassetteStore,
    pipeline_id: str,
    split_name: str,
    tier: str,
    origin: str,
    samples: int = 1,
) -> ReplayedRun:
    """Replay one recorded run from cassettes and grade it.

    ``samples`` is how deep the matrix was recorded. Every generation is replayed and costed,
    but ``calls``, ``cost_usd``, ``outputs`` and ``correct`` describe **sample 0 alone** — the
    single call the pipeline makes. Sampling is a property of a cascade's scorer, not of the
    pipeline, so a deeper matrix must not change what the pipeline's own findings, lint and cost
    figures say about it. The repeats live in the ``sample_*`` fields, for the one caller that
    is entitled to charge for them.
    """
    items = list(bundle.calibration if split_name == "calibration" else bundle.test)
    model_id = registry.roles[tier]
    runner = Runner(
        workload,
        registry,
        snapshot,
        ReplayAdapter(store, name="simulated"),
        provider="simulated",
        handbook=bundle.handbook,
        origin=origin,
    )
    result = asyncio.run(
        runner.run_pipeline(
            pipeline_id, items, model_id=model_id, split=split_name, samples=samples
        )
    )

    by_id = {item.id: item for item in items}
    correct: list[int] = []
    costs: list[Decimal] = []
    outputs: list[str] = []
    calls: list[CallRow] = []
    sample_outputs: list[tuple[str, ...]] = []
    sample_costs: list[tuple[Decimal, ...]] = []
    sample_correct: list[tuple[int, ...]] = []
    for task in result.tasks:
        item = by_id[task.task_id]
        outcome = grade(task.output, item.gold, item.answer_type, item.aliases)
        correct.append(int(outcome.correct and not task.failed))
        outputs.append(task.output)
        task_cost = Decimal(0)
        per_sample_cost: list[Decimal] = []
        per_sample_output: list[str] = []
        per_sample_correct: list[int] = []
        for request, response, _, _ in task.calls:
            breakdown = snapshot.cost(response.model, response.usage)
            per_sample_cost.append(breakdown.total)
            per_sample_output.append(response.text)
            sample_grade = grade(response.text, item.gold, item.answer_type, item.aliases)
            per_sample_correct.append(int(sample_grade.correct and not task.failed))
            if request.sample_index != 0:
                # A repeat is recorded and costed, but it is not part of what the pipeline did.
                continue
            task_cost += breakdown.total
            calls.append(
                CallRow(
                    task_id=task.task_id,
                    model=response.model,
                    prompt_hash=request.prompt_hash(),
                    tier=tier,
                    attempt=response.attempt,
                    prewarm=request.prewarm,
                    input_uncached=response.usage.input_uncached,
                    cache_write_5m=response.usage.cache_write_5m,
                    cache_write_1h=response.usage.cache_write_1h,
                    cache_read=response.usage.cache_read,
                    output_visible=response.usage.output_visible,
                    output_reasoning=response.usage.output_reasoning,
                    cost_usd=breakdown.total,
                    latency_ms=response.latency_ms,
                    reused=response.reused,
                    error=response.error,
                    system_text=request.system_text,
                    user_text=request.messages[0].text if request.messages else "",
                    static_prefix=request.static_prefix_text,
                )
            )
        costs.append(task_cost)
        sample_outputs.append(tuple(per_sample_output))
        sample_costs.append(tuple(per_sample_cost))
        sample_correct.append(tuple(per_sample_correct))

    prewarm_cost = Decimal(0)
    for warm_request, warm_response in result.prewarm:
        breakdown = snapshot.cost(warm_response.model, warm_response.usage)
        prewarm_cost += breakdown.total
        calls.append(
            CallRow(
                task_id="__prewarm__",
                model=warm_response.model,
                prompt_hash=warm_request.prompt_hash(),
                tier=tier,
                attempt=1,
                prewarm=True,
                input_uncached=warm_response.usage.input_uncached,
                cache_write_5m=warm_response.usage.cache_write_5m,
                cache_write_1h=warm_response.usage.cache_write_1h,
                cache_read=warm_response.usage.cache_read,
                output_visible=warm_response.usage.output_visible,
                output_reasoning=warm_response.usage.output_reasoning,
                cost_usd=breakdown.total,
                latency_ms=warm_response.latency_ms,
                reused=warm_response.reused,
                static_prefix=warm_request.static_prefix_text,
            )
        )

    return ReplayedRun(
        pipeline_id=pipeline_id,
        split=split_name,
        tier=tier,
        model_id=model_id,
        task_ids=tuple(t.task_id for t in result.tasks),
        correct=tuple(correct),
        cost_usd=tuple(costs),
        outputs=tuple(outputs),
        calls=tuple(calls),
        total_cost=sum(costs, Decimal(0)) + prewarm_cost,
        prewarm_cost=prewarm_cost,
        origin=origin,
        sample_outputs=tuple(sample_outputs),
        sample_costs=tuple(sample_costs),
        sample_correct=tuple(sample_correct),
    )


def _views(
    run: ReplayedRun, items: Sequence[DemoItem], handbook: str, samples: int = 1
) -> list[TaskView]:
    """Task views for the scorer. No answer key, by construction.

    ``samples`` is how many recorded generations to expose. A scorer that does not sample
    never reads them; one that does gets exactly the k it is configured for, so two settings
    of k are compared over the same draws rather than over fresh ones.
    """
    by_id = {item.id: item for item in items}
    views = []
    outputs_by_task = dict(zip(run.task_ids, run.outputs, strict=True))
    tokens_by_task: dict[str, int] = {}
    for call in run.calls:
        if not call.prewarm:
            tokens_by_task[call.task_id] = call.total_output
    samples_by_task = dict(zip(run.task_ids, run.sample_outputs, strict=False))
    for task_id in run.task_ids:
        item = by_id[task_id]
        drawn = samples_by_task.get(task_id, (outputs_by_task[task_id],))
        views.append(
            TaskView(
                task_id=task_id,
                question=item.question,
                output=outputs_by_task[task_id],
                context=handbook,
                tier=run.tier,
                output_tokens=tokens_by_task.get(task_id, 0),
                samples=tuple(drawn[:samples]),
            )
        )
    return views


@dataclass(frozen=True)
class CascadeConfig:
    """One candidate operating point, before its thresholds are chosen."""

    scorer: str
    k: int

    @property
    def label(self) -> str:
        return self.scorer if self.k <= 1 else f"{self.scorer} k={self.k}"

    def as_dict(self) -> dict[str, Any]:
        kind = scorer_kind(self.scorer)
        return {
            "scorer": self.scorer,
            "k": self.k,
            "label": self.label,
            "calls_per_scored_task": kind.calls_per_task(self.k),
            "method": kind.method_note(self.k),
        }


@dataclass
class ConfiguredCascade:
    """A configuration, its fitted scorers, and the thresholds calibration chose for it."""

    config: CascadeConfig
    scorers: ScorerBundle
    aurocs: dict[str, float | None]
    search: SearchResult

    def calibration_cost_per_task(self) -> Decimal:
        outcome = self.search.outcome
        return outcome.total_cost / outcome.n if outcome.n else Decimal(0)


def _configurations(cascade_spec: CascadeSpec, recorded_depth: int) -> list[CascadeConfig]:
    """Every (scorer, k) the calibration search is allowed to choose between.

    A scorer whose call count does not move with k is offered once, at k=1: drawing repeats it
    never reads would be charged for nothing.
    """
    out: list[CascadeConfig] = []
    for name in cascade_spec.scorer_choices():
        kind = scorer_kind(name)
        if kind.calls_per_task(2) == kind.calls_per_task(1):
            out.append(CascadeConfig(scorer=name, k=1))
            continue
        for k in cascade_spec.sample_choices():
            if k >= 2:
                out.append(CascadeConfig(scorer=name, k=k))
    return out


def _best_configuration(
    evaluated: Sequence[ConfiguredCascade], objective: str, searchable: Sequence[str]
) -> ConfiguredCascade:
    """Pick the operating point on **calibration**, by the objective the user asked for.

    A configuration whose thresholds could not reach the accuracy floor is never preferred to
    one that could, however cheap it is: the floor is a constraint, not a tiebreak.

    Only scorers the workload allows the search to adopt are eligible. Everything else is still
    evaluated and reported side by side — measuring a scorer and shipping it are different
    decisions, and a workload whose fixtures cannot support the second says so in its spec.
    """
    allowed = set(searchable)
    eligible = [c for c in evaluated if c.config.scorer in allowed] or list(evaluated)

    def rank(candidate: ConfiguredCascade) -> tuple[int, float, float]:
        outcome = candidate.search.outcome
        score = (
            float(outcome.scarce_cost)
            if objective == OBJECTIVE_SCARCE
            else float(candidate.calibration_cost_per_task())
        )
        return (0 if candidate.search.feasible else 1, score, -outcome.accuracy)

    return min(eligible, key=rank)


@dataclass
class ReportPayload:
    """Everything the UI, the tests and the README read."""

    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def headline(self) -> dict[str, Any]:
        proof: dict[str, Any] = self.data["proof"]
        return {
            "baseline_cost_per_successful_task": proof["baseline"]["cost_per_successful_task"][
                "point"
            ],
            "candidate_cost_per_successful_task": proof["candidate"]["cost_per_successful_task"][
                "point"
            ],
            "delta_accuracy": proof["delta_accuracy"]["point"],
            "verdict": proof["verdict"]["label"],
            "n": proof["candidate"]["n"],
            "proof_cost_usd": proof["proof_cost"]["total_usd"],
            "repayment_tasks": proof["repayment_tasks"],
        }


def build_report(
    *,
    protect_scarce: bool = False,
    seed: int = DEFAULT_SEED,
    resamples: int = 5000,
    margin: float | None = None,
) -> ReportPayload:
    """Recompute every headline metric from the committed fixtures."""
    registry = load_registry()
    workload = load_workload(repo_root() / "data/demo/workload.yaml")
    bundle = build_dataset(
        size=workload.dataset.size,
        calibration_size=workload.dataset.calibration_size,
        seed=workload.dataset.seed,
    )
    root = _fixture_root()
    manifest = _load_manifest(root)
    store = CassetteStore(root / "cassettes")
    origin = str(manifest.get("origin", "simulated"))
    snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
    cascade_spec = workload.cascade(CANDIDATE_CASCADE)
    tiers = list(cascade_spec.tiers)

    # ---------------------------------------------------------------- 1. replay
    runs: dict[tuple[str, str, str], ReplayedRun] = {}
    for entry in manifest.get("runs", []):
        key = (str(entry["pipeline"]), str(entry["split"]), str(entry["tier"]))
        runs[key] = replay_run(
            workload,
            registry,
            bundle,
            snapshot,
            store,
            key[0],
            key[1],
            key[2],
            origin,
            samples=int(entry.get("samples", 1)),
        )

    def need(pipeline: str, split: str, tier: str) -> ReplayedRun:
        try:
            return runs[(pipeline, split, tier)]
        except KeyError:
            raise ReportError(
                f"the fixture set has no {pipeline}/{split}/{tier} run. The report needs the "
                "full response matrix; rebuild the fixtures."
            ) from None

    base_pipeline = cascade_spec.base_pipeline

    # ------------------------------------------------- 2. scorers and the operating point
    #
    # The operating point is now a triple: which scorer, how many samples it draws, and the
    # per-tier thresholds. All three are chosen on the **calibration** split, before any test
    # result is computed, which is the ordering the whole proof rests on.
    objective = OBJECTIVE_SCARCE if protect_scarce else OBJECTIVE_COST
    frontier_calibration = need(base_pipeline, "calibration", tiers[-1])
    # Held fixed across configurations on purpose. The floor says what the workload requires of
    # any candidate; letting it move with the candidate would let a worse scorer lower the bar
    # it is judged against.
    accuracy_floor = max(0.0, frontier_calibration.accuracy - cascade_spec.accuracy_slack)
    recorded_depth = min(
        (
            need(base_pipeline, split, tier).sample_depth()
            for split in ("calibration", "test")
            for tier in tiers
        ),
        default=1,
    )

    def fit_bundle(config: CascadeConfig) -> tuple[ScorerBundle, dict[str, float | None]]:
        scorers: dict[str, Scorer] = {}
        warnings: list[ScorerWarning] = []
        aurocs: dict[str, float | None] = {}
        context = ScorerContext(
            feature_names=tuple(workload.scorer_features),
            equivalence=answers_equivalent,
            seed=seed,
            k=config.k,
        )
        for tier in tiers:
            cal = need(base_pipeline, "calibration", tier)
            views = _views(cal, list(bundle.calibration), bundle.handbook, config.k)
            scorer, tier_warnings = fit_scorer(
                config.scorer, tier, views, list(cal.correct), context
            )
            test = need(base_pipeline, "test", tier)
            scorer.auroc = evaluate_auroc(
                scorer,
                _views(test, list(bundle.test), bundle.handbook, config.k),
                list(test.correct),
            )
            aurocs[tier] = scorer.auroc
            scorers[tier] = scorer
            warnings.extend(tier_warnings)
        return ScorerBundle(scorers=scorers, warnings=warnings), aurocs

    def tier_runs(
        split_name: str,
        items: Sequence[DemoItem],
        scorer_bundle: ScorerBundle,
        config: CascadeConfig,
    ) -> list[TierRun]:
        """One tier's recorded answers under one configuration.

        Two things move with the configuration and both have to. The **cost** is every sample
        the scorer drew, because k samples at a tier is k generations and the workload pays for
        all of them. The **answer** is whichever generation the scorer returns, graded as the
        answer it returned — charging for a vote and then grading the first sample would credit
        the cascade with an accuracy it did not deliver.
        """
        out: list[TierRun] = []
        for tier in tiers:
            run = need(base_pipeline, split_name, tier)
            views = _views(run, items, bundle.handbook, config.k)
            scores = scorer_bundle.score(tier, views)
            scorer = scorer_bundle.scorers[tier]
            costs: list[Decimal] = []
            correct: list[int] = []
            for index, view in enumerate(views):
                drawn = run.sample_costs[index][: config.k] or (run.cost_usd[index],)
                costs.append(sum(drawn, Decimal(0)))
                chosen = scorer.answer_index(view)
                graded = run.sample_correct[index] or run.correct[index : index + 1]
                correct.append(graded[chosen])
            out.append(
                tier_run_from_arrays(
                    tier,
                    run.model_id,
                    run.task_ids,
                    correct,
                    costs,
                    scores,
                    scarce=registry.is_scarce(run.model_id),
                )
            )
        return out

    def evaluate_config(config: CascadeConfig) -> ConfiguredCascade:
        scorer_bundle, aurocs = fit_bundle(config)
        calibration_tiers = tier_runs(
            "calibration", list(bundle.calibration), scorer_bundle, config
        )
        found = search_thresholds(
            calibration_tiers,
            accuracy_floor=accuracy_floor,
            step=cascade_spec.threshold_step,
            objective=objective,
        )
        return ConfiguredCascade(config=config, scorers=scorer_bundle, aurocs=aurocs, search=found)

    candidates = [
        candidate
        for candidate in _configurations(cascade_spec, recorded_depth)
        if candidate.k <= recorded_depth
    ]
    if not candidates:
        raise ReportError(
            f"no scorer configuration is runnable: the cascade asks for samples the recorded "
            f"matrix does not hold (recorded depth {recorded_depth})."
        )
    evaluated = [evaluate_config(candidate) for candidate in candidates]
    chosen = _best_configuration(evaluated, objective, cascade_spec.searchable_scorers())

    bundle_scorers = chosen.scorers
    aurocs = chosen.aurocs
    config = chosen.config
    search: SearchResult = chosen.search

    # ---------------------------------------------------------------- 3. simulate on test
    test_tiers = tier_runs("test", list(bundle.test), bundle_scorers, config)
    cascade_outcome = simulate(test_tiers, search.thresholds)
    test_frontier_entries = []
    for thresholds, _accuracy, _cost, _scarce in search.frontier:
        outcome = simulate(test_tiers, thresholds)
        per_task = outcome.total_cost / outcome.n if outcome.n else Decimal(0)
        test_frontier_entries.append((thresholds, outcome.accuracy, per_task, outcome.scarce_share))

    # ---------------------------------------------------------------- 5. arms and proof
    def arm_from_run(run: ReplayedRun, label: str) -> ArmResult:
        # Measured over the same per-task costs the arm totals, not the run total: the run
        # total includes pre-warming, and a share against a different denominator exceeds 100%.
        per_task_total = sum(run.cost_usd, Decimal(0))
        scarce = per_task_total if registry.is_scarce(run.model_id) else Decimal(0)
        return ArmResult(
            pipeline_id=run.pipeline_id,
            label=label,
            task_ids=run.task_ids,
            correct=run.correct,
            cost_usd=run.cost_usd,
            model_ids=(run.model_id,),
            scarce_cost_usd=scarce,
            origin=run.origin,
        )

    baseline_run = need(BASELINE_PIPELINE, "test", tiers[-1])
    baseline_arm = arm_from_run(baseline_run, workload.pipeline(BASELINE_PIPELINE).name)

    candidate_arm = ArmResult(
        pipeline_id=CANDIDATE_CASCADE,
        label=cascade_spec.name,
        task_ids=cascade_outcome.task_ids,
        correct=cascade_outcome.correct,
        cost_usd=cascade_outcome.per_task_cost,
        model_ids=tuple(registry.roles[t] for t in tiers),
        scarce_cost_usd=cascade_outcome.scarce_cost,
        resolved_tier=tuple(tiers[i] for i in cascade_outcome.resolved_tier_index),
        origin=origin,
    )

    # Every category excludes pre-warming, which is its own line. Counting it in both would
    # inflate the proof cost and understate how quickly the saving repays it.
    def spend(run: ReplayedRun) -> Decimal:
        return run.total_cost - run.prewarm_cost

    calibration_cost = sum(
        (spend(r) for (p, s_, _), r in runs.items() if s_ == "calibration" and p == base_pipeline),
        Decimal(0),
    )
    other_cost = sum(
        (
            spend(r)
            for (p, s_, _), r in runs.items()
            if s_ == "test" and p not in (BASELINE_PIPELINE, base_pipeline)
        ),
        Decimal(0),
    )
    matrix_cost = sum(
        (spend(r) for (p, s_, _), r in runs.items() if s_ == "test" and p == base_pipeline),
        Decimal(0),
    )
    prewarm_cost = sum((r.prewarm_cost for r in runs.values()), Decimal(0))

    def sampling_spend(k: int) -> Decimal:
        """What drawing k generations per task costs beyond the first, across the matrix.

        It belongs on the scorer line and *only* there: `spend()` counts sample 0, so adding
        the repeats to the matrix lines as well would double-count them in `ProofCost.total`.
        Zero at k=1, which is what this line has always read.
        """
        return sum(
            (
                cost
                for (pipeline, _, _), run in runs.items()
                if pipeline == base_pipeline
                for task_samples in run.sample_costs
                for cost in task_samples[1:k]
            ),
            Decimal(0),
        )

    # Charged for the samples the chosen configuration actually draws, which is zero for a
    # scorer that reads one call. The repeats beyond that were spent comparing scorers, not
    # proving this cascade beats the baseline, and loading them onto this line would make the
    # shipped optimization look more expensive than it is — a misattribution, not caution. What
    # the comparison cost is reported as its own figure below, where it was incurred.
    sampling_cost = sampling_spend(config.k)
    proof_cost = ProofCost(
        baseline_run_usd=spend(baseline_run),
        calibration_runs_usd=calibration_cost,
        candidate_run_usd=matrix_cost,
        scorer_and_judge_usd=sampling_cost,
        prewarming_usd=prewarm_cost,
        other_pipelines_usd=other_cost,
    )

    items_by_id = {item.id: item for item in bundle.test}
    # An explicit margin overrides the workload's. `tokop prove --margin` needs this: printing
    # a margin the verdict was not computed against would be a wrong displayed number.
    effective_margin = workload.margin if margin is None else margin
    proof = build_proof(
        baseline_arm,
        candidate_arm,
        proof_cost,
        {"calibration": len(bundle.calibration), "test": len(bundle.test)},
        margin=effective_margin,
        seed=seed,
        resamples=resamples,
        items_by_id=items_by_id,
        scorer_auroc=aurocs,
    )

    # ---------------------------------------------------------------- 6. waterfall
    waterfall_arms = [baseline_arm]
    for pipeline_id in ("B1",):
        if (pipeline_id, "test", tiers[-1]) in runs:
            run = runs[(pipeline_id, "test", tiers[-1])]
            waterfall_arms.append(arm_from_run(run, workload.pipeline(pipeline_id).name))
    waterfall_arms.append(
        arm_from_run(need(base_pipeline, "test", tiers[-1]), workload.pipeline(base_pipeline).name)
    )
    waterfall_arms.append(candidate_arm)
    waterfall = build_waterfall(
        waterfall_arms, margin=effective_margin, seed=seed, resamples=resamples
    )

    # ------------------------------------------------- 6b. the same proof, scorer by scorer
    #
    # Every configuration the calibration search considered, carried through to a full test
    # result with the *same* baseline, the same margin, the same seed and the same statistics
    # code. This is what answers whether a scorer pays for the sampling it costs: the quality
    # it buys and the money it spends are both here, in the same row.
    #
    # Each row is charged for the samples that row draws, so the comparison isolates the
    # per-scorer trade. That is deliberately not the same as the headline proof cost above,
    # which carries the whole recorded depth; the difference is the cost of running the search
    # itself, and it is attributed where it was incurred rather than to whichever setting won.
    scorer_comparison = []
    for candidate in evaluated:
        candidate_tiers = tier_runs("test", list(bundle.test), candidate.scorers, candidate.config)
        candidate_outcome = simulate(candidate_tiers, candidate.search.thresholds)
        arm = ArmResult(
            pipeline_id=CANDIDATE_CASCADE,
            label=f"{cascade_spec.name} ({candidate.config.label})",
            task_ids=candidate_outcome.task_ids,
            correct=candidate_outcome.correct,
            cost_usd=candidate_outcome.per_task_cost,
            model_ids=tuple(registry.roles[t] for t in tiers),
            scarce_cost_usd=candidate_outcome.scarce_cost,
            resolved_tier=tuple(tiers[i] for i in candidate_outcome.resolved_tier_index),
            origin=origin,
        )
        row_proof = build_proof(
            baseline_arm,
            arm,
            ProofCost(
                baseline_run_usd=spend(baseline_run),
                calibration_runs_usd=calibration_cost,
                candidate_run_usd=matrix_cost,
                scorer_and_judge_usd=sampling_spend(candidate.config.k),
                prewarming_usd=prewarm_cost,
                other_pipelines_usd=other_cost,
            ),
            {"calibration": len(bundle.calibration), "test": len(bundle.test)},
            margin=effective_margin,
            seed=seed,
            resamples=resamples,
            items_by_id=items_by_id,
            scorer_auroc=candidate.aurocs,
        ).as_dict()
        scorer_comparison.append(
            {
                **candidate.config.as_dict(),
                "is_chosen": candidate.config == config,
                "thresholds": list(candidate.search.thresholds),
                "calibration_feasible": candidate.search.feasible,
                "calibration_cost_per_task_usd": str(candidate.calibration_cost_per_task()),
                "accuracy": row_proof["candidate"]["accuracy"],
                "cost_per_successful_task": row_proof["candidate"]["cost_per_successful_task"],
                "cost_per_task_usd": row_proof["candidate"]["cost_per_task_usd"],
                "scarce_share": row_proof["candidate"]["scarce_share"],
                "tier_shares": row_proof["candidate"]["tier_shares"],
                "delta_accuracy": row_proof["delta_accuracy"],
                "cost_ratio": row_proof["cost_ratio"],
                "mcnemar": row_proof["mcnemar"],
                "verdict": row_proof["verdict"],
                "proof_cost_usd": row_proof["proof_cost"]["total_usd"],
                "sampling_cost_usd": row_proof["proof_cost"]["scorer_and_judge_usd"],
                "repayment_tasks": row_proof["repayment_tasks"],
                "scorer_auroc": candidate.aurocs,
            }
        )

    # ---------------------------------------------------------------- 7. findings and lint
    frontier_model = registry.model(registry.roles[tiers[-1]])
    # Count with the counter the fixtures were built with, not with whichever one this machine
    # happens to have. The lint's dollar projections end up in docs/DEMO.md, so an ambient
    # counter made the same repository generate a different document on a machine with egress to
    # the tokenizer vocabulary than on one without (DECISIONS.md D26). When the recorded counter
    # cannot be loaded here, `counter_named` falls back and `token_counter_matches_fixtures`
    # below reports that the numbers are not the recorded ones.
    report_counter = counter_named(manifest.get("base_token_counter", base_counter_name()))
    lint_context = LintContext(
        counter=report_counter,
        price=frontier_model.price,
        min_cacheable_tokens=frontier_model.min_cacheable_tokens or 512,
        observed_output_tokens=(
            sum(c.total_output for c in baseline_run.calls if not c.prewarm)
            / max(1, len([c for c in baseline_run.calls if not c.prewarm]))
        ),
        needed_output_tokens=NEEDED_OUTPUT_TOKENS,
        tool_use_system_prompt_tokens=frontier_model.tool_use_system_prompt_tokens or 0,
    )
    variables = {
        "handbook": bundle.handbook,
        "question": bundle.test[0].question,
        "timestamp": demo_timestamp(),
    }
    lint_by_pipeline: dict[str, list[Finding]] = {}
    clear_by_pipeline: dict[str, dict[str, int]] = {}
    for pipeline_id in sorted(workload.pipelines):
        request = workload.pipeline(pipeline_id).render(
            "anthropic", frontier_model.model_id, variables
        )
        found = lint(request, lint_context)
        lint_by_pipeline[pipeline_id] = found
        clear_by_pipeline[pipeline_id] = clear_score(found)

    baseline_stats = RunStats(
        pipeline_id=BASELINE_PIPELINE,
        split="test",
        model_id=baseline_run.model_id,
        price=frontier_model.price,
        calls=baseline_run.calls,
        task_count=baseline_run.n,
        successes=sum(baseline_run.correct),
        needed_output_tokens=NEEDED_OUTPUT_TOKENS,
    )
    found = workload_findings(baseline_stats)
    batch = batch_finding(baseline_stats, workload.latency_sensitive)
    if batch:
        found.append(batch)

    cal_frontier = need(base_pipeline, "calibration", tiers[-1])
    cal_cheap = need(base_pipeline, "calibration", tiers[0])
    cheap_ids = {t for t, c in zip(cal_cheap.task_ids, cal_cheap.correct, strict=True) if c}
    frontier_ids = {
        t for t, c in zip(cal_frontier.task_ids, cal_frontier.correct, strict=True) if c
    }
    w04 = cheaper_tier_finding(
        RunStats(
            pipeline_id=base_pipeline,
            split="calibration",
            model_id=cal_frontier.model_id,
            price=frontier_model.price,
            calls=cal_frontier.calls,
            task_count=cal_frontier.n,
            successes=sum(cal_frontier.correct),
            needed_output_tokens=NEEDED_OUTPUT_TOKENS,
        ),
        RunStats(
            pipeline_id=base_pipeline,
            split="calibration",
            model_id=cal_cheap.model_id,
            price=registry.model(cal_cheap.model_id).price,
            calls=cal_cheap.calls,
            task_count=cal_cheap.n,
            successes=sum(cal_cheap.correct),
            needed_output_tokens=NEEDED_OUTPUT_TOKENS,
        ),
        cheap_ids,
        frontier_ids,
    )
    if w04:
        found.append(w04)
    all_findings = rank([*found, *lint_by_pipeline[BASELINE_PIPELINE]])

    # ---------------------------------------------------------------- 8. payload
    state = describe_recording()
    data: dict[str, Any] = {
        "generated_by": "tokop report",
        "workload": {
            "id": workload.id,
            "name": workload.name,
            "description": workload.description,
            "margin": effective_margin,
            "workload_margin": workload.margin,
            "latency_sensitive": workload.latency_sensitive,
            "dataset": {
                "size": len(bundle.items),
                "calibration": len(bundle.calibration),
                "test": len(bundle.test),
                "seed": bundle.seed,
            },
        },
        "provenance": {
            "fixture_source": state.fixture_source,
            "is_test_data": state.is_test_data,
            "recording_state": state.state,
            "recording_reason": state.reason,
            "origin": origin,
            "recorded_at": manifest.get("recorded_at"),
            "note": manifest.get("note", ""),
            "model_ids": {role: registry.roles[role] for role in tiers},
            "price_snapshot_id": snapshot.snapshot_id,
            "prices_verified": snapshot.all_verified,
            "unverified_models": snapshot.unverified_models(),
            # The counter that produced the numbers is the one the fixtures were built with,
            # not whichever one happens to be loadable now: `o200k_base` is downloaded on first
            # use, so the same repository reports different token counts on a machine with
            # egress to the vocabulary host than on one without (DECISIONS.md D26). Naming the
            # ambient counter here made the README's generated block differ by environment.
            # The counter these numbers were actually computed with.
            "base_token_counter": report_counter.name,
            "recorded_token_counter": manifest.get("base_token_counter", report_counter.name),
            "live_token_counter": base_counter_name(),
            "token_counter_matches_fixtures": (
                manifest.get("base_token_counter", report_counter.name) == report_counter.name
            ),
            "git_sha": manifest.get("runs", [{}])[0].get("git_sha", "unknown"),
        },
        "proof": proof.as_dict(),
        "waterfall": [step.as_dict() for step in waterfall],
        "cascade": {
            **search.as_dict(),
            "tiers": [
                {
                    "tier": tier,
                    "model_id": registry.roles[tier],
                    "scarce": registry.is_scarce(registry.roles[tier]),
                    "threshold": search.thresholds[i],
                    # Three different shares, because conflating them is how a graph node ends
                    # up sized by the wrong quantity: how many tasks *stopped* here, how many
                    # *reached* here at all, and what share of the money this tier spent.
                    "share_of_tasks": cascade_outcome.tier_shares(len(tiers))[i],
                    "share_of_attempts": cascade_outcome.tier_attempt_shares()[i],
                    "share_of_cost": cascade_outcome.tier_cost_shares()[i],
                    "cost_usd": str(cascade_outcome.tier_cost[i]),
                    "attempts": cascade_outcome.tier_attempts[i],
                    "auroc": aurocs.get(tier),
                    "p50_latency_ms": _p50(
                        [
                            c.latency_ms
                            for c in need(base_pipeline, "test", tier).calls
                            if not c.prewarm
                        ]
                    ),
                    "latency_is_recorded": True,
                }
                for i, tier in enumerate(tiers)
            ],
            "reached_frontier_share": cascade_outcome.tier_shares(len(tiers))[-1],
            "scorer_choice": {
                **config.as_dict(),
                "chosen_on": "calibration",
                "considered": [candidate.as_dict() for candidate in candidates],
                "adoptable": list(cascade_spec.searchable_scorers()),
                "recorded_sample_depth": recorded_depth,
                # What recording the extra generations cost, so that comparing scorers is not a
                # free lunch on paper. It is not part of the proof cost above: that proof does
                # not need them, and this one does not disappear because it is inconvenient.
                "search_recording_cost_usd": str(sampling_spend(recorded_depth)),
            },
            "scorer_comparison": scorer_comparison,
            "scorers": bundle_scorers.as_dict(),
            "frontier_chart": [
                {
                    "thresholds": list(thresholds),
                    "accuracy": accuracy,
                    "cost_per_task_usd": str(cost),
                    "scarce_share": scarce,
                    "is_operating_point": thresholds == search.thresholds,
                }
                for thresholds, accuracy, cost, scarce in test_frontier_entries
            ],
            "pareto": [
                {"thresholds": list(t), "accuracy": a, "cost_per_task_usd": str(c)}
                for t, a, c, _ in pareto_frontier(test_frontier_entries)
            ],
        },
        "findings": [f.as_dict() for f in all_findings],
        "lint": {
            pipeline_id: {
                "findings": [f.as_dict() for f in findings],
                "clear": clear_by_pipeline[pipeline_id],
                "clear_total": sum(clear_by_pipeline[pipeline_id].values()),
            }
            for pipeline_id, findings in lint_by_pipeline.items()
        },
        "pipelines": {
            pipeline_id: {
                "name": spec.name,
                "description": spec.description,
                "model_role": spec.model_role,
                "max_tokens": spec.max_tokens,
                "output_contract": spec.output_contract,
                "notes": spec.notes,
                "known_antipatterns": spec.known_antipatterns,
            }
            for pipeline_id, spec in workload.pipelines.items()
        },
        "runs": [
            {
                "pipeline": key[0],
                "split": key[1],
                "tier": key[2],
                "model_id": run.model_id,
                "n": run.n,
                "accuracy": run.accuracy,
                "total_cost_usd": str(run.total_cost),
                "prewarm_cost_usd": str(run.prewarm_cost),
                "cache_read_tokens": sum(c.cache_read for c in run.calls),
                "cache_write_tokens": sum(c.cache_write_5m + c.cache_write_1h for c in run.calls),
                "input_tokens": sum(c.total_input for c in run.calls),
                "output_tokens": sum(c.total_output for c in run.calls),
                "calls": len([c for c in run.calls if not c.prewarm]),
                "prewarm_calls": len([c for c in run.calls if c.prewarm]),
                # Replayed calls are excluded: a replayed latency describes a disk read, not a
                # provider (SPEC.md 7.1). None when every call in the run was replayed, which
                # the UI renders as "not measurable in replay" rather than as a missing row.
                "p50_latency_ms": _p50(
                    [c.latency_ms for c in run.calls if not c.prewarm and not c.reused]
                ),
                "recorded_latency_p50_ms": _p50([c.latency_ms for c in run.calls if not c.prewarm]),
                "origin": run.origin,
            }
            for key, run in sorted(runs.items())
        ],
    }
    return ReportPayload(data)


@lru_cache(maxsize=2)
def fitted_cascade(
    protect_scarce: bool = False,
) -> tuple[ScorerBundle, tuple[float, ...], tuple[str, ...]]:
    """The fitted scorers, the chosen thresholds and the tier order.

    Built by the same path as the report and cached alongside it, so the trace drawer shows the
    decision the cascade *actually* made rather than re-deriving one — or, worse, inferring it
    from the grade, which would put a gold-derived fact under a label that claims to be router
    output.
    """
    payload = cached_report(protect_scarce)
    registry = load_registry()
    workload = load_workload(repo_root() / "data/demo/workload.yaml")
    bundle = build_dataset(
        size=workload.dataset.size,
        calibration_size=workload.dataset.calibration_size,
        seed=workload.dataset.seed,
    )
    cascade_spec = workload.cascade(CANDIDATE_CASCADE)
    tiers = tuple(cascade_spec.tiers)
    root = _fixture_root()
    manifest = _load_manifest(root)
    store = CassetteStore(root / "cassettes")
    snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
    origin = str(manifest.get("origin", "simulated"))

    scorers: dict[str, Scorer] = {}
    for tier in tiers:
        cal = replay_run(
            workload,
            registry,
            bundle,
            snapshot,
            store,
            cascade_spec.base_pipeline,
            "calibration",
            tier,
            origin,
        )
        views = _views(cal, list(bundle.calibration), bundle.handbook)
        scorer, _ = fit_scorer(
            cascade_spec.scorer,
            tier,
            views,
            list(cal.correct),
            ScorerContext(
                feature_names=tuple(workload.scorer_features),
                seed=DEFAULT_SEED,
                k=cascade_spec.scorer_samples,
            ),
        )
        scorers[tier] = scorer
    thresholds = tuple(float(t) for t in payload["cascade"]["thresholds"])
    return ScorerBundle(scorers=scorers), thresholds, tiers


def trace_for(task_id: str) -> dict[str, Any]:
    """Every call every pipeline made for one task, for the trace drawer (SPEC.md 5.1).

    Replayed from cassettes on demand rather than held in the report: a full trace for 200
    tasks across 8 runs is far more data than any screen needs at once.
    """
    registry = load_registry()
    workload = load_workload(repo_root() / "data/demo/workload.yaml")
    bundle = build_dataset(
        size=workload.dataset.size,
        calibration_size=workload.dataset.calibration_size,
        seed=workload.dataset.seed,
    )
    item = next((i for i in bundle.items if i.id == task_id), None)
    if item is None:
        raise ReportError(f"no task {task_id!r} in this workload")

    root = _fixture_root()
    manifest = _load_manifest(root)
    store = CassetteStore(root / "cassettes")
    snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
    in_calibration = any(i.id == task_id for i in bundle.calibration)
    split_name = "calibration" if in_calibration else "test"
    handbook = bundle.handbook

    scorer_bundle, thresholds, tier_order = fitted_cascade(False)
    threshold_by_tier = dict(zip(tier_order, thresholds, strict=True))

    calls: list[dict[str, Any]] = []
    for entry in manifest.get("runs", []):
        if str(entry["split"]) != split_name:
            continue
        pipeline_id = str(entry["pipeline"])
        tier = str(entry["tier"])
        model_id = registry.roles[tier]
        request = workload.pipeline(pipeline_id).render(
            "simulated",
            model_id,
            {"handbook": handbook, "question": item.question, "timestamp": demo_timestamp()},
        )
        cassette = store.get(request.cassette_key())
        if cassette is None:
            continue
        outcome = grade(cassette.response_text, item.gold, item.answer_type, item.aliases)
        breakdown = snapshot.cost(cassette.model, cassette.usage)
        view = TaskView(
            task_id=task_id,
            question=item.question,
            output=cassette.response_text,
            context=handbook,
            tier=tier,
            output_tokens=cassette.usage.total_output,
        )
        features = {
            name: FEATURE_REGISTRY[name].fn(view)
            for name in workload.scorer_features
            if name in FEATURE_REGISTRY
        }
        # The scorer's own estimate, and the threshold it was compared against. This is the
        # route decision: it is a function of the scorer alone and knows nothing about gold.
        score: float | None = None
        threshold: float | None = None
        decision = "not routed"
        is_last_tier = tier == tier_order[-1]
        if tier in scorer_bundle.scorers:
            score = float(scorer_bundle.score(tier, [view])[0])
            threshold = threshold_by_tier.get(tier)
            if pipeline_id != workload.cascade(CANDIDATE_CASCADE).base_pipeline:
                decision = "not part of the cascade"
            elif is_last_tier:
                decision = "final tier: answers whatever it produced"
            elif threshold is not None and score >= threshold:
                decision = f"answered here: score {score:.2f} >= threshold {threshold:.2f}"
            else:
                decision = (
                    f"escalated: score {score:.2f} < threshold {threshold:.2f}"
                    if threshold is not None
                    else "escalated"
                )
        calls.append(
            {
                "pipeline": pipeline_id,
                "tier": tier,
                "model_id": model_id,
                "prompt_hash": request.prompt_hash()[:16],
                "cassette_key": cassette.key[:16],
                "reused": True,
                "system_preview": request.system_text[:600],
                "user_preview": (request.messages[0].text[:600] if request.messages else ""),
                "static_prefix_chars": len(request.static_prefix_text),
                "max_tokens": request.max_tokens,
                "response": cassette.response_text,
                "usage": {
                    bucket: {"tokens": value, "source": source}
                    for bucket, value, source in cassette.usage.bucket_items()
                },
                "raw_usage": cassette.raw_usage,
                "total_input": cassette.usage.total_input,
                "total_output": cassette.usage.total_output,
                "cost_usd": str(breakdown.total),
                "cost_formula": breakdown.formula(),
                "price_snapshot_id": snapshot.snapshot_id,
                "latency_ms": cassette.latency_ms,
                "ttft_ms": cassette.ttft_ms,
                "scorer_features": features,
                "scorer_score": score,
                "scorer_threshold": threshold,
                "route_decision": decision,
                "grade": outcome.as_dict(),
                "origin": cassette.origin,
            }
        )

    return {
        "task": {
            "id": item.id,
            "question": item.question,
            "gold": item.gold,
            "answer_type": item.answer_type,
            "question_type": item.question_type,
            "sections": list(item.sections),
            "split": split_name,
        },
        "calls": calls,
        "note": (
            "The question type and supporting sections are shown for analysis. The router "
            "never sees either, and the route decision below is a function of the scorer alone."
        ),
        "thresholds": {tier: threshold_by_tier[tier] for tier in tier_order},
    }


@lru_cache(maxsize=4)
def cached_report(protect_scarce: bool = False) -> ReportPayload:
    """The report, computed once per process. The API serves this."""
    return build_report(protect_scarce=protect_scarce)


def clear_cache() -> None:
    cached_report.cache_clear()


# --------------------------------------------------------------------------- README block

METRICS_START = "<!-- metrics:start -->"
METRICS_END = "<!-- metrics:end -->"


def metrics_block(payload: ReportPayload) -> str:
    """The README's metrics block, generated from the report.

    SPEC.md non-negotiable 1: no metric literals in docs. Everything between the markers is
    written by this function and checked by ``tokop report --check``.
    """
    proof = payload["proof"]
    provenance = payload["provenance"]
    cascade = payload["cascade"]
    base = proof["baseline"]
    cand = proof["candidate"]
    mark = "~" if provenance["is_test_data"] else "▪"
    kind = (
        "simulated test data, not a recording"
        if provenance["is_test_data"]
        else f"recorded {provenance['recorded_at']}"
    )

    lines = [
        METRICS_START,
        "<!-- generated by `tokop report --write-readme`; do not edit by hand -->",
        "",
        f"**{payload['workload']['name']}** — {payload['workload']['dataset']['size']} tasks, "
        f"{payload['workload']['dataset']['calibration']} calibration and "
        f"{payload['workload']['dataset']['test']} test. {kind}.",
        "",
        "| Pipeline | Accuracy (95% CI) | Cost per successful task | Scarce-model share |",
        "| --- | --- | --- | --- |",
    ]
    for step in payload["waterfall"]:
        accuracy = step["accuracy"]
        cost = step["cost_per_successful_task"]
        lines.append(
            f"| {step['pipeline']} {step['label']} | "
            f"{accuracy['point'] * 100:.1f}% ({accuracy['low'] * 100:.1f}–"
            f"{accuracy['high'] * 100:.1f}) | "
            f"${float(cost['point']):.5f} {mark} (${float(cost['low']):.5f}–"
            f"${float(cost['high']):.5f}) | {step['scarce_share'] * 100:.0f}% |"
        )

    reduction = proof["cost_reduction"]
    lines += [
        "",
        f"**Verdict: {proof['verdict']['display']}**",
        "",
        f"> {proof['verdict']['sentence']}",
        "",
        "- Cost per successful task falls from "
        f"${float(base['cost_per_successful_task']['point']):.5f} to "
        f"${float(cand['cost_per_successful_task']['point']):.5f}"
        + (f", a {reduction * 100:.1f}% reduction." if reduction is not None else "."),
        f"- {cascade['reached_frontier_share'] * 100:.0f}% of tasks reached the frontier model "
        f"({provenance['model_ids']['frontier']}).",
        f"- McNemar exact p = {proof['mcnemar']['p_value']:.3f} on "
        f"{proof['mcnemar']['discordant']} discordant pairs.",
        f"- The proof itself cost ${float(proof['proof_cost']['total_usd']):.4f}"
        + (
            f" and repays after {proof['repayment_tasks']:,} tasks."
            if proof["repayment_tasks"]
            else " and does not repay, because the candidate is not cheaper."
        ),
        f"- Prices: {provenance['price_snapshot_id']}, "
        + (
            "all verified against the provider's own page."
            if provenance["prices_verified"]
            else f"unverified for {', '.join(provenance['unverified_models'])}."
        ),
        "",
        f"<sub>{mark} "
        + (
            "simulated: these numbers are real engine output computed over simulated traces, "
            "because this build has no API credentials. See DECISIONS.md D1."
            if provenance["is_test_data"]
            else "provider-reported usage."
        )
        + f" Token estimates use `{provenance['base_token_counter']}`.</sub>",
        METRICS_END,
    ]
    return "\n".join(lines)


def write_readme_metrics(payload: ReportPayload, readme: Path | None = None) -> Path:
    path = readme or repo_root() / "README.md"
    block = metrics_block(payload)
    if not path.exists():
        path.write_text(f"# Tokop\n\n{block}\n")
        return path
    text = path.read_text()
    if METRICS_START in text and METRICS_END in text:
        start = text.index(METRICS_START)
        end = text.index(METRICS_END) + len(METRICS_END)
        text = text[:start] + block + text[end:]
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text)
    return path


def readme_metrics_current(payload: ReportPayload, readme: Path | None = None) -> tuple[bool, str]:
    path = readme or repo_root() / "README.md"
    if not path.exists():
        return False, f"{path} does not exist"
    text = path.read_text()
    if METRICS_START not in text or METRICS_END not in text:
        return False, "the README has no metrics block; run `tokop report --write-readme`"
    start = text.index(METRICS_START)
    end = text.index(METRICS_END) + len(METRICS_END)
    current = text[start:end]
    expected = metrics_block(payload)
    if current.strip() == expected.strip():
        return True, "the README metrics block is current"
    return False, "the README metrics block is stale; run `tokop report --write-readme`"
