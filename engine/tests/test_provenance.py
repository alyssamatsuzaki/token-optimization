"""Where the task set came from, and when that stops it certifying (UPGRADE_V3.md U8).

The danger is narrow and worth stating plainly: **a cascade earns its savings on easy tasks and
its risk lives in the tail.** An eval set whose tail has thinned will certify a router that
fails in production, and every number on the certificate will look fine while it does. So the
refusal is on the *set*, before any of those numbers are computed.
"""

from __future__ import annotations

import pytest

from tokop.workloads.provenance import (
    MIN_TAIL_COVERAGE,
    DeclaredProvenance,
    ProvenanceError,
    build_provenance,
    detector,
    importance_weights,
    tail_shape,
)

SPACE = [f"tpl-{index:02d}" for index in range(20)]


def a_set(counts: dict[str, int]) -> list[str]:
    return [template for template, count in counts.items() for _ in range(count)]


class TestTheCollapsedFixture:
    """U8's acceptance check: a deliberately collapsed set is refused, with the reason named."""

    def test_a_thinned_tail_is_refused_and_says_which_templates_are_gone(self) -> None:
        # Everything piled onto four of twenty templates: the common cases survived and the
        # rare ones did not, which is exactly the shape a self-consuming loop produces.
        collapsed = a_set({template: 75 for template in SPACE[:4]})
        provenance = build_provenance(
            collapsed,
            SPACE,
            DeclaredProvenance(
                model_generated_items=len(collapsed),
                generators=("gen-a",),
                real_traffic_accumulating=True,
            ),
        )
        assert not provenance.assessment.certifiable
        reason = next(r for r in provenance.assessment.refusals if "templates" in r)
        assert "tail has thinned" in reason
        assert "16 templates are missing" in reason
        assert provenance.shape.tail_coverage == pytest.approx(0.2)
        assert len(provenance.shape.missing_templates) == 16

    def test_the_same_set_spread_across_the_space_is_not_refused_for_its_tail(self) -> None:
        """The control. Same size, same origins, same everything but the coverage."""
        spread = a_set({template: 15 for template in SPACE})
        provenance = build_provenance(
            spread,
            SPACE,
            DeclaredProvenance(
                real_traffic_items=len(spread),
                generators=(),
                real_traffic_accumulating=True,
            ),
        )
        assert provenance.assessment.certifiable, provenance.assessment.refusals

    def test_a_model_written_set_with_no_fresh_real_data_is_refused(self) -> None:
        """The self-consuming loop, named. Shumailov et al.'s regime is indiscriminate use of
        model output with nothing fresh coming in, and that is what this rule is looking for."""
        items = a_set({template: 15 for template in SPACE})
        provenance = build_provenance(
            items,
            SPACE,
            DeclaredProvenance(
                real_traffic_items=1,
                model_generated_items=len(items) - 1,
                generators=("gen-a", "gen-b"),
                real_traffic_accumulating=False,
            ),
        )
        assert not provenance.assessment.certifiable
        assert any("self-consuming loop" in r for r in provenance.assessment.refusals)

    def test_the_same_set_with_real_traffic_accumulating_is_allowed(self) -> None:
        items = a_set({template: 15 for template in SPACE})
        provenance = build_provenance(
            items,
            SPACE,
            DeclaredProvenance(
                real_traffic_items=1,
                model_generated_items=len(items) - 1,
                generators=("gen-a", "gen-b"),
                decoding_budget="temperature 1.0, top_p 0.95, 8 samples",
                relabelled_by_frozen_reference=True,
                real_traffic_accumulating=True,
            ),
        )
        assert provenance.assessment.certifiable, provenance.assessment.refusals

    def test_a_block_that_does_not_add_up_is_refused(self) -> None:
        items = a_set({template: 15 for template in SPACE})
        provenance = build_provenance(items, SPACE, DeclaredProvenance(real_traffic_items=7))
        assert not provenance.assessment.certifiable
        assert any("does not add up" in r for r in provenance.assessment.refusals)


class TestTheWarnings:
    def test_one_generator_and_no_relabelling_are_warned_about_not_refused(self) -> None:
        """Hu et al. find generator diversity and relabelling with a frozen model both mitigate
        collapse. Neither is a hard requirement, and saying so is the difference between advice
        and a rule nobody can satisfy."""
        items = a_set({template: 15 for template in SPACE})
        provenance = build_provenance(
            items,
            SPACE,
            DeclaredProvenance(
                real_traffic_items=1,
                model_generated_items=len(items) - 1,
                generators=("gen-a",),
                real_traffic_accumulating=True,
            ),
        )
        assert provenance.assessment.certifiable
        joined = " ".join(provenance.assessment.warnings)
        assert "Generator diversity" in joined
        assert "frozen reference model" in joined
        assert "decoding budget" in joined


class TestTheDemoSet:
    def test_it_is_fully_covered_and_still_refused(self) -> None:
        """The demo's own dataset covers every template it can produce and is still refused,
        because nobody has observed the traffic it stands for. That is the right answer and the
        report says it rather than working around it."""
        from tokop.optimize.report import cached_report

        block = cached_report(False)["dataset_provenance"]
        assert block["declared"]
        assert block["shape"]["tail_coverage"] == pytest.approx(1.0)
        assert block["synthetic_share"] == pytest.approx(1.0)
        assert block["model_generated_share"] == 0.0
        assert not block["certifiable"]
        assert any("no real recorded traffic" in r for r in block["refusals"])

    def test_program_generated_is_counted_apart_from_model_generated(self) -> None:
        """A program templating questions from a policy file is synthetic; it is not a model
        sampling from itself, and the collapse literature is about the second. Conflating them
        would refuse every seeded benchmark for the wrong reason."""
        from tokop.optimize.report import cached_report

        block = cached_report(False)["dataset_provenance"]
        assert block["program_generated_items"] == block["n"]
        assert block["model_generated_items"] == 0
        assert not any("self-consuming" in r for r in block["refusals"])


class TestTheShape:
    def test_coverage_counts_the_space_not_the_set(self) -> None:
        shape = tail_shape(a_set({"tpl-00": 10, "tpl-01": 10}), SPACE)
        assert shape.templates_present == 2
        assert shape.template_space == 20
        assert shape.tail_coverage == pytest.approx(0.1)
        assert shape.tail_coverage < MIN_TAIL_COVERAGE

    def test_concentration_is_zero_when_even_and_high_when_piled_up(self) -> None:
        even = tail_shape(a_set({t: 10 for t in SPACE}), SPACE)
        piled = tail_shape(a_set({SPACE[0]: 191, **{t: 1 for t in SPACE[1:]}}), SPACE)
        assert even.concentration == pytest.approx(0.0, abs=1e-9)
        assert piled.concentration > 0.8

    def test_the_tail_share_counts_items_in_rarely_seen_templates(self) -> None:
        shape = tail_shape(a_set({SPACE[0]: 96, SPACE[1]: 2, SPACE[2]: 2}), SPACE)
        assert shape.tail_share == pytest.approx(4 / 100)

    def test_an_empty_space_is_refused_rather_than_scored(self) -> None:
        """A set with nothing to be missing from cannot be checked for a thinned tail, which is
        the whole difficulty with detecting collapse after the fact."""
        with pytest.raises(ProvenanceError, match="nothing to be missing"):
            tail_shape(["a"], [])


class TestResamplingTowardsHumanText:
    def test_no_detector_is_registered_and_asking_says_why(self) -> None:
        """A detector needs a trained model and this build has none. One that guessed would
        resample the set towards its own guess, which is worse than not resampling."""
        with pytest.raises(ProvenanceError, match="needs a trained model"):
            detector("anything")

    def test_weights_pull_towards_likely_human_items(self) -> None:
        weights = importance_weights([0.05, 0.5, 0.95])
        assert weights[0] > weights[1] > weights[2]
        assert sum(weights) == pytest.approx(1.0)

    def test_nothing_is_ever_unsamplable(self) -> None:
        """The same rule the annotation policy follows: an item that can never be drawn is an
        item the resampled set cannot represent, and a detector is not reliable enough to
        exclude anything outright."""
        weights = importance_weights([1.0, 1.0, 0.0])
        assert min(weights) > 0

    def test_a_detector_that_malfunctions_is_refused_rather_than_clamped(self) -> None:
        with pytest.raises(ProvenanceError, match="not a probability"):
            importance_weights([0.5, 1.4])
        with pytest.raises(ProvenanceError, match="not a probability"):
            importance_weights([float("nan")])

    def test_an_empty_set_is_refused(self) -> None:
        with pytest.raises(ProvenanceError, match="nothing to weight"):
            importance_weights([])
