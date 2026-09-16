"""What a conversation costs, and which way of running one wins (M17).

Every price in this build was a price per request until M17. These cover the two halves of
changing that: ``core/session.py``, which prices a sequence of turns with the cache expiring the
way a provider expires it, and the comparison that runs the same tasks under two policies and
says which is cheaper — with an interval, and with the accuracy side refused rather than
estimated.

``TestTheAcceptanceCheck`` is PLAN.md section 8's condition: a multi-turn workload where holding
the prefix immutable beats compacting, or the reverse, reported with an interval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tokop.adapters.base import Block
from tokop.core.pricing import ModelPrice, PriceProvenance
from tokop.core.session import SessionError, Turn, session_cost
from tokop.optimize.lint import LintContext, session_lint
from tokop.optimize.session_report import (
    COMPACT,
    IMMUTABLE,
    SessionReportError,
    build_session_report,
)

START = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)

PRICE = ModelPrice(
    model_id="test-model",
    provider="test",
    input=Decimal(5),
    output=Decimal(25),
    cache_write_5m=Decimal("6.25"),
    cache_write_1h=Decimal(10),
    cache_read=Decimal("0.5"),
    min_cacheable_tokens=10,
    provenance=PriceProvenance(source_url="https://example.invalid", retrieved=START.date()),
)


def turn(prefix: str, tail: str, seconds: int, *, ttl: str = "5m", label: str = "") -> Turn:
    return Turn(
        blocks=(Block(text=prefix, cache=ttl), Block(text=tail)),
        at=START + timedelta(seconds=seconds),
        output_tokens=10,
        label=label,
    )


PREFIX = "a stable instruction block repeated on every turn. " * 20


class TestThePrice:
    def test_the_first_turn_writes_and_the_rest_read(self) -> None:
        turns = [turn(PREFIX, f"question {i}", i * 30) for i in range(4)]
        cost = session_cost(turns, PRICE)
        assert [t.cache_event for t in cost.turns] == ["write", "read", "read", "read"]
        assert cost.misses == 1
        assert cost.reads > 0 and cost.writes_5m > 0
        assert cost.prefix_stability == 1.0

    def test_an_entry_older_than_its_lifetime_is_written_again(self) -> None:
        """The thing a per-request price cannot see: a pause long enough to lose the entry."""
        turns = [turn(PREFIX, "q1", 0), turn(PREFIX, "q2", 60), turn(PREFIX, "q3", 400)]
        cost = session_cost(turns, PRICE)
        assert [t.cache_event for t in cost.turns] == ["write", "read", "expired"]
        assert cost.expired_entries == 1
        assert cost.misses == 2

    def test_an_hour_long_entry_survives_the_same_pause(self) -> None:
        turns = [
            turn(PREFIX, "q1", 0, ttl="1h"),
            turn(PREFIX, "q2", 400, ttl="1h"),
        ]
        cost = session_cost(turns, PRICE, ttl="1h")
        assert [t.cache_event for t in cost.turns] == ["write", "read"]
        assert cost.writes_1h > 0 and cost.writes_5m == 0

    def test_a_refreshing_read_is_offered_and_is_not_the_default(self) -> None:
        """Appendix B does not say whether a read refreshes the entry, so the default costs more."""
        turns = [turn(PREFIX, f"q{i}", i * 200) for i in range(3)]
        conservative = session_cost(turns, PRICE)
        optimistic = session_cost(turns, PRICE, refresh_on_read=True)
        assert conservative.expired_entries == 1
        assert optimistic.expired_entries == 0
        assert conservative.cost_usd > optimistic.cost_usd
        assert conservative.refresh_on_read is False

    def test_a_prefix_below_the_minimum_is_not_cached_and_does_not_error(self) -> None:
        turns = [turn("tiny", f"q{i}", i * 30) for i in range(3)]
        cost = session_cost(turns, PRICE)
        assert {t.cache_event for t in cost.turns} == {"uncacheable"}
        assert cost.reads == 0 and cost.writes_5m == 0 and cost.uncached_input > 0

    def test_a_moving_prefix_never_reads(self) -> None:
        turns = [turn(PREFIX + f" turn {i}", f"q{i}", i * 30) for i in range(4)]
        cost = session_cost(turns, PRICE)
        assert cost.reads == 0
        assert cost.misses == 4
        assert cost.prefix_stability == 0.0

    def test_the_counter_and_the_scale_it_used_are_recorded(self) -> None:
        """Whether a prefix clears the minimum depends on both, so both are reported."""
        small = session_cost([turn(PREFIX, "q", 0)], PRICE, token_ratio=1.0)
        large = session_cost([turn(PREFIX, "q", 0)], PRICE, token_ratio=1.3)
        assert large.total_input > small.total_input
        assert large.token_ratio == 1.3 and large.counter

    def test_a_nonsense_ratio_is_refused(self) -> None:
        with pytest.raises(SessionError, match="token_ratio must be positive"):
            session_cost([turn(PREFIX, "q", 0)], PRICE, token_ratio=0)

    def test_an_empty_session_is_refused_rather_than_priced_at_zero(self) -> None:
        with pytest.raises(SessionError, match="caller bug"):
            session_cost([], PRICE)


class TestTheSessionLint:
    def context(self) -> LintContext:
        return LintContext(price=PRICE, min_cacheable_tokens=10, calls_per_1k=1000)

    def test_pl15_fires_when_the_prefix_moves_every_turn(self) -> None:
        turns = [turn(PREFIX + f" {i}", f"q{i}", i * 30) for i in range(4)]
        found = {f.id for f in session_lint(turns, self.context())}
        assert "PL15" in found

    def test_pl15_stays_quiet_when_the_prefix_holds(self) -> None:
        turns = [turn(PREFIX, f"q{i}", i * 30) for i in range(4)]
        assert "PL15" not in {f.id for f in session_lint(turns, self.context())}

    def test_pl16_finds_a_block_identical_on_every_turn_after_the_breakpoint(self) -> None:
        stable = "always answer with a single JSON object and nothing else. " * 12
        turns = [turn(PREFIX, stable + f"q{i}", i * 30) for i in range(4)]
        finding = next(f for f in session_lint(turns, self.context()) if f.id == "PL16")
        assert finding.projected_usd_per_1k > 0
        assert finding.transform == "reorder_static_first"

    def test_pl17_recognises_a_compaction_by_what_it_does(self) -> None:
        """A prefix that changed and a tail that got shorter: a history summarised upward."""
        history = "earlier turn. " * 60
        turns = [
            turn(PREFIX, history, 0),
            turn(PREFIX, history * 2, 30),
            turn(PREFIX + " summary of the conversation so far", "q3", 60),
        ]
        finding = next(f for f in session_lint(turns, self.context()) if f.id == "PL17")
        assert "1 compaction(s)" in finding.evidence
        assert finding.projected_usd_per_1k > 0

    def test_one_turn_is_not_a_session(self) -> None:
        assert session_lint([turn(PREFIX, "q", 0)], self.context()) == []


@pytest.fixture(scope="module")
def report():
    return build_session_report("data/incident-agent/workload.yaml")


class TestTheAcceptanceCheck:
    """PLAN.md section 8: one policy beats the other, reported with an interval."""

    def test_one_policy_beats_the_other_with_an_interval(self, report) -> None:
        assert report.winner in (IMMUTABLE, COMPACT), report.sentence()
        assert report.winner == IMMUTABLE
        interval = report.cost_ratio
        assert interval.low > 1 and interval.n == len(report.arms[IMMUTABLE].runs)
        assert "paired bootstrap over sessions" in interval.method

    def test_the_interval_resamples_sessions_and_not_turns(self, report) -> None:
        """Turns inside a session share a cache entry, so the session is the independent unit."""
        assert report.cost_ratio.n < report.arms[IMMUTABLE].turns

    def test_the_result_survives_closing_the_one_bias_that_favours_compacting(self, report) -> None:
        at_budget = report.cost_ratio_at_budget
        assert at_budget.low > 1
        # Closing it moves the ratio further against compacting, which is the direction the
        # short simulated summary was flattering.
        assert at_budget.point > report.cost_ratio.point

    def test_it_is_reported_at_more_than_one_pace(self, report) -> None:
        paces = [result.gap_seconds for result in report.sensitivity]
        assert len(paces) >= 3 and paces == sorted(paces)
        assert all(result.winner == IMMUTABLE for result in report.sensitivity)


class TestBothSidesOfCompaction:
    def test_what_compaction_saved_and_what_it_spent_are_both_reported(self, report) -> None:
        compact = report.arms[COMPACT].as_dict()
        immutable = report.arms[IMMUTABLE].as_dict()
        assert compact["dropped_history_tokens"] > 0
        assert compact["reestablished_prefix_tokens"] > 0
        assert Decimal(compact["compaction_usd"]) > 0
        # It did shorten the history it was there to shorten.
        assert compact["uncached_input_tokens"] < immutable["uncached_input_tokens"]
        # And it paid for it in writes.
        assert compact["cache_writes_tokens"] > immutable["cache_writes_tokens"]

    def test_rewriting_the_prefix_is_what_stops_the_entry_expiring(self, report) -> None:
        """Non-obvious and worth having in the trace: compaction refreshes what it destroys."""
        assert report.arms[IMMUTABLE].expired_entries > 0
        assert report.arms[COMPACT].expired_entries == 0

    def test_the_compacting_arm_is_the_one_the_prefix_lint_fires_on(self, report) -> None:
        assert "PL17" in {f.id for f in report.arms[COMPACT].findings}
        assert "PL17" not in {f.id for f in report.arms[IMMUTABLE].findings}


class TestWhatItRefuses:
    def test_the_quality_side_is_refused_and_says_why(self, report) -> None:
        payload = report.as_dict()
        assert payload["quality"]["measured"] is False
        assert "provider answers from the task and the model alone" in payload["quality"]["note"]

    def test_the_bill_says_it_was_counted_rather_than_reported(self, report) -> None:
        assert "not provider-reported" in report.as_dict()["basis"]
        assert report.counter in report.as_dict()["basis"]

    def test_a_workload_with_no_session_block_is_refused_by_name(self) -> None:
        with pytest.raises(SessionReportError, match="declares no `session:` block"):
            build_session_report("data/incident-triage/workload.yaml")

    def test_a_session_that_runs_a_graph_is_refused_at_load(self) -> None:
        import yaml

        from tokop.paths import repo_root
        from tokop.workloads.spec import WorkloadError, load_workload

        raw = yaml.safe_load((repo_root() / "data/incident-agent/workload.yaml").read_text())
        raw["session"]["pipeline"] = "G0"
        broken = repo_root() / "data/incident-agent/.broken.yaml"
        broken.write_text(yaml.safe_dump(raw))
        try:
            with pytest.raises(WorkloadError, match="which declares steps"):
                load_workload(broken)
        finally:
            broken.unlink()
