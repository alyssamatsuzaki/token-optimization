"""The spend guard (SPEC.md 7.3, 7.7)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from tokop.core.budget import BudgetExceeded, SpendGuard, projected_cost


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class TestPreflight:
    def test_a_call_within_both_caps_is_allowed(self) -> None:
        guard = SpendGuard(run_cap=Decimal("30"), daily_cap=Decimal("5"))
        guard.preflight(Decimal("0.05"))  # no exception

    def test_a_call_over_the_run_cap_is_refused_by_name(self) -> None:
        guard = SpendGuard(run_cap=Decimal("1"), daily_cap=Decimal("100"))
        guard.record(Decimal("0.95"))
        with pytest.raises(BudgetExceeded) as exc:
            guard.preflight(Decimal("0.10"))
        assert exc.value.cap_name == "run"
        assert "RUN_USD" in str(exc.value) or "run" in str(exc.value)

    def test_a_call_over_the_daily_cap_is_refused(self) -> None:
        guard = SpendGuard(run_cap=None, daily_cap=Decimal("5"))
        guard.record(Decimal("4.99"))
        with pytest.raises(BudgetExceeded) as exc:
            guard.preflight(Decimal("0.02"))
        assert exc.value.cap_name == "daily"

    def test_the_error_never_offers_to_raise_the_cap_itself(self) -> None:
        guard = SpendGuard(run_cap=Decimal("1"))
        with pytest.raises(BudgetExceeded) as exc:
            guard.preflight(Decimal("2"))
        assert "will not raise it for you" in str(exc.value)

    def test_exactly_at_the_cap_is_allowed_and_one_cent_over_is_not(self) -> None:
        guard = SpendGuard(run_cap=Decimal("1.00"))
        guard.preflight(Decimal("1.00"))
        with pytest.raises(BudgetExceeded):
            guard.preflight(Decimal("1.01"))

    def test_no_caps_means_nothing_is_refused(self) -> None:
        SpendGuard().preflight(Decimal("1000"))

    def test_prior_spend_from_the_ledger_counts_against_the_daily_cap(self) -> None:
        guard = SpendGuard(daily_cap=Decimal("5"), prior_daily_spend=Decimal("4.90"))
        with pytest.raises(BudgetExceeded):
            guard.preflight(Decimal("0.20"))

    def test_a_negative_projection_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            SpendGuard().preflight(Decimal("-1"))

    def test_would_exceed_reports_without_raising(self) -> None:
        guard = SpendGuard(run_cap=Decimal("1"))
        assert guard.would_exceed(Decimal("0.5")) is None
        assert guard.would_exceed(Decimal("2")) == "run"


class TestStatus:
    def test_warning_at_eighty_percent_and_stop_at_one_hundred(self) -> None:
        guard = SpendGuard(daily_cap=Decimal("10"))
        assert [s.state for s in guard.status() if s.name == "daily"] == ["ok"]
        guard.record(Decimal("8"))
        assert [s.state for s in guard.status() if s.name == "daily"] == ["warning"]
        guard.record(Decimal("2"))
        assert [s.state for s in guard.status() if s.name == "daily"] == ["stopped"]

    def test_uncapped_budgets_report_uncapped(self) -> None:
        assert all(s.state == "uncapped" for s in SpendGuard().status())

    def test_remaining_never_goes_negative(self) -> None:
        guard = SpendGuard(run_cap=Decimal("1"))
        guard.record(Decimal("3"))
        run = next(s for s in guard.status() if s.name == "run")
        assert run.remaining == Decimal(0)

    def test_warnings_name_the_budget_and_the_cap(self) -> None:
        guard = SpendGuard(daily_cap=Decimal("5"))
        guard.record(Decimal("4.5"))
        assert guard.warnings() == ["daily budget is at 90% of $5"]

    def test_status_serializes_for_the_spend_screen(self) -> None:
        guard = SpendGuard(daily_cap=Decimal("5"))
        guard.record(Decimal("1"))
        daily = next(s for s in guard.status() if s.name == "daily").as_dict()
        assert daily["cap_usd"] == "5"
        assert daily["spent_usd"] == "1"
        assert daily["remaining_usd"] == "4"
        assert daily["state"] == "ok"


class TestRecording:
    def test_actuals_accumulate_on_both_counters(self) -> None:
        guard = SpendGuard(run_cap=Decimal("10"), daily_cap=Decimal("10"))
        guard.record(Decimal("1.5"))
        guard.record(Decimal("0.5"))
        assert guard.run_spend == Decimal("2")
        assert guard.daily_spend == Decimal("2")

    def test_an_overshoot_past_the_projection_is_still_recorded(self) -> None:
        guard = SpendGuard(run_cap=Decimal("1"))
        guard.preflight(Decimal("0.10"))
        guard.record(Decimal("0.90"))
        guard.record(Decimal("0.50"))
        assert guard.run_spend == Decimal("1.40")
        with pytest.raises(BudgetExceeded):
            guard.preflight(Decimal("0.01"))

    def test_the_daily_counter_resets_on_a_new_day_and_the_run_counter_does_not(self) -> None:
        clock = Clock(datetime(2026, 9, 11, 23, 0))
        guard = SpendGuard(daily_cap=Decimal("5"), clock=clock)
        guard.record(Decimal("4"))
        assert guard.daily_spend == Decimal("4")
        clock.now = datetime(2026, 9, 12, 1, 0)
        assert guard.daily_spend == Decimal(0)
        assert guard.run_spend == Decimal("4")
        guard.preflight(Decimal("4.5"))

    def test_negative_actuals_are_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            SpendGuard().record(Decimal("-1"))

    def test_negative_caps_are_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="run cap"):
            SpendGuard(run_cap=Decimal("-1"))
        with pytest.raises(ValueError, match="daily cap"):
            SpendGuard(daily_cap=Decimal("-1"))


class TestProjectedCost:
    def test_hand_computed_upper_bound(self) -> None:
        """7,144 input at $5/Mtok + 2,000 output at $25/Mtok.

        7,144 x 5 / 1e6  = $0.03572
        2,000 x 25 / 1e6 = $0.05
        total            = $0.08572
        """
        assert projected_cost(7144, 2000, Decimal("5"), Decimal("25")) == Decimal("0.08572")

    def test_a_shorter_completion_can_only_cost_less(self) -> None:
        upper = projected_cost(1000, 2000, Decimal("5"), Decimal("25"))
        actual = projected_cost(1000, 200, Decimal("5"), Decimal("25"))
        assert actual < upper

    def test_negative_tokens_are_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            projected_cost(-1, 10, Decimal("5"), Decimal("25"))
