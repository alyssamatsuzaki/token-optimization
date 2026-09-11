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
    mcnemar,
    non_inferiority_verdict,
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
