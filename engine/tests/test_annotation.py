"""The annotation budget goes where it buys interval (UPGRADE_V3.md U2).

Two claims are on trial here and neither is taken on faith:

* **the policy beats uniform sampling at the same budget.** Asserted as a variance reduction
  computed exactly, not as an eyeballed interval. The variance of the paired active estimator
  under a given ``pi`` is ``(1/T^2) sum delta_t^2 (1 - pi_t) / pi_t`` with
  ``delta_t = d_H_t - d_G_t``, so with the full judge and strong-grader labels in hand it can
  be evaluated for both policies without drawing anything at all. That removes the draw's noise
  from the comparison, which is the thing that would otherwise let a flattering seed decide it.
* **the estimator stays unbiased under a non-uniform policy.** This is the property people
  assume you broke when you started sampling unevenly, so it is measured by Monte Carlo over
  many draws rather than argued from the algebra.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from tokop.core.stats import active_eval_estimate, paired_active_eval_estimate
from tokop.optimize.annotation import (
    COST_OPTIMAL,
    UNIFORM,
    AnnotationError,
    ItemFeatures,
    allocate,
    allocation_policy,
    strong_only_items_for_width,
    uniform_draws,
)

#: Committed seeds. Every claim below holds at each of them; a claim that needed a particular
#: seed would be a claim about that seed.
SEEDS = (20260914, 20260915, 20260916, 20260917, 20260918)

BUDGETS = (Decimal("0.30"), Decimal("0.60"), Decimal("1.20"))
COST_PER_ITEM = Decimal("0.0124")


def _population(n: int = 200, seed: int = 7) -> tuple[list[ItemFeatures], np.ndarray, np.ndarray]:
    """A synthetic workload with a judge that is wrong where it says it is unsure.

    Built rather than borrowed from the fixtures so the relationship the policy exploits is
    explicit: the judge's error concentrates on items it flags as uncertain and on items the
    two arms disagree about, which is the assumption the allocation rests on. The demo's own
    recorded verdicts are exercised in ``test_judged_proof.py``.
    """
    rng = np.random.default_rng(seed)
    features: list[ItemFeatures] = []
    d_cheap = np.zeros(n)
    d_strong = np.zeros(n)
    for index in range(n):
        uncertain = rng.random() < 0.25
        base_unc = rng.uniform(0.6, 1.0) if uncertain else rng.uniform(0.0, 0.2)
        cand_unc = rng.uniform(0.6, 1.0) if uncertain else rng.uniform(0.0, 0.2)
        gb, gc = int(rng.random() < 0.9), int(rng.random() < 0.9)
        # The strong grader differs from the judge mostly where the judge was unsure.
        hb = 1 - gb if (uncertain and rng.random() < 0.5) else gb
        hc = 1 - gc if (uncertain and rng.random() < 0.5) else gc
        features.append(
            ItemFeatures(
                task_id=f"t{index:03d}",
                baseline_judged=gb,
                candidate_judged=gc,
                baseline_uncertainty=base_unc,
                candidate_uncertainty=cand_unc,
            )
        )
        d_cheap[index] = gc - gb
        d_strong[index] = hc - hb
    return features, d_cheap, d_strong


def exact_variance(rates: tuple[float, ...], delta: np.ndarray) -> float:
    """Var of the paired active estimate under these rates, with the labels known.

    ``Var(term_t) = delta_t^2 (1 - pi_t) / pi_t`` because the only random thing in the per-item
    term is ``xi_t ~ Bernoulli(pi_t)``. Summing and dividing by ``T^2`` gives the estimator's
    variance, which is what "interval width" means once the normal approximation is fixed.
    """
    pi = np.asarray(rates, dtype=float)
    return float(np.sum(delta**2 * (1 - pi) / pi) / len(pi) ** 2)


class TestTheAllocation:
    def test_the_budget_sets_the_mean_rate(self) -> None:
        features, _, _ = _population()
        allocation = allocate(
            features, budget_usd=Decimal("0.60"), cost_per_item_usd=COST_PER_ITEM, seed=1
        )
        expected = float(Decimal("0.60") / COST_PER_ITEM) / len(features)
        assert allocation.target_rate == pytest.approx(expected, rel=1e-9)
        assert np.mean(allocation.rates) == pytest.approx(expected, rel=1e-6)

    def test_no_item_is_ever_unsamplable(self) -> None:
        """A zero rate makes the inverse weight infinite and destroys the guarantee."""
        features, _, _ = _population()
        for policy in (COST_OPTIMAL, UNIFORM):
            allocation = allocate(
                features,
                policy=policy,
                budget_usd=Decimal("0.05"),
                cost_per_item_usd=COST_PER_ITEM,
                seed=1,
                floor=0.05,
            )
            assert min(allocation.rates) >= 0.05

    def test_discordant_pairs_are_sampled_harder_than_concordant_ones(self) -> None:
        features, _, _ = _population()
        allocation = allocate(
            features, budget_usd=Decimal("0.60"), cost_per_item_usd=COST_PER_ITEM, seed=1
        )
        rates = np.asarray(allocation.rates)
        discordant = np.array([f.discordant for f in features])
        assert rates[discordant].mean() > rates[~discordant].mean()

    def test_the_uniform_policy_really_is_uniform(self) -> None:
        features, _, _ = _population()
        allocation = allocate(
            features,
            policy=UNIFORM,
            budget_usd=Decimal("0.60"),
            cost_per_item_usd=COST_PER_ITEM,
            seed=1,
        )
        assert len(set(allocation.rates)) == 1

    def test_a_free_strong_rater_is_refused_as_a_bug(self) -> None:
        features, _, _ = _population()
        with pytest.raises(AnnotationError, match="free expensive rater"):
            allocate(features, budget_usd=Decimal("1"), cost_per_item_usd=Decimal(0), seed=1)

    def test_an_unknown_policy_names_the_registry(self) -> None:
        with pytest.raises(AnnotationError, match="cost-optimal"):
            allocation_policy("whatever-is-cheapest")

    def test_the_draw_is_keyed_by_task_so_order_cannot_change_it(self) -> None:
        ids = [f"t{i}" for i in range(20)]
        forward = uniform_draws(ids, 11)
        backward = uniform_draws(list(reversed(ids)), 11)
        assert forward[0] == pytest.approx(backward[-1])

    def test_a_smaller_budget_samples_a_subset(self) -> None:
        """What makes `prove --annotation-budget` able to replay a cheaper set out of an
        expensive one instead of needing a fresh annotation run."""
        features, _, _ = _population()
        big = allocate(
            features, budget_usd=Decimal("1.20"), cost_per_item_usd=COST_PER_ITEM, seed=3
        )
        small = allocate(
            features, budget_usd=Decimal("0.40"), cost_per_item_usd=COST_PER_ITEM, seed=3
        )
        chosen_big = {t for t, s in zip(big.task_ids, big.sampled, strict=True) if s}
        chosen_small = {t for t, s in zip(small.task_ids, small.sampled, strict=True) if s}
        assert chosen_small <= chosen_big
        assert len(chosen_small) < len(chosen_big)


class TestVarianceReduction:
    """U2's acceptance check."""

    def test_the_policy_beats_uniform_at_every_committed_budget(self) -> None:
        features, d_cheap, d_strong = _population()
        delta = d_strong - d_cheap
        for budget in BUDGETS:
            cost_optimal = allocate(
                features,
                policy=COST_OPTIMAL,
                budget_usd=budget,
                cost_per_item_usd=COST_PER_ITEM,
                seed=1,
            )
            uniform = allocate(
                features,
                policy=UNIFORM,
                budget_usd=budget,
                cost_per_item_usd=COST_PER_ITEM,
                seed=1,
            )
            # Same budget, same expected spend: the comparison is about *where* the money went.
            assert cost_optimal.expected_cost_usd == uniform.expected_cost_usd
            reduced = exact_variance(cost_optimal.rates, delta)
            flat = exact_variance(uniform.rates, delta)
            assert reduced < flat, f"no variance reduction at B={budget}"
            # Not a marginal win: the policy should cut the variance by a clear margin, or it is
            # not worth the complexity of having a policy at all.
            assert reduced / flat < 0.75, f"only {1 - reduced / flat:.1%} reduction at B={budget}"

    def test_the_realised_interval_is_narrower_at_every_committed_seed(self) -> None:
        """The exact variance above is the claim; this is the claim as a user meets it — an
        interval, from an actual draw, at a seed nobody chose after seeing the answer."""
        features, d_cheap, d_strong = _population()
        wins = 0
        for seed in SEEDS:
            widths = {}
            for policy in (COST_OPTIMAL, UNIFORM):
                allocation = allocate(
                    features,
                    policy=policy,
                    budget_usd=Decimal("0.60"),
                    cost_per_item_usd=COST_PER_ITEM,
                    seed=seed,
                )
                expensive: list[float | None] = [
                    float(value) if drawn else None
                    for value, drawn in zip(d_strong, allocation.sampled, strict=True)
                ]
                estimate = active_eval_estimate(
                    list(d_cheap), expensive, list(allocation.sampled), list(allocation.rates)
                )
                widths[policy] = estimate.interval.width
            wins += widths[COST_OPTIMAL] < widths[UNIFORM]
        assert wins == len(SEEDS), f"uniform sampling won at {len(SEEDS) - wins} committed seeds"


class TestUnbiasedness:
    """The property people assume a non-uniform policy broke."""

    def test_the_estimate_is_unbiased_under_the_non_uniform_policy(self) -> None:
        features, d_cheap, d_strong = _population(n=120)
        allocation = allocate(
            features,
            policy=COST_OPTIMAL,
            budget_usd=Decimal("0.30"),
            cost_per_item_usd=COST_PER_ITEM,
            seed=1,
        )
        rates = np.asarray(allocation.rates)
        truth = float(d_strong.mean())

        rng = np.random.default_rng(20260914)
        draws = 4000
        estimates = np.empty(draws)
        for index in range(draws):
            xi = (rng.random(len(rates)) < rates).astype(int)
            terms = d_cheap + (d_strong - d_cheap) * xi / rates
            estimates[index] = terms.mean()

        standard_error = estimates.std(ddof=1) / np.sqrt(draws)
        off_by = abs(estimates.mean() - truth)
        assert off_by < 4 * standard_error, (
            f"mean estimate {estimates.mean():.5f} is {off_by / standard_error:.1f} Monte Carlo "
            f"standard errors from the truth {truth:.5f}"
        )

    def test_a_deliberately_lopsided_policy_is_still_unbiased(self) -> None:
        """The strongest version: rates that have nothing to do with the data. Bias would show
        here first, and it does not, because the weight never depended on the rates being good.
        """
        n = 80
        rng = np.random.default_rng(5)
        d_cheap = rng.integers(-1, 2, size=n).astype(float)
        d_strong = d_cheap + rng.integers(-1, 2, size=n).astype(float)
        rates = np.where(np.arange(n) % 2 == 0, 0.9, 0.08)
        truth = float(d_strong.mean())

        estimates = []
        for _ in range(4000):
            xi = (rng.random(n) < rates).astype(int)
            estimates.append(float((d_cheap + (d_strong - d_cheap) * xi / rates).mean()))
        arr = np.array(estimates)
        assert abs(arr.mean() - truth) < 4 * arr.std(ddof=1) / np.sqrt(len(arr))

    def test_the_paired_estimator_agrees_with_the_single_arm_difference(self) -> None:
        """Pairing inside the estimator, rather than differencing two estimates afterwards, is
        an implementation choice. It has to give the same number."""
        features, _, _ = _population(n=60)
        allocation = allocate(
            features, budget_usd=Decimal("0.60"), cost_per_item_usd=COST_PER_ITEM, seed=2
        )
        rng = np.random.default_rng(1)
        gb = rng.integers(0, 2, size=60).astype(float)
        gc = rng.integers(0, 2, size=60).astype(float)
        hb = rng.integers(0, 2, size=60).astype(float)
        hc = rng.integers(0, 2, size=60).astype(float)
        drawn = list(allocation.sampled)
        exp_b: list[float | None] = [
            float(v) if d else None for v, d in zip(hb, drawn, strict=True)
        ]
        exp_c: list[float | None] = [
            float(v) if d else None for v, d in zip(hc, drawn, strict=True)
        ]
        paired = paired_active_eval_estimate(
            list(gb), list(gc), exp_b, exp_c, drawn, list(allocation.rates)
        )
        base = active_eval_estimate(list(gb), exp_b, drawn, list(allocation.rates))
        cand = active_eval_estimate(list(gc), exp_c, drawn, list(allocation.rates))
        assert paired.estimate == pytest.approx(cand.estimate - base.estimate, abs=1e-12)


class TestStrongOnlyComparison:
    def test_matching_a_standard_error_needs_variance_over_se_squared_items(self) -> None:
        assert strong_only_items_for_width(0.25, 0.05) == 100
        assert strong_only_items_for_width(0.1, 0.02) == 250

    def test_a_zero_standard_error_has_no_answer_rather_than_a_large_one(self) -> None:
        assert strong_only_items_for_width(0.25, 0.0) is None
        assert strong_only_items_for_width(0.0, 0.05) is None

    def test_a_negative_variance_is_refused(self) -> None:
        with pytest.raises(AnnotationError, match="variance"):
            strong_only_items_for_width(-1.0, 0.05)
