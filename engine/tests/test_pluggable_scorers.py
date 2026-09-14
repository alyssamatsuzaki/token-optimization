"""The scorer interface, the sampling scorer, and the money it costs (SPEC.md 7.5).

The load-bearing test in this file is ``TestSamplingIsCharged``. A sampling scorer that was
not charged for its samples would look free, and every cost comparison against it would be a
lie in the product's favour — which is the exact failure this project exists to prevent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from itertools import pairwise

import pytest

from tokop.adapters.base import Block, LLMRequest, Message
from tokop.optimize.scorers import (
    LOGISTIC_V1,
    SELF_CONSISTENCY_V1,
    ScorerContext,
    ScorerError,
    ScorerKind,
    TaskView,
    discrete_entropy,
    fit_scorer,
    group_indices,
    group_samples,
    majority_index,
    register_scorer,
    scorer_kind,
)
from tokop.workloads.grading import answers_equivalent


def answer(value: str, evidence: str = "e", section: str = "s") -> str:
    return json.dumps({"answer": value, "evidence": evidence, "section": section})


@pytest.fixture(scope="module")
def report():
    """Built once for this module. Every configuration is searched and bootstrapped inside a
    single build, so rebuilding per test buys nothing and costs minutes."""
    from tokop.optimize.report import build_report

    return build_report()


class TestRegistry:
    def test_both_scorers_are_registered_and_describe_their_call_cost(self) -> None:
        assert scorer_kind(LOGISTIC_V1).calls_per_task(5) == 1
        assert scorer_kind(SELF_CONSISTENCY_V1).calls_per_task(5) == 5

    def test_an_unknown_scorer_is_refused_and_the_registry_is_listed(self) -> None:
        with pytest.raises(ScorerError, match=LOGISTIC_V1):
            scorer_kind("no-such-scorer")

    def test_a_duplicate_registration_is_refused(self) -> None:
        with pytest.raises(ScorerError, match="already registered"):
            register_scorer(
                ScorerKind(LOGISTIC_V1, "a clash", scorer_kind(LOGISTIC_V1).fit, lambda k: 1)
            )

    def test_a_method_note_names_what_the_scorer_costs(self) -> None:
        assert "5 calls per scored task" in scorer_kind(SELF_CONSISTENCY_V1).method_note(5)
        assert "1 call per scored task" in scorer_kind(LOGISTIC_V1).method_note(5)


class TestGroupingAndEntropy:
    def test_unanimous_samples_carry_no_entropy(self) -> None:
        samples = [answer("60")] * 5
        assert group_samples(samples, answers_equivalent) == [5]
        assert discrete_entropy([5]) == 0.0

    def test_total_disagreement_is_maximal_entropy(self) -> None:
        samples = [answer(str(n)) for n in (60, 74, 80, 12, 5)]
        assert discrete_entropy(group_samples(samples, answers_equivalent)) == pytest.approx(1.0)

    def test_entropy_rises_as_agreement_falls(self) -> None:
        four_one = discrete_entropy(
            group_samples([answer("60")] * 4 + [answer("74")], answers_equivalent)
        )
        three_two = discrete_entropy(
            group_samples([answer("60")] * 3 + [answer("74")] * 2, answers_equivalent)
        )
        assert 0.0 < four_one < three_two < 1.0

    def test_formatting_differences_do_not_count_as_disagreement(self) -> None:
        """The grader would score these identically, so the scorer must group them."""
        samples = [answer("60"), answer("60.00"), answer("$60"), answer("60"), answer("60")]
        assert group_samples(samples, answers_equivalent) == [5]

    def test_unparseable_outputs_never_agree_with_each_other(self) -> None:
        """Two failures are not evidence of consensus. If they grouped, a tier that reliably
        emitted garbage would read as perfectly self-consistent."""
        samples = ["rambling " * 40, "drivel " * 40, answer("60")]
        assert group_samples(samples, answers_equivalent) == [1, 1, 1]

    def test_grouping_is_order_independent(self) -> None:
        forward = group_samples([answer("60"), answer("74"), answer("60")], answers_equivalent)
        backward = group_samples([answer("74"), answer("60"), answer("60")], answers_equivalent)
        assert forward == backward == [2, 1]

    def test_the_majority_answer_is_returned_not_the_first(self) -> None:
        samples = [answer("74"), answer("60"), answer("60"), answer("60"), answer("74")]
        assert group_indices(samples, answers_equivalent) == [[0, 4], [1, 2, 3]]
        assert majority_index(samples, answers_equivalent) == 1

    def test_a_tie_breaks_towards_the_earliest_sample(self) -> None:
        samples = [answer("74"), answer("60")]
        assert majority_index(samples, answers_equivalent) == 0


class TestSelfConsistencyRefuses:
    """It must never quietly score something weaker than it advertises."""

    @property
    def views(self) -> list[TaskView]:
        pair = (answer("60"), answer("74"))
        return [TaskView(f"t{i}", "q", answer("60"), samples=pair) for i in range(40)]

    @property
    def labels(self) -> list[int]:
        return [i % 2 for i in range(40)]

    def test_without_an_equivalence_function_it_refuses_rather_than_using_string_equality(
        self,
    ) -> None:
        with pytest.raises(ScorerError, match="string equality"):
            fit_scorer(SELF_CONSISTENCY_V1, "cheap", self.views, self.labels, ScorerContext(k=2))

    def test_a_single_sample_is_refused_because_it_always_agrees_with_itself(self) -> None:
        context = ScorerContext(k=1, equivalence=answers_equivalent)
        with pytest.raises(ScorerError, match="at least 2 samples"):
            fit_scorer(SELF_CONSISTENCY_V1, "cheap", self.views, self.labels, context)

    def test_a_matrix_recorded_too_shallow_is_refused_by_name(self) -> None:
        context = ScorerContext(k=5, equivalence=answers_equivalent)
        with pytest.raises(ScorerError, match="carries 2 sample"):
            fit_scorer(SELF_CONSISTENCY_V1, "cheap", self.views, self.labels, context)


class TestSampleIndexDoesNotDisturbRecordings:
    def request(self, **kwargs: object) -> LLMRequest:
        return LLMRequest(
            provider="simulated",
            model="m",
            system=[Block(text="sys")],
            messages=[Message(role="user", blocks=[Block(text="hi")])],
            **kwargs,  # type: ignore[arg-type]
        )

    def test_sample_zero_hashes_exactly_as_a_request_with_no_sample_field(self) -> None:
        """The guarantee that kept 1,314 committed cassettes valid (DECISIONS.md D27)."""
        plain = self.request()
        explicit = self.request(sample_index=0)
        assert plain.cassette_key() == explicit.cassette_key()
        assert "sample_index" not in plain.canonical()

    def test_each_repeat_gets_its_own_cassette(self) -> None:
        keys = {self.request(sample_index=i).cassette_key() for i in range(5)}
        assert len(keys) == 5

    def test_the_field_never_reaches_either_provider_payload(self) -> None:
        """It is a recording-side discriminator standing in for provider nondeterminism. If it
        were sent, it would be an invented API parameter on a real request."""
        from tokop.adapters.anthropic import build_payload as anthropic_payload
        from tokop.adapters.openai_compatible import build_payload as openai_payload

        request = self.request(sample_index=3)
        for build in (anthropic_payload, openai_payload):
            assert "sample_index" not in json.dumps(build(request))


class TestSamplingIsCharged:
    """k samples at a tier cost k generations, and the cascade pays for all of them."""

    def test_the_report_charges_every_sample_a_configuration_draws(self, report) -> None:
        rows = {r["label"]: r for r in report["cascade"]["scorer_comparison"]}
        assert len(rows) > 1, "the comparison needs more than one configuration to mean anything"

        single = rows["logistic-v1"]
        assert Decimal(single["sampling_cost_usd"]) == 0, "a one-call scorer must cost no samples"

        sampled = [r for label, r in rows.items() if r["k"] > 1]
        assert sampled, "no sampling configuration was evaluated"
        for row in sampled:
            assert Decimal(row["sampling_cost_usd"]) > 0, (
                f"{row['label']} drew {row['k']} samples per task and was charged nothing for "
                "them; its cost comparison against a one-call scorer would be meaningless"
            )

    def test_drawing_more_samples_costs_strictly_more(self, report) -> None:
        rows = {
            r["k"]: Decimal(r["sampling_cost_usd"])
            for r in report["cascade"]["scorer_comparison"]
            if r["scorer"] == SELF_CONSISTENCY_V1
        }
        ordered = sorted(rows)
        assert len(ordered) >= 2
        for smaller, larger in pairwise(ordered):
            assert rows[larger] > rows[smaller], f"k={larger} was not charged more than k={smaller}"

    def test_the_sampling_cost_is_counted_once_in_the_proof_total(self, report) -> None:
        """It lives on the scorer line. Counting it in the matrix line too would inflate the
        proof cost and understate how quickly the saving repays it."""
        cost = report["proof"]["proof_cost"]
        total = sum(Decimal(v) for k, v in cost.items() if k != "total_usd")
        assert total == Decimal(cost["total_usd"])


class TestTheSearchRespectsWhatMayBeAdopted:
    def test_the_demo_measures_both_scorers_but_adopts_only_the_one_it_trusts(self, report) -> None:
        choice = report["cascade"]["scorer_choice"]
        considered = {c["scorer"] for c in choice["considered"]}
        assert SELF_CONSISTENCY_V1 in considered, "it must still be measured"
        assert choice["adoptable"] == [LOGISTIC_V1]
        assert choice["scorer"] == LOGISTIC_V1

    def test_the_operating_point_note_says_what_was_withheld(self, report) -> None:
        note = report["proof"]["operating_point_note"]
        assert "calibration split" in note
        assert SELF_CONSISTENCY_V1 in note
        assert "does not let the search adopt it" in note
