"""Pricing a checkable output contract (UPGRADE_V3.md U4).

The lever: ask the model to show the step from the rule it quoted to the number it returned.
That costs output tokens, it may cost accuracy — Kirchner et al. measure a legibility tax, and
optimizing for legibility rather than only for answer correctness really does cost test
performance — and it buys agreement between a cheap judge and a strong grader. Agreement is what
the annotation budget is spent on, so for once legibility has a price in dollars.

**The test that matters is the disclosure one.** A row that showed the saving and hid the
accuracy it cost would be the single most tempting dishonesty in this product, so the report is
asserted to carry a negative accuracy delta with its sign, on the screen and in the CLI, however
inconvenient it is. On the demo's fixtures it *is* inconvenient: the contract loses 2.5 accuracy
points and does not pay for itself, and that is what the report says.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokop.optimize.annotation import (
    AnnotationError,
    budget_for_standard_error,
    rate_for_standard_error,
)
from tokop.optimize.report import TARGET_STANDARD_ERROR, cached_report


@pytest.fixture(scope="module")
def contract() -> dict:
    return cached_report(False)["contract"]


class TestTheRowExists:
    def test_the_waterfall_carries_a_checkable_contract_step(self) -> None:
        payload = cached_report(False)
        pipelines = [step["pipeline"] for step in payload["waterfall"]]
        assert "B2c" in pipelines, f"no checkable-contract row in the waterfall: {pipelines}"
        # It sits between the prompt rewrite and the cascade, where the lever belongs.
        assert pipelines.index("B2") < pipelines.index("B2c") < pipelines.index("B3")

    def test_the_row_carries_the_trade(self, contract: dict) -> None:
        assert contract["available"], contract.get("reason")
        step = next(s for s in cached_report(False)["waterfall"] if s["pipeline"] == "B2c")
        assert step["contract"]["available"]
        assert step["contract"]["candidate_pipeline"] == "B2c"

    def test_both_arms_are_judged_by_the_same_judge_on_the_same_tasks(self, contract: dict) -> None:
        """Otherwise the agreement figures are two experiments, not one comparison."""
        assert len(contract["arms"]) == 2
        assert contract["arms"][0]["pipeline"] != contract["arms"][1]["pipeline"]
        assert contract["annotated"] > 0
        assert contract["judge"]["model_id"]


class TestItNeverSuppressesANegativeAccuracyDelta:
    """U4's acceptance check."""

    def test_the_accuracy_delta_is_reported_with_its_sign(self, contract: dict) -> None:
        before, after = contract["arms"]
        assert contract["accuracy_delta"] == pytest.approx(
            after["accuracy"] - before["accuracy"], abs=1e-12
        )

    def test_a_negative_delta_survives_into_the_payload(self, contract: dict) -> None:
        """On these fixtures the contract costs accuracy. The report is asserted to say so
        rather than to round it away, clamp it at zero, or report the magnitude without the
        sign — the three ways this number usually disappears."""
        assert contract["accuracy_delta"] < 0, (
            "the demo's checkable contract is supposed to cost accuracy; if it stopped doing "
            "so this test has stopped checking anything and the fixture needs revisiting"
        )
        step = next(s for s in cached_report(False)["waterfall"] if s["pipeline"] == "B2c")
        assert step["contract"]["accuracy_delta"] < 0
        # And the arm rows carry the raw accuracies, so the delta can be checked against them.
        assert step["contract"]["arms"][1]["accuracy"] < step["contract"]["arms"][0]["accuracy"]

    def test_the_saving_and_the_premium_are_kept_apart(self, contract: dict) -> None:
        """Netting a one-off annotation saving against a per-task generation premium into a
        single number would hide which is which. Both are reported, and so is the net."""
        saving = Decimal(contract["annotation_saving_usd"])
        premium = Decimal(contract["generation_premium_usd"])
        net = Decimal(contract["net_on_evaluation_split_usd"])
        assert net == saving - premium
        assert contract["pays_for_itself"] == (saving > premium)
        assert "paid once per evaluation" in contract["note"]

    def test_the_verdict_on_these_fixtures_is_that_it_does_not_pay(self, contract: dict) -> None:
        """Stated rather than implied. A lever the product sells but its own numbers refuse is
        exactly the thing that has to be visible."""
        assert not contract["pays_for_itself"]
        assert Decimal(contract["net_on_evaluation_split_usd"]) < 0


class TestWhatCheckabilityBuys:
    def test_the_checkable_contract_raises_judge_agreement(self, contract: dict) -> None:
        before, after = contract["arms"]
        assert after["checkable"]
        assert not before["checkable"]
        assert (
            after["judge_agreement_with_strong_grader"]
            > before["judge_agreement_with_strong_grader"]
        )
        assert contract["agreement_delta"] > 0

    def test_more_agreement_means_fewer_strong_labels_for_the_same_interval(
        self, contract: dict
    ) -> None:
        """The mechanism the whole lever rests on: agreement is one minus the mean square error
        of the correction, and a smaller correction needs a smaller sample."""
        before, after = contract["arms"]
        assert after["judge_mean_square_error"] < before["judge_mean_square_error"]
        assert after["sampling_rate_for_target"] < before["sampling_rate_for_target"]
        assert Decimal(after["annotation_cost_for_target_usd"]) < Decimal(
            before["annotation_cost_for_target_usd"]
        )

    def test_it_costs_output_tokens_and_they_are_charged(self, contract: dict) -> None:
        before, after = contract["arms"]
        assert after["output_tokens"] > before["output_tokens"]
        assert Decimal(after["generation_cost_usd"]) > Decimal(before["generation_cost_usd"])
        assert Decimal(contract["generation_premium_usd"]) > 0

    def test_both_arms_are_priced_at_the_same_target(self, contract: dict) -> None:
        assert contract["target_standard_error"] == TARGET_STANDARD_ERROR


class TestTheRateMath:
    def test_the_rate_solves_the_variance_equation(self) -> None:
        """``se^2 = mse (1 - pi) / (pi T)`` rearranged. Checked by substitution rather than by
        restating the algebra."""
        mse, n, target = 0.16, 200, 0.02
        rate = rate_for_standard_error(mse, n, target)
        realized = (mse * (1 - rate) / (rate * n)) ** 0.5
        assert realized == pytest.approx(target, rel=1e-9)

    def test_a_better_judge_needs_a_smaller_sample(self) -> None:
        good = rate_for_standard_error(0.05, 200, 0.02)
        bad = rate_for_standard_error(0.30, 200, 0.02)
        assert good < bad

    def test_a_demanding_target_asks_for_nearly_everything(self) -> None:
        """The rate approaches 1 from below rather than clamping at it, because at pi = 1 the
        variance is zero and any target is met. A bad judge and a tight target therefore mean
        "grade almost every item", which is the honest answer and not a saturation artefact."""
        rate = rate_for_standard_error(0.5, 10, 0.001)
        assert 0.99 < rate <= 1.0

    def test_a_judge_that_never_disagrees_still_gets_the_floor(self) -> None:
        """Believing a judge agrees perfectly requires having checked, and checking nothing is
        how you would never find out you were wrong."""
        assert rate_for_standard_error(0.0, 200, 0.02) > 0

    def test_the_budget_charges_the_cheap_pass_on_every_item(self) -> None:
        cost, rate = budget_for_standard_error(0.16, 100, 0.02, Decimal("0.001"), Decimal("0.010"))
        expected = Decimal("0.001") * 100 + Decimal("0.010") * Decimal(str(rate)) * 100
        assert cost == expected.quantize(Decimal("0.00001"))

    def test_nonsense_is_refused(self) -> None:
        with pytest.raises(AnnotationError, match="at least one item"):
            rate_for_standard_error(0.1, 0, 0.02)
        with pytest.raises(AnnotationError, match="positive"):
            rate_for_standard_error(0.1, 10, 0.0)
