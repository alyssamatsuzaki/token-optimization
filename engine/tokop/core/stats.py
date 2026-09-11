"""Proof statistics (SPEC.md 7.6).

Everything the product claims about quality comes out of this module, so it is written to be
read: each function states what it estimates, what it assumes, and what it refuses to do.

Two ideas do the work.

*Pairing.* Baseline and candidate answer the **same** tasks, so the informative quantity is the
per-task difference, not two independent accuracies. Pairing removes task difficulty from the
comparison and is why a 200-task split can settle a 3-point margin at all.

*Non-inferiority.* "Not worse by more than d" is the claim a buyer needs, and it is not the
same as "no significant difference". A verdict here is a statement about where the confidence
interval sits relative to -d, never about a p-value crossing 0.05.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy import stats as scipy_stats

DEFAULT_SEED = 20260911
DEFAULT_RESAMPLES = 5000
DEFAULT_MARGIN = 0.03
Z_95 = 1.959963984540054


class StatsError(ValueError):
    """A statistic was asked for that the data cannot support."""


@dataclass(frozen=True)
class Interval:
    """A point estimate with a two-sided confidence interval."""

    point: float
    low: float
    high: float
    level: float = 0.95
    n: int = 0
    method: str = ""
    dropped_resamples: int = 0

    @property
    def width(self) -> float:
        return self.high - self.low

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "point": self.point,
            "low": self.low,
            "high": self.high,
            "level": self.level,
            "n": self.n,
            "method": self.method,
            "dropped_resamples": self.dropped_resamples,
        }


# --------------------------------------------------------------------------- accuracy


def wilson(successes: int, n: int, level: float = 0.95) -> Interval:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because it stays inside [0, 1] and keeps close to
    nominal coverage near 0 and 1 — exactly where an accuracy claim about a good pipeline
    lives. ``tests/test_stats.py`` runs the coverage simulation that SPEC.md 7.6 asks for.
    """
    if n <= 0:
        raise StatsError("Wilson interval needs at least one observation")
    if not 0 <= successes <= n:
        raise StatsError(f"successes ({successes}) must be between 0 and n ({n})")
    z = float(scipy_stats.norm.ppf(0.5 + level / 2))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return Interval(
        point=p,
        low=max(0.0, centre - half),
        high=min(1.0, centre + half),
        level=level,
        n=n,
        method="Wilson score",
    )


# --------------------------------------------------------------------------- bootstrap


def _resample_indices(n: int, resamples: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, n, size=(resamples, n))


def paired_bootstrap(
    n: int,
    statistic: Callable[[np.ndarray], float | None],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
    method: str = "paired bootstrap",
) -> Interval:
    """Percentile bootstrap over **tasks**, resampling the pairing intact.

    ``statistic`` receives an array of task indices and returns the statistic on that resample,
    or ``None`` when the resample cannot support it (for instance cost per successful task when
    a resample happens to contain no successes). Undefined resamples are dropped and counted
    rather than coerced to infinity, which would make the interval meaningless (DECISIONS.md D8).
    """
    if n <= 0:
        raise StatsError("bootstrap needs at least one task")
    point = statistic(np.arange(n))
    if point is None:
        raise StatsError("the statistic is undefined on the observed data")
    draws: list[float] = []
    dropped = 0
    for idx in _resample_indices(n, resamples, seed):
        value = statistic(idx)
        if value is None:
            dropped += 1
            continue
        draws.append(value)
    if len(draws) < resamples // 10:
        raise StatsError(
            f"only {len(draws)} of {resamples} bootstrap resamples were defined; the interval "
            "would not mean anything"
        )
    arr = np.sort(np.array(draws))
    tail = (1 - level) / 2
    low = float(np.quantile(arr, tail))
    high = float(np.quantile(arr, 1 - tail))
    return Interval(
        point=float(point),
        low=low,
        high=high,
        level=level,
        n=n,
        method=f"{method}, {len(draws)} resamples, seed {seed}",
        dropped_resamples=dropped,
    )


def _as_binary(values: Sequence[int | bool], name: str) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise StatsError(f"{name} must be one-dimensional")
    if not np.isin(arr, (0.0, 1.0)).all():
        raise StatsError(f"{name} must contain only 0/1 outcomes")
    return arr


def delta_accuracy_interval(
    baseline: Sequence[int | bool],
    candidate: Sequence[int | bool],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
) -> Interval:
    """Candidate accuracy minus baseline accuracy, paired over tasks.

    Positive means the candidate is *better*. The non-inferiority verdict reads the lower bound
    of this interval against -margin.
    """
    b = _as_binary(baseline, "baseline")
    c = _as_binary(candidate, "candidate")
    if len(b) != len(c):
        raise StatsError(
            f"paired comparison needs the same tasks in both arms: {len(b)} vs {len(c)}"
        )
    diff = c - b

    def stat(idx: np.ndarray) -> float:
        return float(diff[idx].mean())

    return paired_bootstrap(
        len(b),
        stat,
        resamples=resamples,
        seed=seed,
        level=level,
        method="paired bootstrap, delta accuracy",
    )


def cost_per_successful_task(costs: Sequence[float], correct: Sequence[int | bool]) -> float | None:
    """Total cost divided by the number of successes. ``None`` when nothing succeeded."""
    successes = float(np.asarray(correct, dtype=float).sum())
    if successes == 0:
        return None
    return float(np.asarray(costs, dtype=float).sum()) / successes


def cost_per_successful_task_interval(
    costs: Sequence[float],
    correct: Sequence[int | bool],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
) -> Interval:
    """Bootstrap interval for cost per successful task, resampling tasks.

    Cost and outcome move together within a task, so both are resampled by the same index —
    a resample that draws an expensive failure pays for it, as the real workload would.
    """
    cost_arr = np.asarray(costs, dtype=float)
    ok = _as_binary(correct, "correct")
    if len(cost_arr) != len(ok):
        raise StatsError(f"costs and outcomes must line up: {len(cost_arr)} vs {len(ok)}")

    def stat(idx: np.ndarray) -> float | None:
        successes = ok[idx].sum()
        if successes == 0:
            return None
        return float(cost_arr[idx].sum() / successes)

    return paired_bootstrap(
        len(cost_arr),
        stat,
        resamples=resamples,
        seed=seed,
        level=level,
        method="paired bootstrap, cost per successful task",
    )


def cost_ratio_interval(
    baseline_costs: Sequence[float],
    baseline_correct: Sequence[int | bool],
    candidate_costs: Sequence[float],
    candidate_correct: Sequence[int | bool],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
) -> Interval:
    """Candidate cost per successful task divided by baseline's, paired over tasks.

    Below 1 means the candidate is cheaper per success. The ratio is bootstrapped directly
    rather than divided after the fact, because the two arms share the resampled task set and
    their errors are correlated.
    """
    bc = np.asarray(baseline_costs, dtype=float)
    bo = _as_binary(baseline_correct, "baseline_correct")
    cc = np.asarray(candidate_costs, dtype=float)
    co = _as_binary(candidate_correct, "candidate_correct")
    lengths = {len(bc), len(bo), len(cc), len(co)}
    if len(lengths) != 1:
        raise StatsError(f"all four arrays must have the same length, got {sorted(lengths)}")

    def stat(idx: np.ndarray) -> float | None:
        b_succ, c_succ = bo[idx].sum(), co[idx].sum()
        if b_succ == 0 or c_succ == 0:
            return None
        base = bc[idx].sum() / b_succ
        if base == 0:
            return None
        return float((cc[idx].sum() / c_succ) / base)

    return paired_bootstrap(
        len(bc),
        stat,
        resamples=resamples,
        seed=seed,
        level=level,
        method="paired bootstrap, cost ratio",
    )


# --------------------------------------------------------------------------- McNemar


@dataclass(frozen=True)
class McNemarResult:
    """Discordant pairs and the exact two-sided p-value."""

    baseline_only: int  # baseline correct, candidate wrong
    candidate_only: int  # candidate correct, baseline wrong
    p_value: float
    concordant: int

    @property
    def discordant(self) -> int:
        return self.baseline_only + self.candidate_only


def mcnemar_exact(baseline_only: int, candidate_only: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts.

    Under the null the discordant pairs split 50/50, so this is an exact binomial test on
    ``candidate_only`` out of ``baseline_only + candidate_only`` at p = 0.5. The exact test is
    used rather than the chi-square approximation because the discordant count on a 200-task
    split is routinely small enough for the approximation to mislead.
    """
    if baseline_only < 0 or candidate_only < 0:
        raise StatsError("discordant counts cannot be negative")
    n = baseline_only + candidate_only
    if n == 0:
        return 1.0
    result = scipy_stats.binomtest(candidate_only, n, 0.5, alternative="two-sided")
    return float(result.pvalue)


def mcnemar(baseline: Sequence[int | bool], candidate: Sequence[int | bool]) -> McNemarResult:
    b = _as_binary(baseline, "baseline")
    c = _as_binary(candidate, "candidate")
    if len(b) != len(c):
        raise StatsError(f"paired test needs the same tasks in both arms: {len(b)} vs {len(c)}")
    baseline_only = int(((b == 1) & (c == 0)).sum())
    candidate_only = int(((b == 0) & (c == 1)).sum())
    return McNemarResult(
        baseline_only=baseline_only,
        candidate_only=candidate_only,
        p_value=mcnemar_exact(baseline_only, candidate_only),
        concordant=len(b) - baseline_only - candidate_only,
    )


# --------------------------------------------------------------------------- verdict

VerdictLabel = Literal["non_inferior", "inconclusive", "worse"]


@dataclass(frozen=True)
class Verdict:
    """Where the delta-accuracy interval sits relative to the margin."""

    label: VerdictLabel
    margin: float
    delta: Interval
    additional_tasks_needed: int | None = None
    note: str = ""

    @property
    def display(self) -> str:
        margin_points = self.margin * 100
        if self.label == "non_inferior":
            return f"Non-inferior at a {margin_points:.0f}-point margin"
        if self.label == "worse":
            return "Worse"
        if self.additional_tasks_needed is None:
            return "Inconclusive: more tasks are unlikely to settle it"
        return f"Inconclusive: about {self.additional_tasks_needed:,} more tasks would settle it"


def required_n(p10: float, p01: float, delta_hat: float, margin: float) -> int | None:
    """Tasks needed for the lower bound of the delta interval to clear -margin.

    ``v = p10 + p01 - (p10 - p01)^2`` is the variance of the per-task paired difference, which
    takes values in {-1, 0, +1}. Solving ``delta_hat - 1.96*sqrt(v/n) > -margin`` for n gives
    ``n >= 1.96^2 * v / (delta_hat + margin)^2``.

    Returns ``None`` when ``delta_hat + margin <= 0``: the candidate's observed deficit is
    already at or past the margin, so more tasks tighten the interval around a point that is
    on the wrong side of it.
    """
    if not 0 <= p10 <= 1 or not 0 <= p01 <= 1:
        raise StatsError("discordant proportions must be in [0, 1]")
    slack = delta_hat + margin
    if slack <= 0:
        return None
    v = p10 + p01 - (p10 - p01) ** 2
    if v <= 0:
        return 1
    return math.ceil(Z_95**2 * v / slack**2)


def non_inferiority_verdict(
    baseline: Sequence[int | bool],
    candidate: Sequence[int | bool],
    *,
    margin: float = DEFAULT_MARGIN,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> Verdict:
    """Non-inferior when the interval's lower bound clears -margin; worse when its upper
    bound falls below -margin; inconclusive when it straddles."""
    if margin <= 0:
        raise StatsError("margin must be positive; a zero margin can never be proven")
    delta = delta_accuracy_interval(baseline, candidate, resamples=resamples, seed=seed)
    b = _as_binary(baseline, "baseline")
    c = _as_binary(candidate, "candidate")
    n = len(b)

    if delta.low > -margin:
        return Verdict("non_inferior", margin, delta)
    if delta.high < -margin:
        return Verdict("worse", margin, delta)

    p10 = float(((b == 1) & (c == 0)).sum()) / n
    p01 = float(((b == 0) & (c == 1)).sum()) / n
    needed = required_n(p10, p01, delta.point, margin)
    if needed is None:
        return Verdict(
            "inconclusive",
            margin,
            delta,
            None,
            "The observed accuracy difference is already at or beyond the margin, so more "
            "tasks are unlikely to change the verdict.",
        )
    return Verdict("inconclusive", margin, delta, max(0, needed - n))


# --------------------------------------------------------------------- active evaluation


@dataclass(frozen=True)
class ActiveEstimate:
    """Result of the cost-optimal active evaluation estimator (SPEC.md Appendix A2)."""

    estimate: float
    standard_error: float
    interval: Interval
    n_sampled: int
    n_total: int
    per_item_terms: tuple[float, ...]

    @property
    def expensive_calls_saved(self) -> int:
        return self.n_total - self.n_sampled


def active_eval_estimate(
    cheap: Sequence[float],
    expensive: Sequence[float | None],
    sampled: Sequence[int | bool],
    rates: Sequence[float],
    *,
    level: float = 0.95,
) -> ActiveEstimate:
    """Unbiased estimate of the expensive rater's mean score from a sampled subset.

    From Angelopoulos et al. (arXiv:2506.07949): a cheap rater G scores every item, an
    expensive rater H scores a sample drawn with probability pi(x), and

        theta-hat = (1/T) * sum_t [ G_t + (H_t - G_t) * xi_t / pi(X_t) ]

    is unbiased for E[H] because the inverse-probability weight makes the correction term's
    expectation equal ``E[H - G]`` regardless of how good G is. G only has to be *cheap*; if it
    is also *accurate* the correction term is small and the variance collapses.

    The interval comes from the empirical variance of the per-item terms, which is the honest
    spread: it widens automatically when the cheap rater is unreliable or the rates are low.
    """
    g = np.asarray(cheap, dtype=float)
    xi = _as_binary(sampled, "sampled")
    pi = np.asarray(rates, dtype=float)
    if not (len(g) == len(xi) == len(pi) == len(expensive)):
        raise StatsError("cheap, expensive, sampled and rates must all have the same length")
    if len(g) == 0:
        raise StatsError("the estimator needs at least one item")
    if np.any(pi <= 0):
        raise StatsError(
            "every sampling probability must be positive; an item that can never be sampled "
            "makes the inverse weight infinite"
        )
    if np.any(pi > 1):
        raise StatsError("sampling probabilities must be at most 1")

    h = np.zeros(len(g), dtype=float)
    for i, value in enumerate(expensive):
        if xi[i] == 1:
            if value is None:
                raise StatsError(
                    f"item {i} is marked sampled but has no expensive rating; the estimator "
                    "cannot correct with a score that was never produced"
                )
            h[i] = float(value)

    terms = g + (h - g) * xi / pi
    estimate = float(terms.mean())
    n = len(terms)
    se = float(terms.std(ddof=1) / math.sqrt(n)) if n > 1 else float("inf")
    z = float(scipy_stats.norm.ppf(0.5 + level / 2))
    half = z * se if math.isfinite(se) else float("inf")
    return ActiveEstimate(
        estimate=estimate,
        standard_error=se,
        interval=Interval(
            point=estimate,
            low=estimate - half,
            high=estimate + half,
            level=level,
            n=n,
            method="active evaluation (Angelopoulos et al. 2025), empirical variance",
        ),
        n_sampled=int(xi.sum()),
        n_total=n,
        per_item_terms=tuple(float(t) for t in terms),
    )


def paired_active_eval_estimate(
    cheap_baseline: Sequence[float],
    cheap_candidate: Sequence[float],
    expensive_baseline: Sequence[float | None],
    expensive_candidate: Sequence[float | None],
    sampled: Sequence[int | bool],
    rates: Sequence[float],
    *,
    level: float = 0.95,
) -> ActiveEstimate:
    """The same estimator applied to the per-item **difference** between two pipelines.

    ``d = score_candidate - score_baseline`` is what a non-inferiority claim is about, and
    pairing inside the estimator keeps the item-difficulty variance out of the interval, just
    as it does in the bootstrap above.
    """
    gb = np.asarray(cheap_baseline, dtype=float)
    gc = np.asarray(cheap_candidate, dtype=float)
    if len(gb) != len(gc):
        raise StatsError("both arms must score the same items")
    diff_cheap = gc - gb
    diff_expensive: list[float | None] = []
    for hb, hc in zip(expensive_baseline, expensive_candidate, strict=True):
        diff_expensive.append(None if hb is None or hc is None else float(hc) - float(hb))
    return active_eval_estimate(diff_cheap, diff_expensive, sampled, rates, level=level)


def optimal_fixed_rate(mse: float, var_h: float, cost_cheap: float, cost_expensive: float) -> float:
    """The cost-optimal fixed sampling rate p* (Appendix A2).

        p* = sqrt((c_g / c_h) * MSE / (Var(H) - MSE))   when MSE < (c_h / (c_h + c_g)) * Var(H)
        p* = 1                                          otherwise

    where ``MSE = E[(H - G)^2]``. The regime boundary is where p* would reach 1: once the cheap
    rater's error is large relative to the variance it is trying to explain, sampling
    everything is cheapest per unit of precision. The formula is continuous at the boundary.
    """
    if mse < 0:
        raise StatsError("MSE cannot be negative")
    if var_h < 0:
        raise StatsError("Var(H) cannot be negative")
    if cost_cheap < 0 or cost_expensive <= 0:
        raise StatsError("rater costs must be non-negative, and the expensive rater positive")
    if var_h == 0:
        return 1.0
    if mse >= (cost_expensive / (cost_expensive + cost_cheap)) * var_h:
        return 1.0
    rate = math.sqrt((cost_cheap / cost_expensive) * mse / (var_h - mse))
    return min(1.0, rate)


MIN_SAMPLING_RATE = 0.05


def uncertainty_proportional_rates(
    uncertainty: Sequence[float],
    target_rate: float,
    *,
    floor: float = MIN_SAMPLING_RATE,
) -> np.ndarray:
    """Per-item sampling rates proportional to an uncertainty score, clipped to [floor, 1].

    **This is a simplification of the paper's active policy**, not the policy itself:
    Angelopoulos et al. derive an input-dependent rate from a calibrated uncertainty model,
    whereas this scales whatever uncertainty score the caller supplies so the mean rate matches
    ``target_rate``. The floor is the important part — it bounds the inverse weight ``1/pi`` at
    ``1/floor``, so one unlucky draw on a near-zero-probability item cannot dominate the
    estimate.
    """
    u = np.asarray(uncertainty, dtype=float)
    if len(u) == 0:
        raise StatsError("need at least one item")
    if np.any(u < 0):
        raise StatsError("uncertainty scores cannot be negative")
    if not 0 < target_rate <= 1:
        raise StatsError("target_rate must be in (0, 1]")
    if not 0 < floor <= 1:
        raise StatsError("floor must be in (0, 1]")
    mean_u = u.mean()
    raw = np.full(len(u), target_rate) if mean_u == 0 else u * (target_rate / mean_u)
    clipped: np.ndarray = np.clip(raw, floor, 1.0)
    return clipped
