"""Report the tie, not just the winner (UPGRADE_V3.md U7).

The waterfall produces one candidate per step and one verdict, and that is how a buyer ends up
with a single-vendor dependency they never chose. When two configurations' cost intervals
overlap, the ranking between them is not evidence: it is the noise in a bootstrap, dressed as a
recommendation.

**Why the report refuses to pick.** Sinha et al. show that outcome-level mode collapse is a
structural consequence of the expected-return objective itself, not a symptom of weak
exploration: the log-probability ratio between two outcomes diverges exponentially in their
reward difference, so greedy selection collapses onto one outcome even when several are equally
good. Their correction removes the frequency amplification from the learning signal. The product
version of that correction is this module: when the intervals overlap, every tied configuration
is reported, ordered by dollars, and the one that has to be *run* is named as one of several
rather than as the best.

**And one of them is a fallback.** A tie is only useful if you know which of the tied options to
reach for when a provider goes down, so the tie carries a designated fallback: the tied
configuration that leans least on the same scarce capacity as the operating point. Naming it
costs nothing and is the difference between a list and a plan.

**What is not built.** Online exploration — trying configurations against live traffic — does not
exist in this build (DECISIONS.md D24), so ``exploration_weights`` is the rule an explorer would
use and nothing calls it in anger. It is here, and tested, because the rule is the interesting
part and inventing it later under deadline is how greedy selection creeps back in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class TieError(ValueError):
    """A tie could not be decided, or was decided the wrong way."""


@dataclass(frozen=True)
class Candidate:
    """One configuration, with the interval that decides whether it is distinguishable."""

    label: str
    cost_point: float
    cost_low: float
    cost_high: float
    accuracy_point: float
    scarce_share: float
    #: Whether the workload lets the search adopt this configuration at all. A tied option that
    #: may not be run is worth reporting and is not a fallback, so the two are kept apart.
    adoptable: bool = True
    #: Everything the caller wants carried through to the report row.
    extra: dict[str, Any] | None = None

    def overlaps(self, other: Candidate) -> bool:
        """Whether the two cost intervals intersect at all.

        Interval overlap, not a formal test of difference. It is the *conservative* direction —
        two intervals can overlap while a paired test separates them — and conservative here
        means reporting more options rather than fewer, which is the failure mode this module
        prefers.
        """
        return self.cost_low <= other.cost_high and other.cost_low <= self.cost_high


@dataclass
class TieReport:
    """Every configuration statistically tied for cheapest, and which to reach for."""

    cheapest: Candidate
    tied: tuple[Candidate, ...]
    fallback: Candidate | None
    fallback_reason: str

    @property
    def is_tie(self) -> bool:
        return len(self.tied) > 1

    def labels(self) -> list[str]:
        return [candidate.label for candidate in self.tied]

    def note(self) -> str:
        if not self.is_tie:
            return (
                "One configuration is cheaper than every other by more than the intervals allow "
                "for, so there is no tie to report."
            )
        note = (
            "These configurations are not distinguishable on cost at this split size: their "
            "intervals overlap, so the ordering between them is the bootstrap's noise and not a "
            "recommendation. One of them has to run; which one is not something this data "
            "decides."
        )
        if not self.cheapest.adoptable:
            note += (
                f" The cheapest of them, {self.cheapest.label}, is one this workload does not "
                "let the search adopt, so it is reported and not run — measuring a configuration "
                "and shipping it are different decisions."
            )
        return note

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_tie": self.is_tie,
            "cheapest": self.cheapest.label,
            "tied": [
                {
                    "label": candidate.label,
                    "cost_per_successful_task": candidate.cost_point,
                    "cost_low": candidate.cost_low,
                    "cost_high": candidate.cost_high,
                    "accuracy": candidate.accuracy_point,
                    "scarce_share": candidate.scarce_share,
                    "adoptable": candidate.adoptable,
                    "is_cheapest": candidate.label == self.cheapest.label,
                    "is_fallback": bool(self.fallback and candidate.label == self.fallback.label),
                    **(candidate.extra or {}),
                }
                for candidate in self.tied
            ],
            "fallback": self.fallback.label if self.fallback else None,
            "fallback_reason": self.fallback_reason,
            "exploration_weights": dict(
                zip(self.labels(), exploration_weights(self.tied), strict=True)
            ),
            "note": self.note(),
        }


def find_ties(candidates: Sequence[Candidate], operating_point: str | None = None) -> TieReport:
    """Every configuration whose cost interval overlaps the cheapest's, ordered by dollars.

    ``operating_point`` is what is actually running. The fallback is an alternative to *that*,
    not to whichever row happens to be cheapest — on a workload where the cheapest tied option
    is one the search may not adopt, those are different rows and designating the running
    configuration as its own fallback would be worse than designating none.
    """
    if not candidates:
        raise TieError("there is nothing to compare")
    ranked = sorted(candidates, key=lambda c: (c.cost_point, -c.accuracy_point, c.label))
    cheapest = ranked[0]
    tied = tuple(c for c in ranked if c.overlaps(cheapest))
    running = next(
        (c for c in tied if c.label == operating_point),
        next((c for c in tied if c.adoptable), cheapest),
    )
    fallback, reason = designate_fallback(running, tied)
    return TieReport(cheapest=cheapest, tied=tied, fallback=fallback, fallback_reason=reason)


def designate_fallback(
    running: Candidate, tied: Sequence[Candidate]
) -> tuple[Candidate | None, str]:
    """Which tied option to reach for when the operating point's provider is down.

    The one that leans least on the same scarce capacity. A fallback that shares the constraint
    it is meant to survive is not a fallback, so the choice is by scarce-model share and ties
    within that are broken by dollars — deterministically, because a fallback that moved between
    runs would be worse than none.

    Only configurations the workload lets the search adopt are eligible. A fallback nobody is
    allowed to run is not a fallback; it is a footnote.
    """
    alternatives = [c for c in tied if c.label != running.label and c.adoptable]
    if not alternatives:
        return None, (
            "No fallback: nothing else in the tie is both a different configuration and one this "
            "workload lets the search adopt, so there is nothing to fall back *to* that this "
            "data says is equivalent."
        )
    chosen = min(alternatives, key=lambda c: (c.scarce_share, c.cost_point, c.label))
    if chosen.scarce_share >= running.scarce_share:
        return chosen, (
            f"{chosen.label} is the designated fallback, but it leans on scarce capacity at "
            f"least as hard as the operating point ({chosen.scarce_share:.0%} against "
            f"{running.scarce_share:.0%}). It survives a price change, not an outage."
        )
    return chosen, (
        f"{chosen.label} is the designated fallback: it is tied on cost and puts "
        f"{chosen.scarce_share:.0%} of its spend on scarce capacity against the operating "
        f"point's {running.scarce_share:.0%}, so an outage there does not take both."
    )


def exploration_weights(tied: Sequence[Candidate]) -> list[float]:
    """How an online explorer should split traffic across tied configurations.

    Uniform over the tie. That is the whole correction: a greedy explorer puts all of its mass
    on the current best, which amplifies whichever option happened to win the last sample and
    is exactly the frequency amplification Sinha et al. remove. Spreading the mass evenly over
    options the data cannot separate keeps every one of them measurable, which is the only way
    the tie ever gets broken by evidence rather than by inertia.

    Not uniform over *all* configurations: an option the data has separated is not a coin flip,
    and exploring it equally would be a different mistake.
    """
    if not tied:
        raise TieError("there is nothing to explore")
    return [1.0 / len(tied)] * len(tied)


def single_winner(report: TieReport) -> Candidate:
    """The one configuration the data names as cheapest — or a refusal.

    A caller that wants "the winner" gets one only when there is one. On a tie this raises with
    the tied set named, because "pick the cheapest" is one line and "report every option the
    data cannot separate" is twenty, and the one-line version is what gets written when nobody
    is looking. The report assembly deliberately does not call this: it reads ``cheapest`` and
    carries the tie alongside, which is the honest shape. This exists for everyone else.
    """
    if report.is_tie:
        raise TieError(
            "there is no single cheapest configuration: "
            + ", ".join(
                f"{c.label} at ${c.cost_point:.6f} [{c.cost_low:.6f}, {c.cost_high:.6f}]"
                for c in report.tied
            )
            + ". Their cost intervals overlap, so the ordering between them is the bootstrap's "
            "noise. Report the tie, or pick on a criterion this data does not supply."
        )
    return report.cheapest
