"""Reporting the tie rather than the winner (UPGRADE_V3.md U7).

When two configurations' cost intervals overlap, the ordering between them is the bootstrap's
noise. A report that presented it as a ranking would be manufacturing a single-vendor
recommendation out of sampling error — which is the product version of the mode collapse Sinha
et al. show is structural to expected-return maximization, not a symptom of weak exploration.
"""

from __future__ import annotations

import pytest

from tokop.optimize.ties import (
    Candidate,
    TieError,
    designate_fallback,
    exploration_weights,
    find_ties,
    single_winner,
)


def candidate(
    label: str,
    point: float,
    half_width: float = 0.0005,
    *,
    accuracy: float = 0.96,
    scarce: float = 0.0,
    adoptable: bool = True,
) -> Candidate:
    return Candidate(
        label=label,
        cost_point=point,
        cost_low=point - half_width,
        cost_high=point + half_width,
        accuracy_point=accuracy,
        scarce_share=scarce,
        adoptable=adoptable,
    )


class TestTheAcceptanceCheck:
    def test_overlapping_intervals_are_reported_as_a_tie(self) -> None:
        report = find_ties([candidate("a", 0.0040), candidate("b", 0.0042)])
        assert report.is_tie
        assert set(report.labels()) == {"a", "b"}
        assert "not distinguishable" in report.as_dict()["note"]

    def test_separated_intervals_are_not_a_tie(self) -> None:
        report = find_ties([candidate("a", 0.0040), candidate("b", 0.0090)])
        assert not report.is_tie
        assert report.labels() == ["a"]
        assert "no tie to report" in report.as_dict()["note"]

    def test_greedy_selection_is_refused_on_a_tie(self) -> None:
        """The other half of the acceptance check. A caller that wants "the winner" gets one
        only when the data names one."""
        tied = find_ties([candidate("a", 0.0040), candidate("b", 0.0042)])
        with pytest.raises(TieError, match="no single cheapest configuration"):
            single_winner(tied)
        # And the refusal names what it could not separate, so the caller can decide on a
        # criterion this data does not supply.
        try:
            single_winner(tied)
        except TieError as exc:
            assert "a at $" in str(exc) and "b at $" in str(exc)

    def test_a_clear_winner_is_returned(self) -> None:
        clear = find_ties([candidate("a", 0.0040), candidate("b", 0.0090)])
        assert single_winner(clear).label == "a"

    def test_the_tie_is_ordered_by_dollars(self) -> None:
        report = find_ties([candidate("c", 0.0043), candidate("a", 0.0040), candidate("b", 0.0041)])
        assert report.labels() == ["a", "b", "c"]


class TestTheFallback:
    def test_it_prefers_the_tied_option_that_leans_least_on_scarce_capacity(self) -> None:
        """A fallback that shares the constraint it is meant to survive is not a fallback."""
        running = candidate("op", 0.0040, scarce=0.4)
        report = find_ties(
            [
                running,
                candidate("also-scarce", 0.0041, scarce=0.5),
                candidate("cheap-tier", 0.0042),
            ],
            "op",
        )
        assert report.fallback is not None
        assert report.fallback.label == "cheap-tier"
        assert "does not take both" in report.fallback_reason

    def test_it_says_so_when_the_fallback_shares_the_constraint(self) -> None:
        running = candidate("op", 0.0040, scarce=0.1)
        report = find_ties([running, candidate("other", 0.0041, scarce=0.9)], "op")
        assert report.fallback is not None
        assert "survives a price change, not an outage" in report.fallback_reason

    def test_a_configuration_the_search_may_not_adopt_is_never_the_fallback(self) -> None:
        """A fallback nobody is allowed to run is a footnote, not a plan."""
        running = candidate("op", 0.0042)
        report = find_ties([candidate("cheaper", 0.0040, adoptable=False), running], "op")
        assert report.cheapest.label == "cheaper"
        assert report.fallback is None
        assert "nothing to fall back" in report.fallback_reason

    def test_the_operating_point_is_never_its_own_fallback(self) -> None:
        running = candidate("op", 0.0042)
        report = find_ties([candidate("cheaper", 0.0040, adoptable=False), running], "op")
        assert report.fallback is None or report.fallback.label != "op"

    def test_a_tie_of_one_has_no_fallback(self) -> None:
        report = find_ties([candidate("a", 0.0040)])
        assert report.fallback is None
        fallback, reason = designate_fallback(candidate("a", 0.0040), [candidate("a", 0.0040)])
        assert fallback is None
        assert "nothing to fall back" in reason


class TestExploration:
    def test_traffic_is_split_evenly_over_the_tie(self) -> None:
        """The correction itself. A greedy explorer puts all of its mass on whichever option
        won the last sample, which amplifies noise into a decision; spreading it evenly over the
        options the data cannot separate keeps every one of them measurable."""
        weights = exploration_weights([candidate("a", 0.004), candidate("b", 0.004)])
        assert weights == [0.5, 0.5]

    def test_it_is_not_spread_over_options_the_data_has_separated(self) -> None:
        report = find_ties([candidate("a", 0.0040), candidate("b", 0.0090)])
        assert report.as_dict()["exploration_weights"] == {"a": 1.0}

    def test_nothing_to_explore_is_refused(self) -> None:
        with pytest.raises(TieError, match="nothing to explore"):
            exploration_weights([])
        with pytest.raises(TieError, match="nothing to compare"):
            find_ties([])


class TestTheDemoReport:
    def test_the_payload_carries_the_tie(self) -> None:
        from tokop.optimize.report import cached_report

        ties = cached_report(False)["cascade"]["ties"]
        assert ties is not None
        assert len(ties["tied"]) >= 1
        assert all("cost_low" in row and "cost_high" in row for row in ties["tied"])

    def test_the_operating_point_is_in_the_tie_and_is_not_claimed_to_be_best(self) -> None:
        """On the demo the cheapest tied configuration is one D27 refuses to adopt, so the
        report has to say both things at once: these are indistinguishable, and the one running
        is not the cheapest of them."""
        from tokop.optimize.report import cached_report

        ties = cached_report(False)["cascade"]["ties"]
        running = [row for row in ties["tied"] if row["is_operating_point"]]
        assert len(running) == 1
        if ties["is_tie"] and not ties["tied"][0]["adoptable"]:
            assert "does not let the search adopt" in ties["note"]
            assert ties["fallback"] is None
