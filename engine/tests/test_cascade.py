"""The cascade simulator and threshold search (SPEC.md 7.5).

Two of the three M4 acceptance checks live here:
  * the threshold search on a synthetic fixture returns the known optimum;
  * the simulator agrees with brute-force per-task evaluation.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from tokop.optimize.cascade import (
    OBJECTIVE_COST,
    OBJECTIVE_SCARCE,
    CascadeError,
    brute_force,
    pareto_frontier,
    search_thresholds,
    simulate,
    tier_run_from_arrays,
)

CHEAP = Decimal("0.001")
MID = Decimal("0.004")
FRONTIER = Decimal("0.010")


def make_tiers(n: int = 100, *, seed: int = 7):
    """A synthetic fixture with a **known** optimum.

    Construction: the cheap tier is right on exactly the first 70 tasks and its scorer is
    perfect — it scores 0.9 when right and 0.1 when wrong. The frontier tier is right on
    everything. So a threshold anywhere in (0.1, 0.9] routes exactly the 70 cheap-correct tasks
    to the cheap tier and escalates the other 30, which is both the cheapest feasible setting
    and 100% accurate. Any threshold at or below 0.1 keeps the 30 wrong answers; any threshold
    above 0.9 escalates everything and pays for the cheap attempt on top.
    """
    del seed
    task_ids = [f"t{i:03d}" for i in range(n)]
    cheap_correct = [1 if i < 70 else 0 for i in range(n)]
    cheap_scores = [0.9 if c else 0.1 for c in cheap_correct]
    cheap = tier_run_from_arrays(
        "cheap", "m-cheap", task_ids, cheap_correct, [CHEAP] * n, cheap_scores
    )
    frontier = tier_run_from_arrays(
        "frontier", "m-frontier", task_ids, [1] * n, [FRONTIER] * n, [1.0] * n, scarce=True
    )
    return [cheap, frontier]


def make_three_tiers(n: int = 90):
    task_ids = [f"t{i:03d}" for i in range(n)]
    cheap_correct = [1 if i % 3 == 0 else 0 for i in range(n)]
    mid_correct = [1 if i % 3 != 2 else 0 for i in range(n)]
    return [
        tier_run_from_arrays(
            "cheap",
            "m-cheap",
            task_ids,
            cheap_correct,
            [CHEAP] * n,
            [0.8 if c else 0.2 for c in cheap_correct],
        ),
        tier_run_from_arrays(
            "mid",
            "m-mid",
            task_ids,
            mid_correct,
            [MID] * n,
            [0.7 if c else 0.3 for c in mid_correct],
        ),
        tier_run_from_arrays(
            "frontier", "m-frontier", task_ids, [1] * n, [FRONTIER] * n, [1.0] * n, scarce=True
        ),
    ]


class TestSimulator:
    def test_an_escalated_task_pays_for_every_attempt(self) -> None:
        """The rule that makes or breaks every saving the product reports."""
        tiers = make_tiers(n=10)
        # Threshold 0.5: tasks 0-6 stop at cheap, tasks 7-9 escalate.
        outcome = simulate(tiers, (0.5, 0.0))
        stopped = [
            c
            for c, t in zip(outcome.per_task_cost, outcome.resolved_tier_index, strict=True)
            if t == 0
        ]
        escalated = [
            c
            for c, t in zip(outcome.per_task_cost, outcome.resolved_tier_index, strict=True)
            if t == 1
        ]
        assert all(c == CHEAP for c in stopped)
        assert all(c == CHEAP + FRONTIER for c in escalated), "an escalated task must pay twice"

    def test_a_threshold_of_zero_never_escalates(self) -> None:
        outcome = simulate(make_tiers(), (0.0, 0.0))
        assert set(outcome.resolved_tier_index) == {0}
        assert outcome.accuracy == 0.7
        assert outcome.total_cost == CHEAP * 100

    def test_a_threshold_above_every_score_escalates_everything(self) -> None:
        outcome = simulate(make_tiers(), (1.01, 0.0))
        assert set(outcome.resolved_tier_index) == {1}
        assert outcome.accuracy == 1.0
        # And pays for the wasted cheap attempt on every task.
        assert outcome.total_cost == (CHEAP + FRONTIER) * 100

    def test_scarce_spend_is_tracked_separately(self) -> None:
        outcome = simulate(make_tiers(), (0.5, 0.0))
        assert outcome.scarce_cost == FRONTIER * 30
        assert outcome.scarce_share == pytest.approx(float(FRONTIER * 30 / outcome.total_cost))

    def test_tier_shares_sum_to_one(self) -> None:
        outcome = simulate(make_three_tiers(), (0.5, 0.5, 0.0))
        shares = outcome.tier_shares(3)
        assert sum(shares) == pytest.approx(1.0)

    def test_cost_per_successful_task_is_none_with_no_successes(self) -> None:
        n = 5
        ids = [f"t{i}" for i in range(n)]
        only = tier_run_from_arrays("only", "m", ids, [0] * n, [CHEAP] * n, [1.0] * n)
        assert simulate([only], (0.0,)).cost_per_successful_task is None

    def test_a_ragged_matrix_is_refused(self) -> None:
        with pytest.raises(CascadeError, match="ragged arrays"):
            tier_run_from_arrays("t", "m", ["a", "b"], [1], [CHEAP], [0.5])

    def test_tiers_answering_different_tasks_are_refused(self) -> None:
        a = tier_run_from_arrays("a", "m", ["t1"], [1], [CHEAP], [0.5])
        b = tier_run_from_arrays("b", "m", ["t2"], [1], [MID], [0.5])
        with pytest.raises(CascadeError, match="different task set"):
            simulate([a, b], (0.5, 0.0))

    def test_wrong_threshold_count_is_refused(self) -> None:
        with pytest.raises(CascadeError, match="one per tier"):
            simulate(make_tiers(), (0.5,))


class TestSimulatorAgreesWithBruteForce:
    """M4 acceptance: the simulator agrees with brute-force per-task evaluation."""

    @pytest.mark.parametrize("threshold", [0.0, 0.05, 0.1, 0.15, 0.5, 0.85, 0.9, 0.95, 1.0])
    def test_two_tiers_agree_at_every_threshold(self, threshold: float) -> None:
        tiers = make_tiers()
        fast = simulate(tiers, (threshold, 0.0))
        slow = brute_force(tiers, (threshold, 0.0))
        assert fast.total_cost == slow.total_cost
        assert fast.correct == slow.correct
        assert fast.resolved_tier_index == slow.resolved_tier_index
        assert fast.per_task_cost == slow.per_task_cost
        assert fast.scarce_cost == slow.scarce_cost

    def test_three_tiers_agree_across_a_grid(self) -> None:
        tiers = make_three_tiers()
        grid = [round(x * 0.1, 2) for x in range(11)]
        checked = 0
        for a, b in itertools.product(grid, grid):
            fast = simulate(tiers, (a, b, 0.0))
            slow = brute_force(tiers, (a, b, 0.0))
            assert fast.total_cost == slow.total_cost, (a, b)
            assert fast.correct == slow.correct, (a, b)
            assert fast.resolved_tier_index == slow.resolved_tier_index, (a, b)
            checked += 1
        assert checked == 121

    def test_they_agree_on_a_matrix_with_no_clean_separation(self) -> None:
        """Scores that interleave with correctness are where an off-by-one would hide."""
        n = 60
        ids = [f"t{i}" for i in range(n)]
        correct_a = [(i * 7) % 3 == 0 for i in range(n)]
        scores_a = [((i * 13) % 100) / 100 for i in range(n)]
        a = tier_run_from_arrays("a", "m", ids, [int(c) for c in correct_a], [CHEAP] * n, scores_a)
        b = tier_run_from_arrays("b", "m", ids, [1] * n, [FRONTIER] * n, [1.0] * n)
        for threshold in [round(x * 0.02, 2) for x in range(51)]:
            assert (
                simulate([a, b], (threshold, 0.0)).total_cost
                == brute_force([a, b], (threshold, 0.0)).total_cost
            )


class TestThresholdSearch:
    """M4 acceptance: the search on a synthetic fixture returns the known optimum."""

    def test_it_finds_the_known_optimum(self) -> None:
        tiers = make_tiers()
        result = search_thresholds(tiers, accuracy_floor=1.0, step=0.02)
        # Known construction: any threshold in (0.1, 0.9] routes exactly the 70 cheap-correct
        # tasks and is 100% accurate. The cheapest such setting costs
        #   70 x 0.001 + 30 x (0.001 + 0.010) = 0.070 + 0.330 = 0.400
        assert 0.1 < result.thresholds[0] <= 0.9
        assert result.outcome.accuracy == 1.0
        assert result.outcome.total_cost == Decimal("0.400")
        assert result.outcome.tier_shares(2) == pytest.approx([0.7, 0.3])
        assert result.feasible > 0

    def test_the_optimum_beats_both_extremes(self) -> None:
        tiers = make_tiers()
        best = search_thresholds(tiers, accuracy_floor=1.0, step=0.02).outcome
        all_frontier = simulate(tiers, (1.01, 0.0))
        assert best.total_cost < all_frontier.total_cost
        assert best.accuracy == all_frontier.accuracy

    def test_a_lower_floor_buys_a_cheaper_setting(self) -> None:
        tiers = make_tiers()
        strict = search_thresholds(tiers, accuracy_floor=1.0, step=0.02)
        loose = search_thresholds(tiers, accuracy_floor=0.70, step=0.02)
        assert loose.outcome.total_cost <= strict.outcome.total_cost
        assert loose.outcome.accuracy >= 0.70

    def test_protecting_scarce_models_changes_the_objective(self) -> None:
        tiers = make_three_tiers()
        cheapest = search_thresholds(tiers, accuracy_floor=0.6, step=0.05, objective=OBJECTIVE_COST)
        protected = search_thresholds(
            tiers, accuracy_floor=0.6, step=0.05, objective=OBJECTIVE_SCARCE
        )
        assert protected.objective == OBJECTIVE_SCARCE
        assert protected.outcome.scarce_cost <= cheapest.outcome.scarce_cost
        # Both must still respect the accuracy constraint.
        assert protected.outcome.accuracy >= 0.6

    def test_an_unreachable_floor_returns_the_best_available_with_a_note(self) -> None:
        tiers = make_tiers()
        cheap_only = [tiers[0]]
        result = search_thresholds(cheap_only, accuracy_floor=0.99, step=0.1)
        assert result.feasible == 0
        assert "No threshold setting reached" in result.note
        assert result.outcome.accuracy == 0.7

    def test_the_search_records_what_it_did(self) -> None:
        result = search_thresholds(make_tiers(), accuracy_floor=1.0, step=0.02)
        assert result.evaluated == 51  # one searchable tier, 0.00 to 1.00 at 0.02
        assert result.runtime_seconds >= 0
        assert len(result.frontier) == 51
        payload = result.as_dict()
        assert payload["objective"] == OBJECTIVE_COST
        assert payload["accuracy_floor"] == 1.0

    def test_three_tiers_search_the_full_grid(self) -> None:
        result = search_thresholds(make_three_tiers(), accuracy_floor=0.5, step=0.1)
        assert result.evaluated == 11 * 11

    def test_more_than_four_tiers_is_refused_rather_than_run(self) -> None:
        tiers = make_three_tiers() + make_three_tiers()[:2]
        with pytest.raises(CascadeError, match="up to four"):
            search_thresholds(tiers, accuracy_floor=0.5, step=0.5)

    def test_a_bad_step_is_refused(self) -> None:
        with pytest.raises(CascadeError, match="step must be"):
            search_thresholds(make_tiers(), accuracy_floor=0.5, step=0)


class TestParetoFrontier:
    def test_the_frontier_keeps_only_undominated_settings(self) -> None:
        entries = [
            ((0.1, 0.0), 0.80, Decimal("0.010"), 0.5),
            ((0.2, 0.0), 0.85, Decimal("0.012"), 0.5),
            ((0.3, 0.0), 0.82, Decimal("0.015"), 0.5),  # dominated by (0.2)
            ((0.4, 0.0), 0.90, Decimal("0.020"), 0.5),
        ]
        frontier = pareto_frontier(entries)
        accuracies = [e[1] for e in frontier]
        assert accuracies == [0.80, 0.85, 0.90]

    def test_the_frontier_is_monotone_in_both_axes(self) -> None:
        result = search_thresholds(make_three_tiers(), accuracy_floor=0.0, step=0.1)
        frontier = pareto_frontier(result.frontier)
        costs = [float(e[2]) for e in frontier]
        accuracies = [e[1] for e in frontier]
        assert costs == sorted(costs)
        assert accuracies == sorted(accuracies)
