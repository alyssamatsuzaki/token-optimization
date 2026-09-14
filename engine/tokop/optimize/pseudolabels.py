"""Calibration labels for a workload that has none (UPGRADE_V3.md U3).

U1 made the *test* comparison label-free. The cascade's operating point is the other half: its
per-tier thresholds and its scorer are fitted on the calibration split, and under
``grading: judged`` that split has no answer key either. Something has to stand in for one.

The obvious substitute is a majority vote over repeated generations, and the obvious substitute
is the one to be careful about. Yu et al. built RESTRAIN because majority votes on unlabelled
data are spurious often enough to poison training, and their answer is not to trust the vote
harder but to *penalize* the rollouts that would otherwise dominate it. That is the mechanism
this module borrows. It does not borrow the result: RESTRAIN is policy optimization on reasoning
benchmarks, this is threshold calibration, and nothing about the first transfers quantitatively
to the second. What transfers is the warning.

Two kinds are registered, and they exist as a pair because the comparison is the point:

``majority-vote``
    The naive substitute, implemented honestly so it can be measured rather than strawmanned.
    Pool every generation from every tier, take the plurality answer, and label a tier correct
    when its answer agrees with it. Every item counts, and every item counts the same.

``penalized-v1``
    The same pooled distribution, with three corrections, each of which is a way the naive
    version goes wrong:

    1. **A tier does not vote on its own label.** Its own k generations are removed from the
       pool before the consensus is taken. A tier that repeats one answer five times otherwise
       contributes a third of the vote that judges it, and a deterministic tier would confirm
       itself by construction.
    2. **An answer distribution with no clear winner is excluded, not resolved.** When the
       plurality leads by one or two generations, or the clusters are evenly spread, the winner
       is an accident of the draw and the label is noise. Those items are dropped from fitting
       and *counted*, because a calibration set that silently shrank is a calibration set nobody
       can size. A task is excluded when the pooled distribution has no clear winner **or** any
       single tier's leave-one-out reference does not, because the threshold search needs the
       same tasks at every tier and a tier labelled from a coin flip poisons the whole row.
    3. **Confident disagreement weighs more, not less.** A tier that repeats one answer and
       that answer is not the consensus is the overconfident rollout RESTRAIN penalizes: the
       naive reading is "it is sure, so maybe it is right", and the evidence points the other
       way. Its 0 label carries extra weight rather than being softened.

    Items are otherwise weighted by the consensus margin, so a 9-to-1 agreement counts for more
    than a 6-to-5 one.

**What none of this can do.** If every tier confidently gives the *same* wrong answer, the
consensus is wrong, unanimous and unexcludable, and no amount of penalization sees it. Nothing
label-free can. The failure this module addresses is the one it can address: a plurality that is
an artefact of a split distribution, or of one tier shouting. ``tests/test_pseudolabels.py``
tests exactly that case and says so.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tokop.optimize.scorers import discrete_entropy, group_indices

MAJORITY_VOTE = "majority-vote"
PENALIZED_V1 = "penalized-v1"
GOLD = "gold"

#: How far ahead the plurality has to be before it counts as a consensus, as a share of the
#: generations pooled. Below this the winner is decided by one or two draws and the label is a
#: coin flip wearing a majority's clothes. This is the rule that does the excluding; the
#: uniformity cutoff below catches the rarer shape it misses.
MIN_CONSENSUS_MARGIN = 0.15

#: Entropy of the cluster proportions **relative to the most those clusters could carry**, at or
#: above which the distribution is treated as uniform. Normalizing by the number of clusters
#: rather than by the sample count is what makes "close to uniform" mean what it sounds like: a
#: 5/5/5 split reads as 1.0 here, where dividing by log(15) would read as 0.4 and look decisive.
UNIFORMITY_CUTOFF = 0.95

#: How self-consistent a tier has to be before disagreement with the consensus counts as
#: *confident* disagreement. A tier whose generations land in one cluster this often is not
#: guessing.
OVERCONFIDENT_CONSISTENCY = 0.8

#: How much extra weight a confidently-disagreeing tier's 0 label carries. Named rather than
#: fitted: there is nothing to fit it against without labels, which is the whole situation.
OVERCONFIDENCE_PENALTY = 1.0

#: The floor on an item's weight, so that a weak but usable consensus still contributes.
MIN_WEIGHT = 0.05

EquivalenceFn = Callable[[str, str], bool]


class PseudoLabelError(ValueError):
    """Pseudo-labels could not be built from the generations supplied."""


@dataclass(frozen=True)
class TaskConsensus:
    """What the pooled generations for one task say, and how strongly."""

    task_id: str
    #: One representative answer per cluster with its count, largest first.
    clusters: tuple[tuple[str, int], ...]
    consensus: str
    support: float
    margin: float
    entropy: float
    #: Entropy relative to the most these clusters could carry. 1.0 is a flat distribution.
    uniformity: float
    excluded: bool
    reason: str = ""

    @property
    def pool_size(self) -> int:
        return sum(count for _, count in self.clusters)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cluster_sizes": [count for _, count in self.clusters],
            "support": self.support,
            "margin": self.margin,
            "entropy": self.entropy,
            "uniformity": self.uniformity,
            "excluded": self.excluded,
            "reason": self.reason,
        }


@dataclass
class PseudoLabels:
    """Per-tier labels and fitting weights, with the count of what was thrown away."""

    kind: str
    tiers: tuple[str, ...]
    task_ids: tuple[str, ...]
    labels: dict[tuple[str, str], int] = field(default_factory=dict)
    weights: dict[tuple[str, str], float] = field(default_factory=dict)
    consensus: dict[str, TaskConsensus] = field(default_factory=dict)
    penalized: set[tuple[str, str]] = field(default_factory=set)
    #: The answer each tier's label was judged against. Kept per (tier, task) rather than per
    #: task because a leave-one-out consensus is a different answer for each tier.
    reference: dict[tuple[str, str], str] = field(default_factory=dict)
    #: The relation used to compare answers, so a caller can relabel a different generation
    #: against the same consensus without re-deriving one.
    equivalence: EquivalenceFn | None = None

    def label_for(self, tier: str, task_id: str, answer: str) -> int:
        """Whether *this* answer agrees with the consensus that judged this tier.

        A sampling scorer returns one of k generations and it need not be the first, so the
        label has to follow the answer the cascade actually returned. Charging for a vote and
        labelling the first draw would be the calibration-time version of the mistake
        ``tier_runs`` avoids at test time.
        """
        if self.equivalence is None:  # pragma: no cover - set by the builder
            raise PseudoLabelError("this label set carries no equivalence relation")
        reference = self.reference.get((tier, task_id))
        if reference is None:
            raise PseudoLabelError(f"no consensus recorded for tier {tier!r} task {task_id!r}")
        return int(self.equivalence(reference, answer))

    @property
    def excluded(self) -> tuple[str, ...]:
        return tuple(t for t in self.task_ids if self.consensus[t].excluded)

    @property
    def kept(self) -> tuple[str, ...]:
        return tuple(t for t in self.task_ids if not self.consensus[t].excluded)

    def for_tier(self, tier: str) -> tuple[list[str], list[int], list[float]]:
        """The kept tasks for one tier, with their labels and weights, in split order."""
        ids, labels, weights = [], [], []
        for task_id in self.kept:
            ids.append(task_id)
            labels.append(self.labels[(tier, task_id)])
            weights.append(self.weights[(tier, task_id)])
        return ids, labels, weights

    def agreement_with(self, tier: str, truth: dict[str, int]) -> float | None:
        """How often this tier's pseudo-label matches a real label, on the kept tasks.

        Only computable where real labels exist, which is a demo, not a customer. It is the
        number that says whether the substitute is any good, so it is reported wherever it can
        be computed at all.
        """
        kept = [t for t in self.kept if t in truth]
        if not kept:
            return None
        return sum(1 for t in kept if self.labels[(tier, t)] == truth[t]) / len(kept)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "n": len(self.task_ids),
            "kept": len(self.kept),
            "excluded": len(self.excluded),
            "excluded_share": len(self.excluded) / len(self.task_ids) if self.task_ids else 0.0,
            "excluded_task_ids": list(self.excluded),
            "penalized": len(self.penalized),
            "mean_weight": (
                sum(self.weights.values()) / len(self.weights) if self.weights else 0.0
            ),
            "positive_rate": {
                tier: (
                    sum(self.labels[(tier, t)] for t in self.kept) / len(self.kept)
                    if self.kept
                    else 0.0
                )
                for tier in self.tiers
            },
        }


@dataclass(frozen=True)
class PseudoLabelKind:
    """One registered way of standing in for an answer key."""

    name: str
    description: str
    #: Whether a tier's own generations are removed before its consensus is taken.
    leave_one_out: bool
    #: Whether near-uniform distributions are dropped.
    exclude_uniform: bool
    #: Whether a confidently-disagreeing tier's label is weighted up.
    penalize_overconfidence: bool
    #: Whether items are weighted by the strength of their consensus.
    weight_by_margin: bool


PSEUDO_LABEL_REGISTRY: dict[str, PseudoLabelKind] = {
    MAJORITY_VOTE: PseudoLabelKind(
        name=MAJORITY_VOTE,
        description=(
            "plurality answer over every generation from every tier, taken at face value: "
            "every item counts, every item counts the same, and a tier votes on its own label"
        ),
        leave_one_out=False,
        exclude_uniform=False,
        penalize_overconfidence=False,
        weight_by_margin=False,
    ),
    PENALIZED_V1: PseudoLabelKind(
        name=PENALIZED_V1,
        description=(
            "self-penalized consensus after RESTRAIN's mechanism: a tier does not vote on its "
            "own label, near-uniform answer distributions are excluded and counted, confident "
            "disagreement with the consensus is weighted up rather than softened, and items "
            "are weighted by the consensus margin"
        ),
        leave_one_out=True,
        exclude_uniform=True,
        penalize_overconfidence=True,
        weight_by_margin=True,
    ),
}


def pseudo_label_kind(name: str) -> PseudoLabelKind:
    try:
        return PSEUDO_LABEL_REGISTRY[name]
    except KeyError:
        raise PseudoLabelError(
            f"unknown pseudo-label kind {name!r}; the registry offers "
            f"{', '.join(sorted(PSEUDO_LABEL_REGISTRY))}"
        ) from None


def _clusters(samples: Sequence[str], equivalence: EquivalenceFn) -> tuple[tuple[str, int], ...]:
    grouped = group_indices(list(samples), equivalence)
    sized = sorted(
        ((samples[group[0]], len(group)) for group in grouped), key=lambda pair: -pair[1]
    )
    return tuple(sized)


def _shape(clusters: Sequence[tuple[str, int]]) -> tuple[float, float, float, float]:
    """(support, margin, entropy, uniformity) for one pooled answer distribution."""
    total = sum(count for _, count in clusters)
    if not clusters or total == 0:
        return 0.0, 0.0, 0.0, 0.0
    support = clusters[0][1] / total
    margin = support - (clusters[1][1] / total if len(clusters) > 1 else 0.0)
    sizes = [count for _, count in clusters]
    entropy = discrete_entropy(sizes)
    if len(clusters) < 2:
        uniformity = 0.0
    else:
        proportions = [size / total for size in sizes]
        raw = -sum(p * math.log(p) for p in proportions if p > 0)
        uniformity = raw / math.log(len(clusters))
    return support, margin, entropy, uniformity


def _ambiguous(clusters: Sequence[tuple[str, int]], min_margin: float) -> bool:
    _, margin, _, uniformity = _shape(clusters)
    return margin < min_margin or uniformity >= UNIFORMITY_CUTOFF


def _consistency(samples: Sequence[str], equivalence: EquivalenceFn) -> float:
    """Share of a tier's own generations that land in its own largest cluster."""
    if not samples:
        return 0.0
    clusters = _clusters(samples, equivalence)
    return clusters[0][1] / len(samples) if clusters else 0.0


def build_pseudo_labels(
    samples_by_tier: dict[str, dict[str, tuple[str, ...]]],
    equivalence: EquivalenceFn,
    *,
    kind: str = PENALIZED_V1,
    min_margin: float = MIN_CONSENSUS_MARGIN,
) -> PseudoLabels:
    """Stand in for an answer key, from repeated generations alone.

    ``samples_by_tier[tier][task_id]`` is that tier's generations for that task, in draw order;
    ``samples_by_tier[tier][task_id][0]`` is the answer the tier returned. The pool has to be
    balanced across tiers — every tier contributing the same number of generations — or the
    consensus is decided by whichever tier was sampled deepest, which is a property of the
    recording rather than of the answers.
    """
    chosen = pseudo_label_kind(kind)
    tiers = tuple(samples_by_tier)
    if not tiers:
        raise PseudoLabelError("no tiers were supplied; there is nothing to build a vote from")
    task_ids = tuple(samples_by_tier[tiers[0]])
    for tier in tiers:
        if tuple(samples_by_tier[tier]) != task_ids:
            raise PseudoLabelError(
                f"tier {tier!r} covers different tasks from {tiers[0]!r}. A consensus across "
                "tiers needs every tier to have answered the same questions."
            )
    depths = {len(samples_by_tier[tier][task]) for tier in tiers for task in task_ids}
    if len(depths) != 1:
        raise PseudoLabelError(
            f"the tiers were sampled to different depths ({sorted(depths)}). The pooled vote "
            "would be decided by whichever tier was recorded deepest, which is a fact about the "
            "recording and not about the answers. Re-record at one depth."
        )

    out = PseudoLabels(kind=kind, tiers=tiers, task_ids=task_ids, equivalence=equivalence)
    for task_id in task_ids:
        pooled = [s for tier in tiers for s in samples_by_tier[tier][task_id]]
        clusters = _clusters(pooled, equivalence)
        total = sum(count for _, count in clusters)
        support, margin, entropy, uniformity = _shape(clusters)

        references: dict[str, tuple[tuple[str, int], ...]] = {}
        for tier in tiers:
            if chosen.leave_one_out:
                others = [
                    s for other in tiers if other != tier for s in samples_by_tier[other][task_id]
                ]
                references[tier] = _clusters(others, equivalence) if others else clusters
            else:
                references[tier] = clusters

        excluded = chosen.exclude_uniform and (
            _ambiguous(clusters, min_margin)
            or any(_ambiguous(references[tier], min_margin) for tier in tiers)
        )
        out.consensus[task_id] = TaskConsensus(
            task_id=task_id,
            clusters=clusters,
            consensus=clusters[0][0] if clusters else "",
            support=support,
            margin=margin,
            entropy=entropy,
            uniformity=uniformity,
            excluded=excluded,
            reason=(
                f"{total} generations across {len(clusters)} answers, plurality ahead by "
                f"{margin:.0%} at uniformity {uniformity:.2f}: no winner a label could rest on"
                if excluded
                else ""
            ),
        )

        for tier in tiers:
            own = samples_by_tier[tier][task_id]
            reference = references[tier]
            if not reference:
                out.labels[(tier, task_id)] = 0
                out.weights[(tier, task_id)] = MIN_WEIGHT
                out.reference[(tier, task_id)] = ""
                continue
            consensus_answer = reference[0][0]
            _, reference_margin, _, _ = _shape(reference)
            agrees = bool(own) and equivalence(consensus_answer, own[0])
            weight = max(MIN_WEIGHT, reference_margin) if chosen.weight_by_margin else 1.0
            if (
                chosen.penalize_overconfidence
                and not agrees
                and _consistency(own, equivalence) >= OVERCONFIDENT_CONSISTENCY
            ):
                weight *= 1.0 + OVERCONFIDENCE_PENALTY
                out.penalized.add((tier, task_id))
            out.labels[(tier, task_id)] = int(agrees)
            out.weights[(tier, task_id)] = weight
            out.reference[(tier, task_id)] = consensus_answer
    return out
