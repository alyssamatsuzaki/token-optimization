"""Certificates expire, and looking repeatedly does not manufacture alarms (UPGRADE_V3.md U5).

Two claims, and the second is the one people get wrong. A canary that checks a stable quantity
at the 5% level every week will raise about once a season on a workload where nothing happened,
and a product that cried wolf on that schedule would be turned off inside a month. So alpha is
spent across the looks a certificate plans, and the family-wise error rate is measured by
simulation rather than argued from the algebra.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest
from scipy import stats as scipy_stats

from tokop.optimize.certificate import (
    DEFAULT_ALPHA,
    OBRIEN_FLEMING,
    POCOCK,
    CanaryLog,
    Certificate,
    CertificateError,
    ModelSnapshot,
    alpha_schedule,
    cumulative_alpha,
    issue,
    paired_delta,
    stratified_subset,
    take_look,
)

TODAY = date(2026, 9, 14)


def a_certificate(**overrides: object) -> Certificate:
    defaults = dict(
        workload="w",
        baseline_pipeline="B0",
        candidate_pipeline="B3",
        verdict="non_inferior",
        margin=0.03,
        delta_point=0.005,
        delta_low=-0.030,
        delta_high=0.040,
        n=200,
        split_sizes={"calibration": 100, "test": 200},
        grading_mode="gold",
        calibration_mode="gold",
        model_snapshots=(
            ModelSnapshot("cheap", "haiku", "haiku@2026-09-01"),
            ModelSnapshot("frontier", "opus", "opus@2026-09-01"),
        ),
        price_snapshot_id="prices-1",
        origin="simulated",
        dataset_provenance={"certifiable": True, "refusals": []},
        issued=TODAY,
        expires=TODAY + timedelta(days=30),
        looks_planned=4,
        alpha=DEFAULT_ALPHA,
        spending=POCOCK,
    )
    defaults.update(overrides)
    return Certificate(**defaults)  # type: ignore[arg-type]


class TestAlphaSpending:
    """The acceptance check: repeated looks hold the family-wise error rate."""

    @pytest.mark.parametrize("looks", [1, 2, 4, 12, 52])
    @pytest.mark.parametrize("spending", [POCOCK, OBRIEN_FLEMING])
    def test_the_levels_spend_exactly_alpha_across_the_family(
        self, looks: int, spending: str
    ) -> None:
        """Under independence the probability of never raising is the product of (1 - alpha_k),
        so the family-wise error rate is one minus that product. It has to come to alpha."""
        levels = alpha_schedule(DEFAULT_ALPHA, looks, spending)
        assert len(levels) == looks
        assert all(0 <= level <= 1 for level in levels)
        never = 1.0
        for level in levels:
            never *= 1 - level
        assert 1 - never == pytest.approx(DEFAULT_ALPHA, abs=1e-9)

    def test_the_measured_error_rate_matches_the_stated_one(self) -> None:
        """Simulated rather than derived. Each look is a fresh stratified subset, so the looks
        are independent draws; a certificate's whole life is one trial and the question is how
        often *any* look in it raises when nothing has changed."""
        looks = 4
        levels = alpha_schedule(DEFAULT_ALPHA, looks, POCOCK)
        critical = [float(scipy_stats.norm.ppf(1 - level / 2)) for level in levels]
        rng = np.random.default_rng(20260914)
        trials = 40_000
        z = rng.standard_normal((trials, looks))
        raised = (np.abs(z) > np.array(critical)).any(axis=1)
        rate = float(raised.mean())
        standard_error = (DEFAULT_ALPHA * (1 - DEFAULT_ALPHA) / trials) ** 0.5
        assert abs(rate - DEFAULT_ALPHA) < 4 * standard_error, (
            f"family-wise error rate {rate:.4f} against a stated {DEFAULT_ALPHA}"
        )

    def test_looking_once_at_the_nominal_level_would_not_hold_it(self) -> None:
        """The failure the spending exists to prevent, measured so the correction is not taken
        on faith: four looks at an uncorrected 5% raise on nearly one workload in five."""
        rng = np.random.default_rng(1)
        critical = float(scipy_stats.norm.ppf(1 - DEFAULT_ALPHA / 2))
        z = rng.standard_normal((40_000, 4))
        naive = float((np.abs(z) > critical).any(axis=1).mean())
        assert naive > 0.15

    def test_pocock_spends_evenly_and_obrien_fleming_spends_late(self) -> None:
        even = alpha_schedule(DEFAULT_ALPHA, 4, POCOCK)
        late = alpha_schedule(DEFAULT_ALPHA, 4, OBRIEN_FLEMING)
        assert max(even) - min(even) < 0.002
        assert late[-1] > late[0] * 10

    def test_nonsense_is_refused(self) -> None:
        with pytest.raises(CertificateError, match="at least one look"):
            alpha_schedule(DEFAULT_ALPHA, 0)
        with pytest.raises(CertificateError, match="unknown alpha-spending"):
            cumulative_alpha(DEFAULT_ALPHA, 0.5, "vibes")
        with pytest.raises(CertificateError, match="alpha must be"):
            cumulative_alpha(0.0, 0.5)


class TestTheCanaryRaises:
    def test_a_swapped_snapshot_identifier_raises(self) -> None:
        """UPGRADE_V3.md U5's acceptance check. A provider shipping a new revision under the
        same name is the case the whole feature exists for, and it is not a statistical
        question: the certificate is about a model that is no longer there."""
        cert = a_certificate()
        log = CanaryLog(cert.fingerprint())
        identity = cert.identity()
        identity["model_snapshots"] = [
            {"role": "cheap", "model_id": "haiku", "snapshot": "haiku@2026-09-01"},
            {"role": "frontier", "model_id": "opus", "snapshot": "opus@2026-10-01"},
        ]
        result = take_look(cert, log, today=TODAY, identity=identity)
        assert result.raised
        assert any("frontier model changed" in reason for reason in result.reasons)

    def test_an_unchanged_world_does_not_raise(self) -> None:
        cert = a_certificate()
        log = CanaryLog(cert.fingerprint())
        result = take_look(cert, log, today=TODAY, identity=cert.identity())
        assert not result.raised
        assert not result.drift_tested
        assert "cannot produce a different answer" in result.drift_note

    def test_a_changed_price_snapshot_raises(self) -> None:
        cert = a_certificate()
        identity = {**cert.identity(), "price_snapshot_id": "prices-2"}
        result = take_look(cert, CanaryLog(cert.fingerprint()), today=TODAY, identity=identity)
        assert result.raised
        assert any("price_snapshot_id changed" in reason for reason in result.reasons)

    def test_a_changed_grading_or_calibration_mode_raises(self) -> None:
        """A certificate earned with gold labels is not a certificate earned without them."""
        cert = a_certificate()
        for key, value in (("grading_mode", "judged"), ("calibration_mode", "penalized-v1")):
            identity = {**cert.identity(), key: value}
            result = take_look(cert, CanaryLog(cert.fingerprint()), today=TODAY, identity=identity)
            assert result.raised, key

    def test_an_expired_certificate_raises(self) -> None:
        cert = a_certificate()
        result = take_look(
            cert,
            CanaryLog(cert.fingerprint()),
            today=cert.expires + timedelta(days=1),
            identity=cert.identity(),
        )
        assert result.raised
        assert any("expired" in reason for reason in result.reasons)

    def test_a_moved_interval_raises_and_a_steady_one_does_not(self) -> None:
        cert = a_certificate()
        steady = take_look(
            cert,
            CanaryLog(cert.fingerprint()),
            today=TODAY,
            identity=cert.identity(),
            observed=(0.007, 0.02),
        )
        assert not steady.raised
        assert steady.drift_tested
        moved = take_look(
            cert,
            CanaryLog(cert.fingerprint()),
            today=TODAY,
            identity=cert.identity(),
            observed=(-0.20, 0.02),
        )
        assert moved.raised
        assert any("accuracy difference moved" in reason for reason in moved.reasons)


class TestTheLog:
    def test_alpha_runs_out(self) -> None:
        """A canary cannot look again by re-running: the looks a certificate planned are the
        looks it has, and after that it has to be re-earned."""
        cert = a_certificate(looks_planned=2)
        log = CanaryLog(cert.fingerprint())
        take_look(cert, log, today=TODAY, identity=cert.identity())
        take_look(cert, log, today=TODAY, identity=cert.identity())
        with pytest.raises(CertificateError, match="no alpha left"):
            take_look(cert, log, today=TODAY, identity=cert.identity())

    def test_each_look_spends_its_own_level(self) -> None:
        cert = a_certificate()
        log = CanaryLog(cert.fingerprint())
        levels = [
            take_look(cert, log, today=TODAY, identity=cert.identity()).alpha_this_look
            for _ in range(cert.looks_planned)
        ]
        assert levels == cert.levels()
        assert log.taken == cert.looks_planned

    def test_a_log_from_another_certificate_is_refused(self, tmp_path) -> None:
        cert = a_certificate()
        path = tmp_path / "log.json"
        CanaryLog("somebody-elses").write(path)
        with pytest.raises(CertificateError, match="belongs to certificate"):
            CanaryLog.read(path, cert.fingerprint())

    def test_a_missing_log_starts_empty(self, tmp_path) -> None:
        cert = a_certificate()
        assert CanaryLog.read(tmp_path / "absent.json", cert.fingerprint()).taken == 0


class TestTheSubset:
    def test_every_stratum_is_represented(self) -> None:
        task_ids = [f"t{i:03d}" for i in range(200)]
        strata = ["lookup"] * 120 + ["two_hop"] * 50 + ["computation"] * 25 + ["exception"] * 5
        chosen = set(stratified_subset(task_ids, strata, 0.25, seed=1))
        by_stratum = {s: 0 for s in set(strata)}
        for task_id, stratum in zip(task_ids, strata, strict=True):
            if task_id in chosen:
                by_stratum[stratum] += 1
        assert all(count > 0 for count in by_stratum.values()), by_stratum
        # The rarest stratum is exactly where a cascade's risk is, so it is never rounded away.
        assert by_stratum["exception"] >= 1

    def test_the_draw_is_seeded(self) -> None:
        task_ids = [f"t{i}" for i in range(40)]
        strata = ["a"] * 20 + ["b"] * 20
        assert stratified_subset(task_ids, strata, 0.5, 7) == stratified_subset(
            task_ids, strata, 0.5, 7
        )
        assert stratified_subset(task_ids, strata, 0.5, 7) != stratified_subset(
            task_ids, strata, 0.5, 8
        )

    def test_nonsense_is_refused(self) -> None:
        with pytest.raises(CertificateError, match="every task needs a stratum"):
            stratified_subset(["a", "b"], ["x"], 0.5, 1)
        with pytest.raises(CertificateError, match="fraction must be"):
            stratified_subset(["a"], ["x"], 0.0, 1)


class TestPairedDelta:
    def test_it_matches_the_direct_computation(self) -> None:
        baseline = [1, 1, 0, 1, 0, 1, 1, 1]
        candidate = [1, 0, 1, 1, 0, 1, 0, 1]
        delta, standard_error = paired_delta(baseline, candidate)
        differences = np.array(candidate) - np.array(baseline)
        assert delta == pytest.approx(float(differences.mean()))
        assert standard_error == pytest.approx(
            float(differences.std(ddof=1) / np.sqrt(len(differences)))
        )

    def test_it_refuses_mismatched_arms(self) -> None:
        with pytest.raises(CertificateError, match="same non-empty task set"):
            paired_delta([1, 0], [1])


class TestTheDemoCertificate:
    def test_it_issues_and_round_trips(self, tmp_path) -> None:
        from tokop.optimize.report import cached_report

        cert = issue(cached_report(False), today=TODAY)
        path = cert.write(tmp_path / "cert.json")
        read_back = Certificate.read(path)
        assert read_back.as_dict() == cert.as_dict()
        assert read_back.fingerprint() == cert.fingerprint()

    def test_it_carries_what_it_rests_on(self, tmp_path) -> None:
        from tokop.optimize.report import cached_report

        cert = issue(cached_report(False), today=TODAY)
        assert cert.expires > cert.issued
        assert cert.looks_planned >= 1
        assert {s.role for s in cert.model_snapshots} == {"cheap", "mid", "frontier"}
        assert cert.price_snapshot_id
        assert cert.grading_mode in ("gold", "judged")
        assert cert.dataset_provenance["n"] > 0

    def test_the_demo_is_not_certifiable_and_the_certificate_says_so(self) -> None:
        """The demo's task set is entirely program-generated with no real traffic behind it, so
        it cannot certify production behaviour. The certificate is still issued — everything on
        it is true about the set — and it carries the refusal rather than omitting it."""
        from tokop.optimize.report import cached_report

        cert = issue(cached_report(False), today=TODAY)
        assert not cert.certifiable
        assert cert.dataset_provenance["refusals"]

    def test_a_certificate_from_a_different_schema_is_refused(self) -> None:
        with pytest.raises(CertificateError, match="schema"):
            Certificate.from_dict(json.loads('{"schema": "something.else"}'))
