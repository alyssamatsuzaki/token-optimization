"""Calibrating a cascade with no answer key (UPGRADE_V3.md U3).

The acceptance check the brief asks for, built around the failure it is actually about.

**The fixture.** A cheap tier with a *characteristic* error: on most of the tasks it gets wrong
it does not produce a fresh mistake each time, it produces the same confusable answer, five
draws out of five — and the mid tier shares that answer some of the time. That is what a real
model does and what the demo's simulated provider deliberately does not (DECISIONS.md D27): its
wrong answers are independent draws, so the correct answer is the unique plurality almost always
and a majority vote cannot go wrong there. It can go wrong here, and the whole question is what
happens when it does.

**What a naive majority vote does with it.** It reads five identical wrong answers as
correctness. The cheap tier's apparent accuracy goes *up*, the frontier's goes *down* — the
frontier is now the one disagreeing with the consensus — and the calibration concludes that the
cheap tier is nearly as good as the frontier at a twentieth of the price. The threshold search
then routes **everything** to the cheap tier. That is not a small error in a hyperparameter; it
is the cascade the customer ships.

**What the penalized version does.** A tier does not vote on its own label, so the cheap tier's
repetition stops confirming itself; the remaining distribution has no clear winner on those
tasks, so they are excluded and counted; and the cheap tier's confident disagreement is weighted
up rather than softened. The labels that survive agree with the answer key, and the operating
point lands near the gold-calibrated one.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from tokop.optimize.cascade import OBJECTIVE_COST, search_thresholds, simulate, tier_run_from_arrays
from tokop.optimize.pseudolabels import (
    MAJORITY_VOTE,
    MIN_CONSENSUS_MARGIN,
    PENALIZED_V1,
    PseudoLabelError,
    build_pseudo_labels,
    pseudo_label_kind,
)
from tokop.workloads.grading import answers_equivalent

#: Committed seeds. Every claim below holds at each of them.
SEEDS = (20260914, 20260915, 20260916, 20260917, 20260918)

TIERS = ("cheap", "mid", "frontier")
COST = {"cheap": Decimal("0.001"), "mid": Decimal("0.004"), "frontier": Decimal("0.020")}
ACCURACY = {"cheap": 0.55, "mid": 0.75, "frontier": 0.97}
#: How separable the scorer's output is. Wide enough that the threshold has real work to do.
SCORE_SPREAD = 0.25
#: Share of the cheap tier's errors that are the *same* confusable answer rather than a fresh
#: mistake. This is the parameter the whole fixture turns on.
CORRELATED_ERROR_SHARE = 0.6
CONFUSABLE = "C"
TRUTH = "A"

#: How far a label-free calibration may land from the gold-calibrated threshold and still count
#: as having found it. Two and a half grid steps per tier; the naive version misses by forty.
THRESHOLD_TOLERANCE = 0.4


def make_calibration(seed: int, n: int = 300) -> dict[str, object]:
    """A calibration split where the cheap tier's mistakes repeat."""
    rng = np.random.default_rng(seed)
    task_ids = [f"t{index:03d}" for index in range(n)]
    samples: dict[str, dict[str, tuple[str, ...]]] = {tier: {} for tier in TIERS}
    truth: dict[str, dict[str, int]] = {tier: {} for tier in TIERS}
    for task_id in task_ids:
        for tier in TIERS:
            draws = [
                TRUTH if rng.random() < ACCURACY[tier] else f"wrong-{task_id}-{tier}-{index}"
                for index in range(5)
            ]
            truth[tier][task_id] = int(draws[0] == TRUTH)
            samples[tier][task_id] = tuple(draws)
        if truth["cheap"][task_id] == 0 and rng.random() < CORRELATED_ERROR_SHARE:
            samples["cheap"][task_id] = (CONFUSABLE,) * 5
            samples["mid"][task_id] = (CONFUSABLE,) * 3 + samples["mid"][task_id][3:]
            truth["mid"][task_id] = 0
    scores = {
        tier: {
            task_id: float(
                np.clip(rng.normal(0.70 if truth[tier][task_id] else 0.40, SCORE_SPREAD), 0, 1)
            )
            for task_id in task_ids
        }
        for tier in TIERS
    }
    return {"task_ids": task_ids, "samples": samples, "truth": truth, "scores": scores}


def calibrate(
    fixture: dict[str, object],
    labels: dict[tuple[str, str], int],
    keep: set[str],
) -> tuple[tuple[float, ...], float]:
    """Run the real threshold search against one source of labels."""
    task_ids = [t for t in fixture["task_ids"] if t in keep]  # type: ignore[union-attr]
    scores = fixture["scores"]
    runs = [
        tier_run_from_arrays(
            tier,
            tier,
            task_ids,
            [labels[(tier, task_id)] for task_id in task_ids],
            [COST[tier]] * len(task_ids),
            [scores[tier][task_id] for task_id in task_ids],  # type: ignore[index]
        )
        for tier in TIERS
    ]
    frontier = [labels[("frontier", task_id)] for task_id in task_ids]
    floor = max(0.0, sum(frontier) / len(frontier) - 0.01)
    found = search_thresholds(runs, accuracy_floor=floor, step=0.02, objective=OBJECTIVE_COST)
    return found.thresholds, floor


def gold_labels(fixture: dict[str, object]) -> dict[tuple[str, str], int]:
    truth = fixture["truth"]
    return {
        (tier, task_id): truth[tier][task_id]  # type: ignore[index]
        for tier in TIERS
        for task_id in fixture["task_ids"]  # type: ignore[union-attr]
    }


def accuracy_under(fixture: dict[str, object], thresholds: tuple[float, ...]) -> float:
    """What an operating point actually delivers, graded against the answer key."""
    labels = gold_labels(fixture)
    task_ids = list(fixture["task_ids"])  # type: ignore[arg-type]
    runs = [
        tier_run_from_arrays(
            tier,
            tier,
            task_ids,
            [labels[(tier, task_id)] for task_id in task_ids],
            [COST[tier]] * len(task_ids),
            [fixture["scores"][tier][task_id] for task_id in task_ids],  # type: ignore[index]
        )
        for tier in TIERS
    ]
    return simulate(runs, thresholds).accuracy


class TestTheAcceptanceCheck:
    """U3's acceptance check, at every committed seed."""

    @pytest.mark.parametrize("seed", SEEDS)
    def test_penalized_finds_the_gold_threshold_and_the_naive_vote_does_not(
        self, seed: int
    ) -> None:
        fixture = make_calibration(seed)
        gold_thresholds, _ = calibrate(
            fixture,
            gold_labels(fixture),
            set(fixture["task_ids"]),  # type: ignore[arg-type]
        )

        results = {}
        for kind in (MAJORITY_VOTE, PENALIZED_V1):
            labels = build_pseudo_labels(
                fixture["samples"],
                answers_equivalent,
                kind=kind,  # type: ignore[arg-type]
            )
            thresholds, floor = calibrate(fixture, labels.labels, set(labels.kept))
            results[kind] = {
                "labels": labels,
                "thresholds": thresholds,
                "floor": floor,
                "gap": max(abs(a - b) for a, b in zip(thresholds, gold_thresholds, strict=True)),
            }

        naive, penalized = results[MAJORITY_VOTE], results[PENALIZED_V1]

        # The headline: the penalized calibration lands on the gold operating point, the naive
        # one routes everything to the cheap tier because it believes the cheap tier's repeated
        # wrong answer.
        assert penalized["gap"] <= THRESHOLD_TOLERANCE, (
            f"penalized thresholds {penalized['thresholds']} are "
            f"{penalized['gap']:.2f} from gold's {gold_thresholds}"
        )
        assert naive["gap"] > THRESHOLD_TOLERANCE, (
            f"the naive vote landed within tolerance ({naive['gap']:.2f}); this fixture is "
            "supposed to break it"
        )
        assert naive["thresholds"][0] == 0.0, (
            f"the naive calibration set a cheap-tier threshold of {naive['thresholds'][0]}; "
            "the failure it is supposed to show is routing everything to the cheap tier"
        )
        assert penalized["thresholds"][0] > 0.5

    @pytest.mark.parametrize("seed", SEEDS)
    def test_the_naive_vote_gets_the_accuracy_floor_badly_wrong(self, seed: int) -> None:
        """The floor is the constraint the whole search runs under, and it is where the damage
        is done: a calibration that thinks its frontier scores 70% when it scores 95% will
        accept any cascade at all."""
        fixture = make_calibration(seed)
        _, gold_floor = calibrate(
            fixture,
            gold_labels(fixture),
            set(fixture["task_ids"]),  # type: ignore[arg-type]
        )
        floors = {}
        for kind in (MAJORITY_VOTE, PENALIZED_V1):
            labels = build_pseudo_labels(
                fixture["samples"],
                answers_equivalent,
                kind=kind,  # type: ignore[arg-type]
            )
            floors[kind] = calibrate(fixture, labels.labels, set(labels.kept))[1]
        assert abs(floors[PENALIZED_V1] - gold_floor) <= 0.02
        assert gold_floor - floors[MAJORITY_VOTE] > 0.15

    @pytest.mark.parametrize("seed", SEEDS)
    def test_the_labels_that_survive_agree_with_the_answer_key(self, seed: int) -> None:
        fixture = make_calibration(seed)
        truth = fixture["truth"]
        for kind, floor in ((PENALIZED_V1, 0.99), (MAJORITY_VOTE, None)):
            labels = build_pseudo_labels(
                fixture["samples"],
                answers_equivalent,
                kind=kind,  # type: ignore[arg-type]
            )
            agreements = [
                labels.agreement_with(tier, truth[tier])  # type: ignore[index]
                for tier in TIERS
            ]
            worst = min(a for a in agreements if a is not None)
            if floor is None:
                assert worst <= 0.85, f"the naive vote agreed {worst:.3f} of the time"
            else:
                assert worst >= floor, f"{kind} agreed only {worst:.3f} of the time"

    @pytest.mark.parametrize("seed", SEEDS)
    def test_the_operating_point_the_naive_vote_picks_is_worse_in_practice(self, seed: int) -> None:
        """Thresholds are a means. This is the end: what the chosen cascade actually scores on
        the calibration split, graded against the answer key it never saw."""
        fixture = make_calibration(seed)
        gold_thresholds, _ = calibrate(
            fixture,
            gold_labels(fixture),
            set(fixture["task_ids"]),  # type: ignore[arg-type]
        )
        gold_accuracy = accuracy_under(fixture, gold_thresholds)
        delivered = {}
        for kind in (MAJORITY_VOTE, PENALIZED_V1):
            labels = build_pseudo_labels(
                fixture["samples"],
                answers_equivalent,
                kind=kind,  # type: ignore[arg-type]
            )
            thresholds, _ = calibrate(fixture, labels.labels, set(labels.kept))
            delivered[kind] = accuracy_under(fixture, thresholds)
        assert gold_accuracy - delivered[MAJORITY_VOTE] > 0.10
        assert gold_accuracy - delivered[PENALIZED_V1] < gold_accuracy - delivered[MAJORITY_VOTE]


class TestWhatExclusionCosts:
    """Exclusion is not free, and the report says how much of the split it took."""

    def test_the_excluded_items_are_counted_and_named(self) -> None:
        fixture = make_calibration(SEEDS[0])
        labels = build_pseudo_labels(
            fixture["samples"],
            answers_equivalent,
            kind=PENALIZED_V1,  # type: ignore[arg-type]
        )
        summary = labels.as_dict()
        assert summary["excluded"] > 0
        assert summary["excluded"] + summary["kept"] == summary["n"]
        assert len(summary["excluded_task_ids"]) == summary["excluded"]
        assert 0 < summary["excluded_share"] < 0.5
        for task_id in labels.excluded:
            assert labels.consensus[task_id].reason

    def test_the_naive_vote_excludes_nothing_by_construction(self) -> None:
        fixture = make_calibration(SEEDS[0])
        labels = build_pseudo_labels(
            fixture["samples"],
            answers_equivalent,
            kind=MAJORITY_VOTE,  # type: ignore[arg-type]
        )
        assert labels.excluded == ()


class TestTheMechanism:
    """Each of the three corrections, on its own."""

    def _samples(self, per_tier: dict[str, list[str]]) -> dict[str, dict[str, tuple[str, ...]]]:
        return {tier: {"t1": tuple(answers)} for tier, answers in per_tier.items()}

    def test_a_tier_does_not_vote_on_its_own_label(self) -> None:
        """The cheap tier repeats a wrong answer five times. Pooled, it is a third of the vote
        that judges it; left out, the other two tiers settle the question."""
        samples = self._samples(
            {
                "cheap": ["C"] * 5,
                "mid": ["C"] * 3 + ["A"] * 2,
                "frontier": ["A"] * 5,
            }
        )
        naive = build_pseudo_labels(samples, answers_equivalent, kind=MAJORITY_VOTE)
        assert naive.consensus["t1"].consensus == "C"
        assert naive.labels[("cheap", "t1")] == 1  # confirmed itself

        penalized = build_pseudo_labels(samples, answers_equivalent, kind=PENALIZED_V1)
        # Left out of its own vote, the cheap tier is judged against 3 C and 7 A.
        assert penalized.reference[("cheap", "t1")] == "A"
        assert penalized.labels[("cheap", "t1")] == 0

    def test_a_distribution_with_no_clear_winner_is_excluded(self) -> None:
        samples = self._samples(
            {"cheap": ["C"] * 5, "mid": ["C"] * 3 + ["A"] * 2, "frontier": ["A"] * 5}
        )
        penalized = build_pseudo_labels(samples, answers_equivalent, kind=PENALIZED_V1)
        # 8 C against 7 A: the plurality leads by one generation in fifteen.
        assert penalized.consensus["t1"].margin < MIN_CONSENSUS_MARGIN
        assert penalized.excluded == ("t1",)

    def test_a_clear_consensus_is_kept(self) -> None:
        samples = self._samples(
            {"cheap": ["A"] * 5, "mid": ["A"] * 4 + ["Z"], "frontier": ["A"] * 5}
        )
        penalized = build_pseudo_labels(samples, answers_equivalent, kind=PENALIZED_V1)
        assert penalized.excluded == ()
        assert penalized.consensus["t1"].consensus == "A"
        assert all(penalized.labels[(tier, "t1")] == 1 for tier in ("cheap", "mid", "frontier"))

    def test_confident_disagreement_weighs_more_than_unsure_disagreement(self) -> None:
        confident = self._samples(
            {"cheap": ["C"] * 5, "mid": ["A"] * 5, "frontier": ["A"] * 4 + ["Q"]}
        )
        unsure = self._samples(
            {
                "cheap": ["C", "D", "E", "F", "G"],
                "mid": ["A"] * 5,
                "frontier": ["A"] * 4 + ["Q"],
            }
        )
        a = build_pseudo_labels(confident, answers_equivalent, kind=PENALIZED_V1)
        b = build_pseudo_labels(unsure, answers_equivalent, kind=PENALIZED_V1)
        assert a.labels[("cheap", "t1")] == 0
        assert b.labels[("cheap", "t1")] == 0
        assert a.weights[("cheap", "t1")] > b.weights[("cheap", "t1")]
        assert ("cheap", "t1") in a.penalized
        assert ("cheap", "t1") not in b.penalized

    def test_a_unanimous_wrong_answer_is_invisible_and_the_module_says_so(self) -> None:
        """The limit of the method, asserted rather than left to be discovered. Every tier
        confidently gives the same wrong answer; the consensus is wrong, unanimous and
        unexcludable. Nothing label-free sees this, and the docstring says so."""
        samples = self._samples({tier: ["C"] * 5 for tier in TIERS})
        penalized = build_pseudo_labels(samples, answers_equivalent, kind=PENALIZED_V1)
        assert penalized.excluded == ()
        assert penalized.consensus["t1"].consensus == "C"
        assert all(penalized.labels[(tier, "t1")] == 1 for tier in TIERS)

    def test_relabelling_a_different_generation_uses_the_same_consensus(self) -> None:
        """A sampling scorer returns one of k generations and it need not be the first."""
        samples = self._samples(
            {"cheap": ["C", "A", "C", "C", "C"], "mid": ["A"] * 5, "frontier": ["A"] * 5}
        )
        penalized = build_pseudo_labels(samples, answers_equivalent, kind=PENALIZED_V1)
        assert penalized.label_for("cheap", "t1", "C") == 0
        assert penalized.label_for("cheap", "t1", "A") == 1


class TestRefusals:
    def test_an_unknown_kind_names_the_registry(self) -> None:
        with pytest.raises(PseudoLabelError, match=PENALIZED_V1):
            pseudo_label_kind("just-ask-the-model")

    def test_tiers_covering_different_tasks_are_refused(self) -> None:
        samples = {
            "cheap": {"t1": ("A",), "t2": ("A",)},
            "frontier": {"t1": ("A",), "t3": ("A",)},
        }
        with pytest.raises(PseudoLabelError, match="same questions"):
            build_pseudo_labels(samples, answers_equivalent)

    def test_tiers_sampled_to_different_depths_are_refused(self) -> None:
        """Otherwise the vote is decided by whichever tier was recorded deepest, which is a
        fact about the recording rather than about the answers."""
        samples = {
            "cheap": {"t1": ("A", "A", "A", "A", "A")},
            "frontier": {"t1": ("B",)},
        }
        with pytest.raises(PseudoLabelError, match="different depths"):
            build_pseudo_labels(samples, answers_equivalent)

    def test_no_tiers_at_all_is_refused(self) -> None:
        with pytest.raises(PseudoLabelError, match="no tiers"):
            build_pseudo_labels({}, answers_equivalent)
