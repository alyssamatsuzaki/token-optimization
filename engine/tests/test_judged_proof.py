"""Proving a workload nobody labelled (UPGRADE_V3.md U1).

The claim this file exists to defend is narrow and load-bearing: **a biased judge costs
interval width, never correctness.** The inverse-probability weight makes the correction term's
expectation ``E[H - G]`` whatever ``G`` does, so an estimate built on a bad judge is still
centred on the strong grader's mean — just less precisely.

That is the property a buyer who has been burned by LLM-as-judge will not take on trust, so it
is tested the way they would test it: by injecting a judge that is wrong in a specific,
systematic, plausible way and watching the naive estimate walk off while the corrected one
stays put.

``TestTheAdversarialJudge`` is a release blocker. If it fails, the estimator is not doing the
one thing it is for.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from tokop.core.stats import active_eval_estimate, paired_active_eval_estimate, wilson
from tokop.optimize.annotation import (
    COST_OPTIMAL,
    UNIFORM,
    AnnotationSet,
    ItemFeatures,
    allocate,
    annotations_path,
)
from tokop.optimize.proof import build_judged_delta
from tokop.paths import fixtures_dir, repo_root
from tokop.workloads.grading import grade
from tokop.workloads.verification import binary_entropy

#: Committed seeds for the annotation draw. Every claim below holds at each of them.
SEEDS = (20260914, 20260915, 20260916, 20260917, 20260918)

COST_PER_ITEM = Decimal("0.0124")
BUDGET = Decimal("0.60")

#: The corruption UPGRADE_V3.md names: a judge that marks this share of correct answers of one
#: class wrong. Applied by a stable hash rather than a random draw, so the number of corrupted
#: items is exact and a release-blocking test never depends on which way a coin fell.
ADVERSARIAL_SHARE = 0.20


@pytest.fixture(scope="module")
def demo() -> dict[str, Any]:
    """The demo's two arms on the test split, gold-graded, plus the committed annotation set."""
    from tokop.optimize.report import arms_for_annotation
    from tokop.workloads.demo.dataset import build as build_dataset
    from tokop.workloads.spec import load_workload

    workload = load_workload(repo_root() / "data/demo/workload.yaml")
    bundle = build_dataset(
        size=workload.dataset.size,
        calibration_size=workload.dataset.calibration_size,
        seed=workload.dataset.seed,
    )
    arms = arms_for_annotation()
    by_id = {item.id: item for item in bundle.test}
    task_ids = list(arms["task_ids"])

    def gold_of(answers: dict[str, str]) -> np.ndarray:
        out = []
        for task_id in task_ids:
            item = by_id[task_id]
            out.append(
                int(grade(answers[task_id], item.gold, item.answer_type, item.aliases).correct)
            )
        return np.array(out, dtype=float)

    return {
        "task_ids": task_ids,
        "items": [by_id[task_id] for task_id in task_ids],
        "gold_baseline": gold_of(arms["baseline_answers"]),
        "gold_candidate": gold_of(arms["candidate_answers"]),
        "annotations": AnnotationSet.read(annotations_path(fixtures_dir() / "test")),
    }


def corrupt(
    truth: np.ndarray,
    items: list[Any],
    *,
    answer_type: str,
    share: float = ADVERSARIAL_SHARE,
) -> np.ndarray:
    """A judge that marks ``share`` of the correct answers of one class wrong.

    Deterministic: the corrupted items are the lowest ``share`` of eligible task ids by hash, so
    the count is exact and reproducible. That matters for a release blocker — a randomised
    corruption would make the test's effect size a draw rather than a fact.
    """
    eligible = [
        index
        for index, item in enumerate(items)
        if item.answer_type == answer_type and truth[index] == 1
    ]
    ranked = sorted(eligible, key=lambda i: hashlib.sha256(items[i].id.encode()).hexdigest())
    flipped = ranked[: round(share * len(ranked))]
    judged = truth.copy()
    judged[flipped] = 0.0
    return judged


def _features(judged_baseline: np.ndarray, judged_candidate: np.ndarray) -> list[ItemFeatures]:
    """Allocation features from a judge that states no confidence.

    An injected judge has no calibrated confidence to offer, so both uncertainties are the
    binary entropy of its verdict: the policy is left with disagreement between the arms as its
    only per-item signal. Deliberately the *weakest* information the policy can run on, so the
    test is not leaning on a helpful uncertainty estimate.
    """
    return [
        ItemFeatures(
            task_id=f"t{index:04d}",
            baseline_judged=int(judged_baseline[index]),
            candidate_judged=int(judged_candidate[index]),
            baseline_uncertainty=binary_entropy(0.5),
            candidate_uncertainty=binary_entropy(0.5),
        )
        for index in range(len(judged_baseline))
    ]


class TestTheAdversarialJudge:
    """**Release blocker.** A judge with a class-conditional bias must not move the estimate."""

    def test_the_naive_estimate_leaves_its_interval_and_the_active_one_does_not(
        self, demo: dict[str, Any]
    ) -> None:
        """The headline check, on the demo's own answers.

        ``number`` is the class used here rather than ``yes_no``. The construction is the one
        UPGRADE_V3.md names — 20% of one class's correct answers marked wrong — but the demo's
        test split holds 29 yes/no items against 91 numeric ones, and 20% of 29 correct answers
        is six items, or three accuracy points against a Wilson half-width of three. The effect
        would be real and undetectable at n = 200, which would make a release blocker pass or
        fail on rounding. Corrupting the class the split actually contains makes the same point
        with a margin nobody has to squint at. The yes/no version runs below, asserting what
        that class *can* support.
        """
        truth = demo["gold_baseline"]
        judged = corrupt(truth, demo["items"], answer_type="number")
        flipped = int((truth - judged).sum())
        assert flipped >= 15, f"only {flipped} items corrupted; the check would prove nothing"

        naive = wilson(int(judged.sum()), len(judged))
        assert not naive.contains(float(truth.mean())), (
            f"the judge-only interval [{naive.low:.3f}, {naive.high:.3f}] still covers the true "
            f"accuracy {truth.mean():.3f}, so this judge is not adversarial enough to test with"
        )

        covered = 0
        for seed in SEEDS:
            allocation = allocate(
                _features(judged, judged),
                policy=COST_OPTIMAL,
                budget_usd=BUDGET,
                cost_per_item_usd=COST_PER_ITEM,
                seed=seed,
            )
            expensive: list[float | None] = [
                float(value) if drawn else None
                for value, drawn in zip(truth, allocation.sampled, strict=True)
            ]
            estimate = active_eval_estimate(
                list(judged), expensive, list(allocation.sampled), list(allocation.rates)
            )
            covered += estimate.interval.contains(float(truth.mean()))
        assert covered == len(SEEDS), (
            f"the active estimate's interval missed the true accuracy at "
            f"{len(SEEDS) - covered} of {len(SEEDS)} committed seeds"
        )

    def test_the_construction_upgrade_v3_names_verbatim(self, demo: dict[str, Any]) -> None:
        """20% of correct **yes**-answers marked wrong, exactly as written.

        What this class can support at n = 200 is the bias, not the interval: the naive point
        estimate moves by the full corrupted share and the corrected one does not. Asserting
        the interval as well would be asserting that a three-point shift is visible through a
        three-point half-width.
        """
        truth = demo["gold_baseline"]
        judged = corrupt(truth, demo["items"], answer_type="yes_no")
        flipped = int((truth - judged).sum())
        assert flipped > 0
        naive_bias = float(judged.mean() - truth.mean())
        assert naive_bias == pytest.approx(-flipped / len(truth), abs=1e-12)

        allocation = allocate(
            _features(judged, judged),
            policy=COST_OPTIMAL,
            budget_usd=BUDGET,
            cost_per_item_usd=COST_PER_ITEM,
            seed=SEEDS[0],
        )
        expensive: list[float | None] = [
            float(value) if drawn else None
            for value, drawn in zip(truth, allocation.sampled, strict=True)
        ]
        estimate = active_eval_estimate(
            list(judged), expensive, list(allocation.sampled), list(allocation.rates)
        )
        assert abs(estimate.estimate - float(truth.mean())) < abs(naive_bias)
        assert estimate.interval.contains(float(truth.mean()))

    def test_the_corrected_estimate_is_unbiased_however_bad_the_judge_is(
        self, demo: dict[str, Any]
    ) -> None:
        """The property in its strongest form: a judge that is wrong about *everything*.

        ``G = 1 - H`` carries no information at all — it is an anti-judge. The estimate stays
        centred, because the correction never depended on the judge being any good; only the
        interval width did.
        """
        truth = demo["gold_baseline"]
        judged = 1.0 - truth
        allocation = allocate(
            _features(judged, judged),
            policy=COST_OPTIMAL,
            budget_usd=BUDGET,
            cost_per_item_usd=COST_PER_ITEM,
            seed=SEEDS[0],
        )
        rates = np.asarray(allocation.rates)
        rng = np.random.default_rng(20260914)
        draws = 3000
        estimates = np.empty(draws)
        for index in range(draws):
            xi = (rng.random(len(rates)) < rates).astype(int)
            estimates[index] = (judged + (truth - judged) * xi / rates).mean()
        error = abs(float(estimates.mean()) - float(truth.mean()))
        assert error < 4 * float(estimates.std(ddof=1)) / np.sqrt(draws)

    def test_a_judge_that_is_wrong_about_one_arm_only_biases_the_delta(
        self, demo: dict[str, Any]
    ) -> None:
        """The paired case. A judge whose error hits both arms equally partly cancels in the
        difference; one that hits a single arm does not, and that is the case a cascade meets —
        the two arms return different answers, so a class-conditional judge sees different
        numbers of them."""
        base_truth, cand_truth = demo["gold_baseline"], demo["gold_candidate"]
        base_judged = corrupt(base_truth, demo["items"], answer_type="number")
        cand_judged = cand_truth.copy()
        true_delta = float(cand_truth.mean() - base_truth.mean())
        naive_delta = float(cand_judged.mean() - base_judged.mean())
        assert abs(naive_delta - true_delta) > 0.05, "the corruption did not move the delta"

        allocation = allocate(
            _features(base_judged, cand_judged),
            policy=COST_OPTIMAL,
            budget_usd=BUDGET,
            cost_per_item_usd=COST_PER_ITEM,
            seed=SEEDS[0],
        )
        drawn = list(allocation.sampled)
        paired = paired_active_eval_estimate(
            list(base_judged),
            list(cand_judged),
            [float(v) if d else None for v, d in zip(base_truth, drawn, strict=True)],
            [float(v) if d else None for v, d in zip(cand_truth, drawn, strict=True)],
            drawn,
            list(allocation.rates),
        )
        assert paired.interval.contains(true_delta)
        assert abs(paired.estimate - true_delta) < abs(naive_delta - true_delta)


class TestCoverage:
    """The interval means what it says it means."""

    def test_the_judged_interval_covers_the_gold_delta_at_every_committed_seed(
        self, demo: dict[str, Any]
    ) -> None:
        """U1's acceptance check, on the demo's **recorded** judge verdicts.

        The cheap judge here is the real one: its verdicts came out of cassettes written by
        `tokop annotate`. The strong grader is gold, because the committed annotation set holds
        strong verdicts only for the items its own seed drew, and a different seed draws
        different items. What is under test is the estimator's coverage over the draw, and gold
        is the right reference for that.
        """
        annotations = demo["annotations"]
        by_task = {item.task_id: item for item in annotations.items}
        order = [item.task_id for item in annotations.items]
        index_of = {task_id: i for i, task_id in enumerate(demo["task_ids"])}

        judged_base = np.array([by_task[t].baseline.judge_correct for t in order], dtype=float)
        judged_cand = np.array([by_task[t].candidate.judge_correct for t in order], dtype=float)
        truth_base = np.array([demo["gold_baseline"][index_of[t]] for t in order])
        truth_cand = np.array([demo["gold_candidate"][index_of[t]] for t in order])
        true_delta = float(truth_cand.mean() - truth_base.mean())

        features = [
            ItemFeatures(
                task_id=item.task_id,
                baseline_judged=item.baseline.judge_correct,
                candidate_judged=item.candidate.judge_correct,
                baseline_uncertainty=binary_entropy(item.baseline.judge_confidence),
                candidate_uncertainty=binary_entropy(item.candidate.judge_confidence),
            )
            for item in annotations.items
        ]
        for seed in SEEDS:
            allocation = allocate(
                features,
                policy=COST_OPTIMAL,
                budget_usd=BUDGET,
                cost_per_item_usd=COST_PER_ITEM,
                seed=seed,
            )
            drawn = list(allocation.sampled)
            paired = paired_active_eval_estimate(
                list(judged_base),
                list(judged_cand),
                [float(v) if d else None for v, d in zip(truth_base, drawn, strict=True)],
                [float(v) if d else None for v, d in zip(truth_cand, drawn, strict=True)],
                drawn,
                list(allocation.rates),
            )
            assert paired.interval.contains(true_delta), (
                f"seed {seed}: [{paired.interval.low:+.4f}, {paired.interval.high:+.4f}] does "
                f"not cover the gold delta {true_delta:+.4f}"
            )

    def test_the_interval_keeps_close_to_its_nominal_coverage(self, demo: dict[str, Any]) -> None:
        """A coverage simulation, the same way `wilson` is checked in `test_stats.py`.

        Five seeds passing is five seeds passing; a 95% interval is a claim about the long run,
        so the long run is what is measured.
        """
        truth = demo["gold_baseline"]
        judged = corrupt(truth, demo["items"], answer_type="number")
        allocation = allocate(
            _features(judged, judged),
            policy=COST_OPTIMAL,
            budget_usd=BUDGET,
            cost_per_item_usd=COST_PER_ITEM,
            seed=SEEDS[0],
        )
        rates = np.asarray(allocation.rates)
        rng = np.random.default_rng(20260914)
        target = float(truth.mean())
        covered = 0
        trials = 600
        for _ in range(trials):
            xi = (rng.random(len(rates)) < rates).astype(int)
            expensive: list[float | None] = [
                float(v) if d else None for v, d in zip(truth, xi, strict=True)
            ]
            estimate = active_eval_estimate(
                list(judged), expensive, list(xi), list(allocation.rates)
            )
            covered += estimate.interval.contains(target)
        rate = covered / trials
        # The interval is a normal approximation on the empirical variance of the per-item
        # terms, so it is not exact at n = 200 with weights up to 1/floor. What would be a bug
        # is systematic under-coverage, and that is what this bounds.
        assert rate >= 0.90, f"coverage was {rate:.1%}, nominally 95%"


class TestTheCommittedAnnotationSet:
    """What `tokop annotate` actually produced, read back the way the report reads it."""

    def test_the_judged_estimate_covers_the_gold_delta(self, demo: dict[str, Any]) -> None:
        judged = build_judged_delta(demo["annotations"], margin=0.03)
        true_delta = float(demo["gold_candidate"].mean() - demo["gold_baseline"].mean())
        assert judged.delta.contains(true_delta)

    def test_the_report_states_what_the_annotation_cost_and_what_it_saved(
        self, demo: dict[str, Any]
    ) -> None:
        """U1's third acceptance check: the budget spent, and the budget strong-only grading
        would have needed for the same interval width. It is allowed to be the *larger* number —
        a judge can be bad enough that mixing does not pay, and the report says so rather than
        hiding it."""
        judged = build_judged_delta(demo["annotations"], margin=0.03).as_dict()
        annotation = judged["annotation"]
        assert Decimal(annotation["annotation_cost_usd"]) > 0
        assert annotation["strong_only_items_for_same_width"] is not None
        assert annotation["strong_only_cost_usd"] is not None
        assert 0 < annotation["cost_optimal_rate"] <= 1.0
        assert annotation["method"].startswith("cost-optimal active evaluation")

    def test_every_item_keeps_a_positive_sampling_rate(self, demo: dict[str, Any]) -> None:
        assert all(item.rate > 0 for item in demo["annotations"].items)

    def test_a_drawn_item_without_both_arms_graded_is_not_counted_as_sampled(self) -> None:
        """A strong-grader call that came back unreadable is not a cheaper observation, it is
        no observation. Counting it would bias the correction towards whichever arm failed
        less often."""
        from tokop.optimize.annotation import AnnotatedArm, AnnotatedItem

        def arm(strong: int | None, failed: bool = False) -> AnnotatedArm:
            return AnnotatedArm(
                tier="cheap",
                answer_hash="x",
                judge_correct=1,
                judge_confidence=0.9,
                judge_parsed=True,
                judge_cassette_key="k",
                strong_correct=strong,
                strong_failed=failed,
            )

        half = AnnotatedItem("t1", 0.5, 1, arm(1), arm(None, failed=True))
        whole = AnnotatedItem("t2", 0.5, 1, arm(1), arm(0))
        assert not half.has_strong
        assert whole.has_strong

    def test_an_annotation_set_with_no_strong_labels_refuses(self, demo: dict[str, Any]) -> None:
        empty = AnnotationSet.from_dict(demo["annotations"].as_dict())
        for item in empty.items:
            item.sampled = 0
            item.baseline.strong_correct = None
            item.candidate.strong_correct = None
        with pytest.raises(Exception, match="nothing to correct the judge with"):
            build_judged_delta(empty, margin=0.03)

    def test_a_smaller_budget_replays_as_a_subset(self, demo: dict[str, Any]) -> None:
        recorded = demo["annotations"]
        smaller = recorded.restrict_to_budget(Decimal("0.30"))
        kept = {item.task_id for item in smaller.items if item.sampled}
        original = {item.task_id for item in recorded.items if item.sampled}
        assert kept <= original
        assert len(kept) < len(original)

    def test_a_larger_budget_is_refused_with_the_command_that_would_fix_it(
        self, demo: dict[str, Any]
    ) -> None:
        with pytest.raises(Exception, match="tokop annotate"):
            demo["annotations"].restrict_to_budget(Decimal("99"))


class TestThePolicyOnRecordedVerdicts:
    """U2's acceptance check again, on the real judge rather than a synthetic one."""

    def test_the_policy_beats_uniform_on_the_recorded_verdicts(self, demo: dict[str, Any]) -> None:
        annotations = demo["annotations"]
        index_of = {task_id: i for i, task_id in enumerate(demo["task_ids"])}
        delta = np.array(
            [
                (
                    demo["gold_candidate"][index_of[item.task_id]]
                    - demo["gold_baseline"][index_of[item.task_id]]
                )
                - (item.candidate.judge_correct - item.baseline.judge_correct)
                for item in annotations.items
            ],
            dtype=float,
        )
        features = [
            ItemFeatures(
                task_id=item.task_id,
                baseline_judged=item.baseline.judge_correct,
                candidate_judged=item.candidate.judge_correct,
                baseline_uncertainty=binary_entropy(item.baseline.judge_confidence),
                candidate_uncertainty=binary_entropy(item.candidate.judge_confidence),
            )
            for item in annotations.items
        ]

        def variance(rates: tuple[float, ...]) -> float:
            pi = np.asarray(rates)
            return float(np.sum(delta**2 * (1 - pi) / pi) / len(pi) ** 2)

        for budget in (Decimal("0.30"), BUDGET, Decimal("1.20")):
            reduced = variance(
                allocate(
                    features,
                    policy=COST_OPTIMAL,
                    budget_usd=budget,
                    cost_per_item_usd=COST_PER_ITEM,
                    seed=SEEDS[0],
                ).rates
            )
            flat = variance(
                allocate(
                    features,
                    policy=UNIFORM,
                    budget_usd=budget,
                    cost_per_item_usd=COST_PER_ITEM,
                    seed=SEEDS[0],
                ).rates
            )
            assert reduced < flat, f"no variance reduction at B={budget} on recorded verdicts"
