"""Entropy over meanings rather than over strings (UPGRADE_V3.md U6).

``self-consistency-v1`` groups repeated generations by exact match and has always said it is an
approximation of semantic entropy rather than the thing itself (DECISIONS.md D27). The gap is
narrow on a workload of numbers and yes/no and wide on free-form text, so it is tested against
the pairs that expose it rather than against a split that cannot.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokop.optimize.entailment import (
    EntailmentError,
    cluster_by_entailment,
    effective_k,
    entailment_reply,
    intraclass_correlation,
    measure_correlation,
    parse_entailment,
    render_entailment_prompt,
    semantic_entropy,
)
from tokop.optimize.scorers import SCORER_REGISTRY, ScorerContext, ScorerError, TaskView, fit_scorer
from tokop.workloads.demo.entailment_responder import means_the_same
from tokop.workloads.grading import answers_equivalent

QUESTION = "How many days do I have to return a backpack?"


def oracle(question: str, a: str, b: str) -> bool:
    """A perfect entailment model, so the clustering is tested and not the model."""
    return means_the_same(a, b)


class TestTheAcceptanceCheck:
    """Entailment clustering merges known paraphrase pairs; exact match does not."""

    #: Pairs that mean the same thing and that the workload's exact-match relation keeps
    #: apart. Chosen by checking, not by assumption: `answers_equivalent` already handles a
    #: currency symbol, a trailing percent and a hedge before a yes or no, so those are *not*
    #: cases the method improves on and claiming them would overstate it. What it does not
    #: handle is a unit word after a number, which is the gap below.
    PARAPHRASES = (
        ("60", "60 days"),
        ("30", "30 business days"),
        ("14.00", "14 dollars"),
        ("45", "45 percent"),
    )

    @pytest.mark.parametrize(("left", "right"), PARAPHRASES)
    def test_a_paraphrase_pair_lands_in_one_meaning_cluster(self, left: str, right: str) -> None:
        result = cluster_by_entailment(QUESTION, [left, right], oracle)
        assert result.sizes == [2], f"{left!r} and {right!r} were kept apart"

    @pytest.mark.parametrize(("left", "right"), PARAPHRASES)
    def test_exact_match_keeps_the_same_pair_apart(self, left: str, right: str) -> None:
        """The gap the method exists to close, measured rather than asserted. If this ever
        starts passing, `self-consistency-v1` has stopped being an approximation and the
        comparison between the two scorers has stopped meaning anything."""
        assert not answers_equivalent(left, right)

    def test_answers_that_differ_are_kept_apart(self) -> None:
        result = cluster_by_entailment(QUESTION, ["60", "74", "60 days"], oracle)
        assert sorted(result.sizes, reverse=True) == [2, 1]

    def test_a_paraphrase_lowers_the_entropy_it_would_otherwise_inflate(self) -> None:
        """The consequence for routing: three ways of saying one thing read as certainty under
        entailment and as total disagreement under exact match, and the second sends a task it
        did not need to escalate to the frontier."""
        answers = ["60", "60 days", "$60"]
        merged = semantic_entropy(cluster_by_entailment(QUESTION, answers, oracle).sizes)
        split = semantic_entropy([1, 1, 1])
        assert merged == 0.0
        assert split == pytest.approx(1.0)


class TestTheClustering:
    def test_identical_answers_cost_no_calls(self) -> None:
        """Paying a model to confirm that "60" means what "60" means is money for nothing, and
        on a workload of numbers and yes/no it is most of the bill."""
        result = cluster_by_entailment(QUESTION, ["60"] * 5, oracle)
        assert result.sizes == [5]
        assert result.calls == 0

    def test_a_distinct_pair_costs_two_directed_calls(self) -> None:
        """Both directions, because entailment is not symmetric and a model asked a symmetric
        question answers a symmetric one."""
        calls: list[tuple[str, str]] = []

        def counting(question: str, a: str, b: str) -> bool:
            calls.append((a, b))
            return means_the_same(a, b)

        result = cluster_by_entailment(QUESTION, ["60", "60 days"], counting)
        assert result.calls == 2
        assert calls == [("60", "60 days"), ("60 days", "60")]

    def test_a_rejected_pair_costs_one_call(self) -> None:
        """The second direction is only asked when the first said yes; there is nothing to
        confirm about a no."""
        result = cluster_by_entailment(QUESTION, ["60", "74"], oracle)
        assert result.calls == 1
        assert result.sizes == [1, 1]

    def test_an_unreadable_output_is_its_own_cluster_and_is_never_compared(self) -> None:
        """A generation that produced no answer means nothing, and two of them do not agree
        with each other. The same rule `grading.py` applies one level up."""
        result = cluster_by_entailment(QUESTION, ["60", None, None], oracle)
        assert sorted(result.sizes, reverse=True) == [1, 1, 1]
        assert result.calls == 0

    def test_a_pair_the_model_would_not_judge_is_kept_apart_and_counted(self) -> None:
        def refuses(question: str, a: str, b: str) -> bool:
            raise EntailmentError("the model waffled")

        result = cluster_by_entailment(QUESTION, ["60", "60 days"], refuses)
        assert result.sizes == [1, 1]
        assert result.unreadable == 1

    def test_the_grouping_is_reproducible_even_though_it_is_not_canonical(self) -> None:
        """First fit over a relation that is not transitive depends on the order the samples
        arrive in. The order is the recording's, so the grouping reproduces — which is what a
        report needs — and the module says it is not canonical rather than implying it is."""
        answers = ["60", "60 days", "74", "60"]
        first = cluster_by_entailment(QUESTION, answers, oracle)
        second = cluster_by_entailment(QUESTION, answers, oracle)
        assert first.groups == second.groups


class TestTheParser:
    def test_the_json_contract(self) -> None:
        assert parse_entailment(entailment_reply(True, "same value")) is True
        assert parse_entailment(entailment_reply(False)) is False

    def test_a_line_is_accepted(self) -> None:
        assert parse_entailment("Entails: yes") is True
        assert parse_entailment("verdict: no") is False

    def test_an_unreadable_reply_is_none_and_not_false(self) -> None:
        """`False` would silently split clusters and inflate every task's entropy, which would
        route everything to the frontier and look like caution."""
        assert parse_entailment("hard to say, honestly") is None
        assert parse_entailment("") is None

    def test_the_prompt_asks_one_direction(self) -> None:
        prompt = render_entailment_prompt(QUESTION, "60", "60 days")
        assert "Answer A: 60" in prompt
        assert "Answer B: 60 days" in prompt
        assert "entail" in prompt


class TestEffectiveK:
    def test_independent_draws_are_worth_k(self) -> None:
        """Samples drawn independently with the same probability everywhere: no more alike
        within a task than across them, so the design effect is about 1 and k samples are worth
        about k. This is the shape the demo's simulator produces, which is why its effective k
        comes out close to k."""
        import numpy as np

        rng = np.random.default_rng(20260914)
        independent = (rng.random((2000, 4)) < 0.7).astype(int).tolist()
        rho = intraclass_correlation(independent)
        # The estimate is noisy at k = 4 and negative values are clipped to zero, so it sits a
        # little above zero under true independence rather than on it. What matters is the
        # comparison below, not the absolute figure.
        assert rho < 0.06
        assert effective_k(4, rho) > 3.5

        # The same k, drawn correlated: each task repeats one answer with probability 0.8.
        correlated = []
        for _ in range(2000):
            if rng.random() < 0.8:
                value = int(rng.random() < 0.7)
                correlated.append([value] * 4)
            else:
                correlated.append((rng.random(4) < 0.7).astype(int).tolist())
        assert effective_k(4, intraclass_correlation(correlated)) < 1.5

    def test_perfectly_correlated_draws_are_worth_one(self) -> None:
        """Every sample in a task agrees and tasks differ: the same draw repeated, whatever k
        says on the invoice."""
        indicators = [[1, 1, 1, 1], [0, 0, 0, 0]] * 20
        assert intraclass_correlation(indicators) == pytest.approx(1.0)
        assert effective_k(4, 1.0) == pytest.approx(1.0)

    def test_the_design_effect_is_monotone(self) -> None:
        assert effective_k(5, 0.0) > effective_k(5, 0.2) > effective_k(5, 0.8)

    def test_total_agreement_everywhere_reads_as_total_correlation(self) -> None:
        assert intraclass_correlation([[1, 1], [1, 1], [1, 1]]) == 1.0

    def test_it_measures_from_cluster_sizes(self) -> None:
        correlation = measure_correlation([[5], [3, 2], [5], [4, 1]] * 10, 5)
        assert correlation.k == 5
        assert 0.0 <= correlation.rho <= 1.0
        assert 1.0 <= correlation.effective_k <= 5.0
        assert "design effect" in correlation.as_dict()["method"]

    def test_mismatched_depths_are_refused(self) -> None:
        with pytest.raises(EntailmentError, match="different depths"):
            measure_correlation([[5], [3]], 5)

    def test_nonsense_is_refused(self) -> None:
        with pytest.raises(EntailmentError, match="at least two tasks"):
            intraclass_correlation([[1, 1]])
        with pytest.raises(EntailmentError, match="at least two samples"):
            intraclass_correlation([[1], [0]])
        with pytest.raises(EntailmentError, match="same number of samples"):
            intraclass_correlation([[1, 1], [0]])
        with pytest.raises(EntailmentError, match=r"\[0, 1\]"):
            effective_k(3, 1.4)


class TestSepRefuses:
    """`sep-v1` reads hidden states and no provider API returns them (D27.4)."""

    def test_it_is_registered_so_asking_gets_an_answer(self) -> None:
        assert "sep-v1" in SCORER_REGISTRY

    def test_it_refuses_when_no_model_exposes_hidden_states(self) -> None:
        views = [TaskView(task_id="t1", question="q", output="a")]
        with pytest.raises(ScorerError, match="hidden states"):
            fit_scorer("sep-v1", "cheap", views, [1], ScorerContext())

    def test_the_refusal_names_the_alternative_and_the_way_out(self) -> None:
        views = [TaskView(task_id="t1", question="q", output="a")]
        try:
            fit_scorer("sep-v1", "cheap", views, [1], ScorerContext())
        except ScorerError as exc:
            assert "semantic-entropy-v1" in str(exc)
            assert "self-host" in str(exc)
            assert "official APIs only" in str(exc)

    def test_no_model_in_the_registry_exposes_them(self) -> None:
        from tokop.core.registry import load_registry

        assert load_registry().hidden_state_models() == ()


class TestSemanticEntropyNeedsItsJudge:
    def test_it_refuses_without_an_entailment_function(self) -> None:
        """Falling back to exact match would silently make this `self-consistency-v1` under a
        different name, which is the one thing D27 was careful to keep apart."""
        views = [TaskView(task_id="t1", question="q", output="a", samples=("a", "b"))]
        with pytest.raises(ScorerError, match="needs an entailment function"):
            fit_scorer("semantic-entropy-v1", "cheap", views, [1], ScorerContext(k=2))

    def test_it_refuses_below_two_samples(self) -> None:
        views = [TaskView(task_id="t1", question="q", output="a", samples=("a",))]
        with pytest.raises(ScorerError, match="at least 2 samples"):
            fit_scorer(
                "semantic-entropy-v1", "cheap", views, [1], ScorerContext(k=1, entailment=oracle)
            )

    def test_it_charges_for_the_calls_it_makes(self) -> None:
        """A scorer that spends money the cascade is not charged for is how a comparison stops
        meaning anything (D27). The price is on the protocol now, not at the call site."""
        views = [
            TaskView(
                task_id=f"t{index}",
                question=QUESTION,
                output="60",
                samples=("60", "60 days" if index % 2 else "60"),
            )
            for index in range(40)
        ]
        scorer, _ = fit_scorer(
            "semantic-entropy-v1",
            "cheap",
            views,
            [index % 2 for index in range(40)],
            ScorerContext(k=2, entailment=oracle, entailment_price=Decimal("0.001")),
        )
        distinct = views[1]
        identical = views[0]
        assert scorer.extra_cost(distinct) == Decimal("0.002")
        assert scorer.extra_cost(identical) == Decimal(0)


class TestTheDemoReport:
    def test_semantic_entropy_is_reported_beside_logistic_with_effective_k(self) -> None:
        """U6's acceptance check, as the report carries it."""
        from tokop.optimize.report import cached_report

        rows = {r["label"]: r for r in cached_report(False)["cascade"]["scorer_comparison"]}
        assert "logistic-v1" in rows
        semantic = [label for label in rows if label.startswith("semantic-entropy-v1")]
        assert semantic, f"no semantic-entropy row among {sorted(rows)}"
        for label in semantic:
            row = rows[label]
            assert row["scorer_auroc"]
            correlation = row["sample_correlation"]
            assert correlation, f"{label} reports no effective k"
            for tier, measured in correlation.items():
                assert 1.0 <= measured["effective_k"] <= row["k"], tier

    def test_the_effective_k_says_these_samples_are_independent(self) -> None:
        """The number that replaces D27's paragraph. On these fixtures the repeats are drawn
        independently, so k samples really are worth k — which is a fact about the simulator
        and not about sampling, and is exactly why the scorer comparison is still withheld."""
        from tokop.optimize.report import cached_report

        rows = [
            r
            for r in cached_report(False)["cascade"]["scorer_comparison"]
            if r["sample_correlation"]
        ]
        assert rows
        worst = min(
            measured["effective_k"] / row["k"]
            for row in rows
            for measured in row["sample_correlation"].values()
        )
        assert worst > 0.8, (
            "the fixtures' repeated samples have become correlated; the scorer comparison's "
            "caveat needs rewriting, not just this test"
        )

    def test_clustering_by_meaning_costs_money_and_buys_nothing_here(self) -> None:
        """The honest result on this workload, pinned so it cannot quietly stop being reported.
        The answers are numbers, enums and yes/no, so entailment and exact match agree on every
        pair the fixtures contain — and the entailment calls are charged anyway."""
        from tokop.optimize.report import cached_report

        rows = {r["label"]: r for r in cached_report(False)["cascade"]["scorer_comparison"]}
        for k in (3, 5):
            semantic = rows.get(f"semantic-entropy-v1 k={k}")
            consistency = rows.get(f"self-consistency-v1 k={k}")
            if not semantic or not consistency:
                continue
            assert semantic["accuracy"]["point"] == consistency["accuracy"]["point"]
            assert (
                semantic["cost_per_successful_task"]["point"]
                > consistency["cost_per_successful_task"]["point"]
            ), "the entailment calls are not being charged"
