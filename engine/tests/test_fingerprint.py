"""A certificate binds to what its tasks looked like, and expires when they stop (M18).

Until M18 a certificate bound to models, prices, grading mode and the dataset's provenance, and
nothing in it noticed when the traffic it was being quoted about stopped resembling the split it
was measured on. A cascade proven on a mix that is 10% hard exceptions is not a claim about a
month where exceptions are half the queue, and nothing was checking.

``TestTheAcceptanceCheck`` is PLAN.md section 9's condition: shift the task mix and show the
certificate expiring for **distribution** reasons, with the divergence and the uncovered region
named. The other thing this file pins is the one PLAN.md calls out by name — distribution drift
and the canary's existing outcome drift are different checks with different fields, so a passing
outcome look can never read as a covered distribution.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pytest

from tokop.optimize.certificate import CanaryLog, Certificate, ModelSnapshot, take_look
from tokop.optimize.fingerprint import (
    DRIFT_TVD,
    FingerprintError,
    divergence,
    fingerprint,
)
from tokop.workloads.grading import AnswerType

TODAY = date(2026, 9, 15)


@dataclass(frozen=True)
class FakeItem:
    """The narrowest thing satisfying ``Item`` that a fingerprint needs."""

    id: str
    question: str
    gold: str = "x"
    answer_type: AnswerType = "string"
    question_type: str = "lookup"
    aliases: tuple[str, ...] = ()
    template_id: str = ""
    sections: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id}

    def router_view(self) -> dict[str, object]:
        return {"id": self.id, "question": self.question}


def mix(**counts: int) -> list[FakeItem]:
    """A set of tasks with the given number of each type, all the same length."""
    items: list[FakeItem] = []
    for question_type, count in counts.items():
        for index in range(count):
            items.append(
                FakeItem(
                    id=f"{question_type}-{index}",
                    question=f"a question of about the usual length about {question_type} "
                    f"number {index}",
                    question_type=question_type,
                )
            )
    return items


def certificate_over(items: list[FakeItem]) -> Certificate:
    return Certificate(
        workload="w",
        baseline_pipeline="B0",
        candidate_pipeline="B3",
        verdict="non_inferior",
        margin=0.03,
        delta_point=0.005,
        delta_low=-0.03,
        delta_high=0.04,
        n=len(items),
        split_sizes={"test": len(items)},
        grading_mode="gold",
        calibration_mode="gold",
        model_snapshots=(ModelSnapshot("frontier", "m", "m@1"),),
        price_snapshot_id="p1",
        origin="simulated",
        dataset_provenance={"certifiable": True},
        issued=TODAY,
        expires=date(2026, 10, 15),
        looks_planned=4,
        alpha=0.05,
        spending="pocock",
        workload_fingerprint=fingerprint(items),
    )


class TestTheFingerprint:
    def test_it_measures_the_mix_the_lengths_and_a_proxy(self) -> None:
        shape = fingerprint(mix(lookup=60, computation=20, exception=20))
        assert shape.task_type_mix == {"computation": 0.2, "exception": 0.2, "lookup": 0.6}
        assert set(shape.length_quantiles) == {"p10", "p50", "p90", "p99"}
        assert set(shape.difficulty) == {
            "type_concentration",
            "rarest_type_share",
            "mean_question_tokens",
        }
        assert shape.n == 100 and shape.counter

    def test_concentration_is_zero_when_every_type_is_equal_and_one_when_one_type_is_all(
        self,
    ) -> None:
        even = fingerprint(mix(a=25, b=25, c=25, d=25))
        assert even.difficulty["type_concentration"] == pytest.approx(0.0, abs=1e-9)
        single = fingerprint(mix(a=100))
        assert single.difficulty["type_concentration"] == 1.0

    def test_a_fingerprint_of_nothing_is_refused(self) -> None:
        with pytest.raises(FingerprintError, match="not a fingerprint"):
            fingerprint([])

    def test_it_round_trips_through_json(self) -> None:
        from tokop.optimize.fingerprint import Fingerprint

        shape = fingerprint(mix(lookup=10, exception=5))
        assert Fingerprint.from_dict(shape.as_dict()) == shape

    def test_comparing_across_counters_is_refused_rather_than_reported(self) -> None:
        """Length quantiles move with the tokenizer; comparing them would invent drift."""
        a = fingerprint(mix(lookup=10))
        b = replace(a, counter="some-other-counter")
        with pytest.raises(FingerprintError, match="would report a counter change"):
            divergence(a, b)


class TestTheAcceptanceCheck:
    """PLAN.md section 9: shift the mix, watch the certificate expire for distribution reasons."""

    def test_a_shifted_mix_raises_and_names_the_divergence_and_the_region(self) -> None:
        certified = mix(lookup=90, computation=90, exception=18, two_hop=2)
        recent = mix(lookup=20, computation=20, exception=60, two_hop=100)
        cert = certificate_over(certified)
        log = CanaryLog(certificate_fingerprint=cert.fingerprint())

        result = take_look(cert, log, today=TODAY, recent=fingerprint(recent))

        assert result.raised
        assert result.distribution_tested
        gap = result.divergence
        assert gap is not None and gap.total_variation >= DRIFT_TVD
        # The divergence is named as a share of traffic, with the types that moved.
        assert any("the task mix moved by" in reason for reason in result.reasons)
        # And the uncovered region is named individually, with both shares.
        assert [name for name, _, _ in gap.uncovered] == ["two_hop"]
        assert any("two_hop" in reason and "uncovered" in reason for reason in result.reasons), (
            result.reasons
        )

    def test_traffic_that_still_looks_like_the_split_does_not_raise(self) -> None:
        certified = mix(lookup=60, computation=25, exception=15)
        recent = mix(lookup=58, computation=27, exception=15)
        cert = certificate_over(certified)
        log = CanaryLog(certificate_fingerprint=cert.fingerprint())
        result = take_look(cert, log, today=TODAY, recent=fingerprint(recent))
        assert not result.raised
        assert result.distribution_tested
        assert result.divergence is not None and not result.divergence.drifted

    def test_a_length_shift_raises_even_when_the_mix_holds(self) -> None:
        """Cost per task moves with input length whether or not accuracy does."""
        certified = mix(lookup=50, computation=50)
        recent = [
            replace(item, question=item.question * 4) for item in mix(lookup=50, computation=50)
        ]
        cert = certificate_over(certified)
        log = CanaryLog(certificate_fingerprint=cert.fingerprint())
        result = take_look(cert, log, today=TODAY, recent=fingerprint(recent))
        assert result.raised
        assert result.divergence is not None and result.divergence.length_moves
        assert any("input length at p50 moved" in reason for reason in result.reasons)


class TestTheTwoDriftsStaySeparate:
    """PLAN.md section 9: two names, two fields, both reported."""

    def test_an_outcome_look_that_passes_says_nothing_about_coverage(self) -> None:
        cert = certificate_over(mix(lookup=60, computation=40))
        log = CanaryLog(certificate_fingerprint=cert.fingerprint())
        result = take_look(cert, log, today=TODAY, observed=(0.005, 0.01))
        assert result.drift_tested and not result.raised
        assert not result.distribution_tested
        assert "No distribution check" in result.distribution_note

    def test_a_coverage_look_that_raises_says_nothing_about_the_outcome(self) -> None:
        cert = certificate_over(mix(lookup=98, computation=2))
        log = CanaryLog(certificate_fingerprint=cert.fingerprint())
        result = take_look(cert, log, today=TODAY, recent=fingerprint(mix(computation=100)))
        assert result.raised and result.distribution_tested
        assert not result.drift_tested
        assert result.observed_delta is None

    def test_the_two_are_different_keys_in_the_log(self) -> None:
        cert = certificate_over(mix(lookup=60, computation=40))
        log = CanaryLog(certificate_fingerprint=cert.fingerprint())
        payload = take_look(
            cert, log, today=TODAY, observed=(0.005, 0.01), recent=fingerprint(mix(lookup=100))
        ).as_dict()
        assert payload["drift_tested"] is True
        assert payload["distribution_tested"] is True
        assert payload["divergence"]["total_variation"] > 0

    def test_a_certificate_issued_without_a_fingerprint_cannot_be_checked_and_says_so(
        self,
    ) -> None:
        cert = replace(certificate_over(mix(lookup=100)), workload_fingerprint=None)
        log = CanaryLog(certificate_fingerprint=cert.fingerprint())
        result = take_look(cert, log, today=TODAY, recent=fingerprint(mix(computation=100)))
        assert not result.distribution_tested
        assert "issued without a workload fingerprint" in result.distribution_note
        assert not result.raised


class TestTheCertificateCarriesIt:
    def test_it_round_trips_through_the_certificate_json(self) -> None:
        cert = certificate_over(mix(lookup=60, computation=40))
        again = Certificate.from_dict(cert.as_dict())
        assert again.workload_fingerprint == cert.workload_fingerprint

    def test_an_older_certificate_without_one_still_reads(self) -> None:
        raw = certificate_over(mix(lookup=10)).as_dict()
        del raw["workload_fingerprint"]
        assert Certificate.from_dict(raw).workload_fingerprint is None

    def test_the_identity_hash_is_not_the_workload_fingerprint(self) -> None:
        """Two things called a fingerprint; they answer different questions and never merge."""
        cert = certificate_over(mix(lookup=60, computation=40))
        assert isinstance(cert.fingerprint(), str)
        assert cert.workload_fingerprint is not None
        assert cert.fingerprint() not in str(cert.workload_fingerprint.as_dict())
