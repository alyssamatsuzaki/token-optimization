"""Tests for core/stats.py, including the two simulations SPEC.md 7.6 requires.

The simulations are the point: a confidence interval is a claim about long-run coverage, and
an estimator's unbiasedness is a claim about a long-run mean. Neither can be checked by
inspecting a single call, so both are checked by simulating from known ground truth.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tokop.core.stats import (
    DEFAULT_SEED,
    Interval,
    StatsError,
    active_eval_estimate,
    cost_per_successful_task,
    cost_per_successful_task_interval,
    cost_ratio_interval,
    delta_accuracy_interval,
    mcnemar,
    mcnemar_exact,
    non_inferiority_verdict,
    optimal_fixed_rate,
    paired_active_eval_estimate,
    required_n,
    uncertainty_proportional_rates,
    wilson,
)


class TestWilson:
    def test_hand_computed_midpoint(self) -> None:
        # 189/200 = 0.945. Wilson centre = (p + z^2/2n) / (1 + z^2/n).
        iv = wilson(189, 200)
        assert iv.point == pytest.approx(0.945)
        assert iv.low < 0.945 < iv.high
        assert iv.method == "Wilson score"
        assert iv.n == 200

    def test_stays_inside_the_unit_interval_at_the_edges(self) -> None:
        assert wilson(0, 20).low == 0.0
        assert wilson(20, 20).high == 1.0
        # And unlike the normal approximation, it is not a degenerate point at the edges.
        assert wilson(20, 20).low < 1.0
        assert wilson(0, 20).high > 0.0

    def test_rejects_impossible_inputs(self) -> None:
        with pytest.raises(StatsError, match="at least one observation"):
            wilson(0, 0)
        with pytest.raises(StatsError, match="between 0 and n"):
            wilson(5, 3)

    @pytest.mark.parametrize(("p", "n"), [(0.5, 40), (0.9, 100), (0.95, 200), (0.2, 60)])
    def test_coverage_simulation(self, p: float, n: int) -> None:
        """The interval covers the true proportion about 95% of the time.

        This is the property the product actually leans on when it prints an accuracy with a
        95% interval. 4,000 trials puts the standard error of the coverage estimate near
        0.35 points, so a +/-3 point band is a real test rather than a formality.
        """
        rng = np.random.default_rng(DEFAULT_SEED)
        trials = 4000
        draws = rng.binomial(n, p, size=trials)
        covered = sum(1 for k in draws if wilson(int(k), n).contains(p))
        coverage = covered / trials
        assert 0.92 <= coverage <= 0.98, f"coverage {coverage:.3f} at p={p}, n={n}"


class TestMcNemar:
    def test_hand_computed_symmetric_case(self) -> None:
        """10 vs 10 discordant pairs: perfectly symmetric, so p = 1."""
        assert mcnemar_exact(10, 10) == pytest.approx(1.0)

    def test_hand_computed_small_case(self) -> None:
        """b=0, c=5: two-sided exact binomial p = 2 * 0.5^5 = 0.0625."""
        assert mcnemar_exact(0, 5) == pytest.approx(0.0625)

    def test_hand_computed_one_of_six(self) -> None:
        """b=1, c=5, n=6: two-sided p = 2 * (C(6,0)+C(6,1)) / 2^6 = 2 * 7/64 = 0.21875."""
        assert mcnemar_exact(1, 5) == pytest.approx(0.21875)

    def test_no_discordant_pairs_is_p_one(self) -> None:
        assert mcnemar_exact(0, 0) == 1.0

    def test_counts_come_out_of_paired_outcomes(self) -> None:
        baseline = [1, 1, 1, 0, 0, 1]
        candidate = [1, 0, 0, 1, 0, 1]
        r = mcnemar(baseline, candidate)
        assert r.baseline_only == 2  # tasks 2 and 3
        assert r.candidate_only == 1  # task 4
        assert r.concordant == 3
        assert r.discordant == 3
        assert r.p_value == pytest.approx(mcnemar_exact(2, 1))

    def test_negative_counts_raise(self) -> None:
        with pytest.raises(StatsError, match="cannot be negative"):
            mcnemar_exact(-1, 2)

    def test_mismatched_arms_raise(self) -> None:
        with pytest.raises(StatsError, match="same tasks"):
            mcnemar([1, 0], [1, 0, 1])


class TestDeltaAccuracy:
    def test_point_estimate_is_the_mean_paired_difference(self) -> None:
        baseline = [1] * 90 + [0] * 10
        candidate = [1] * 85 + [0] * 15
        iv = delta_accuracy_interval(baseline, candidate, resamples=500)
        assert iv.point == pytest.approx(-0.05)
        assert iv.low < -0.05 < iv.high
        assert "seed" in iv.method

    def test_identical_arms_give_a_zero_width_interval(self) -> None:
        outcomes = [1, 0, 1, 1, 0] * 20
        iv = delta_accuracy_interval(outcomes, outcomes, resamples=200)
        assert iv.point == 0.0
        assert iv.low == 0.0 and iv.high == 0.0

    def test_is_reproducible_under_the_same_seed(self) -> None:
        b = [1, 1, 0, 1, 0, 1, 1, 1, 0, 1] * 10
        c = [1, 0, 0, 1, 1, 1, 1, 0, 0, 1] * 10
        first = delta_accuracy_interval(b, c, resamples=500, seed=7)
        second = delta_accuracy_interval(b, c, resamples=500, seed=7)
        assert (first.low, first.high) == (second.low, second.high)

    def test_non_binary_outcomes_raise(self) -> None:
        with pytest.raises(StatsError, match="only 0/1"):
            delta_accuracy_interval([0.5, 1], [1, 1], resamples=10)

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(StatsError, match="same tasks"):
            delta_accuracy_interval([1, 0], [1, 0, 1], resamples=10)


class TestCostPerSuccessfulTask:
    def test_point_estimate_is_total_cost_over_successes(self) -> None:
        costs = [0.01, 0.02, 0.03, 0.04]
        correct = [1, 1, 0, 1]
        # By hand: 0.10 total spent, 3 successes -> 0.03333... per success.
        assert cost_per_successful_task(costs, correct) == pytest.approx(0.1 / 3)

    def test_zero_successes_is_undefined_not_infinite(self) -> None:
        assert cost_per_successful_task([0.01, 0.02], [0, 0]) is None

    def test_interval_brackets_the_point(self) -> None:
        rng = np.random.default_rng(3)
        costs = list(rng.uniform(0.005, 0.02, size=200))
        correct = list(rng.binomial(1, 0.9, size=200))
        iv = cost_per_successful_task_interval(costs, correct, resamples=800)
        assert iv.low < iv.point < iv.high
        assert iv.dropped_resamples == 0

    def test_undefined_resamples_are_dropped_and_counted(self) -> None:
        """With only one success, many resamples contain none. They are dropped, not coerced."""
        costs = [0.01] * 12
        correct = [1] + [0] * 11
        iv = cost_per_successful_task_interval(costs, correct, resamples=2000, seed=11)
        assert iv.dropped_resamples > 0
        assert math.isfinite(iv.low) and math.isfinite(iv.high)

    def test_all_failures_raise_rather_than_returning_infinity(self) -> None:
        with pytest.raises(StatsError, match="undefined on the observed data"):
            cost_per_successful_task_interval([0.01] * 10, [0] * 10, resamples=100)

    def test_cost_ratio_below_one_when_candidate_is_cheaper(self) -> None:
        n = 200
        base_costs = [0.02] * n
        cand_costs = [0.005] * n
        outcomes = [1] * 190 + [0] * 10
        iv = cost_ratio_interval(base_costs, outcomes, cand_costs, outcomes, resamples=600)
        assert iv.point == pytest.approx(0.25)
        assert iv.high < 1.0

    def test_cost_ratio_rejects_ragged_inputs(self) -> None:
        with pytest.raises(StatsError, match="same length"):
            cost_ratio_interval([0.1], [1], [0.1, 0.2], [1, 1], resamples=10)


class TestVerdict:
    def test_clearly_equivalent_arms_are_non_inferior(self) -> None:
        rng = np.random.default_rng(1)
        baseline = list(rng.binomial(1, 0.95, size=400))
        candidate = list(baseline)
        v = non_inferiority_verdict(baseline, candidate, margin=0.03, resamples=800)
        assert v.label == "non_inferior"
        assert v.display == "Non-inferior at a 3-point margin"

    def test_clearly_worse_arms_are_worse(self) -> None:
        baseline = [1] * 200
        candidate = [1] * 140 + [0] * 60  # 30 points down
        v = non_inferiority_verdict(baseline, candidate, margin=0.03, resamples=800)
        assert v.label == "worse"
        assert v.display == "Worse"

    def test_a_straddling_interval_is_inconclusive_with_a_task_count(self) -> None:
        # A 1-point deficit on 60 tasks cannot resolve a 3-point margin.
        baseline = [1] * 57 + [0] * 3
        candidate = [1] * 56 + [0] * 4
        v = non_inferiority_verdict(baseline, candidate, margin=0.03, resamples=800)
        assert v.label == "inconclusive"
        assert v.additional_tasks_needed is not None
        assert v.additional_tasks_needed > 0
        assert "more tasks would settle it" in v.display

    def test_inconclusive_beyond_the_margin_says_more_tasks_will_not_help(self) -> None:
        baseline = [1] * 20 + [0] * 0
        candidate = [1] * 18 + [0] * 2  # 10 points down on 20 tasks: wide, and past the margin
        v = non_inferiority_verdict(baseline, candidate, margin=0.03, resamples=800)
        assert v.label == "inconclusive"
        assert v.additional_tasks_needed is None
        assert "unlikely" in v.display

    def test_zero_margin_is_refused(self) -> None:
        with pytest.raises(StatsError, match="never be proven"):
            non_inferiority_verdict([1, 0], [1, 0], margin=0.0, resamples=10)

    def test_required_n_matches_the_formula_by_hand(self) -> None:
        """p10 = 0.05, p01 = 0.03, delta = -0.02, margin = 0.03.

        v = 0.05 + 0.03 - (0.05 - 0.03)^2 = 0.08 - 0.0004 = 0.0796
        n >= 1.96^2 * 0.0796 / (0.01)^2 = 3.8415 * 0.0796 / 0.0001 = 3057.8 -> 3058
        """
        n = required_n(0.05, 0.03, -0.02, 0.03)
        assert n == 3058

    def test_required_n_is_none_past_the_margin(self) -> None:
        assert required_n(0.10, 0.02, -0.08, 0.03) is None
        assert required_n(0.10, 0.02, -0.03, 0.03) is None  # exactly at the margin

    def test_required_n_with_no_discordant_pairs_is_one(self) -> None:
        assert required_n(0.0, 0.0, 0.0, 0.03) == 1

    def test_required_n_rejects_impossible_proportions(self) -> None:
        with pytest.raises(StatsError, match=r"\[0, 1\]"):
            required_n(1.5, 0.0, 0.0, 0.03)


class TestActiveEvaluation:
    def test_perfect_cheap_rater_needs_no_correction(self) -> None:
        cheap = [1.0, 0.0, 1.0, 1.0]
        expensive = [1.0, None, None, None]
        est = active_eval_estimate(cheap, expensive, [1, 0, 0, 0], [0.25] * 4)
        assert est.estimate == pytest.approx(0.75)
        assert est.n_sampled == 1
        assert est.expensive_calls_saved == 3

    def test_sampling_everything_reproduces_the_expensive_mean(self) -> None:
        expensive = [1.0, 0.0, 1.0, 1.0, 0.0]
        est = active_eval_estimate([0.5] * 5, expensive, [1] * 5, [1.0] * 5)
        assert est.estimate == pytest.approx(0.6)

    def test_unbiasedness_simulation(self) -> None:
        """2,000 trials against known ground truth; the mean error must vanish.

        This is the property that makes the estimator worth having: it is unbiased for E[H]
        *whatever* the cheap rater does, because the inverse-probability weight rescales the
        correction. The cheap rater here is deliberately biased (it systematically overrates)
        to prove the correction is doing the work rather than the cheap rater being good.
        """
        rng = np.random.default_rng(DEFAULT_SEED)
        n_items, trials, rate = 200, 2000, 0.2
        # Ground truth: H is Bernoulli(0.7) per item. G overrates: it says 1 for 85% of items.
        h_truth = rng.binomial(1, 0.7, size=n_items).astype(float)
        g = rng.binomial(1, 0.85, size=n_items).astype(float)
        target = float(h_truth.mean())

        errors = []
        for _ in range(trials):
            xi = rng.binomial(1, rate, size=n_items)
            expensive: list[float | None] = [
                float(h_truth[i]) if xi[i] else None for i in range(n_items)
            ]
            est = active_eval_estimate(list(g), expensive, list(xi), [rate] * n_items)
            errors.append(est.estimate - target)

        mean_error = float(np.mean(errors))
        # Standard error of the mean across trials; the bound is ~4 of them.
        se = float(np.std(errors, ddof=1) / math.sqrt(trials))
        assert abs(mean_error) < 4 * se, f"mean error {mean_error:.5f} vs 4*SE {4 * se:.5f}"
        assert abs(mean_error) < 0.005

        # And the naive cheap-only estimate is badly biased, which is the point of the estimator.
        assert abs(float(g.mean()) - target) > 0.05

    def test_paired_version_estimates_the_difference(self) -> None:
        cheap_base = [1.0, 1.0, 0.0, 1.0]
        cheap_cand = [1.0, 0.0, 0.0, 1.0]
        exp_base: list[float | None] = [1.0, 1.0, None, None]
        exp_cand: list[float | None] = [1.0, 1.0, None, None]
        est = paired_active_eval_estimate(
            cheap_base, cheap_cand, exp_base, exp_cand, [1, 1, 0, 0], [0.5] * 4
        )
        # Items 0 and 1 sampled at rate 0.5: cheap diffs are 0 and -1, expensive diffs 0 and 0.
        # terms = [0 + (0-0)/0.5, -1 + (0-(-1))/0.5, 0, 0] = [0, 1, 0, 0] -> mean 0.25
        assert est.estimate == pytest.approx(0.25)

    def test_sampled_item_without_a_rating_raises(self) -> None:
        with pytest.raises(StatsError, match="never produced"):
            active_eval_estimate([1.0, 1.0], [None, None], [1, 0], [0.5, 0.5])

    def test_zero_rate_is_refused(self) -> None:
        with pytest.raises(StatsError, match="inverse weight infinite"):
            active_eval_estimate([1.0], [None], [0], [0.0])

    def test_rate_above_one_is_refused(self) -> None:
        with pytest.raises(StatsError, match="at most 1"):
            active_eval_estimate([1.0], [1.0], [1], [1.5])

    def test_ragged_inputs_are_refused(self) -> None:
        with pytest.raises(StatsError, match="same length"):
            active_eval_estimate([1.0, 1.0], [None], [1, 0], [0.5, 0.5])


class TestOptimalFixedRate:
    def test_cheap_and_accurate_rater_samples_rarely(self) -> None:
        # MSE tiny relative to Var(H): p* is small.
        rate = optimal_fixed_rate(mse=0.01, var_h=0.25, cost_cheap=0.001, cost_expensive=0.1)
        assert 0 < rate < 0.1

    def test_boundary_condition_gives_exactly_one(self) -> None:
        """At MSE = (c_h / (c_h + c_g)) * Var(H) the formula is continuous and equals 1."""
        c_g, c_h, var_h = 0.02, 0.08, 0.25
        mse = (c_h / (c_h + c_g)) * var_h
        assert optimal_fixed_rate(mse - 1e-12, var_h, c_g, c_h) == pytest.approx(1.0, abs=1e-5)
        assert optimal_fixed_rate(mse, var_h, c_g, c_h) == 1.0

    def test_useless_cheap_rater_samples_everything(self) -> None:
        assert optimal_fixed_rate(mse=0.5, var_h=0.25, cost_cheap=0.01, cost_expensive=0.1) == 1.0

    def test_zero_variance_target_samples_everything(self) -> None:
        assert optimal_fixed_rate(mse=0.0, var_h=0.0, cost_cheap=0.01, cost_expensive=0.1) == 1.0

    def test_rejects_nonsense(self) -> None:
        with pytest.raises(StatsError, match="MSE cannot be negative"):
            optimal_fixed_rate(-1, 0.2, 0.01, 0.1)
        with pytest.raises(StatsError, match=r"Var\(H\) cannot be negative"):
            optimal_fixed_rate(0.1, -0.2, 0.01, 0.1)
        with pytest.raises(StatsError, match="expensive rater positive"):
            optimal_fixed_rate(0.1, 0.2, 0.01, 0.0)


class TestUncertaintyProportionalRates:
    def test_mean_rate_matches_the_target_when_nothing_clips(self) -> None:
        u = [0.4, 0.5, 0.6, 0.5]
        rates = uncertainty_proportional_rates(u, 0.3)
        assert float(rates.mean()) == pytest.approx(0.3)

    def test_the_floor_bounds_the_inverse_weight(self) -> None:
        rates = uncertainty_proportional_rates([0.0, 0.0, 1.0], 0.2)
        assert rates.min() >= 0.05
        assert (1 / rates).max() <= 20

    def test_rates_never_exceed_one(self) -> None:
        rates = uncertainty_proportional_rates([0.001, 10.0], 0.9)
        assert rates.max() <= 1.0

    def test_all_zero_uncertainty_falls_back_to_the_flat_target(self) -> None:
        rates = uncertainty_proportional_rates([0.0, 0.0, 0.0], 0.25)
        assert list(rates) == [0.25, 0.25, 0.25]

    def test_rejects_nonsense(self) -> None:
        with pytest.raises(StatsError, match="at least one item"):
            uncertainty_proportional_rates([], 0.2)
        with pytest.raises(StatsError, match="cannot be negative"):
            uncertainty_proportional_rates([-1.0], 0.2)
        with pytest.raises(StatsError, match=r"target_rate must be in \(0, 1\]"):
            uncertainty_proportional_rates([1.0], 0.0)
        with pytest.raises(StatsError, match=r"floor must be in \(0, 1\]"):
            uncertainty_proportional_rates([1.0], 0.2, floor=0.0)


class TestInterval:
    def test_width_and_containment(self) -> None:
        iv = Interval(point=0.5, low=0.4, high=0.6)
        assert iv.width == pytest.approx(0.2)
        assert iv.contains(0.45) and not iv.contains(0.7)

    def test_as_dict_carries_the_method(self) -> None:
        d = wilson(5, 10).as_dict()
        assert d["method"] == "Wilson score"
        assert d["n"] == 10
