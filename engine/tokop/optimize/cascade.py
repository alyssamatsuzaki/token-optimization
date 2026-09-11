"""The cascade and its simulator (SPEC.md 7.5, FrugalGPT adapted).

A cascade is an ordered list of tiers cheapest first, a scorer per tier, and a threshold per
tier. Each tier answers, the scorer estimates the probability the answer is right, and the first
answer clearing its tier's threshold is returned. The last tier has no threshold: something has
to be returned.

**An escalated task pays for every attempt it made.** A task that goes cheap → mid pays for
both calls. Any simulator that forgets this reports savings that do not exist, so it is enforced
in one place and tested directly against brute force.

The simulator exists because evaluating a threshold setting must not cost money. Once every tier
has answered every task — which is what the recording produces — any threshold setting can be
evaluated exactly from the recorded matrix, so a grid search over thousands of settings is
arithmetic rather than an API bill. The live confirmation run then checks the simulator against
reality.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np

DEFAULT_STEP = 0.02
#: The last tier answers whatever it produces, so its threshold is meaningless. Stored as 0 so
#: the arrays stay rectangular and nothing has to special-case the length.
FINAL_TIER_THRESHOLD = 0.0


class CascadeError(ValueError):
    """A cascade could not be simulated or searched."""


@dataclass(frozen=True)
class TierRun:
    """One tier's recorded answers over a split: what it said, what it cost, how it scored."""

    tier: str
    model_id: str
    task_ids: tuple[str, ...]
    correct: tuple[int, ...]
    cost_usd: tuple[Decimal, ...]
    scores: tuple[float, ...]
    scarce: bool = False

    def __post_init__(self) -> None:
        lengths = {
            len(self.task_ids),
            len(self.correct),
            len(self.cost_usd),
            len(self.scores),
        }
        if len(lengths) != 1:
            raise CascadeError(
                f"tier {self.tier!r} has ragged arrays: {sorted(lengths)}. Every tier must have "
                "answered every task for the simulator to be exact."
            )

    @property
    def n(self) -> int:
        return len(self.task_ids)


@dataclass(frozen=True)
class CascadeOutcome:
    """What one threshold setting produces on one split."""

    thresholds: tuple[float, ...]
    total_cost: Decimal
    correct: tuple[int, ...]
    resolved_tier_index: tuple[int, ...]
    per_task_cost: tuple[Decimal, ...]
    task_ids: tuple[str, ...]
    scarce_cost: Decimal

    @property
    def n(self) -> int:
        return len(self.task_ids)

    @property
    def successes(self) -> int:
        return sum(self.correct)

    @property
    def accuracy(self) -> float:
        return self.successes / self.n if self.n else 0.0

    @property
    def cost_per_successful_task(self) -> Decimal | None:
        return self.total_cost / self.successes if self.successes else None

    @property
    def scarce_share(self) -> float:
        return float(self.scarce_cost / self.total_cost) if self.total_cost else 0.0

    def tier_shares(self, tier_count: int) -> list[float]:
        """Share of tasks resolved at each tier."""
        if not self.n:
            return [0.0] * tier_count
        counts = [0] * tier_count
        for index in self.resolved_tier_index:
            counts[index] += 1
        return [c / self.n for c in counts]

    def reached_final_share(self) -> float:
        if not self.n:
            return 0.0
        last = max(self.resolved_tier_index) if self.resolved_tier_index else 0
        return sum(1 for i in self.resolved_tier_index if i == last) / self.n


def simulate(tiers: Sequence[TierRun], thresholds: Sequence[float]) -> CascadeOutcome:
    """Evaluate one threshold setting exactly, from the recorded response matrix.

    ``thresholds`` has one entry per tier; the last is ignored because the last tier always
    answers. A task escalates when its score at a tier is **below** that tier's threshold, and
    it pays for every tier it touched on the way.
    """
    if not tiers:
        raise CascadeError("a cascade needs at least one tier")
    if len(thresholds) != len(tiers):
        raise CascadeError(
            f"{len(thresholds)} thresholds for {len(tiers)} tiers; supply one per tier "
            "(the last is ignored)"
        )
    task_ids = tiers[0].task_ids
    for tier in tiers[1:]:
        if tier.task_ids != task_ids:
            raise CascadeError(
                f"tier {tier.tier!r} answered a different task set from tier {tiers[0].tier!r}; "
                "the simulator is exact only over a complete matrix"
            )

    correct: list[int] = []
    resolved: list[int] = []
    per_task: list[Decimal] = []
    scarce_total = Decimal(0)
    total = Decimal(0)

    for i in range(len(task_ids)):
        spent = Decimal(0)
        scarce_spent = Decimal(0)
        chosen = len(tiers) - 1
        for index, tier in enumerate(tiers):
            # Every tier touched is paid for, whether or not its answer is returned.
            spent += tier.cost_usd[i]
            if tier.scarce:
                scarce_spent += tier.cost_usd[i]
            is_last = index == len(tiers) - 1
            if is_last or tier.scores[i] >= thresholds[index]:
                chosen = index
                break
        correct.append(tiers[chosen].correct[i])
        resolved.append(chosen)
        per_task.append(spent)
        total += spent
        scarce_total += scarce_spent

    return CascadeOutcome(
        thresholds=tuple(thresholds),
        total_cost=total,
        correct=tuple(correct),
        resolved_tier_index=tuple(resolved),
        per_task_cost=tuple(per_task),
        task_ids=task_ids,
        scarce_cost=scarce_total,
    )


def brute_force(tiers: Sequence[TierRun], thresholds: Sequence[float]) -> CascadeOutcome:
    """A second, deliberately naive implementation, used only to check ``simulate``.

    Written independently — a per-task Python loop with no shared helpers — because a simulator
    that agrees only with itself proves nothing. ``tests/test_cascade.py`` asserts the two agree
    across a grid of settings.
    """
    task_ids = tiers[0].task_ids
    correct, resolved, per_task = [], [], []
    scarce_total = Decimal(0)
    for i in range(len(task_ids)):
        paid = Decimal(0)
        scarce_paid = Decimal(0)
        answer_index = None
        for index in range(len(tiers)):
            paid = paid + tiers[index].cost_usd[i]
            if tiers[index].scarce:
                scarce_paid = scarce_paid + tiers[index].cost_usd[i]
            if index == len(tiers) - 1:
                answer_index = index
                break
            if not tiers[index].scores[i] < thresholds[index]:
                answer_index = index
                break
        assert answer_index is not None
        correct.append(tiers[answer_index].correct[i])
        resolved.append(answer_index)
        per_task.append(paid)
        scarce_total = scarce_total + scarce_paid
    return CascadeOutcome(
        thresholds=tuple(thresholds),
        total_cost=sum(per_task, Decimal(0)),
        correct=tuple(correct),
        resolved_tier_index=tuple(resolved),
        per_task_cost=tuple(per_task),
        task_ids=task_ids,
        scarce_cost=scarce_total,
    )


@dataclass
class SearchResult:
    """What the threshold search found, and what it looked at."""

    thresholds: tuple[float, ...]
    outcome: CascadeOutcome
    objective: str
    accuracy_floor: float
    evaluated: int
    runtime_seconds: float
    feasible: int
    #: Every evaluated setting, for the cost-quality frontier chart.
    frontier: list[tuple[tuple[float, ...], float, Decimal, float]] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "thresholds": list(self.thresholds),
            "objective": self.objective,
            "accuracy_floor": self.accuracy_floor,
            "evaluated": self.evaluated,
            "feasible": self.feasible,
            "runtime_seconds": round(self.runtime_seconds, 3),
            "accuracy": self.outcome.accuracy,
            "total_cost_usd": str(self.outcome.total_cost),
            "cost_per_task_usd": str(self.outcome.total_cost / self.outcome.n)
            if self.outcome.n
            else None,
            "scarce_share": self.outcome.scarce_share,
            "note": self.note,
        }


OBJECTIVE_COST = "cost_per_task"
OBJECTIVE_SCARCE = "scarce_model_spend"


def search_thresholds(
    tiers: Sequence[TierRun],
    *,
    accuracy_floor: float,
    step: float = DEFAULT_STEP,
    objective: str = OBJECTIVE_COST,
) -> SearchResult:
    """Exhaustive grid search on the **calibration** split.

    Minimizes cost per task — or scarce-model spend, with "Protect scarce models" on — subject
    to calibration accuracy of at least ``accuracy_floor``. Exhaustive for up to four tiers at a
    0.02 step is 51^3 = 132,651 settings at worst, which is arithmetic over recorded answers and
    takes well under a second; the runtime is logged so a future larger cascade shows its cost.

    The operating point is chosen here, on calibration, **before any test result is computed**.
    That ordering is the whole reason the test-split number is worth anything.
    """
    if not tiers:
        raise CascadeError("a cascade needs at least one tier")
    if len(tiers) > 4:
        raise CascadeError(
            f"{len(tiers)} tiers: the exhaustive search is specified for up to four "
            "(SPEC.md 7.5). Add a search strategy before adding a tier."
        )
    if not 0 < step <= 1:
        raise CascadeError("step must be in (0, 1]")

    grid = [round(x * step, 10) for x in range(int(1 / step) + 1)]
    searchable = len(tiers) - 1  # the last tier always answers
    started = time.perf_counter()

    best: SearchResult | None = None
    frontier: list[tuple[tuple[float, ...], float, Decimal, float]] = []
    evaluated = 0
    feasible = 0

    combinations = itertools.product(grid, repeat=searchable) if searchable else [()]
    for combo in combinations:
        thresholds = (*combo, FINAL_TIER_THRESHOLD)
        outcome = simulate(tiers, thresholds)
        evaluated += 1
        per_task = outcome.total_cost / outcome.n if outcome.n else Decimal(0)
        frontier.append((thresholds, outcome.accuracy, per_task, outcome.scarce_share))

        if outcome.accuracy < accuracy_floor:
            continue
        feasible += 1
        score = float(outcome.scarce_cost) if objective == OBJECTIVE_SCARCE else float(per_task)
        if best is None:
            best_score = float("inf")
        else:
            best_per_task = (
                best.outcome.total_cost / best.outcome.n if best.outcome.n else Decimal(0)
            )
            best_score = (
                float(best.outcome.scarce_cost)
                if objective == OBJECTIVE_SCARCE
                else float(best_per_task)
            )
        # Ties break towards higher accuracy: two settings that cost the same are not equally
        # good, and preferring the more accurate one costs the user nothing.
        better = score < best_score or (
            score == best_score and best is not None and outcome.accuracy > best.outcome.accuracy
        )
        if best is None or better:
            best = SearchResult(
                thresholds=thresholds,
                outcome=outcome,
                objective=objective,
                accuracy_floor=accuracy_floor,
                evaluated=evaluated,
                runtime_seconds=0.0,
                feasible=feasible,
            )

    runtime = time.perf_counter() - started

    if best is None:
        # No setting clears the floor. Returning the most accurate setting with a note is more
        # useful than raising: the caller can show why the cascade cannot be built here.
        fallback = max(frontier, key=lambda entry: (entry[1], -float(entry[2])))
        outcome = simulate(tiers, fallback[0])
        return SearchResult(
            thresholds=fallback[0],
            outcome=outcome,
            objective=objective,
            accuracy_floor=accuracy_floor,
            evaluated=evaluated,
            runtime_seconds=runtime,
            feasible=0,
            frontier=frontier,
            note=(
                f"No threshold setting reached the {accuracy_floor:.1%} calibration floor. The "
                f"most accurate setting available reaches {outcome.accuracy:.1%}. Either the "
                "cheap tiers are not good enough for this workload or the scorer cannot tell "
                "when they are right."
            ),
        )

    best.runtime_seconds = runtime
    best.evaluated = evaluated
    best.feasible = feasible
    best.frontier = frontier
    return best


def pareto_frontier(
    entries: Sequence[tuple[tuple[float, ...], float, Decimal, float]],
) -> list[tuple[tuple[float, ...], float, Decimal, float]]:
    """The cost-accuracy frontier: settings nothing else beats on both axes.

    The chart draws every evaluated setting, but the frontier is what a reader is choosing
    between, so it is computed rather than eyeballed.
    """
    ordered = sorted(entries, key=lambda e: (float(e[2]), -e[1]))
    out: list[tuple[tuple[float, ...], float, Decimal, float]] = []
    best_accuracy = -1.0
    for entry in ordered:
        if entry[1] > best_accuracy:
            out.append(entry)
            best_accuracy = entry[1]
    return out


def tier_run_from_arrays(
    tier: str,
    model_id: str,
    task_ids: Sequence[str],
    correct: Sequence[int],
    cost_usd: Sequence[Decimal],
    scores: Sequence[float] | np.ndarray,
    *,
    scarce: bool = False,
) -> TierRun:
    return TierRun(
        tier=tier,
        model_id=model_id,
        task_ids=tuple(task_ids),
        correct=tuple(int(c) for c in correct),
        cost_usd=tuple(cost_usd),
        scores=tuple(float(s) for s in scores),
        scarce=scarce,
    )
