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
from tokop.core.stats import DEFAULT_SEED, StatsError
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
from tokop.optimize.entailment import (
    EntailmentError,
    measure_correlation,
    parse_entailment,
)
from tokop.optimize.evidence import assess
from tokop.optimize.findings import (
    CallRow,
    RunStats,
    batch_finding,
    cheaper_tier_finding,
    rank,
    workload_findings,
)
from tokop.optimize.graph import compile_graph
from tokop.optimize.lint import Finding, LintContext, clear_score, lint
from tokop.optimize.proof import (
    ArmResult,
    Proof,
    ProofCost,
    build_judged_delta,
    build_proof,
    build_waterfall,
)
from tokop.optimize.pseudolabels import (
    GOLD,
    MAJORITY_VOTE,
    PENALIZED_V1,
    PseudoLabels,
    build_pseudo_labels,
    pseudo_label_kind,
)
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
from tokop.optimize.ties import Candidate, find_ties
from tokop.paths import fixtures_dir, repo_root
from tokop.recording_state import describe as describe_recording
from tokop.workloads.bundle import Bundle, load_bundle
from tokop.workloads.grading import answers_equivalent, grade
from tokop.workloads.item import Item
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
    def recorded_cost(self) -> Decimal:
        """Every dollar this run spent, repeats included.

        ``total_cost`` is the one generation per task the pipeline makes. When the matrix was
        recorded deeper so that scorers could be compared, this is the larger, true figure —
        reported so that the runs block and the recorder's own total agree instead of leaving a
        gap a reader would have to discover.
        """
        repeats = sum(
            (cost for task_samples in self.sample_costs for cost in task_samples[1:]),
            Decimal(0),
        )
        return self.total_cost + repeats

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


#: The workload every entry point reports on unless told otherwise. Named once rather than
#: spelled out at six call sites, because six spellings is how five of them stay the demo's.
DEFAULT_WORKLOAD = "data/demo/workload.yaml"


def load_reported_workload(workload_path: str | None = None) -> WorkloadSpec:
    return load_workload(repo_root() / (workload_path or DEFAULT_WORKLOAD))


def _fixture_root(workload: WorkloadSpec | None = None) -> Path:
    """Where this workload's recording lives.

    A workload that names its own fixture directory gets it. The demo names none, and falls
    through to the recording state, which chooses between `fixtures/demo/` — a real recording —
    and `fixtures/test/`, the simulated set this build actually ships (UPGRADE_V4.md M15).
    """
    if workload is not None and workload.fixtures:
        return fixtures_dir() / workload.fixtures
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
    bundle: Bundle,
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
        grounding=bundle.grounding,
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
        for task_call in task.calls:
            request, response = task_call.request, task_call.response
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
                    step=task_call.step,
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


class ReplayEntailment:
    """An entailment judgement read back from the call that made it (UPGRADE_V3.md U6).

    The scorer asks "does A entail B"; this renders the workload's entailment prompt, finds the
    cassette that request was recorded under, and reads the verdict out of the reply. A missing
    cassette is a **fixture** problem and propagates; a reply the parser could not read is a
    *data* condition and raises ``EntailmentError``, which the clustering catches and counts as
    a pair it could not judge. The two are different and are kept different: one means re-record,
    the other means the model waffled.
    """

    def __init__(
        self,
        workload: WorkloadSpec,
        registry: Registry,
        store: CassetteStore,
        grounding: str,
        provider: str = "simulated",
    ) -> None:
        spec = workload.entailment
        if spec is None:
            raise ReportError(
                f"workload {workload.id!r} defines no `entailment:` block, so answers cannot be "
                "clustered by meaning. Add one, or drop `semantic-entropy-v1` from the grid."
            )
        self.spec = spec
        self.model_id = registry.roles[spec.model_role]
        self.store = store
        self.grounding = grounding
        self.provider = provider
        self.calls = 0
        self._cache: dict[tuple[str, str, str], bool] = {}

    def __call__(self, question: str, premise: str, hypothesis: str) -> bool:
        key = (question, premise, hypothesis)
        if key in self._cache:
            return self._cache[key]
        request = self.spec.render(
            self.provider,
            self.model_id,
            {
                "question": question,
                "answer_a": premise,
                "answer_b": hypothesis,
                "grounding": self.grounding,
                "timestamp": demo_timestamp(),
            },
        )
        cassette = self.store.get(request.cassette_key())
        if cassette is None:
            raise ReportError(
                f"no recorded entailment judgement for a pair on {question[:60]!r}. The "
                "response matrix was recorded without the entailment pass, or at a different "
                "sample depth. Run `tokop build-test-fixtures`."
            )
        self.calls += 1
        verdict = parse_entailment(cassette.response_text)
        if verdict is None:
            raise EntailmentError(
                "the entailment model's reply could not be read as a judgement; the two answers "
                "stay in separate clusters rather than being merged on a guess"
            )
        self._cache[key] = verdict
        return verdict


def entailment_price(manifest: dict[str, Any]) -> Decimal:
    """The mean recorded price of one entailment call.

    Charged per call a clustering makes. The recording paid for every pair a clustering *might*
    ask about, which is slightly more than any one clustering asks; that excess is reported as
    the recording's own cost rather than loaded onto a scorer that did not spend it — the same
    split the deeper sample matrix already gets (DECISIONS.md D27).
    """
    calls = int(manifest.get("entailment_calls", 0) or 0)
    if calls <= 0:
        return Decimal(0)
    return Decimal(str(manifest.get("entailment_recording_cost_usd", "0"))) / Decimal(calls)


def _views(
    run: ReplayedRun,
    items: Sequence[Item],
    grounding: str,
    samples: int = 1,
    keep: set[str] | None = None,
) -> list[TaskView]:
    """Task views for the scorer. No answer key, by construction.

    ``samples`` is how many recorded generations to expose. A scorer that does not sample
    never reads them; one that does gets exactly the k it is configured for, so two settings
    of k are compared over the same draws rather than over fresh ones.

    ``keep`` drops tasks the caller has decided cannot be labelled. Under label-free
    calibration that is the tasks whose answer distribution is close to uniform: their
    pseudo-label would be an accident of the draw, so they are excluded from fitting rather
    than fitted against noise (UPGRADE_V3.md U3).
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
        if keep is not None and task_id not in keep:
            continue
        item = by_id[task_id]
        drawn = samples_by_task.get(task_id, (outputs_by_task[task_id],))
        views.append(
            TaskView(
                task_id=task_id,
                question=item.question,
                output=outputs_by_task[task_id],
                context=grounding,
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
    """Every (scorer, k) to evaluate, given how deep the response matrix was recorded.

    Evaluating is not adopting: which of these the search may take as the operating point is a
    separate question, answered by the workload's `scorer_search_grid`.

    A scorer whose call count does not move with k is offered once, at k=1 — drawing repeats it
    never reads would be charged for nothing. A k deeper than the recording is dropped here
    rather than left to fail later with a matrix it cannot use.
    """
    out: list[CascadeConfig] = []
    for name in cascade_spec.scorer_choices():
        kind = scorer_kind(name)
        if kind.calls_per_task(2) == kind.calls_per_task(1):
            out.append(CascadeConfig(scorer=name, k=1))
            continue
        out.extend(
            CascadeConfig(scorer=name, k=k)
            for k in cascade_spec.sample_choices()
            if 2 <= k <= recorded_depth
        )
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


def _operating_point_note(
    config: CascadeConfig,
    considered: Sequence[CascadeConfig],
    search: SearchResult,
    adoptable: Sequence[str],
) -> str:
    """What was chosen on calibration, naming every dimension that was searched.

    Computed rather than fixed. The note is a claim about how the operating point was picked,
    and a claim that stops matching the search is worse than no note at all: a reader checks it
    to decide whether the test number means anything.
    """
    # Only settings the search was allowed to adopt count as things it chose between. The rest
    # are measured and reported, and saying the operating point was picked from them would
    # overstate what the search did.
    eligible = [c for c in considered if c.scorer in set(adoptable)]
    dimensions = [f"{search.evaluated:,} threshold settings"]
    if len({c.scorer for c in eligible}) > 1:
        dimensions.append(f"{len({c.scorer for c in eligible})} scorers")
    if len({c.k for c in eligible}) > 1:
        dimensions.append(f"sample counts {sorted({c.k for c in eligible})}")
    searched = (
        ", ".join(dimensions[:-1]) + f" and {dimensions[-1]}"
        if len(dimensions) > 1
        else dimensions[0]
    )
    note = (
        f"The operating point — {config.label}, thresholds "
        f"{', '.join(f'{t:.2f}' for t in search.thresholds[:-1])} — was chosen on the "
        f"calibration split from {searched}, before any test result was computed."
    )
    withheld = sorted({c.scorer for c in considered} - set(adoptable))
    if withheld:
        note += (
            f" {', '.join(withheld)} was measured alongside it and reported below, but this "
            "workload does not let the search adopt it."
        )
    return note


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


def _fixtures_exercise_pseudolabels(alternatives: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Whether this fixture set can say anything about label-free calibration at all.

    Computed rather than asserted, because the answer is currently no and pretending otherwise
    would be the D27 mistake repeated. The simulated provider draws wrong answers independently
    and each one is a *different* perturbation, so the correct answer is almost always the
    unique plurality across pooled generations and every pseudo-label agrees with gold. That
    makes the demo's agreement figure a statement about the noise model, not about the method.

    The failure U3 exists for — a plurality that is an artefact of a split distribution, or one
    tier repeating a wrong answer loudly — is exercised in ``tests/test_pseudolabels.py``
    against a constructed calibration set instead, where the majority vote is wrong on a known
    subset by construction.
    """
    agreements = [
        value
        for alternative in alternatives
        for value in alternative["label_agreement_with_gold"].values()
        if value is not None
    ]
    perfect = bool(agreements) and min(agreements) >= 0.999
    return {
        "min_label_agreement_with_gold": min(agreements) if agreements else None,
        "answer": not perfect,
        "reason": (
            "No. Every pseudo-label on this split agrees with the answer key, because the "
            "simulated provider draws wrong answers independently and each is a different "
            "perturbation — so the correct answer is the unique plurality almost every time and "
            "a majority vote cannot go wrong here. Agreement of 1.00 is a fact about the noise "
            "model, not evidence that the method works. The case it exists for is tested "
            "against a constructed calibration set in tests/test_pseudolabels.py."
            if perfect
            else "Yes: the pseudo-labels and the answer key disagree on this split, so the "
            "comparison above measures something."
        ),
    }


#: The interval width the checkable-contract lever is priced against (UPGRADE_V3.md U4). A
#: standard error of one accuracy point is a 95% interval of about plus or minus two, which is
#: the resolution a 3-point non-inferiority margin actually needs. Any fixed target works; the
#: comparison is between two judges at the *same* target, and the target is named so a reader
#: can see it is not the flattering one.
TARGET_STANDARD_ERROR = 0.01


def _horvitz_thompson(values: Sequence[float], rates: Sequence[float], n: int) -> float:
    """``(1/T) sum v_t / pi_t`` over the sampled items. The annotated subset is not a simple
    random sample, so a plain average over it would be weighted towards whatever the policy
    found interesting."""
    if n <= 0:
        return 0.0
    return sum(value / rate for value, rate in zip(values, rates, strict=True)) / n


def canary_observation(
    payload: ReportPayload, *, fraction: float, seed: int, workload_path: str | None = None
) -> tuple[int, float, float]:
    """Re-score a stratified subset of the current report: (size, delta, standard error).

    The subset is stratified by question type because a canary that happened to draw only
    lookups would miss exactly the drift worth catching — a cascade's risk lives in its hardest
    types, and those are its smallest strata.
    """
    from tokop.optimize.certificate import paired_delta, stratified_subset

    per_task = payload["proof"]["per_task"]
    task_ids = list(per_task["task_ids"])
    by_type = {b["question_type"]: b for b in payload["proof"]["by_type"]}
    workload = load_reported_workload(workload_path)
    bundle = load_bundle(workload)
    types = {item.id: item.question_type for item in bundle.test}
    strata = [types.get(task_id, "unknown") for task_id in task_ids]
    if not by_type:  # pragma: no cover - defensive; the demo always has types
        strata = ["all"] * len(task_ids)

    chosen = set(stratified_subset(task_ids, strata, fraction, seed))
    index = [i for i, task_id in enumerate(task_ids) if task_id in chosen]
    baseline = [int(per_task["baseline_correct"][i]) for i in index]
    candidate = [int(per_task["candidate_correct"][i]) for i in index]
    delta, standard_error = paired_delta(baseline, candidate)
    return len(index), delta, standard_error


def current_identity(payload: ReportPayload) -> dict[str, Any]:
    """What the world says today about the models, prices and modes a certificate rests on."""
    provenance = payload["provenance"]
    return {
        "model_snapshots": [
            {
                "role": role,
                "model_id": model_id,
                "snapshot": f"{model_id}@{provenance.get('recorded_at') or 'unrecorded'}",
            }
            for role, model_id in sorted(provenance["model_ids"].items())
        ],
        "price_snapshot_id": str(provenance["price_snapshot_id"]),
        "grading_mode": str(payload["workload"].get("grading", "gold")),
        "calibration_mode": str(payload["calibration"]["mode"]),
    }


def _template_space(workload: WorkloadSpec) -> list[str]:
    """Every template the workload's generator can produce, or none when nothing generated it.

    Coverage is a statement about a generator's space, so only a generator can supply it. The
    demo's is imported here and nowhere else — lazily, inside the branch that needs it, which is
    what lets a workload that was not generated run without importing the demo at all
    (UPGRADE_V4.md M15). A set that nothing templated gets an empty space, and
    `workloads/provenance.py` reports coverage as not measurable rather than as zero.
    """
    if workload.dataset.generator == "demo":
        from tokop.workloads.demo.generator import all_templates

        return [template.id for template in all_templates()]
    return []


def dataset_provenance_block(workload: WorkloadSpec, bundle: Bundle) -> dict[str, Any]:
    """Where the task set came from, and whether it can back a certificate (UPGRADE_V3.md U8).

    The declared origins come from the workload; the shape — template coverage, how much of the
    set sits in rarely-seen templates, how concentrated it is — is computed from the items. A
    workload that declares nothing gets a block saying so and is refused, because "we did not
    say" and "it is fine" have to look different.
    """
    from tokop.workloads.provenance import DeclaredProvenance, build_provenance

    declared_spec = workload.dataset_provenance
    declared = (
        DeclaredProvenance(
            real_traffic_items=declared_spec.real_traffic_items,
            model_generated_items=declared_spec.model_generated_items,
            program_generated_items=declared_spec.program_generated_items,
            generators=tuple(declared_spec.generators),
            decoding_budget=declared_spec.decoding_budget,
            relabelled_by_frozen_reference=declared_spec.relabelled_by_frozen_reference,
            real_traffic_accumulating=declared_spec.real_traffic_accumulating,
            notes=tuple(declared_spec.notes),
        )
        if declared_spec is not None
        else DeclaredProvenance(
            notes=(
                "This workload declares no `dataset_provenance:` block, so nothing is known "
                "about where its tasks came from.",
            )
        )
    )
    provenance = build_provenance(
        [item.template_id for item in bundle.items],
        _template_space(workload),
        declared,
    )
    return {**provenance.as_dict(), "declared": declared_spec is not None}


def _contract_block(
    root: Path,
    workload: WorkloadSpec,
    runs: dict[tuple[str, str, str], ReplayedRun],
    tier: str,
    origin: str,
) -> dict[str, Any]:
    """What a checkable output contract costs and what it buys (UPGRADE_V3.md U4).

    The trade, in one place, with both halves:

    * **what it costs** — the extra output tokens the derivation takes, and any accuracy the
      contract loses. Kirchner et al. call that second one a legibility tax and measure it;
      this row is allowed to show it as a negative number and is tested for not hiding it.
    * **what it buys** — agreement between the cheap judge and the strong grader. Higher
      agreement is a smaller ``E[(H - G)^2]``, which is a smaller sampling rate for a given
      interval width, which is fewer dollars of annotation.

    Both are measured over the same tasks with the same judge, so the comparison is between two
    contracts and not between two experiments.
    """
    from tokop.optimize.annotation import (
        AnnotationError,
        AnnotationSet,
        annotations_path,
        budget_for_standard_error,
    )

    path = annotations_path(root, contract=True)
    if not path.exists():
        return {
            "available": False,
            "reason": (
                f"no contract annotation set at {path.name}. Run `tokop annotate --contract` to "
                "judge the checkable pair and price what the contract buys."
            ),
        }
    try:
        annotations = AnnotationSet.read(path)
    except AnnotationError as exc:
        return {"available": False, "reason": str(exc)}

    pair = (annotations.baseline_pipeline, annotations.candidate_pipeline)
    missing = [p for p in pair if (p, "test", tier) not in runs]
    if missing:
        return {
            "available": False,
            "reason": (
                f"the contract annotation set judged {pair[0]} against {pair[1]}, and this "
                f"fixture set has no test run for {', '.join(missing)}."
            ),
        }

    drawn = [item for item in annotations.items if item.sampled and item.has_strong]
    if not drawn:
        return {
            "available": False,
            "reason": "no item in the contract annotation set has a strong label on both arms.",
        }
    rates = [item.rate for item in drawn]
    n = annotations.n
    cheap_per_item = Decimal(str(annotations.cost["judge_usd"])) / (Decimal(n) * 2)
    strong_per_item = Decimal(str(annotations.policy["cost_per_item_usd"])) / 2

    arms: list[dict[str, Any]] = []
    for pipeline_id, side in zip(pair, ("baseline", "candidate"), strict=True):
        verdicts = [getattr(item, side) for item in drawn]
        squared = [float((arm.judge_correct - (arm.strong_correct or 0)) ** 2) for arm in verdicts]
        mse = _horvitz_thompson(squared, rates, n)
        agreement = 1.0 - mse
        cost, rate = budget_for_standard_error(
            mse, n, TARGET_STANDARD_ERROR, cheap_per_item, strong_per_item
        )
        run = runs[(pipeline_id, "test", tier)]
        arms.append(
            {
                "pipeline": pipeline_id,
                "label": workload.pipeline(pipeline_id).name,
                "checkable": workload.pipeline(pipeline_id).checkable,
                "judge_agreement_with_strong_grader": agreement,
                "judge_mean_square_error": mse,
                "sampling_rate_for_target": rate,
                "annotation_cost_for_target_usd": str(cost),
                "accuracy": run.accuracy,
                "generation_cost_usd": str(run.total_cost - run.prewarm_cost),
                "output_tokens": sum(c.total_output for c in run.calls if not c.prewarm),
            }
        )

    before, after = arms
    accuracy_delta = after["accuracy"] - before["accuracy"]
    annotation_saving = Decimal(before["annotation_cost_for_target_usd"]) - Decimal(
        after["annotation_cost_for_target_usd"]
    )
    generation_premium = Decimal(after["generation_cost_usd"]) - Decimal(
        before["generation_cost_usd"]
    )
    return {
        "available": True,
        "baseline_pipeline": before["pipeline"],
        "candidate_pipeline": after["pipeline"],
        "target_standard_error": TARGET_STANDARD_ERROR,
        "n": n,
        "annotated": len(drawn),
        "judge": dict(annotations.judge),
        "strong_grader": dict(annotations.strong_grader),
        "arms": arms,
        # Reported with its sign, always. A row that showed the saving and hid the accuracy it
        # cost would be the single most tempting dishonesty in this product.
        "accuracy_delta": accuracy_delta,
        "agreement_delta": (
            after["judge_agreement_with_strong_grader"]
            - before["judge_agreement_with_strong_grader"]
        ),
        "annotation_saving_usd": str(annotation_saving),
        "generation_premium_usd": str(generation_premium),
        "net_on_evaluation_split_usd": str(annotation_saving - generation_premium),
        "pays_for_itself": annotation_saving > generation_premium,
        "note": (
            "The annotation saving is paid once per evaluation; the generation premium is paid "
            "on every task the pipeline ever runs. A contract that wins on this split can still "
            "lose in production, and the two figures are kept apart rather than netted into one "
            "number that hides which is which."
        ),
        "origin": annotations.origin,
    }


def _judged_block(
    workload: WorkloadSpec,
    root: Path,
    operating_point: dict[str, Any],
    proof: Proof,
    *,
    margin: float,
    seed: int,
    resamples: int,
    annotation_budget: Decimal | None,
    origin: str,
) -> dict[str, Any]:
    """The same comparison with the answer key withheld (UPGRADE_V3.md U1).

    Reported beside the gold one rather than instead of it, because the interesting number is
    the relationship between the two: an estimate that never sees a label, and the labelled
    truth it is supposed to land on. A workload that arrives with no labels gets only the first
    half, which is the point of the whole upgrade.

    Never raises. An annotation set that is missing or was drawn against a different operating
    point makes the block unavailable *with the reason and the command that fixes it*; a report
    that could not be built at all would hide which of those happened. ``make verify`` fails on
    an unavailable block for the demo, so a stale set cannot ship.
    """
    from tokop.optimize.annotation import AnnotationError, AnnotationSet, annotations_path

    if workload.judge is None:
        return {
            "available": False,
            "reason": f"workload {workload.id!r} defines no `judge:` block.",
        }
    path = annotations_path(root)
    if not path.exists():
        return {
            "available": False,
            "reason": (
                f"no annotation set at {path.name}. Run `tokop annotate` to buy the strong "
                "labels the estimator corrects the judge with."
            ),
        }
    try:
        annotations = AnnotationSet.read(path)
    except AnnotationError as exc:
        return {"available": False, "reason": str(exc)}

    drawn_for = annotations.operating_point
    mismatched = [
        f"{key} {drawn_for.get(key)!r} vs {operating_point.get(key)!r}"
        for key in ("scorer", "k", "thresholds", "protect_scarce")
        if drawn_for.get(key) != operating_point.get(key)
    ]
    if mismatched:
        return {
            "available": False,
            "stale": True,
            "reason": (
                "the annotation set was drawn against a different operating point ("
                + "; ".join(mismatched)
                + "). The cascade answers different tasks at different tiers under a different "
                "operating point, so those verdicts are about answers this cascade did not "
                "give. Re-run `tokop annotate`."
            ),
        }
    if (
        annotations.baseline_pipeline != proof.baseline.pipeline_id
        or annotations.candidate_pipeline != proof.candidate.pipeline_id
    ):
        return {
            "available": False,
            "stale": True,
            "reason": (
                f"the annotation set judged {annotations.baseline_pipeline} against "
                f"{annotations.candidate_pipeline}; this report compares "
                f"{proof.baseline.pipeline_id} with {proof.candidate.pipeline_id}."
            ),
        }

    try:
        if annotation_budget is not None:
            annotations = annotations.restrict_to_budget(annotation_budget)
        judged = build_judged_delta(
            annotations,
            margin=margin,
            seed=seed,
            resamples=resamples,
            caveats=_judged_caveats(workload, annotations, origin),
        )
    except (AnnotationError, StatsError) as exc:
        return {"available": False, "reason": str(exc)}

    payload = {"available": True, **judged.as_dict()}
    # Only computable here because this demo happens to have gold. A workload that arrives
    # unlabelled has no such check, which is exactly why the demo runs it: it is the evidence
    # that the gold-free path lands where the labelled one does.
    gold_delta = proof.delta_accuracy.point
    payload["coverage_check"] = {
        "gold_delta_accuracy": gold_delta,
        "judged_interval_covers_gold": judged.delta.contains(gold_delta),
        "judge_only_interval_covers_gold": judged.judge_only_delta.contains(gold_delta),
        "gold_delta_minus_judged": gold_delta - judged.delta.point,
        "note": (
            "This demo has gold answers, so the gold-free estimate can be checked against the "
            "labelled one. A workload that arrives unlabelled cannot run this check; it is "
            "here as evidence that the estimator lands where it claims to."
        ),
    }
    return payload


def _judged_caveats(workload: WorkloadSpec, annotations: Any, origin: str) -> list[str]:
    """What the judged estimate does not establish. Rendered verbatim wherever it appears."""
    caveats = [
        "The estimate is unbiased for the **strong grader's** mean, not for a perfect one. A "
        "strong grader that is itself wrong moves the target, and the inverse-probability "
        "weight cannot correct for that. What it does correct for, completely, is the cheap "
        "judge.",
        "The cascade's operating point was still calibrated against gold labels on the "
        "calibration split. Only the *test* comparison here is label-free. Calibrating without "
        "labels is UPGRADE_V3.md U3 and is not built yet.",
    ]
    if annotations.strong_grader.get("source") == "human":
        caveats.append(
            "The strong labels came from a human review queue, so they are as good as the "
            "review was. Tokop records who produced them and nothing about their quality."
        )
    if origin != "recorded":
        caveats.append(
            "Both the answers and the judge verdicts come from the deterministic simulated "
            "provider, not from a recording (DECISIONS.md D1). The judge's error rates are "
            "invented parameters; the estimator's unbiasedness does not depend on them, which "
            "is what `tests/test_judged_proof.py`'s adversarial judge demonstrates against an "
            "injected judge rather than against these fixtures."
        )
    return caveats


def build_report(
    *,
    workload_path: str | None = None,
    protect_scarce: bool = False,
    seed: int = DEFAULT_SEED,
    resamples: int = 5000,
    margin: float | None = None,
    annotation_budget: Decimal | None = None,
    calibration: str | None = None,
) -> ReportPayload:
    """Recompute every headline metric from the committed fixtures."""
    registry = load_registry()
    workload = load_reported_workload(workload_path)
    bundle = load_bundle(workload)
    root = _fixture_root(workload)
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
    recorded_depth = min(
        (
            need(base_pipeline, split, tier).sample_depth()
            for split in ("calibration", "test")
            for tier in tiers
        ),
        default=1,
    )

    # ------------------------------------------------- 2a. where calibration labels come from
    #
    # `gold` reads the dataset's answer key. Anything else stands in for one from the repeated
    # generations themselves (UPGRADE_V3.md U3): the workload arrived unlabelled, and the
    # cascade still has to choose thresholds somehow. The mode is a parameter rather than a
    # consequence of the grading mode alone, because the demo *has* an answer key and the
    # interesting number is how far the label-free calibration lands from the labelled one.
    calibration_mode = calibration or (GOLD if workload.grading == "gold" else PENALIZED_V1)

    def build_labels(kind: str) -> PseudoLabels:
        return build_pseudo_labels(
            {
                tier: dict(
                    zip(
                        need(base_pipeline, "calibration", tier).task_ids,
                        need(base_pipeline, "calibration", tier).sample_outputs,
                        strict=True,
                    )
                )
                for tier in tiers
            },
            answers_equivalent,
            kind=kind,
        )

    # The entailment judge, read back from the calls that made it. Built once: it caches, and
    # the same pair recurs across tiers and across configurations.
    entails = (
        ReplayEntailment(workload, registry, store, bundle.grounding)
        if workload.entailment is not None
        else None
    )
    entailment_call_price = entailment_price(manifest)

    pseudo = None if calibration_mode == GOLD else build_labels(calibration_mode)

    def kept_and_items(labels: PseudoLabels | None) -> tuple[set[str] | None, list[Item]]:
        keep = None if labels is None else set(labels.kept)
        items = [item for item in bundle.calibration if keep is None or item.id in keep]
        if labels is not None and not items:
            raise ReportError(
                f"{labels.kind} excluded every calibration task: no answer distribution on this "
                "split carries a usable consensus. There is nothing to fit thresholds on."
            )
        return keep, items

    # Called once here so a label source that excludes everything fails immediately, with the
    # kind that did it, rather than at the first fit.
    kept_and_items(pseudo)

    def calibration_labels(
        run: ReplayedRun,
        views: Sequence[TaskView],
        scorer: Scorer | None,
        labels: PseudoLabels | None,
    ) -> list[int]:
        """The label for each kept calibration task at one tier.

        With gold, it is the checker's verdict on the answer the tier returned. Without, it is
        whether that answer agrees with the consensus the other tiers reached — the same answer
        in both cases, so a scorer that returns a different generation is labelled on what it
        returned rather than on what it drew first.
        """
        index_of = {task_id: i for i, task_id in enumerate(run.task_ids)}
        out: list[int] = []
        for view in views:
            index = index_of[view.task_id]
            chosen = 0 if scorer is None else scorer.answer_index(view)
            if labels is None:
                graded = run.sample_correct[index] or run.correct[index : index + 1]
                out.append(graded[chosen])
            else:
                answers = run.sample_outputs[index] or (run.outputs[index],)
                out.append(labels.label_for(run.tier, view.task_id, answers[chosen]))
        return out

    def fit_bundle(
        config: CascadeConfig, labels: PseudoLabels | None = None
    ) -> tuple[ScorerBundle, dict[str, float | None]]:
        source = pseudo if labels is None else labels
        keep, items = kept_and_items(source)
        scorers: dict[str, Scorer] = {}
        warnings: list[ScorerWarning] = []
        aurocs: dict[str, float | None] = {}
        for tier in tiers:
            cal = need(base_pipeline, "calibration", tier)
            views = _views(cal, items, bundle.grounding, config.k, keep=keep)
            context = ScorerContext(
                feature_names=tuple(workload.scorer_features),
                equivalence=answers_equivalent,
                entailment=entails,
                entailment_price=entailment_call_price,
                hidden_state_models=registry.hidden_state_models(),
                seed=seed,
                k=config.k,
                sample_weights=(
                    None
                    if source is None
                    else tuple(source.weights[(tier, view.task_id)] for view in views)
                ),
            )
            fitted_on = calibration_labels(cal, views, None, source)
            scorer, tier_warnings = fit_scorer(config.scorer, tier, views, fitted_on, context)
            test = need(base_pipeline, "test", tier)
            # AUROC is measured against **gold** on the test split wherever gold exists. It is
            # a statement about whether the scorer predicts correctness, and scoring it against
            # the same pseudo-labels it was fitted on would make it a statement about whether
            # the scorer predicts the consensus — which it was trained to do.
            scorer.auroc = evaluate_auroc(
                scorer,
                _views(test, list(bundle.test), bundle.grounding, config.k),
                list(test.correct),
            )
            aurocs[tier] = scorer.auroc
            scorers[tier] = scorer
            warnings.extend(tier_warnings)
        return ScorerBundle(scorers=scorers, warnings=warnings), aurocs

    def tier_runs(
        split_name: str,
        items: Sequence[Item],
        scorer_bundle: ScorerBundle,
        config: CascadeConfig,
        labels: PseudoLabels | None = None,
    ) -> list[TierRun]:
        """One tier's recorded answers under one configuration.

        Two things move with the configuration and both have to. The **cost** is every sample
        the scorer drew, because k samples at a tier is k generations and the workload pays for
        all of them. The **answer** is whichever generation the scorer returns, graded as the
        answer it returned — charging for a vote and then grading the first sample would credit
        the cascade with an accuracy it did not deliver.
        """
        out: list[TierRun] = []
        calibrating = split_name == "calibration"
        source = pseudo if labels is None else labels
        keep = (None if source is None else set(source.kept)) if calibrating else None
        for tier in tiers:
            run = need(base_pipeline, split_name, tier)
            views = _views(run, items, bundle.grounding, config.k, keep=keep)
            scores = scorer_bundle.score(tier, views)
            scorer = scorer_bundle.scorers[tier]
            index_of = {task_id: i for i, task_id in enumerate(run.task_ids)}
            costs: list[Decimal] = []
            correct: list[int] = []
            if calibrating:
                correct = calibration_labels(run, views, scorer, source)
            for view in views:
                index = index_of[view.task_id]
                drawn = run.sample_costs[index][: config.k] or (run.cost_usd[index],)
                # Whatever the scorer spends beyond the tier's own generations is charged here
                # and nowhere else. `semantic-entropy-v1` buys entailment calls; the others buy
                # nothing and return zero (DECISIONS.md D27's rule, on the protocol this time).
                costs.append(sum(drawn, Decimal(0)) + scorer.extra_cost(view))
                if not calibrating:
                    chosen = scorer.answer_index(view)
                    graded = run.sample_correct[index] or run.correct[index : index + 1]
                    correct.append(graded[chosen])
            out.append(
                tier_run_from_arrays(
                    tier,
                    run.model_id,
                    tuple(view.task_id for view in views),
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

    # Held fixed across configurations on purpose. The floor says what the workload requires of
    # any candidate; letting it move with the candidate would let a worse scorer lower the bar
    # it is judged against. Under label-free calibration it is the frontier's *pseudo* accuracy
    # on the kept tasks — a floor derived from gold would be a gold label reaching into a
    # calibration that claims not to have one.
    if pseudo is None:
        frontier_accuracy = frontier_calibration.accuracy
    else:
        _, frontier_labels, _ = pseudo.for_tier(tiers[-1])
        frontier_accuracy = sum(frontier_labels) / len(frontier_labels)
    accuracy_floor = max(0.0, frontier_accuracy - cascade_spec.accuracy_slack)

    candidates = _configurations(cascade_spec, recorded_depth)
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

    # ------------------------------------------------- 2b. what a label-free calibration does
    #
    # The demo has an answer key, so the question U3 asks can actually be answered here: how far
    # does a calibration that never saw one land from the one that did? Computed for the chosen
    # configuration only — re-searching every configuration under every label source would
    # triple the report's run time to answer a question about the operating point.
    def calibrate_with(labels: PseudoLabels | None) -> tuple[ScorerBundle, SearchResult, float]:
        scorer_bundle, _ = fit_bundle(config, labels)
        calibration_tiers = tier_runs(
            "calibration", kept_and_items(labels)[1], scorer_bundle, config, labels
        )
        if labels is None:
            floor = max(0.0, frontier_calibration.accuracy - cascade_spec.accuracy_slack)
        else:
            _, frontier_pseudo, _ = labels.for_tier(tiers[-1])
            floor = max(
                0.0, sum(frontier_pseudo) / len(frontier_pseudo) - cascade_spec.accuracy_slack
            )
        return (
            scorer_bundle,
            search_thresholds(
                calibration_tiers,
                accuracy_floor=floor,
                step=cascade_spec.threshold_step,
                objective=objective,
            ),
            floor,
        )

    gold_truth = {
        tier: dict(
            zip(
                need(base_pipeline, "calibration", tier).task_ids,
                need(base_pipeline, "calibration", tier).correct,
                strict=True,
            )
        )
        for tier in tiers
    }
    alternatives: list[dict[str, Any]] = []
    for kind_name in (PENALIZED_V1, MAJORITY_VOTE):
        if kind_name == calibration_mode:
            # Already the operating point; reporting it as an alternative to itself would be a
            # row comparing a number with itself.
            continue
        labels = build_labels(kind_name)
        alt_bundle, alt_search, alt_floor = calibrate_with(labels)
        alt_test = tier_runs("test", list(bundle.test), alt_bundle, config)
        alt_outcome = simulate(alt_test, alt_search.thresholds)
        gaps = [abs(a - b) for a, b in zip(alt_search.thresholds, search.thresholds, strict=True)]
        alternatives.append(
            {
                **{"kind": kind_name, "description": pseudo_label_kind(kind_name).description},
                **labels.as_dict(),
                "thresholds": list(alt_search.thresholds),
                "accuracy_floor": alt_floor,
                "feasible": alt_search.feasible,
                "max_threshold_gap": max(gaps) if gaps else 0.0,
                "label_agreement_with_gold": {
                    tier: labels.agreement_with(tier, gold_truth[tier]) for tier in tiers
                },
                "test_accuracy": alt_outcome.accuracy,
                "test_cost_per_task_usd": str(
                    alt_outcome.total_cost / alt_outcome.n if alt_outcome.n else Decimal(0)
                ),
                "test_scarce_share": alt_outcome.scarce_share,
            }
        )

    calibration_block = {
        "mode": calibration_mode,
        "description": (
            "the dataset's answer key"
            if calibration_mode == GOLD
            else pseudo_label_kind(calibration_mode).description
        ),
        "thresholds": list(search.thresholds),
        "accuracy_floor": accuracy_floor,
        "excluded": 0 if pseudo is None else len(pseudo.excluded),
        "n": len(bundle.calibration),
        "alternatives": alternatives,
        "note": (
            "Calibration labels come from the answer key. The rows below are what a calibration "
            "that never saw one would have chosen on the same data, which is the only way to "
            "size how much the answer key was worth."
            if calibration_mode == GOLD
            else "Calibration labels stand in for an answer key this workload does not have."
        ),
        "limits": (
            "No label-free consensus can see a wrong answer every tier agrees on. What it can "
            "see is a plurality that is an accident of a split distribution, or one tier "
            "repeating itself loudly enough to win a weak vote; those are what the exclusions "
            "and the overconfidence penalty are for."
        ),
        "fixtures_can_exercise_this": _fixtures_exercise_pseudolabels(alternatives),
    }

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
        operating_point_note=_operating_point_note(
            config, candidates, search, cascade_spec.searchable_scorers()
        ),
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
    # Between the prompt rewrite and the cascade sits the checkable-contract lever
    # (UPGRADE_V3.md U4). Found from the spec rather than named here, so a workload that defines
    # no such pipeline simply has no row and nothing has to know about it.
    checkable_pipelines = [
        pipeline_id
        for pipeline_id in sorted(workload.pipelines)
        if workload.pipeline(pipeline_id).checkable and (pipeline_id, "test", tiers[-1]) in runs
    ]
    for pipeline_id in checkable_pipelines:
        waterfall_arms.append(
            arm_from_run(
                runs[(pipeline_id, "test", tiers[-1])], workload.pipeline(pipeline_id).name
            )
        )
    waterfall_arms.append(candidate_arm)
    waterfall = build_waterfall(
        waterfall_arms, margin=effective_margin, seed=seed, resamples=resamples
    )

    # ------------------------------------------------- 6a. the same proof, without gold
    operating_point = {
        "scorer": config.scorer,
        "k": config.k,
        "label": config.label,
        "thresholds": list(search.thresholds),
        "protect_scarce": protect_scarce,
        "chosen_on": "calibration",
    }
    judged = _judged_block(
        workload,
        root,
        operating_point,
        proof,
        margin=effective_margin,
        seed=seed,
        resamples=resamples,
        annotation_budget=annotation_budget,
        origin=origin,
    )

    contract_block = _contract_block(root, workload, runs, tiers[-1], origin)
    provenance_block = dataset_provenance_block(workload, bundle)

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
        # How much k repeated samples agreed with each other, and therefore how many
        # independent draws they were worth (UPGRADE_V3.md U6). This is the number that
        # replaces D27's paragraph about correlated sampling: an effective k close to k means
        # the draws really were independent, which on these fixtures is true and is a fact
        # about the simulator rather than about sampling.
        correlation: dict[str, Any] | None = None
        if candidate.config.k >= 2:
            per_tier: dict[str, Any] = {}
            for tier in tiers:
                run = need(base_pipeline, "test", tier)
                sizes = candidate.scorers.scorers[tier].cluster_sizes(
                    _views(run, list(bundle.test), bundle.grounding, candidate.config.k)
                )
                if sizes is None:
                    continue
                per_tier[tier] = measure_correlation(sizes, candidate.config.k).as_dict()
            correlation = per_tier or None

        scorer_comparison.append(
            {
                **candidate.config.as_dict(),
                "sample_correlation": correlation,
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

    # ------------------------------------------------- 6c. every configuration tied for cheapest
    #
    # The scorer comparison already carries a cost interval per configuration. Whether one of
    # them is *cheaper* than the others is a question those intervals answer, and when they
    # overlap the answer is no (UPGRADE_V3.md U7).
    tie_candidates = [
        Candidate(
            label=row["label"],
            cost_point=float(row["cost_per_successful_task"]["point"]),
            cost_low=float(row["cost_per_successful_task"]["low"]),
            cost_high=float(row["cost_per_successful_task"]["high"]),
            accuracy_point=float(row["accuracy"]["point"]),
            scarce_share=float(row["scarce_share"]),
            adoptable=row["scorer"] in set(cascade_spec.searchable_scorers()),
            extra={
                "scorer": row["scorer"],
                "k": row["k"],
                "is_operating_point": bool(row["is_chosen"]),
                "verdict": row["verdict"]["label"],
            },
        )
        for row in scorer_comparison
    ]
    tie_report = find_ties(tie_candidates, config.label) if tie_candidates else None

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
        "grounding": bundle.grounding,
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
            "grading": workload.grading,
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
            # Whether this recording was made over the power refusal. False on a fixture set
            # nobody overrode anything to build, which is every set this repository has shipped.
            "recorded_underpowered": bool(manifest.get("underpowered", False)),
        },
        "proof": proof.as_dict(),
        # Added last, because it reads the blocks above. See `optimize/evidence.py`.
        "evidence": {},
        "summary": {},
        "judged": judged,
        "calibration": calibration_block,
        "contract": contract_block,
        "dataset_provenance": provenance_block,
        "waterfall": [
            {
                **step.as_dict(),
                **(
                    {"contract": contract_block}
                    if contract_block.get("available")
                    and step.pipeline_id == contract_block.get("candidate_pipeline")
                    else {}
                ),
            }
            for step in waterfall
        ],
        "cascade": {
            **search.as_dict(),
            # Which prompt this cascade routes between tiers of. The cascade id names no prompt,
            # so without this the report describes an operating point nobody can reproduce, and
            # `tokop export` would have to guess (UPGRADE_V4.md M14.4).
            "base_pipeline": base_pipeline,
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
            # Which tier actually answered each task. `tokop annotate` needs it: the judged
            # proof reviews the answer the cascade *returned*, and that is a different tier's
            # output on every task. Small enough to carry (one short string per task) and the
            # alternative is re-running the calibration search to re-derive it.
            "resolved_tier_by_task": {
                task_id: tiers[index]
                for task_id, index in zip(
                    cascade_outcome.task_ids,
                    cascade_outcome.resolved_tier_index,
                    strict=True,
                )
            },
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
            "ties": tie_report.as_dict() if tie_report else None,
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
                # What this pipeline runs as. One `generate` step for a single-call pipeline,
                # which is every pipeline shipped today — stated rather than implied, so the
                # screens and the findings read the same shape whether or not it is a graph.
                "graph": compile_graph(spec).as_dict(),
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
                "total_cost_usd": str(run.recorded_cost),
                # What the pipeline's own single generation per task cost. The two differ only
                # when the matrix was recorded deeper to compare scorers; the gap is the price
                # of the comparison, not of the pipeline.
                "single_sample_cost_usd": str(run.total_cost),
                "samples_per_task": run.sample_depth(),
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
    payload = ReportPayload(data)
    data["evidence"] = assess(payload).as_dict()
    data["summary"] = _summary(payload)
    return payload


def _summary(payload: ReportPayload) -> dict[str, Any]:
    """The six numbers a visitor needs in the first thirty seconds (UPGRADE_V4.md M14.3).

    Computed here rather than in the component. Non-negotiable 1 bans metric literals in the UI,
    and a component that divides one number by another to get a saving has written a metric — it
    just wrote it in TypeScript. Every field below is a string the screen prints as it stands.
    """
    proof = payload["proof"]
    baseline = proof["baseline"]
    candidate = proof["candidate"]
    delta = proof["delta_accuracy"]
    mark = "~" if payload["provenance"]["is_test_data"] else ""
    return {
        "workload": payload["workload"]["name"],
        "current": {
            "pipeline": baseline["pipeline"],
            "label": payload["pipelines"][baseline["pipeline"]]["name"],
            "cost_per_successful_task_usd": str(baseline["cost_per_successful_task"]["point"]),
        },
        "recommended": {
            "pipeline": candidate["pipeline"],
            "label": payload["pipelines"]
            .get(candidate["pipeline"], {})
            .get("name", candidate["pipeline"]),
            "cost_per_successful_task_usd": str(candidate["cost_per_successful_task"]["point"]),
        },
        "saving": {
            "fraction": proof["cost_reduction"],
            "interval": proof["cost_ratio"],
            "per_task_usd": proof["savings_per_task_usd"],
            "repayment_tasks": proof["repayment_tasks"],
            "proof_cost_usd": proof["proof_cost"]["total_usd"],
        },
        "quality": {
            "delta_points": delta["point"] * 100,
            "low_points": delta["low"] * 100,
            "high_points": delta["high"] * 100,
            "allowed_points": proof["verdict"]["margin"] * 100,
            "n": candidate["n"],
        },
        "verdict": {
            "label": proof["verdict"]["label"],
            "display": proof["verdict"]["display"],
            "sentence": proof["verdict"]["sentence"],
        },
        "provenance_mark": mark,
        "is_test_data": payload["provenance"]["is_test_data"],
    }


@lru_cache(maxsize=4)
def fitted_cascade(
    protect_scarce: bool = False, workload_path: str | None = None
) -> tuple[ScorerBundle, tuple[float, ...], tuple[str, ...]]:
    """The fitted scorers, the chosen thresholds and the tier order.

    Built by the same path as the report and cached alongside it, so the trace drawer shows the
    decision the cascade *actually* made rather than re-deriving one — or, worse, inferring it
    from the grade, which would put a gold-derived fact under a label that claims to be router
    output.
    """
    payload = cached_report(protect_scarce)
    registry = load_registry()
    workload = load_reported_workload(workload_path)
    bundle = load_bundle(workload)
    cascade_spec = workload.cascade(CANDIDATE_CASCADE)
    tiers = tuple(cascade_spec.tiers)
    root = _fixture_root(workload)
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
        views = _views(cal, list(bundle.calibration), bundle.grounding)
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


def arms_for_annotation(
    protect_scarce: bool = False, workload_path: str | None = None
) -> dict[str, Any]:
    """The two arms' answers on the test split, ready for a judge (UPGRADE_V3.md U1).

    The baseline arm is one pipeline, so its answer is whatever it said. The candidate arm is a
    cascade: the answer it *returned* is the output of whichever tier the router stopped at, and
    that is a different tier on every task. Judging the frontier's answer on a task the cascade
    answered at the cheap tier would grade a pipeline nobody is proposing to run.

    Refuses rather than approximates when the operating point draws more than one sample per
    task: which of k generations the scorer returned is the scorer's own decision, the judge
    cassettes cover the first, and guessing here would judge an answer the cascade did not give.
    """
    payload = cached_report(protect_scarce)
    cascade = payload["cascade"]
    choice = cascade["scorer_choice"]
    if int(choice["k"]) > 1:
        raise ReportError(
            f"the operating point is {choice['label']}, which returns one of "
            f"{choice['k']} generations per task. Annotation judges the answer the cascade "
            "returned, and only the first generation of each task has been judged. Record "
            "judge calls for every sample, or annotate an operating point that draws one."
        )

    registry = load_registry()
    workload = load_reported_workload(workload_path)
    bundle = load_bundle(workload)
    cascade_spec = workload.cascade(CANDIDATE_CASCADE)
    tiers = list(cascade_spec.tiers)
    root = _fixture_root(workload)
    manifest = _load_manifest(root)
    store = CassetteStore(root / "cassettes")
    snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
    origin = str(manifest.get("origin", "simulated"))

    def replay(pipeline_id: str, tier: str) -> ReplayedRun:
        return replay_run(
            workload, registry, bundle, snapshot, store, pipeline_id, "test", tier, origin
        )

    baseline_run = replay(BASELINE_PIPELINE, tiers[-1])
    by_tier = {tier: replay(cascade_spec.base_pipeline, tier) for tier in tiers}
    outputs_by_tier = {
        tier: dict(zip(run.task_ids, run.outputs, strict=True)) for tier, run in by_tier.items()
    }
    resolved: dict[str, str] = dict(cascade["resolved_tier_by_task"])
    questions = {item.id: item.question for item in bundle.test}

    return {
        "task_ids": list(baseline_run.task_ids),
        "questions": {task_id: questions[task_id] for task_id in baseline_run.task_ids},
        "baseline_answers": dict(zip(baseline_run.task_ids, baseline_run.outputs, strict=True)),
        "candidate_answers": {
            task_id: outputs_by_tier[resolved[task_id]][task_id]
            for task_id in baseline_run.task_ids
        },
        "candidate_tiers": {task_id: resolved[task_id] for task_id in baseline_run.task_ids},
        "baseline_tier": tiers[-1],
        "baseline_pipeline": BASELINE_PIPELINE,
        "candidate_pipeline": CANDIDATE_CASCADE,
        "operating_point": {
            "scorer": choice["scorer"],
            "k": choice["k"],
            "label": choice["label"],
            "thresholds": list(cascade["thresholds"]),
            "protect_scarce": protect_scarce,
            "chosen_on": choice["chosen_on"],
        },
        "origin": origin,
    }


def arms_for_contract(
    baseline_id: str = "B2",
    candidate_id: str | None = None,
    workload_path: str | None = None,
) -> dict[str, Any]:
    """Two single-call pipelines on the test split, ready for a judge (UPGRADE_V3.md U4).

    The pair the checkable-contract lever is about: the same prompt with and without a contract
    that asks the model to show the step from the rule it quoted to the number it returned.
    Judging both with the same judge is how the lever gets priced — a contract that makes a
    cheap verifier agree with a strong grader more often needs fewer strong labels for the same
    interval width, and that is a number in dollars.

    ``candidate_id`` defaults to the workload's checkable pipeline. Refuses rather than guesses
    when there is more than one.
    """
    registry = load_registry()
    workload = load_reported_workload(workload_path)
    bundle = load_bundle(workload)
    if candidate_id is None:
        checkable = sorted(p for p, spec in workload.pipelines.items() if spec.checkable)
        if len(checkable) != 1:
            raise ReportError(
                f"the workload defines {len(checkable)} checkable pipelines ({checkable}); name "
                "the one to price with --candidate."
            )
        candidate_id = checkable[0]

    tier = workload.cascade(CANDIDATE_CASCADE).tiers[-1]
    root = _fixture_root(workload)
    manifest = _load_manifest(root)
    store = CassetteStore(root / "cassettes")
    snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
    origin = str(manifest.get("origin", "simulated"))

    def replay(pipeline_id: str) -> ReplayedRun:
        return replay_run(
            workload, registry, bundle, snapshot, store, pipeline_id, "test", tier, origin
        )

    base_run, cand_run = replay(baseline_id), replay(candidate_id)
    if base_run.task_ids != cand_run.task_ids:
        raise ReportError(
            f"{baseline_id} and {candidate_id} answered different task sets; the contract "
            "comparison is paired and cannot be approximated."
        )
    questions = {item.id: item.question for item in bundle.test}
    return {
        "task_ids": list(base_run.task_ids),
        "questions": {task_id: questions[task_id] for task_id in base_run.task_ids},
        "baseline_answers": dict(zip(base_run.task_ids, base_run.outputs, strict=True)),
        "candidate_answers": dict(zip(cand_run.task_ids, cand_run.outputs, strict=True)),
        "candidate_tiers": dict.fromkeys(base_run.task_ids, tier),
        "baseline_tier": tier,
        "baseline_pipeline": baseline_id,
        "candidate_pipeline": candidate_id,
        "operating_point": {
            "scorer": "none",
            "k": 1,
            "label": f"{baseline_id} vs {candidate_id}, single call at the {tier} tier",
            "thresholds": [],
            "protect_scarce": False,
            "chosen_on": "not applicable: neither arm routes",
        },
        "origin": origin,
    }


def trace_for(task_id: str, workload_path: str | None = None) -> dict[str, Any]:
    """Every call every pipeline made for one task, for the trace drawer (SPEC.md 5.1).

    Replayed from cassettes on demand rather than held in the report: a full trace for 200
    tasks across 8 runs is far more data than any screen needs at once.
    """
    registry = load_registry()
    workload = load_reported_workload(workload_path)
    bundle = load_bundle(workload)
    item = next((i for i in bundle.items if i.id == task_id), None)
    if item is None:
        raise ReportError(f"no task {task_id!r} in this workload")

    root = _fixture_root(workload)
    manifest = _load_manifest(root)
    store = CassetteStore(root / "cassettes")
    snapshot = registry.snapshot(list(registry.roles.values()), date(2026, 9, 11))
    in_calibration = any(i.id == task_id for i in bundle.calibration)
    split_name = "calibration" if in_calibration else "test"
    grounding = bundle.grounding

    scorer_bundle, thresholds, tier_order = fitted_cascade(False, workload_path)
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
            {"grounding": grounding, "question": item.question, "timestamp": demo_timestamp()},
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
            context=grounding,
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


def _provenance_metrics_lines(payload: ReportPayload) -> list[str]:
    """Where the task set came from, and whether it can certify (UPGRADE_V3.md U8)."""
    block = payload["dataset_provenance"]
    shape = block["shape"]
    lines = [
        f"**Where these tasks came from** — {block['n']} items: "
        f"{block['real_traffic_items']} from real traffic, "
        f"{block['model_generated_items']} written by a model, "
        f"{block['program_generated_items']} templated by a program. "
        f"{shape['templates_present']} of {shape['template_space']} question templates appear "
        f"({shape['tail_coverage'] * 100:.0f}% tail coverage).",
        "",
        f"- **Certifiable: {'yes' if block['certifiable'] else 'no'}.**",
    ]
    lines += [f"  - Refused: {refusal}" for refusal in block["refusals"]]
    lines.append("")
    return lines


def _contract_metrics_lines(payload: ReportPayload) -> list[str]:
    """What a checkable output contract cost and what it bought (UPGRADE_V3.md U4)."""
    contract = payload["contract"]
    if not contract.get("available"):
        return []
    before, after = contract["arms"]
    lines = [
        f"**What checkability costs** — {after['pipeline']} is {before['pipeline']}'s prompt "
        "plus one line showing how the quoted rule produces the answer:",
        "",
        "| Pipeline | Cheap judge agrees with the strong grader | Strong labels needed | "
        "Annotation | Accuracy | Generation |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for arm in (before, after):
        lines.append(
            f"| {arm['pipeline']} {arm['label']} | "
            f"{arm['judge_agreement_with_strong_grader'] * 100:.1f}% | "
            f"{arm['sampling_rate_for_target'] * 100:.0f}% of tasks | "
            f"${float(arm['annotation_cost_for_target_usd']):.4f} | "
            f"{arm['accuracy'] * 100:.1f}% | "
            f"${float(arm['generation_cost_usd']):.4f} |"
        )
    verdict = (
        "it pays for itself on this split"
        if contract["pays_for_itself"]
        else f"net ${contract['net_on_evaluation_split_usd']} — **it does not pay here**"
    )
    lines += [
        "",
        f"- Judge agreement moves {contract['agreement_delta'] * 100:+.1f} points and accuracy "
        f"{contract['accuracy_delta'] * 100:+.1f} points. The accuracy cost is reported with "
        "its sign; a row that showed the saving and hid it would be the most tempting "
        "dishonesty in this product.",
        f"- Annotation saving ${contract['annotation_saving_usd']} against a generation premium "
        f"of ${contract['generation_premium_usd']} at a target standard error of "
        f"{contract['target_standard_error']:.2f}: {verdict}.",
        "",
    ]
    return lines


def _effective_samples(row: dict[str, Any]) -> str:
    """The worst tier's effective sample count, for one scorer configuration (U6).

    k repeats are worth k only when the draws are independent, and a sampled scorer's whole
    argument rests on them being so. The cell reports the design effect at the tier where the
    repeats agree with themselves most, because that is the tier whose routing the correlation
    eats first.
    """
    correlation = row.get("sample_correlation")
    if not correlation:
        return "—"
    tier, measured = min(correlation.items(), key=lambda item: item[1]["effective_k"])
    return f"{measured['effective_k']:.2f} of {measured['k']} ({tier})"


def _scorer_metrics_lines(payload: ReportPayload) -> list[str]:
    """Every configuration the search compared, and every one it cannot separate (U6, U7)."""
    cascade = payload["cascade"]
    rows = cascade["scorer_comparison"]
    if not rows:
        return []
    ties = cascade["ties"]
    mark = "~" if payload["provenance"]["is_test_data"] else "▪"
    adoptable = set(cascade["scorer_choice"]["adoptable"])
    tied = {row["label"] for row in ties["tied"]} if ties and ties["is_tie"] else set()
    lines = [
        "**What else was measured** — every scorer configuration the calibration search compared, "
        "scored on the same test split:",
        "",
        "| Configuration | Calls per scored task | Accuracy | Cost per successful task | "
        "Effective samples | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        cost = row["cost_per_successful_task"]
        notes = [
            "operating point" if row["is_chosen"] else "",
            "tied for cheapest" if row["label"] in tied else "",
            "" if row["label"] in adoptable else "not adoptable here",
        ]
        lines.append(
            f"| {row['label']} | {row['calls_per_scored_task']} | "
            f"{row['accuracy']['point'] * 100:.1f}% | "
            f"${float(cost['point']):.6f} {mark} (${float(cost['low']):.6f}–"
            f"${float(cost['high']):.6f}) | {_effective_samples(row)} | "
            f"{', '.join(note for note in notes if note)} |"
        )

    lines.append("")
    rhos = [
        measured["intraclass_correlation"]
        for row in rows
        for measured in (row.get("sample_correlation") or {}).values()
    ]
    if rhos:
        lines.append(
            "- Effective samples is the cluster-sampling design effect `k / (1 + (k-1) rho)` over "
            f"the intraclass correlation of within-task agreement. Here rho runs {min(rhos):.3f} "
            f"to {max(rhos):.3f}, so "
            + (
                "k repeats are worth very nearly k — which is a property of a simulator that "
                "draws them independently, not of real sampled generations, and is why the "
                "sampling scorers are measured here and not adopted."
                if max(rhos) < 0.1
                else "k repeats are worth appreciably less than k, and the routing decision is "
                "weaker than the raw sample count suggests."
            )
        )
    if ties:
        lines += [f"- {ties['note']}", f"- {ties['fallback_reason']}"]
    lines.append("")
    return lines


def _judged_metrics_lines(payload: ReportPayload) -> list[str]:
    """The same comparison without the answer key, for the README (UPGRADE_V3.md U1).

    Generated like everything else between the markers: a workload without labels is the case
    the upgrade exists for, so the number it would get belongs in the README rather than in a
    paragraph claiming it works.
    """
    judged = payload["judged"]
    if not judged.get("available"):
        return [
            "**Without the answer key:** not computed — "
            + str(judged.get("reason", "no reason given")),
            "",
        ]
    delta = judged["delta_accuracy"]
    judge_only = judged["judge_only"]["delta_accuracy"]
    annotation = judged["annotation"]
    coverage = judged.get("coverage_check") or {}
    lines = [
        "**Without the answer key** — the same test split graded by "
        f"{judged['judge']['model_id']} on every task, corrected from "
        f"{annotation['n_annotated']} tasks re-graded by "
        + (
            "a human reviewer"
            if judged["strong_grader"]["source"] == "human"
            else str(judged["strong_grader"]["model_id"])
        )
        + ":",
        "",
        "| Estimate | Accuracy difference | 95% CI | What it cost |",
        "| --- | --- | --- | --- |",
        f"| The cheap judge alone | {judge_only['point'] * 100:+.1f} pt | "
        f"{judge_only['low'] * 100:+.1f} to {judge_only['high'] * 100:+.1f} | "
        f"${float(annotation['judge_cost_usd']):.4f} |",
        f"| Corrected (active evaluation) | {delta['point'] * 100:+.1f} pt | "
        f"{delta['low'] * 100:+.1f} to {delta['high'] * 100:+.1f} | "
        f"${float(annotation['annotation_cost_usd']):.4f} for "
        f"{annotation['annotated_share'] * 100:.0f}% of tasks |",
    ]
    if coverage:
        lines.append(
            f"| The answer key, for comparison | "
            f"{coverage['gold_delta_accuracy'] * 100:+.1f} pt | — | "
            "not available to a real unlabelled workload |"
        )
    lines += [
        "",
        f"- The judge's bias is {judged['judge_only']['bias_vs_corrected'] * 100:+.1f} points, "
        "measured rather than assumed away. The correction removes it; a biased judge costs "
        "interval width, never correctness.",
    ]
    if coverage:
        lines.append(
            "- The gold-free interval "
            + ("covers" if coverage["judged_interval_covers_gold"] else "does **not** cover")
            + f" the gold-graded difference of {coverage['gold_delta_accuracy'] * 100:+.1f} "
            "points. That check exists only because this demo happens to have labels."
        )
    if annotation["strong_only_items_for_same_width"] is not None:
        lines.append(
            f"- Strong-only grading would need "
            f"{annotation['strong_only_items_for_same_width']} items at "
            f"${float(annotation['strong_only_cost_usd']):.4f} for the same interval width, "
            f"against ${float(annotation['annotation_cost_usd']):.4f} spent here. The "
            f"cost-optimal sampling rate at this judge quality is "
            f"{annotation['cost_optimal_rate'] * 100:.0f}%."
        )
    lines.append("")
    return lines


def readme_block(payload: ReportPayload) -> str:
    """The README's metrics block: one headline result, and what it rests on in one line.

    SPEC.md non-negotiable 1: no metric literals in docs. Everything between the markers is
    written by this function and checked by ``tokop report --check``.

    Everything a reader needs *before* deciding whether to care lives here. Everything they need
    to check the claim afterwards lives in :func:`method_block`, which writes `docs/METHOD.md`.
    That is the whole of UPGRADE_V4.md M14: the qualifications are not softened or deleted, they
    move one click away, and both files are generated by the same code path so neither can drift
    from the other or from the report.
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
        # M17. Every figure above is per request, and until M17 nothing said so because nothing
        # in this build could price anything else. A session is a different accounting with a
        # different answer — the same prompt order that wins here can lose over turns once an
        # entry expires between them — so the claim states its unit rather than leaving a reader
        # to assume the larger one (UPGRADE_V4.md M17).
        "- Every figure here is **per request**: one task, one call, a cache entry that is warm "
        "because the run warmed it. Over a conversation the answer can differ, because an entry "
        "expires between turns and a history grows after the breakpoint. `tokop session` prices "
        "that separately and reports it separately.",
        "",
        f"**Evidence: {payload['evidence']['grade']}.** {payload['evidence']['headline']} "
        "The whole chain, and every method behind these numbers, is in "
        "[docs/METHOD.md](docs/METHOD.md).",
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


def _evidence_lines(payload: ReportPayload) -> list[str]:
    """The chain behind the grade, as a table (UPGRADE_V4.md M14.2)."""
    evidence = payload["evidence"]
    lines = [
        f"## Evidence: {evidence['grade']}",
        "",
        evidence["headline"],
        "",
        "The grade is the lowest rung any link forces — never an average, and never the best "
        "available reading. Each row below is computed by `optimize/evidence.py` from the same "
        "report the screens display.",
        "",
        "| Link | Where it stands | Reading | What would change it |",
        "| --- | --- | --- | --- |",
    ]
    for link in evidence["inputs"]:
        lines.append(
            f"| {link['name']} | {link['standing']} | {link['value']} | "
            f"{link['what_would_change_it'] or '—'} |"
        )
    lines += [
        "",
        "Sources, in the same order: "
        + "; ".join(f"**{link['name']}** — {link['source']}" for link in evidence["inputs"])
        + ".",
        "",
    ]
    return lines


def method_block(payload: ReportPayload) -> str:
    """`docs/METHOD.md`: how every number on the first screen was arrived at.

    The README used to carry all of this above the fold, which meant a reader met the estimator
    caveats before the promise. Moving it here changes the order and nothing else — the same
    tables, generated by the same functions, checked by the same command.
    """
    lines = [
        METRICS_START,
        "<!-- generated by `tokop report --write-readme`; do not edit by hand -->",
        "",
        "# Method",
        "",
        "Everything on this page is computed by engine code over the traces in `fixtures/`, and "
        "regenerated by `tokop report --write-readme`. `tokop report --check` fails the build if "
        "any figure here stops matching the report. Nothing on this page is typed by hand.",
        "",
        *_evidence_lines(payload),
        *_scorer_metrics_lines(payload),
        *_provenance_metrics_lines(payload),
        *_contract_metrics_lines(payload),
        *_judged_metrics_lines(payload),
        METRICS_END,
    ]
    return "\n".join(lines)


def _replace_block(path: Path, block: str, title: str) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{title}{block}\n")
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


def _block_current(path: Path, expected: str, what: str) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path} does not exist"
    text = path.read_text()
    if METRICS_START not in text or METRICS_END not in text:
        return False, f"{what} has no generated block; run `tokop report --write-readme`"
    start = text.index(METRICS_START)
    end = text.index(METRICS_END) + len(METRICS_END)
    if text[start:end].strip() == expected.strip():
        return True, f"{what} is current"
    return False, f"{what} is stale; run `tokop report --write-readme`"


def method_doc_path() -> Path:
    return repo_root() / "docs" / "METHOD.md"


def write_method_doc(payload: ReportPayload, path: Path | None = None) -> Path:
    return _replace_block(path or method_doc_path(), method_block(payload), "")


def method_doc_current(payload: ReportPayload, path: Path | None = None) -> tuple[bool, str]:
    return _block_current(path or method_doc_path(), method_block(payload), "docs/METHOD.md")


def write_readme_metrics(payload: ReportPayload, readme: Path | None = None) -> Path:
    path = readme or repo_root() / "README.md"
    return _replace_block(path, readme_block(payload), "# Tokop\n\n")


def readme_metrics_current(payload: ReportPayload, readme: Path | None = None) -> tuple[bool, str]:
    path = readme or repo_root() / "README.md"
    return _block_current(path, readme_block(payload), "the README metrics block")
