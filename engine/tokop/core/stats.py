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
from typing import Any, Literal

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


def z_for(alpha: float) -> float:
    """The two-sided normal critical value at ``alpha``. ``z_for(0.05)`` is exactly ``Z_95``."""
    if not 0 < alpha < 1:
        raise StatsError(f"alpha must be strictly between 0 and 1, got {alpha}")
    return float(scipy_stats.norm.ppf(1 - alpha / 2))


def required_n(
    p10: float, p01: float, delta_hat: float, margin: float, alpha: float = 0.05
) -> int | None:
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
    return math.ceil(z_for(alpha) ** 2 * v / slack**2)


@dataclass(frozen=True)
class PowerEstimate:
    """How many test tasks a conclusive verdict needs, from a run that already happened.

    Sizing a split before recording it is the one calculation that has to happen *before* the
    money moves: a split too small to conclude spends the whole budget and returns
    "inconclusive", which is the most expensive outcome available (UPGRADE_V4.md M13.1).

    Every field is read off an observed run rather than assumed, and ``source`` says which run.
    That matters more than it looks: a discordance rate measured on simulated arms whose errors
    are drawn independently is the *optimistic* case, and a real pair of models that fail on the
    same hard tasks will be more concordant, need more tasks, or both.
    """

    required_n: int | None
    observed_n: int
    p10: float
    p01: float
    delta_hat: float
    margin: float
    alpha: float
    source: str

    @property
    def discordant(self) -> int:
        return round((self.p10 + self.p01) * self.observed_n)

    @property
    def is_sufficient(self) -> bool:
        return self.required_n is not None and self.observed_n >= self.required_n

    @property
    def shortfall(self) -> int:
        """How many tasks short the observed split is. Zero when it is already large enough."""
        if self.required_n is None:
            return 0
        return max(0, self.required_n - self.observed_n)

    @property
    def display(self) -> str:
        if self.required_n is None:
            return (
                f"No split size settles this: the observed difference of "
                f"{self.delta_hat * 100:+.1f} points is already at or past the "
                f"{self.margin * 100:.0f}-point margin, so more tasks tighten the interval "
                "around a point on the wrong side of it."
            )
        if self.is_sufficient:
            return (
                f"{self.required_n:,} tasks at the observed discordance; the split has "
                f"{self.observed_n:,}."
            )
        return (
            f"{self.required_n:,} tasks at the observed discordance, "
            f"{self.shortfall:,} more than the {self.observed_n:,} in the split."
        )

    @classmethod
    def from_counts(
        cls,
        *,
        baseline_only: int,
        candidate_only: int,
        n: int,
        margin: float = DEFAULT_MARGIN,
        alpha: float = 0.05,
        source: str = "the observed run",
    ) -> PowerEstimate:
        """Size a split from discordant counts a run already reported.

        The CLI reads the counts the proof published rather than re-deriving them from the
        arms, so the tasks-needed figure and the verdict's own interval cannot drift apart.
        """
        if n <= 0:
            raise StatsError("a power estimate needs at least one task")
        if baseline_only < 0 or candidate_only < 0:
            raise StatsError("discordant counts cannot be negative")
        if baseline_only + candidate_only > n:
            raise StatsError(
                f"{baseline_only + candidate_only} discordant pairs in a split of {n} tasks"
            )
        p10 = baseline_only / n
        p01 = candidate_only / n
        delta_hat = p01 - p10
        return cls(
            required_n=required_n(p10, p01, delta_hat, margin, alpha),
            observed_n=n,
            p10=p10,
            p01=p01,
            delta_hat=delta_hat,
            margin=margin,
            alpha=alpha,
            source=source,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_n": self.required_n,
            "observed_n": self.observed_n,
            "discordant": self.discordant,
            "p10": self.p10,
            "p01": self.p01,
            "delta_hat": self.delta_hat,
            "margin": self.margin,
            "alpha": self.alpha,
            "source": self.source,
            "sufficient": self.is_sufficient,
            "shortfall": self.shortfall,
            "display": self.display,
        }


def power_for_split(
    baseline: Sequence[int | bool],
    candidate: Sequence[int | bool],
    *,
    margin: float = DEFAULT_MARGIN,
    alpha: float = 0.05,
    source: str = "the observed run",
) -> PowerEstimate:
    """Size the test split a conclusive McNemar verdict would need.

    Takes the two arms of a run that already happened, because the quantity that drives the
    answer — how often the arms disagree — cannot be guessed from accuracy alone. Two pipelines
    both at 96% might disagree on 2% of tasks or on 8%, and the required split differs by a
    factor of four.
    """
    result = mcnemar(baseline, candidate)
    return PowerEstimate.from_counts(
        baseline_only=result.baseline_only,
        candidate_only=result.candidate_only,
        n=len(baseline),
        margin=margin,
        alpha=alpha,
        source=source,
    )


def verdict_from_interval(
    delta: Interval,
    *,
    margin: float = DEFAULT_MARGIN,
    p10: float = 0.0,
    p01: float = 0.0,
    n: int | None = None,
) -> Verdict:
    """Where a delta-accuracy interval sits relative to the margin.

    Split out from ``non_inferiority_verdict`` so that an interval produced some other way —
    the active estimator of UPGRADE_V3.md U1, whose interval comes from the empirical variance
    of its per-item terms rather than from a bootstrap — reaches the same verdict by the same
    rule. A second rule for the judged path would be a second place for the product to be
    generous about what counts as proven.

    ``p10`` and ``p01`` are the discordant proportions used to size how many more tasks would
    settle an inconclusive result. They are optional because not every estimator has them; with
    both at zero the answer is the smallest n that clears the margin at the observed delta.
    """
    if margin <= 0:
        raise StatsError("margin must be positive; a zero margin can never be proven")
    if delta.low > -margin:
        return Verdict("non_inferior", margin, delta)
    if delta.high < -margin:
        return Verdict("worse", margin, delta)
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
    return Verdict("inconclusive", margin, delta, max(0, needed - (delta.n if n is None else n)))


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
    p10 = float(((b == 1) & (c == 0)).sum()) / n
    p01 = float(((b == 0) & (c == 1)).sum()) / n
    return verdict_from_interval(delta, margin=margin, p10=p10, p01=p01, n=n)


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
    """Per-item sampling rates proportional to an uncertainty score, bounded by [floor, 1].

    **This is a simplification of the paper's active policy**, not the policy itself:
    Angelopoulos et al. derive an input-dependent rate from a calibrated uncertainty model,
    whereas this scales whatever uncertainty score the caller supplies so the mean rate matches
    ``target_rate``. The floor is the important part — it bounds the inverse weight ``1/pi`` at
    ``1/floor``, so one unlucky draw on a near-zero-probability item cannot dominate the
    estimate.

    The mean is preserved **after** bounding, not before. Scaling and then clipping does not
    give the mean you asked for — the floor lifts the bottom of the distribution and the cap
    flattens the top — and since the caller's ``target_rate`` is how a budget is expressed, a
    drifted mean is a budget quietly overspent. So the scale is *solved for* rather than
    computed: ``mean(clip(lambda * u, floor, 1))`` is continuous and non-decreasing in
    ``lambda``, running from ``floor`` to 1, so bisection finds the ``lambda`` that hits the
    target exactly.
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
    if target_rate < floor:
        raise StatsError(
            f"a mean rate of {target_rate} is impossible with a floor of {floor}: every item is "
            "sampled at least that often. Lower the floor or raise the budget."
        )

    n = len(u)
    mean_u = float(u.mean())
    if mean_u == 0:
        # No uncertainty anywhere is not a reason to prefer one item over another.
        return np.full(n, target_rate)

    def mean_at(scale: float) -> float:
        return float(np.clip(u * scale, floor, 1.0).mean())

    low, high = 0.0, target_rate / mean_u
    while mean_at(high) < target_rate and high < 1e12:
        high *= 2
    if mean_at(high) < target_rate:
        # Every item with any uncertainty is already sampled every time and the mean is still
        # short, which only happens when most of the weight is exactly zero. Scaling cannot
        # reach the target, so the remainder is spread evenly over the items that are not yet
        # at 1 — no longer proportional, and the alternative is silently under-spending.
        rates = np.clip(u * high, floor, 1.0)
        room = rates < 1.0
        shortfall = target_rate * n - float(rates.sum())
        if room.any() and shortfall > 0:
            rates[room] = np.minimum(1.0, rates[room] + shortfall / float(room.sum()))
        out: np.ndarray = rates
        return out

    for _ in range(200):
        middle = (low + high) / 2
        if mean_at(middle) < target_rate:
            low = middle
        else:
            high = middle
    solved: np.ndarray = np.clip(u * high, floor, 1.0)
    return solved
