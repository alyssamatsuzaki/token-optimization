"""Spend the annotation budget where it buys interval (UPGRADE_V3.md U2).

``core/stats.py`` has had ``active_eval_estimate`` and its paired form since M1, and until now
nothing decided *who* gets the expensive label. This module does. It allocates a sampling
probability ``pi(x)`` to every task under a stated budget B, draws the sample, and prices what
that draw cost.

**Why a non-uniform policy is worth the complexity.** With the cheap rater's score ``d_G`` and
the strong rater's ``d_H``, the paired estimator's per-item term is
``d_G + (d_H - d_G) * xi / pi``, whose variance is ``delta^2 * (1 - pi) / pi`` for
``delta = d_H - d_G``. Minimising the sum of those under a budget constraint is Neyman
allocation: the optimal rate is proportional to ``|delta|`` — to *how wrong the judge is on
that item*, which is exactly what nobody knows in advance. So the policy samples proportionally
to a proxy for ``E|delta|``:

* **the judge's own uncertainty on each arm.** A calibrated judge that says 0.55 is telling you
  it is close to a coin flip on that item, and that is where ``H`` is most likely to differ.
  The two arms' uncertainties add, because either one differing moves the pair.
* **disagreement between the two arms.** A discordant pair is where the point estimate lives,
  and it is also where a judge error *fails to cancel*: on a concordant pair a judge that is
  wrong the same way about both answers contributes nothing to the difference, while on a
  discordant pair the same error moves the estimate by a whole item.

Both are computable before a dollar of strong grading is spent. Whether they actually buy
interval is not argued here — ``tests/test_annotation.py`` measures it against uniform sampling
at the same budget and fails if the variance reduction is not there.

**Unbiasedness does not depend on any of this.** The inverse-probability weight makes the
correction term's expectation ``E[H - G]`` for *any* positive ``pi``, so a policy that
allocates badly widens the interval and never moves the estimate. That is why the floor matters
and a zero rate is refused: at ``pi = 0`` the weight is infinite and the guarantee is gone.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from tokop.core.stats import MIN_SAMPLING_RATE, StatsError, uncertainty_proportional_rates

COST_OPTIMAL = "cost-optimal"
UNIFORM = "uniform"

#: How much more a discordant pair is sampled than a concordant one at equal judge uncertainty.
#: One extra unit of weight, on a scale where each arm's uncertainty contributes at most one:
#: a discordant pair with a confident judge is sampled about as hard as a concordant pair the
#: judge is completely unsure about on both arms. Chosen rather than fitted, and the test that
#: justifies it measures interval width rather than arguing from this number.
DISCORDANT_BOOST = 1.0

ANNOTATION_SCHEMA = "tokop.annotation-set.v1"

#: Where an annotation set lives, next to the fixtures it was drawn against.
ANNOTATIONS_FILE = "annotations.json"

#: The second set: the same machinery pointed at the checkable-contract pair rather than at the
#: proof's two arms, so the lever in UPGRADE_V3.md U4 can be priced in the same units.
CONTRACT_ANNOTATIONS_FILE = "contract-annotations.json"


def annotations_path(root: Path, *, contract: bool = False) -> Path:
    return root / (CONTRACT_ANNOTATIONS_FILE if contract else ANNOTATIONS_FILE)


class AnnotationError(ValueError):
    """The annotation budget could not be allocated, or an annotation set is unusable."""


# --------------------------------------------------------------------------- the policy


@dataclass(frozen=True)
class ItemFeatures:
    """What the policy may look at before deciding whether to buy a strong label.

    All of it is known from the cheap judge alone. Nothing here is a label.
    """

    task_id: str
    #: The cheap judge's verdict on each arm, 0 or 1.
    baseline_judged: int
    candidate_judged: int
    #: Binary entropy of the judge's stated confidence on each arm, in [0, 1].
    baseline_uncertainty: float
    candidate_uncertainty: float

    @property
    def discordant(self) -> bool:
        return self.baseline_judged != self.candidate_judged

    def weight(self) -> float:
        """The proxy for ``E|d_H - d_G|`` this item carries."""
        return (
            self.baseline_uncertainty
            + self.candidate_uncertainty
            + (DISCORDANT_BOOST if self.discordant else 0.0)
        )


@dataclass(frozen=True)
class AllocationPolicy:
    """One registered way of turning features and a budget into sampling rates."""

    name: str
    description: str

    def weights(self, features: Sequence[ItemFeatures]) -> np.ndarray:
        if self.name == UNIFORM:
            return np.ones(len(features), dtype=float)
        return np.array([f.weight() for f in features], dtype=float)


POLICY_REGISTRY: dict[str, AllocationPolicy] = {
    COST_OPTIMAL: AllocationPolicy(
        name=COST_OPTIMAL,
        description=(
            "cost-optimal active evaluation: sampling rate proportional to the judge's "
            "uncertainty on both arms plus a boost for pairs the two arms disagree on, scaled "
            "so the expected spend meets the budget and floored so no item is unsamplable"
        ),
    ),
    UNIFORM: AllocationPolicy(
        name=UNIFORM,
        description="uniform: every item sampled with the same probability",
    ),
}


def allocation_policy(name: str) -> AllocationPolicy:
    try:
        return POLICY_REGISTRY[name]
    except KeyError:
        raise AnnotationError(
            f"unknown annotation policy {name!r}; the registry offers "
            f"{', '.join(sorted(POLICY_REGISTRY))}"
        ) from None


def uniform_draws(task_ids: Sequence[str], seed: int) -> np.ndarray:
    """A fixed uniform draw per task, keyed by task id and seed.

    Keyed rather than sequential so that the draw is a property of the *task*: it does not move
    when the split is reordered, and — the part that matters — a smaller budget lowers every
    rate, so ``u_t < pi_t`` selects a **subset** of what a larger budget selected. That is what
    lets `tokop prove --annotation-budget` replay a cheaper annotation set out of an expensive
    one instead of needing a fresh run.
    """
    out = np.empty(len(task_ids), dtype=float)
    for index, task_id in enumerate(task_ids):
        digest = hashlib.sha256(f"{seed}|{task_id}".encode()).hexdigest()
        out[index] = int(digest[:12], 16) / 0xFFFFFFFFFFFF
    return out


@dataclass(frozen=True)
class Allocation:
    """Rates, draws, and what the budget bought."""

    policy: str
    task_ids: tuple[str, ...]
    rates: tuple[float, ...]
    sampled: tuple[int, ...]
    target_rate: float
    budget_usd: Decimal
    cost_per_item_usd: Decimal
    floor: float
    seed: int
    #: True when the floor alone already costs more than the budget, so the budget could not be
    #: honoured without making some item unsamplable.
    floor_exceeds_budget: bool = False

    @property
    def n(self) -> int:
        return len(self.task_ids)

    @property
    def n_sampled(self) -> int:
        return int(sum(self.sampled))

    @property
    def expected_cost_usd(self) -> Decimal:
        return (Decimal(str(sum(self.rates))) * self.cost_per_item_usd).quantize(Decimal("0.00001"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "n": self.n,
            "n_sampled": self.n_sampled,
            "sampled_share": self.n_sampled / self.n if self.n else 0.0,
            "target_rate": self.target_rate,
            "mean_rate": float(np.mean(self.rates)) if self.rates else 0.0,
            "min_rate": min(self.rates) if self.rates else 0.0,
            "max_rate": max(self.rates) if self.rates else 0.0,
            "budget_usd": str(self.budget_usd),
            "cost_per_item_usd": str(self.cost_per_item_usd),
            "expected_cost_usd": str(self.expected_cost_usd),
            "floor": self.floor,
            "seed": self.seed,
            "floor_exceeds_budget": self.floor_exceeds_budget,
        }


def allocate(
    features: Sequence[ItemFeatures],
    *,
    policy: str = COST_OPTIMAL,
    budget_usd: Decimal,
    cost_per_item_usd: Decimal,
    seed: int,
    floor: float = MIN_SAMPLING_RATE,
) -> Allocation:
    """Choose pi(x) under a budget, and draw.

    ``cost_per_item_usd`` is what one strong-graded **item** costs — both arms, since the
    estimator is paired and half an item buys nothing.
    """
    if not features:
        raise AnnotationError("there is nothing to allocate over: no items")
    if cost_per_item_usd <= 0:
        raise AnnotationError(
            f"a strong-graded item is priced at ${cost_per_item_usd}. A free expensive rater "
            "would make the whole allocation question vacuous, so this is refused as a bug."
        )
    if budget_usd < 0:
        raise AnnotationError(f"the annotation budget is ${budget_usd}, which is not a budget")

    chosen = allocation_policy(policy)
    n = len(features)
    affordable = float(budget_usd / cost_per_item_usd)
    target = min(1.0, max(floor, affordable / n))
    weights = chosen.weights(features)
    try:
        rates = uncertainty_proportional_rates([float(w) for w in weights], target, floor=floor)
    except StatsError as exc:  # pragma: no cover - guarded by the checks above
        raise AnnotationError(str(exc)) from exc

    draws = uniform_draws([f.task_id for f in features], seed)
    sampled = (draws < rates).astype(int)
    return Allocation(
        policy=policy,
        task_ids=tuple(f.task_id for f in features),
        rates=tuple(float(r) for r in rates),
        sampled=tuple(int(s) for s in sampled),
        target_rate=target,
        budget_usd=budget_usd,
        cost_per_item_usd=cost_per_item_usd,
        floor=floor,
        seed=seed,
        floor_exceeds_budget=floor * n * float(cost_per_item_usd) > float(budget_usd),
    )


# ------------------------------------------------------------------- what it would have cost


def strong_only_items_for_width(variance_h: float, standard_error: float) -> int | None:
    """How many items strong-only grading needs to match a standard error.

    Grading a sample of m items with the strong rater and nothing else gives
    ``se = sqrt(Var(H) / m)``, so matching an active estimator's ``se`` needs
    ``m = Var(H) / se^2``. ``None`` when the standard error is zero or the variance is
    unknown, where the question has no answer rather than a large one.
    """
    if variance_h < 0:
        raise AnnotationError("a variance cannot be negative")
    if standard_error <= 0 or variance_h == 0:
        return None
    return math.ceil(variance_h / (standard_error**2))


def rate_for_standard_error(
    mean_square_error: float, n: int, target_standard_error: float
) -> float:
    """The uniform sampling rate that buys a given standard error (UPGRADE_V3.md U4).

    At a uniform rate ``pi`` the active estimator's variance is ``mse * (1 - pi) / (pi * T)``
    with ``mse = E[(H - G)^2]``, so matching a target ``se`` needs

        pi = mse / (mse + T * se^2)

    which is the formula behind the whole checkability argument: a judge that agrees with the
    strong grader more often has a smaller ``mse``, and a smaller ``mse`` needs a smaller
    ``pi`` for the same interval width. Returns 1.0 when even sampling everything would not
    reach it, and clamps to (0, 1].
    """
    if n <= 0:
        raise AnnotationError("a rate needs at least one item")
    if target_standard_error <= 0:
        raise AnnotationError("a target standard error must be positive")
    if mean_square_error <= 0:
        # A judge that never disagrees with the strong grader needs no strong labels at all —
        # except that believing that requires having checked, so the floor still applies.
        return MIN_SAMPLING_RATE
    denominator = mean_square_error + n * target_standard_error**2
    return float(min(1.0, max(MIN_SAMPLING_RATE, mean_square_error / denominator)))


def budget_for_standard_error(
    mean_square_error: float,
    n: int,
    target_standard_error: float,
    cost_cheap_per_item: Decimal,
    cost_strong_per_item: Decimal,
) -> tuple[Decimal, float]:
    """(dollars, rate) to reach a target standard error with this judge.

    The cheap pass is paid for every item because that is what a cheap judge is for; the strong
    grader is paid for the sampled share. Both prices are measured, not assumed.
    """
    rate = rate_for_standard_error(mean_square_error, n, target_standard_error)
    cheap = cost_cheap_per_item * Decimal(n)
    strong = cost_strong_per_item * Decimal(str(rate)) * Decimal(n)
    return (cheap + strong).quantize(Decimal("0.00001")), rate


# --------------------------------------------------------------------------- the artifact


@dataclass
class AnnotatedArm:
    """One arm's verdicts for one task."""

    tier: str
    answer_hash: str
    judge_correct: int
    judge_confidence: float
    judge_parsed: bool
    judge_cassette_key: str
    strong_correct: int | None = None
    strong_cassette_key: str | None = None
    strong_failed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "answer_hash": self.answer_hash,
            "judge_correct": self.judge_correct,
            "judge_confidence": self.judge_confidence,
            "judge_parsed": self.judge_parsed,
            "judge_cassette_key": self.judge_cassette_key,
            "strong_correct": self.strong_correct,
            "strong_cassette_key": self.strong_cassette_key,
            "strong_failed": self.strong_failed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AnnotatedArm:
        return cls(
            tier=str(raw["tier"]),
            answer_hash=str(raw["answer_hash"]),
            judge_correct=int(raw["judge_correct"]),
            judge_confidence=float(raw["judge_confidence"]),
            judge_parsed=bool(raw["judge_parsed"]),
            judge_cassette_key=str(raw["judge_cassette_key"]),
            strong_correct=(
                None if raw.get("strong_correct") is None else int(raw["strong_correct"])
            ),
            strong_cassette_key=raw.get("strong_cassette_key"),
            strong_failed=bool(raw.get("strong_failed", False)),
        )


@dataclass
class AnnotatedItem:
    """One task: both arms' verdicts, the rate it was offered and whether it was drawn."""

    task_id: str
    rate: float
    sampled: int
    baseline: AnnotatedArm
    candidate: AnnotatedArm

    @property
    def has_strong(self) -> bool:
        return (
            self.baseline.strong_correct is not None and self.candidate.strong_correct is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "rate": self.rate,
            "sampled": self.sampled,
            "baseline": self.baseline.as_dict(),
            "candidate": self.candidate.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AnnotatedItem:
        return cls(
            task_id=str(raw["task_id"]),
            rate=float(raw["rate"]),
            sampled=int(raw["sampled"]),
            baseline=AnnotatedArm.from_dict(raw["baseline"]),
            candidate=AnnotatedArm.from_dict(raw["candidate"]),
        )


@dataclass
class AnnotationSet:
    """Everything one `tokop annotate` run produced, committed next to the fixtures.

    It is a record of decisions and of money, not a cache: the verdicts in it were read out of
    cassettes, and ``tokop fixtures-check`` replays every one of them and fails if a verdict
    here disagrees with the call it came from.
    """

    workload: str
    split: str
    baseline_pipeline: str
    candidate_pipeline: str
    operating_point: dict[str, Any]
    judge: dict[str, Any]
    strong_grader: dict[str, Any]
    policy: dict[str, Any]
    cost: dict[str, Any]
    items: list[AnnotatedItem]
    origin: str = "simulated"
    generated_at: str = ""
    schema: str = ANNOTATION_SCHEMA

    @property
    def n(self) -> int:
        return len(self.items)

    @property
    def n_sampled(self) -> int:
        return sum(item.sampled for item in self.items)

    @property
    def n_annotated(self) -> int:
        return sum(1 for item in self.items if item.has_strong)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workload": self.workload,
            "split": self.split,
            "baseline_pipeline": self.baseline_pipeline,
            "candidate_pipeline": self.candidate_pipeline,
            "operating_point": self.operating_point,
            "judge": self.judge,
            "strong_grader": self.strong_grader,
            "policy": self.policy,
            "cost": self.cost,
            "origin": self.origin,
            "generated_at": self.generated_at or datetime.now(UTC).isoformat(),
            "items": [item.as_dict() for item in self.items],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True))
        return path

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AnnotationSet:
        schema = str(raw.get("schema", ""))
        if schema != ANNOTATION_SCHEMA:
            raise AnnotationError(
                f"annotation set schema is {schema!r}, not {ANNOTATION_SCHEMA!r}. Re-run "
                "`tokop annotate` rather than reading a set this build does not understand."
            )
        return cls(
            workload=str(raw["workload"]),
            split=str(raw["split"]),
            baseline_pipeline=str(raw["baseline_pipeline"]),
            candidate_pipeline=str(raw["candidate_pipeline"]),
            operating_point=dict(raw["operating_point"]),
            judge=dict(raw["judge"]),
            strong_grader=dict(raw["strong_grader"]),
            policy=dict(raw["policy"]),
            cost=dict(raw["cost"]),
            items=[AnnotatedItem.from_dict(row) for row in raw["items"]],
            origin=str(raw.get("origin", "simulated")),
            generated_at=str(raw.get("generated_at", "")),
            schema=schema,
        )

    @classmethod
    def read(cls, path: Path) -> AnnotationSet:
        if not path.exists():
            raise AnnotationError(
                f"no annotation set at {path}. Judged grading needs one: run "
                "`tokop annotate` to buy the strong labels the estimator corrects with."
            )
        return cls.from_dict(json.loads(path.read_text()))

    def restrict_to_budget(self, budget_usd: Decimal) -> AnnotationSet:
        """The same set as it would have been drawn at a smaller budget.

        Rates scale linearly with the budget below the floor and the cap, and the draw ``u_t``
        is fixed per task, so a lower budget selects a subset. Refused upward: items the larger
        budget never sampled have no strong label, and inventing one is the whole failure mode
        this product exists to avoid.
        """
        recorded = Decimal(str(self.policy["budget_usd"]))
        if budget_usd > recorded:
            raise AnnotationError(
                f"this annotation set was drawn at a ${recorded} budget and you asked for "
                f"${budget_usd}. The extra items were never strong-graded, so the estimate "
                f"cannot be computed at that budget here. Run `tokop annotate "
                f"--budget {budget_usd}` to buy them."
            )
        if budget_usd == recorded:
            return self
        scale = float(budget_usd / recorded) if recorded > 0 else 0.0
        floor = float(self.policy["floor"])
        draws = uniform_draws([item.task_id for item in self.items], int(self.policy["seed"]))
        restricted: list[AnnotatedItem] = []
        for index, item in enumerate(self.items):
            rate = max(floor, min(1.0, item.rate * scale))
            keep = int(draws[index] < rate)
            arms = (item.baseline, item.candidate)
            baseline, candidate = (
                AnnotatedArm(
                    tier=arm.tier,
                    answer_hash=arm.answer_hash,
                    judge_correct=arm.judge_correct,
                    judge_confidence=arm.judge_confidence,
                    judge_parsed=arm.judge_parsed,
                    judge_cassette_key=arm.judge_cassette_key,
                    strong_correct=arm.strong_correct if keep else None,
                    strong_cassette_key=arm.strong_cassette_key if keep else None,
                    strong_failed=arm.strong_failed if keep else False,
                )
                for arm in arms
            )
            restricted.append(
                AnnotatedItem(
                    task_id=item.task_id,
                    rate=rate,
                    sampled=keep,
                    baseline=baseline,
                    candidate=candidate,
                )
            )
        spent = sum(
            (
                Decimal(str(self.policy["cost_per_item_usd"]))
                for item in restricted
                if item.sampled and item.has_strong
            ),
            Decimal(0),
        )
        return AnnotationSet(
            workload=self.workload,
            split=self.split,
            baseline_pipeline=self.baseline_pipeline,
            candidate_pipeline=self.candidate_pipeline,
            operating_point=self.operating_point,
            judge=self.judge,
            strong_grader=self.strong_grader,
            policy={**self.policy, "budget_usd": str(budget_usd), "restricted_from": str(recorded)},
            cost={**self.cost, "strong_usd": str(spent), "restricted": True},
            items=restricted,
            origin=self.origin,
            generated_at=self.generated_at,
        )


@dataclass
class QueueReport:
    """What a human-review annotation run produced instead of verdicts."""

    path: Path
    entries: int
    reason: str = ""
    written: bool = True
    extra: dict[str, Any] = field(default_factory=dict)
