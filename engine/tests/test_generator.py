"""The demo generator and its split (SPEC.md section 6)."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest

from tokop.workloads.demo.generator import (
    TYPE_MIX,
    all_templates,
    generate,
    split_items,
)
from tokop.workloads.demo.handbook import (
    SECTION_IDS,
    handbook_size_report,
    render_handbook,
    section_text,
)
from tokop.workloads.demo.policy import PolicyError, load_policy


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.fixture(scope="module")
def items(policy):
    return generate(policy)


class TestTemplates:
    def test_every_template_has_at_least_four_phrasings(self) -> None:
        for template in all_templates():
            assert len(template.phrasings) >= 4, template.id

    def test_every_question_type_has_templates(self) -> None:
        types = {t.question_type for t in all_templates()}
        assert types == set(TYPE_MIX)

    def test_every_template_names_supporting_sections(self) -> None:
        for template in all_templates():
            assert template.sections, template.id
            for section in template.sections:
                assert section in SECTION_IDS, f"{template.id} cites unknown {section}"


class TestGeneratedSet:
    def test_size_and_mix_match_the_spec(self, items) -> None:
        assert len(items) == 300
        counts = Counter(i.question_type for i in items)
        # 45/25/20/10 of 300.
        assert counts["lookup"] == 135
        assert counts["two_hop"] == 75
        assert counts["computation"] == 60
        assert counts["exception"] == 30

    def test_every_question_is_distinct(self, items) -> None:
        """Two identical questions are not two tasks; counting them twice would inflate n."""
        assert len({i.question for i in items}) == len(items)

    def test_every_id_is_distinct(self, items) -> None:
        assert len({i.id for i in items}) == len(items)

    def test_generation_is_deterministic(self, policy, items) -> None:
        again = generate(policy)
        assert [i.id for i in again] == [i.id for i in items]
        assert [i.gold for i in again] == [i.gold for i in items]

    def test_a_different_seed_gives_a_different_set(self, policy, items) -> None:
        other = generate(policy, seed=12345)
        assert [i.id for i in other] != [i.id for i in items]

    def test_articles_agree_with_the_following_word(self, items) -> None:
        bad = [i.question for i in items if " a Alpine" in i.question or " a A" in i.question]
        assert bad == []

    def test_no_question_leaks_its_own_gold_answer(self, items) -> None:
        """A question that contains its answer would make every tier look identical."""
        leaks = []
        for item in items:
            if item.answer_type in ("number", "money") and item.gold not in ("0", "0.00"):
                # The gold value must not already appear as a standalone number in the question.
                import re

                if re.search(rf"\b{re.escape(item.gold)}\b", item.question):
                    leaks.append((item.id, item.question, item.gold))
        assert leaks == [], leaks[:3]

    def test_the_router_view_hides_type_gold_and_sections(self, items) -> None:
        view = items[0].router_view()
        assert set(view) == {"id", "question"}
        assert "gold" not in view
        assert "question_type" not in view
        assert "sections" not in view


class TestGoldAnswers:
    """Spot checks worked out by hand from data/demo/policy.yaml."""

    def test_backpack_window_for_a_summit_member(self, policy) -> None:
        # Backpacks base 60 days + Summit bonus 14 = 74.
        assert policy.return_window_days("backpacks", "summit") == 74

    def test_holiday_extension_stacks_with_the_tier_bonus(self, policy) -> None:
        # 60 + 14 + 30 = 104.
        assert policy.return_window_days("backpacks", "summit", holiday_purchase=True) == 104

    def test_final_sale_gets_no_bonus_and_no_extension(self, policy) -> None:
        assert policy.return_window_days("nutrition", "alpine") == 0
        assert policy.return_window_days("nutrition", "alpine", holiday_purchase=True) == 0

    def test_excluded_category_gets_no_seasonal_extension(self, policy) -> None:
        # Climbing rope is excluded: 14 + 30 (alpine) = 44, with no holiday bonus.
        assert policy.return_window_days("climbing_rope", "alpine") == 44
        assert policy.return_window_days("climbing_rope", "alpine", holiday_purchase=True) == 44

    def test_alpine_waives_every_restocking_fee(self, policy) -> None:
        assert policy.restocking_fee_pct("climbing_rope", "alpine") == 0
        assert policy.restocking_fee_pct("climbing_rope", "standard") == 20

    def test_opened_seal_raises_the_fee_but_not_for_alpine(self, policy) -> None:
        assert policy.restocking_fee_pct("electronics", "standard", opened_seal=True) == 25
        assert policy.restocking_fee_pct("electronics", "alpine", opened_seal=True) == 0

    def test_refund_arithmetic(self, policy) -> None:
        # $248.75 tent, Standard tier, 10% restocking = $24.88 fee, $223.87 refund.
        fee = policy.restocking_fee_usd("tents", "standard", Decimal("248.75"))
        assert fee == Decimal("24.88")
        assert policy.refund_usd("tents", "standard", Decimal("248.75")) == Decimal("223.87")

    def test_weight_bands_are_inclusive_of_their_upper_bound(self, policy) -> None:
        assert policy.band_for(2.0).id == "band_a"
        assert policy.band_for(2.01).id == "band_b"
        assert policy.band_for(5.0).id == "band_b"
        assert policy.band_for(500.0).id == "band_f"

    def test_oversize_surcharge_applies_once_and_only_to_qualifying_items(self, policy) -> None:
        # Zone 3, 33 lb: band_e is $44.95. Tents qualify and 33 > 25, so +$24.00 = $68.95.
        assert policy.shipping_fee_usd("zone_3", 33.0, "tents") == Decimal("68.95")
        # Footwear does not qualify.
        assert policy.shipping_fee_usd("zone_3", 33.0, "footwear") == Decimal("44.95")
        # Under the threshold, no surcharge even for tents.
        assert policy.shipping_fee_usd("zone_3", 18.0, "tents") == Decimal("29.95")

    def test_free_shipping_beats_the_zone_fee(self, policy) -> None:
        # Alpine threshold is $0, so shipping is always free.
        assert policy.order_shipping_usd("alpine", "zone_5", 60.0, Decimal("1")) == Decimal("0.00")
        # Standard threshold is $75; a $74 order pays.
        assert policy.order_shipping_usd("standard", "zone_1", 1.0, Decimal("74")) == Decimal(
            "5.95"
        )
        assert policy.order_shipping_usd("standard", "zone_1", 1.0, Decimal("75")) == Decimal(
            "0.00"
        )

    def test_escalation_routing(self, policy) -> None:
        assert policy.escalation_level_for(refund_usd=Decimal("499")) == "tier_1"
        assert policy.escalation_level_for(refund_usd=Decimal("501")) == "tier_3"
        # A safety complaint escalates whatever the amount.
        assert policy.escalation_level_for(refund_usd=Decimal("10"), safety=True) == "tier_3"

    def test_used_safety_gear_cannot_be_returned(self, policy) -> None:
        assert not policy.can_return("climbing_rope", used_in_field=True)
        assert not policy.can_return("avalanche_safety", used_in_field=True)
        assert policy.can_return("tents", used_in_field=True)

    def test_unknown_keys_raise(self, policy) -> None:
        with pytest.raises(PolicyError, match="no category"):
            policy.category("hovercraft")
        with pytest.raises(PolicyError, match="no loyalty tier"):
            policy.tier("platinum")


class TestSplit:
    def test_sizes_and_disjointness(self, items) -> None:
        split = split_items(items)
        assert len(split.calibration) == 100
        assert len(split.test) == 200
        assert not set(i.id for i in split.calibration) & set(i.id for i in split.test)

    def test_split_is_stratified_by_question_type(self, items) -> None:
        split = split_items(items)
        cal = Counter(i.question_type for i in split.calibration)
        test = Counter(i.question_type for i in split.test)
        for question_type in TYPE_MIX:
            # A third of each stratum lands in calibration, within one task of exact.
            expected = round((cal[question_type] + test[question_type]) * 100 / len(items))
            assert abs(cal[question_type] - expected) <= 1, question_type

    def test_split_is_deterministic_and_hashed(self, items) -> None:
        first, second = split_items(items), split_items(items)
        assert first.hashes() == second.hashes()
        assert len(first.hashes()["calibration"]) == 16


class TestHandbook:
    def test_rendering_is_deterministic(self, policy) -> None:
        assert render_handbook(policy) == render_handbook(policy)

    def test_every_section_id_is_present_and_extractable(self, policy) -> None:
        handbook = render_handbook(policy)
        for section_id in SECTION_IDS:
            body = section_text(handbook, section_id)
            assert body, section_id
            assert len(body) > 50, section_id

    def test_an_unknown_section_returns_empty_rather_than_raising(self, policy) -> None:
        assert section_text(render_handbook(policy), "sec-does-not-exist") == ""

    def test_prefix_clears_every_minimum_by_twenty_percent(self, policy) -> None:
        """Haiku 4.5 binds: the highest minimum and the tokenizer that counts lowest."""
        report = handbook_size_report(
            render_handbook(policy),
            {"claude-opus-5": 512, "claude-sonnet-5": 1024, "claude-haiku-4-5-20251001": 4096},
            {"claude-opus-5": 1.3, "claude-sonnet-5": 1.3, "claude-haiku-4-5-20251001": 1.0},
        )
        assert report.clears_every_minimum_by_20_percent, report.failures()
        # SPEC.md section 6 predicts roughly 6,500 to 7,500 on Opus 5's count.
        assert 6000 <= report.tokens_by_model["claude-opus-5"] <= 8000

    def test_the_handbook_contains_the_policy_numbers(self, policy) -> None:
        handbook = render_handbook(policy)
        assert "$44.95" in handbook  # zone 3, band e
        assert "60 days" in handbook  # backpacks
        assert "sec-escalation" in handbook
