"""Assemble the proof: is the candidate pipeline cheaper without being worse?

This module turns two runs over the same held-out tasks into the claim on the Optimize screen.
Everything it computes is paired over tasks, because baseline and candidate answered the *same*
questions and the informative quantity is the per-task difference, not two separate averages.

Three things it is careful about, because each is a way to accidentally overclaim:

* **The proof has a price.** The baseline run, the calibration runs at every tier, any judge
  calls and the pre-warming were all paid for to produce this number. That cost is reported next
  to the saving, with the task volume at which the saving repays it.
* **The operating point was chosen on calibration,** before any test result existed. The report
  says so, because a threshold tuned on the test split would make the test number meaningless.
* **An inconclusive result is reported as inconclusive.** Not "no significant difference", not
  rounded towards the pleasing answer.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tokop.core.stats import (
    DEFAULT_MARGIN,
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    Interval,
    McNemarResult,
    StatsError,
    Verdict,
    cost_per_successful_task,
    cost_per_successful_task_interval,
    cost_ratio_interval,
    delta_accuracy_interval,
    mcnemar,
    non_inferiority_verdict,
    verdict_from_interval,
    wilson,
)


@dataclass(frozen=True)
class ArmResult:
    """One pipeline's outcome on the held-out split, task by task."""

    pipeline_id: str
    label: str
    task_ids: tuple[str, ...]
    correct: tuple[int, ...]
    cost_usd: tuple[Decimal, ...]
    model_ids: tuple[str, ...]
    scarce_cost_usd: Decimal = Decimal(0)
    resolved_tier: tuple[str, ...] = ()
    origin: str = "simulated"

    @property
    def n(self) -> int:
        return len(self.task_ids)

    @property
    def successes(self) -> int:
        return sum(self.correct)

    @property
    def total_cost(self) -> Decimal:
        return sum(self.cost_usd, Decimal(0))

    @property
    def accuracy(self) -> float:
        return self.successes / self.n if self.n else 0.0

    @property
    def cost_per_task(self) -> Decimal:
        return self.total_cost / self.n if self.n else Decimal(0)

    @property
    def cost_per_successful_task(self) -> Decimal | None:
        return self.total_cost / self.successes if self.successes else None

    @property
    def scarce_share(self) -> float:
        return float(self.scarce_cost_usd / self.total_cost) if self.total_cost else 0.0

    def accuracy_interval(self) -> Interval:
        return wilson(self.successes, self.n)

    def cost_interval(self, seed: int = DEFAULT_SEED) -> Interval:
        return cost_per_successful_task_interval(
            [float(c) for c in self.cost_usd], list(self.correct), seed=seed
        )

    def tier_shares(self) -> dict[str, float]:
        if not self.resolved_tier:
            return {}
        counts: dict[str, int] = defaultdict(int)
        for tier in self.resolved_tier:
            counts[tier] += 1
        return {tier: count / self.n for tier, count in sorted(counts.items())}


@dataclass(frozen=True)
class ProofCost:
    """What producing the proof cost, itemised (SPEC.md 5.1)."""

    baseline_run_usd: Decimal
    calibration_runs_usd: Decimal
    candidate_run_usd: Decimal
    scorer_and_judge_usd: Decimal
    prewarming_usd: Decimal
    other_pipelines_usd: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return (
            self.baseline_run_usd
            + self.calibration_runs_usd
            + self.candidate_run_usd
            + self.scorer_and_judge_usd
            + self.prewarming_usd
            + self.other_pipelines_usd
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "baseline_run_usd": str(self.baseline_run_usd),
            "calibration_runs_usd": str(self.calibration_runs_usd),
            "candidate_run_usd": str(self.candidate_run_usd),
            "other_pipelines_usd": str(self.other_pipelines_usd),
            "scorer_and_judge_usd": str(self.scorer_and_judge_usd),
            "prewarming_usd": str(self.prewarming_usd),
            "total_usd": str(self.total),
        }


@dataclass
class TypeBreakdown:
    """Accuracy, tier share and cost per successful task, by question type.

    Analysis after the fact: the router never sees the question type (SPEC.md 5.1).
    """

    question_type: str
    n: int
    baseline_accuracy: float
    candidate_accuracy: float
    candidate_tier_shares: dict[str, float]
    baseline_cost_per_success: Decimal | None
    candidate_cost_per_success: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_type": self.question_type,
            "n": self.n,
            "baseline_accuracy": self.baseline_accuracy,
            "candidate_accuracy": self.candidate_accuracy,
            "candidate_tier_shares": self.candidate_tier_shares,
            "baseline_cost_per_success_usd": (
                str(self.baseline_cost_per_success)
                if self.baseline_cost_per_success is not None
                else None
            ),
            "candidate_cost_per_success_usd": (
                str(self.candidate_cost_per_success)
                if self.candidate_cost_per_success is not None
                else None
            ),
        }


@dataclass
class Disagreement:
    """A task the two pipelines graded differently. Opens its trace in the UI."""

    task_id: str
    question: str
    gold: str
    question_type: str
    baseline_correct: bool
    candidate_correct: bool
    candidate_tier: str
    baseline_cost_usd: Decimal
    candidate_cost_usd: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "gold": self.gold,
            "question_type": self.question_type,
            "baseline_correct": self.baseline_correct,
            "candidate_correct": self.candidate_correct,
            "candidate_tier": self.candidate_tier,
            "baseline_cost_usd": str(self.baseline_cost_usd),
            "candidate_cost_usd": str(self.candidate_cost_usd),
        }


@dataclass
class Proof:
    """The whole comparison."""

    baseline: ArmResult
    candidate: ArmResult
    margin: float
    delta_accuracy: Interval
    verdict: Verdict
    mcnemar: McNemarResult
    baseline_accuracy: Interval
    candidate_accuracy: Interval
    baseline_cost: Interval
    candidate_cost: Interval
    cost_ratio: Interval
    proof_cost: ProofCost
    split_sizes: dict[str, int]
    by_type: list[TypeBreakdown] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    scorer_auroc: dict[str, float | None] = field(default_factory=dict)
    operating_point_note: str = (
        "The operating point was chosen on the calibration split, before any test result was "
        "computed."
    )

    @property
    def cost_reduction(self) -> float | None:
        """Fractional reduction in cost per successful task. ``None`` if either is undefined."""
        base = self.baseline.cost_per_successful_task
        cand = self.candidate.cost_per_successful_task
        if base is None or cand is None or base == 0:
            return None
        return float((base - cand) / base)

    @property
    def savings_per_task(self) -> Decimal:
        """Dollars saved per task at the same volume. The repayment unit."""
        return self.baseline.cost_per_task - self.candidate.cost_per_task

    def repayment_tasks(self) -> int | None:
        """How many tasks the saving must cover before the proof has paid for itself.

        ``None`` when the candidate is not cheaper, in which case there is nothing to repay and
        saying "never" would be clearer than a number.
        """
        saving = self.savings_per_task
        if saving <= 0:
            return None
        return int((self.proof_cost.total / saving).to_integral_value(rounding="ROUND_CEILING"))

    def sentence(self) -> str:
        """The plain-language verdict, with its numbers filled in (SPEC.md 5.1)."""
        reduction = self.cost_reduction
        delta_points = self.delta_accuracy.point * 100
        low, high = self.delta_accuracy.low * 100, self.delta_accuracy.high * 100
        margin_points = self.margin * 100
        if reduction is None:
            head = f"{self.candidate.label} could not be compared on cost: no successful tasks"
        else:
            ratio_low = (1 - self.cost_ratio.high) * 100
            ratio_high = (1 - self.cost_ratio.low) * 100
            direction = "cuts" if reduction > 0 else "raises"
            head = (
                f"{self.candidate.label} {direction} cost per successful task by "
                f"{abs(reduction) * 100:.1f}% (interval {min(ratio_low, ratio_high):.1f} to "
                f"{max(ratio_low, ratio_high):.1f}%)"
            )
        inside = "inside" if self.verdict.label == "non_inferior" else "against"
        return (
            f"{head}; accuracy difference {delta_points:+.1f} points, 95% CI "
            f"[{low:+.1f}, {high:+.1f}], n = {self.candidate.n}, {inside} the "
            f"{margin_points:.0f}-point margin."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": {
                "pipeline": self.baseline.pipeline_id,
                "label": self.baseline.label,
                "n": self.baseline.n,
                "accuracy": self.baseline_accuracy.as_dict(),
                "cost_per_successful_task": self.baseline_cost.as_dict(),
                "total_cost_usd": str(self.baseline.total_cost),
                "cost_per_task_usd": str(self.baseline.cost_per_task),
                "scarce_share": self.baseline.scarce_share,
                "model_ids": list(self.baseline.model_ids),
                "origin": self.baseline.origin,
            },
            "candidate": {
                "pipeline": self.candidate.pipeline_id,
                "label": self.candidate.label,
                "n": self.candidate.n,
                "accuracy": self.candidate_accuracy.as_dict(),
                "cost_per_successful_task": self.candidate_cost.as_dict(),
                "total_cost_usd": str(self.candidate.total_cost),
                "cost_per_task_usd": str(self.candidate.cost_per_task),
                "scarce_share": self.candidate.scarce_share,
                "model_ids": list(self.candidate.model_ids),
                "tier_shares": self.candidate.tier_shares(),
                "origin": self.candidate.origin,
            },
            "delta_accuracy": self.delta_accuracy.as_dict(),
            "cost_ratio": self.cost_ratio.as_dict(),
            "cost_reduction": self.cost_reduction,
            "verdict": {
                "label": self.verdict.label,
                "display": self.verdict.display,
                "margin": self.margin,
                "additional_tasks_needed": self.verdict.additional_tasks_needed,
                "note": self.verdict.note,
                "sentence": self.sentence(),
            },
            "mcnemar": {
                "baseline_only": self.mcnemar.baseline_only,
                "candidate_only": self.mcnemar.candidate_only,
                "concordant": self.mcnemar.concordant,
                "discordant": self.mcnemar.discordant,
                "p_value": self.mcnemar.p_value,
            },
            "proof_cost": self.proof_cost.as_dict(),
            "repayment_tasks": self.repayment_tasks(),
            "savings_per_task_usd": str(self.savings_per_task),
            "split_sizes": self.split_sizes,
            "by_type": [b.as_dict() for b in self.by_type],
            "disagreements": [d.as_dict() for d in self.disagreements],
            "scorer_auroc": self.scorer_auroc,
            "operating_point_note": self.operating_point_note,
        }


def build_proof(
    baseline: ArmResult,
    candidate: ArmResult,
    proof_cost: ProofCost,
    split_sizes: dict[str, int],
    *,
    margin: float = DEFAULT_MARGIN,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    items_by_id: dict[str, Any] | None = None,
    scorer_auroc: dict[str, float | None] | None = None,
    operating_point_note: str | None = None,
) -> Proof:
    """Compare two arms over the same tasks."""
    if baseline.task_ids != candidate.task_ids:
        raise StatsError(
            "the two arms answered different task sets. A paired comparison is the only kind "
            "that means anything here, so this is refused rather than approximated."
        )

    delta = non_inferiority_verdict(
        list(baseline.correct),
        list(candidate.correct),
        margin=margin,
        resamples=resamples,
        seed=seed,
    )
    ratio = cost_ratio_interval(
        [float(c) for c in baseline.cost_usd],
        list(baseline.correct),
        [float(c) for c in candidate.cost_usd],
        list(candidate.correct),
        resamples=resamples,
        seed=seed,
    )

    by_type: list[TypeBreakdown] = []
    disagreements: list[Disagreement] = []
    if items_by_id:
        grouped: dict[str, list[int]] = defaultdict(list)
        for index, task_id in enumerate(baseline.task_ids):
            item = items_by_id.get(task_id)
            if item is not None:
                grouped[item.question_type].append(index)

        for question_type in sorted(grouped):
            indices = grouped[question_type]
            base_costs = [float(baseline.cost_usd[i]) for i in indices]
            cand_costs = [float(candidate.cost_usd[i]) for i in indices]
            base_ok = [baseline.correct[i] for i in indices]
            cand_ok = [candidate.correct[i] for i in indices]
            tier_counts: dict[str, int] = defaultdict(int)
            if candidate.resolved_tier:
                for i in indices:
                    tier_counts[candidate.resolved_tier[i]] += 1
            base_cps = cost_per_successful_task(base_costs, base_ok)
            cand_cps = cost_per_successful_task(cand_costs, cand_ok)
            by_type.append(
                TypeBreakdown(
                    question_type=question_type,
                    n=len(indices),
                    baseline_accuracy=sum(base_ok) / len(indices),
                    candidate_accuracy=sum(cand_ok) / len(indices),
                    candidate_tier_shares={
                        tier: count / len(indices) for tier, count in sorted(tier_counts.items())
                    },
                    baseline_cost_per_success=(
                        Decimal(str(round(base_cps, 8))) if base_cps is not None else None
                    ),
                    candidate_cost_per_success=(
                        Decimal(str(round(cand_cps, 8))) if cand_cps is not None else None
                    ),
                )
            )

        for index, task_id in enumerate(baseline.task_ids):
            if baseline.correct[index] == candidate.correct[index]:
                continue
            item = items_by_id.get(task_id)
            disagreements.append(
                Disagreement(
                    task_id=task_id,
                    question=getattr(item, "question", ""),
                    gold=getattr(item, "gold", ""),
                    question_type=getattr(item, "question_type", ""),
                    baseline_correct=bool(baseline.correct[index]),
                    candidate_correct=bool(candidate.correct[index]),
                    candidate_tier=(
                        candidate.resolved_tier[index] if candidate.resolved_tier else ""
                    ),
                    baseline_cost_usd=baseline.cost_usd[index],
                    candidate_cost_usd=candidate.cost_usd[index],
                )
            )

    return Proof(
        baseline=baseline,
        candidate=candidate,
        margin=margin,
        delta_accuracy=delta.delta,
        verdict=delta,
        mcnemar=mcnemar(list(baseline.correct), list(candidate.correct)),
        baseline_accuracy=baseline.accuracy_interval(),
        candidate_accuracy=candidate.accuracy_interval(),
        baseline_cost=baseline.cost_interval(seed),
        candidate_cost=candidate.cost_interval(seed),
        cost_ratio=ratio,
        proof_cost=proof_cost,
        split_sizes=split_sizes,
        by_type=by_type,
        disagreements=disagreements,
        scorer_auroc=scorer_auroc or {},
        **({} if operating_point_note is None else {"operating_point_note": operating_point_note}),
    )


@dataclass
class WaterfallStep:
    """One bar in the savings waterfall (SPEC.md 5.1)."""

    pipeline_id: str
    label: str
    arm: ArmResult
    accuracy: Interval
    cost: Interval
    verdict_vs_baseline: Verdict | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline_id,
            "label": self.label,
            "n": self.arm.n,
            "accuracy": self.accuracy.as_dict(),
            "cost_per_successful_task": self.cost.as_dict(),
            "total_cost_usd": str(self.arm.total_cost),
            "cost_per_task_usd": str(self.arm.cost_per_task),
            "scarce_share": self.arm.scarce_share,
            "verdict": (
                {
                    "label": self.verdict_vs_baseline.label,
                    "display": self.verdict_vs_baseline.display,
                }
                if self.verdict_vs_baseline
                else None
            ),
        }


def build_waterfall(
    arms: Sequence[ArmResult],
    *,
    margin: float = DEFAULT_MARGIN,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> list[WaterfallStep]:
    """Cost per successful task for every pipeline, each with its verdict against the first."""
    if not arms:
        return []
    baseline = arms[0]
    steps: list[WaterfallStep] = []
    for arm in arms:
        verdict = None
        if arm is not baseline and arm.task_ids == baseline.task_ids:
            verdict = non_inferiority_verdict(
                list(baseline.correct),
                list(arm.correct),
                margin=margin,
                resamples=resamples,
                seed=seed,
            )
        steps.append(
            WaterfallStep(
                pipeline_id=arm.pipeline_id,
                label=arm.label,
                arm=arm,
                accuracy=arm.accuracy_interval(),
                cost=arm.cost_interval(seed),
                verdict_vs_baseline=verdict,
            )
        )
    return steps


# --------------------------------------------------------------- the proof without gold


@dataclass
class JudgedDelta:
    """The accuracy comparison when nobody labelled the workload (UPGRADE_V3.md U1).

    Three numbers sit side by side here on purpose, because the difference between them is the
    entire argument:

    * **judge-only** — what you get by believing a cheap verifier. Whatever bias the judge has
      is in this number at full strength, and it is reported so a reader can see how big that
      bias was rather than being told it was handled.
    * **active** — the same judge, corrected on a sampled subset of strong labels. Unbiased for
      the strong grader's mean whatever the judge does, because the inverse-probability weight
      makes the correction term's expectation ``E[H - G]``.
    * **annotation** — what the correction cost, and what buying the same interval width from
      the strong grader alone would have cost.

    The verdict is read off the active interval by the same rule the gold path uses.
    """

    baseline_pipeline: str
    candidate_pipeline: str
    margin: float
    #: The corrected estimate of candidate accuracy minus baseline accuracy.
    delta: Interval
    verdict: Verdict
    #: Candidate and baseline accuracy, each corrected the same way.
    baseline_accuracy: Interval
    candidate_accuracy: Interval
    #: What the cheap judge alone would have claimed, with its own interval.
    judge_only_delta: Interval
    judge_only_baseline_accuracy: float
    judge_only_candidate_accuracy: float
    n: int
    n_sampled: int
    n_annotated: int
    annotation_cost_usd: Decimal
    judge_cost_usd: Decimal
    #: Items the strong grader was paid for and produced nothing readable on.
    failed_annotations: int
    policy: dict[str, Any]
    judge: dict[str, Any]
    strong_grader: dict[str, Any]
    #: Items strong-only grading would need for the same standard error, and what that costs.
    strong_only_items: int | None
    strong_only_cost_usd: Decimal | None
    variance_of_strong_delta: float
    standard_error: float
    #: ``E[(d_H - d_G)^2]``: how far the cheap judge sits from the strong grader, per item.
    #: The quantity the cost-optimal rate is a function of.
    judge_mean_square_error: float
    #: The fixed sampling rate that would buy the most precision per dollar at this judge
    #: quality and this price ratio. 1.0 means "grade everything": the judge is wrong often
    #: enough that the cheap pass is not buying variance reduction.
    cost_optimal_rate: float
    cheap_cost_per_item_usd: Decimal
    strong_cost_per_item_usd: Decimal
    #: Stated limitations. Rendered verbatim; never summarised away.
    caveats: list[str] = field(default_factory=list)

    @property
    def annotation_share(self) -> float:
        return self.n_annotated / self.n if self.n else 0.0

    @property
    def judge_bias(self) -> float:
        """How far the judge-only delta sits from the corrected one, in accuracy points.

        Named ``bias`` rather than ``error`` because that is what it estimates: the correction
        term's whole job is to measure it.
        """
        return self.judge_only_delta.point - self.delta.point

    def rate_note(self) -> str:
        """What the cost-optimal rate is telling you about this judge.

        Reported because the answer is allowed to be unflattering. The mixed design pays off
        when the judge's error is small relative to the variance it is trying to explain; when
        it is not, the formula returns 1 and the honest advice is to grade everything.
        """
        if self.cost_optimal_rate >= 1.0:
            return (
                "The cost-optimal sampling rate is 1: on this workload the cheap judge "
                f"disagrees with the strong grader often enough (mean square error "
                f"{self.judge_mean_square_error:.3f} against a strong-grader variance of "
                f"{self.variance_of_strong_delta:.3f}) that it buys no variance reduction, and "
                "grading every item is the cheapest way to a given interval width. The "
                "estimate above is still unbiased; it is just paying for a cheap pass that is "
                "not earning its place. A judge that agreed more often — which is what a "
                "checkable output contract buys — would move this below 1."
            )
        return (
            f"The cost-optimal sampling rate at this judge quality and price ratio is "
            f"{self.cost_optimal_rate:.0%}; this run sampled "
            f"{float(self.policy.get('mean_rate', 0.0)):.0%} under its budget."
        )

    def sentence(self) -> str:
        points = self.delta.point * 100
        low, high = self.delta.low * 100, self.delta.high * 100
        return (
            f"Without labels: accuracy difference {points:+.1f} points, 95% CI "
            f"[{low:+.1f}, {high:+.1f}], n = {self.n}, from a cheap judge on every task "
            f"corrected by {self.n_annotated} strong-graded tasks "
            f"({self.annotation_share:.0%}) costing ${self.annotation_cost_usd}. "
            f"The judge alone would have said {self.judge_only_delta.point * 100:+.1f}."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_pipeline": self.baseline_pipeline,
            "candidate_pipeline": self.candidate_pipeline,
            "margin": self.margin,
            "delta_accuracy": self.delta.as_dict(),
            "verdict": {
                "label": self.verdict.label,
                "display": self.verdict.display,
                "margin": self.margin,
                "additional_tasks_needed": self.verdict.additional_tasks_needed,
                "note": self.verdict.note,
                "sentence": self.sentence(),
            },
            "baseline_accuracy": self.baseline_accuracy.as_dict(),
            "candidate_accuracy": self.candidate_accuracy.as_dict(),
            "judge_only": {
                "delta_accuracy": self.judge_only_delta.as_dict(),
                "baseline_accuracy": self.judge_only_baseline_accuracy,
                "candidate_accuracy": self.judge_only_candidate_accuracy,
                "bias_vs_corrected": self.judge_bias,
            },
            "annotation": {
                "n": self.n,
                "n_sampled": self.n_sampled,
                "n_annotated": self.n_annotated,
                "annotated_share": self.annotation_share,
                "failed_annotations": self.failed_annotations,
                "annotation_cost_usd": str(self.annotation_cost_usd),
                "judge_cost_usd": str(self.judge_cost_usd),
                "total_cost_usd": str(self.annotation_cost_usd + self.judge_cost_usd),
                "policy": self.policy,
                "strong_only_items_for_same_width": self.strong_only_items,
                "strong_only_cost_usd": (
                    str(self.strong_only_cost_usd)
                    if self.strong_only_cost_usd is not None
                    else None
                ),
                "variance_of_strong_delta": self.variance_of_strong_delta,
                "standard_error": self.standard_error,
                "judge_mean_square_error": self.judge_mean_square_error,
                "cost_optimal_rate": self.cost_optimal_rate,
                "cheap_cost_per_item_usd": str(self.cheap_cost_per_item_usd),
                "strong_cost_per_item_usd": str(self.strong_cost_per_item_usd),
                "cost_optimal_rate_note": self.rate_note(),
                "method": (
                    "cost-optimal active evaluation (Angelopoulos et al., arXiv:2506.07949): a "
                    "cheap judge scores every task, a strong grader scores a sample drawn with "
                    "probability pi(x), and the inverse-probability-weighted correction makes "
                    "the estimate unbiased for the strong grader's mean whatever the judge does"
                ),
            },
            "judge": self.judge,
            "strong_grader": self.strong_grader,
            "caveats": self.caveats,
        }


def _horvitz_thompson_mean(
    values: Sequence[float | None],
    sampled: Sequence[int],
    rates: Sequence[float],
) -> float:
    """``(1/T) sum xi_t * v_t / pi_t`` — the inverse-probability estimate of ``E[v]``.

    The one estimator shape this module uses for anything measured on the sampled subset. The
    annotated items are deliberately not a simple random sample, so a plain average over them
    would be weighted towards whatever the policy found interesting.
    """
    n = len(sampled)
    if n == 0:
        return 0.0
    total = 0.0
    for value, drawn, rate in zip(values, sampled, rates, strict=True):
        if drawn and value is not None:
            total += value / rate
    return total / n


def _horvitz_thompson_variance(
    diff_strong: Sequence[float | None],
    sampled: Sequence[int],
    rates: Sequence[float],
    mean: float,
) -> float:
    """Var(H_candidate - H_baseline) from a pi-weighted sample.

    The annotated items are not a simple random sample — the policy deliberately over-samples
    the informative ones — so a plain variance over them would be biased upward. The second
    moment is estimated the same way the mean is, ``(1/T) sum xi_t * d_t^2 / pi_t``, and the
    variance follows as ``E[d^2] - E[d]^2``.
    """
    squares: list[float | None] = [None if v is None else v**2 for v in diff_strong]
    return max(0.0, _horvitz_thompson_mean(squares, sampled, rates) - mean**2)


def build_judged_delta(
    annotations: Any,
    *,
    margin: float = DEFAULT_MARGIN,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    caveats: Sequence[str] = (),
) -> JudgedDelta:
    """Turn an annotation set into the gold-free comparison.

    ``annotations`` is an ``optimize.annotation.AnnotationSet``; it is typed loosely here for
    the same reason ``build_proof`` takes ``items_by_id`` loosely — this module is the statistics
    assembly and must not import the artifact layer back.
    """
    from tokop.core.stats import (
        active_eval_estimate,
        optimal_fixed_rate,
        paired_active_eval_estimate,
    )
    from tokop.optimize.annotation import AnnotationError, strong_only_items_for_width

    items = list(annotations.items)
    if not items:
        raise AnnotationError("the annotation set holds no items; there is nothing to estimate")

    task_ids = [item.task_id for item in items]
    g_base = [float(item.baseline.judge_correct) for item in items]
    g_cand = [float(item.candidate.judge_correct) for item in items]
    rates = [item.rate for item in items]
    # An item is sampled *for the estimator* only when both arms actually came back with a
    # strong label. A drawn item whose strong grading failed is not a cheaper observation, it is
    # no observation, and counting it as sampled with a missing value would silently bias the
    # correction towards whichever arm failed less often.
    sampled = [1 if (item.sampled and item.has_strong) else 0 for item in items]
    h_base: list[float | None] = [
        float(item.baseline.strong_correct) if drawn else None
        for item, drawn in zip(items, sampled, strict=True)
    ]
    h_cand: list[float | None] = [
        float(item.candidate.strong_correct) if drawn else None
        for item, drawn in zip(items, sampled, strict=True)
    ]
    if not any(sampled):
        raise AnnotationError(
            "no item in this annotation set has a strong label on both arms, so there is "
            "nothing to correct the judge with. The estimate would be the judge's own opinion "
            "wearing a confidence interval, which is the failure this estimator exists to "
            "prevent. Raise the annotation budget, or complete the human review queue."
        )

    paired = paired_active_eval_estimate(g_base, g_cand, h_base, h_cand, sampled, rates)
    base_arm = active_eval_estimate(g_base, h_base, sampled, rates)
    cand_arm = active_eval_estimate(g_cand, h_cand, sampled, rates)

    # What the judge alone claims, through the same bootstrap the gold path uses, so the two
    # intervals are comparable rather than merely adjacent.
    judge_only = delta_accuracy_interval(
        [int(v) for v in g_base], [int(v) for v in g_cand], resamples=resamples, seed=seed
    )

    n = len(items)
    p10 = sum(1 for b, c in zip(g_base, g_cand, strict=True) if b == 1 and c == 0) / n
    p01 = sum(1 for b, c in zip(g_base, g_cand, strict=True) if b == 0 and c == 1) / n
    verdict = verdict_from_interval(paired.interval, margin=margin, p10=p10, p01=p01, n=n)

    diff_strong: list[float | None] = [
        None if hb is None or hc is None else hc - hb for hb, hc in zip(h_base, h_cand, strict=True)
    ]
    variance = _horvitz_thompson_variance(diff_strong, sampled, rates, paired.estimate)
    diff_cheap = [gc - gb for gb, gc in zip(g_base, g_cand, strict=True)]
    residuals: list[float | None] = [
        None if d is None else (d - cheap) ** 2
        for d, cheap in zip(diff_strong, diff_cheap, strict=True)
    ]
    mse = _horvitz_thompson_mean(residuals, sampled, rates)
    strong_only = strong_only_items_for_width(variance, paired.standard_error)
    cost_per_item = Decimal(str(annotations.policy["cost_per_item_usd"]))
    judge_cost = Decimal(str(annotations.cost["judge_usd"]))
    cheap_per_item = (judge_cost / n).quantize(Decimal("0.00000001")) if n else Decimal(0)
    rate = optimal_fixed_rate(mse, variance, float(cheap_per_item), float(cost_per_item))
    strong_only_cost = (
        (Decimal(strong_only) * cost_per_item).quantize(Decimal("0.00001"))
        if strong_only is not None
        else None
    )

    failed = sum(
        1
        for item in items
        if item.sampled and (item.baseline.strong_failed or item.candidate.strong_failed)
    )
    assert len(task_ids) == n
    return JudgedDelta(
        baseline_pipeline=annotations.baseline_pipeline,
        candidate_pipeline=annotations.candidate_pipeline,
        margin=margin,
        delta=paired.interval,
        verdict=verdict,
        baseline_accuracy=base_arm.interval,
        candidate_accuracy=cand_arm.interval,
        judge_only_delta=judge_only,
        judge_only_baseline_accuracy=sum(g_base) / n,
        judge_only_candidate_accuracy=sum(g_cand) / n,
        n=n,
        n_sampled=sum(item.sampled for item in items),
        n_annotated=sum(sampled),
        annotation_cost_usd=Decimal(str(annotations.cost["strong_usd"])),
        judge_cost_usd=Decimal(str(annotations.cost["judge_usd"])),
        failed_annotations=failed,
        policy=dict(annotations.policy),
        judge=dict(annotations.judge),
        strong_grader=dict(annotations.strong_grader),
        strong_only_items=strong_only,
        strong_only_cost_usd=strong_only_cost,
        variance_of_strong_delta=variance,
        standard_error=paired.standard_error,
        judge_mean_square_error=mse,
        cost_optimal_rate=rate,
        cheap_cost_per_item_usd=cheap_per_item,
        strong_cost_per_item_usd=cost_per_item,
        caveats=list(caveats),
    )
