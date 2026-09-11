"""Cost arithmetic, price provenance, and snapshot pinning (SPEC.md 7.3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tokop.core.pricing import (
    ModelPrice,
    PriceProvenance,
    PriceSnapshot,
    PricingError,
    compute_cost,
    cost_disagreement,
    price_from_mapping,
)
from tokop.core.usage import TokenUsage, normalize_anthropic

PROV = PriceProvenance(
    source_url="https://platform.claude.com/docs/en/about-claude/pricing",
    retrieved=date(2026, 9, 11),
    verified=True,
)

# Appendix B: Opus 5 at $5 / $25 per Mtok, writes 1.25x and 2x base input, reads 0.1x.
OPUS = ModelPrice(
    model_id="claude-opus-5",
    provider="anthropic",
    input=Decimal("5"),
    output=Decimal("25"),
    cache_write_5m=Decimal("6.25"),
    cache_write_1h=Decimal("10"),
    cache_read=Decimal("0.50"),
    batch_discount=Decimal("0.5"),
    min_cacheable_tokens=512,
    provenance=PROV,
)


class TestComputeCost:
    def test_hand_computed_cache_write_call(self) -> None:
        """42 uncached + 7,102 written (5m) + 118 output on Opus 5.

        42 x 5 / 1e6        = $0.00021
        7,102 x 6.25 / 1e6  = $0.0443875
        118 x 25 / 1e6      = $0.00295
        total               = $0.0475475
        """
        usage = normalize_anthropic(
            {
                "input_tokens": 42,
                "cache_creation_input_tokens": 7102,
                "cache_read_input_tokens": 0,
                "output_tokens": 118,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 7102,
                    "ephemeral_1h_input_tokens": 0,
                },
            }
        )
        cost = compute_cost(usage, OPUS, "snap1")
        assert cost.total == Decimal("0.0475475")
        assert cost.by_bucket["cache_write_5m"] == Decimal("0.0443875")
        assert cost.price_snapshot_id == "snap1"

    def test_hand_computed_cache_read_call_is_much_cheaper(self) -> None:
        """The same prefix read instead of written: 7,102 x 0.50 / 1e6 = $0.003551."""
        usage = normalize_anthropic(
            {
                "input_tokens": 42,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 7102,
                "output_tokens": 118,
            }
        )
        cost = compute_cost(usage, OPUS, "snap1")
        assert cost.by_bucket["cache_read"] == Decimal("0.003551")
        assert cost.total == Decimal("0.00021") + Decimal("0.003551") + Decimal("0.00295")

    def test_reasoning_tokens_bill_at_the_output_rate(self) -> None:
        usage = TokenUsage(output_visible=100, output_reasoning=400, sources={})
        cost = compute_cost(usage, OPUS)
        assert cost.total == Decimal("500") * Decimal("25") / Decimal(1_000_000)

    def test_batch_discount_halves_the_bill(self) -> None:
        usage = TokenUsage(input_uncached=1_000_000, output_visible=0)
        assert compute_cost(usage, OPUS, batch=True).total == Decimal("2.50")
        assert compute_cost(usage, OPUS, batch=False).total == Decimal("5")

    def test_zero_usage_costs_nothing_and_says_so(self) -> None:
        cost = compute_cost(TokenUsage(), OPUS)
        assert cost.total == Decimal(0)
        assert cost.formula() == "no billable tokens"

    def test_formula_names_every_charged_bucket(self) -> None:
        usage = TokenUsage(input_uncached=10, cache_read=20, output_visible=5)
        assert "input_uncached" in compute_cost(usage, OPUS).formula()
        assert "cache_read" in compute_cost(usage, OPUS).formula()

    def test_reserved_media_buckets_refuse_to_be_priced(self) -> None:
        with pytest.raises(PricingError, match="does not price image"):
            compute_cost(TokenUsage(image_in=10), OPUS)


class TestSnapshot:
    def test_snapshot_id_is_stable_for_the_same_prices(self) -> None:
        a = PriceSnapshot.of([OPUS], date(2026, 9, 11))
        b = PriceSnapshot.of([OPUS], date(2026, 9, 11))
        assert a.snapshot_id == b.snapshot_id

    def test_snapshot_id_changes_when_a_price_changes(self) -> None:
        cheaper = OPUS.model_copy(update={"input": Decimal("4")})
        a = PriceSnapshot.of([OPUS], date(2026, 9, 11))
        b = PriceSnapshot.of([cheaper], date(2026, 9, 11))
        assert a.snapshot_id != b.snapshot_id

    def test_costing_through_a_snapshot_pins_the_id(self) -> None:
        snap = PriceSnapshot.of([OPUS], date(2026, 9, 11))
        cost = snap.cost("claude-opus-5", TokenUsage(input_uncached=1_000_000))
        assert cost.total == Decimal("5")
        assert cost.price_snapshot_id == snap.snapshot_id

    def test_a_model_missing_from_the_snapshot_raises(self) -> None:
        snap = PriceSnapshot.of([OPUS], date(2026, 9, 11))
        with pytest.raises(PricingError, match="no price for"):
            snap.price_for("claude-sonnet-5")

    def test_unverified_prices_are_reported(self) -> None:
        unverified = OPUS.model_copy(
            update={"provenance": PROV.model_copy(update={"verified": False})}
        )
        snap = PriceSnapshot.of([unverified], date(2026, 9, 11))
        assert not snap.all_verified
        assert snap.unverified_models() == ["claude-opus-5"]


class TestPriceFromMapping:
    def test_explicit_cache_rates_are_used_as_given(self) -> None:
        price = price_from_mapping(
            "claude-opus-5",
            "anthropic",
            {
                "input": "5",
                "output": "25",
                "cache_write_5m": "6.25",
                "cache_write_1h": "10",
                "cache_read": "0.50",
                "provenance": {
                    "source_url": "https://example.invalid/pricing",
                    "retrieved": "2026-09-11",
                    "verified": True,
                },
            },
        )
        assert price.cache_read == Decimal("0.50")
        assert price.provenance.note is None

    def test_omitted_cache_rates_are_derived_and_the_note_says_so(self) -> None:
        price = price_from_mapping(
            "claude-sonnet-5",
            "anthropic",
            {
                "input": "2",
                "output": "10",
                "provenance": {
                    "source_url": "https://example.invalid/pricing",
                    "retrieved": "2026-09-11",
                },
            },
        )
        assert price.cache_write_5m == Decimal("2.50")
        assert price.cache_write_1h == Decimal("4")
        assert price.cache_read == Decimal("0.2")
        assert price.provenance.note is not None
        assert "derived" in price.provenance.note
        assert not price.provenance.verified

    def test_a_price_without_provenance_is_refused(self) -> None:
        with pytest.raises(PricingError, match="no provenance"):
            price_from_mapping("x", "y", {"input": "1", "output": "2"})


class TestCostDisagreement:
    def test_agreement_within_two_percent_is_not_flagged(self) -> None:
        assert cost_disagreement(Decimal("0.01000"), Decimal("0.01001")) is None

    def test_disagreement_above_two_percent_is_returned_signed(self) -> None:
        gap = cost_disagreement(Decimal("0.011"), Decimal("0.010"))
        assert gap is not None and gap > 0
        gap_low = cost_disagreement(Decimal("0.009"), Decimal("0.010"))
        assert gap_low is not None and gap_low < 0

    def test_zero_provider_cost_with_a_registry_cost_is_a_full_disagreement(self) -> None:
        assert cost_disagreement(Decimal("0.01"), Decimal(0)) == Decimal(1)

    def test_both_zero_agree(self) -> None:
        assert cost_disagreement(Decimal(0), Decimal(0)) is None
