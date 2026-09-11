"""The model and price registry (SPEC.md 7.3)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tokop.core.registry import (
    RegistryError,
    load_prices,
    load_providers,
    load_registry,
    parse_openrouter_listing,
)
from tokop.core.sync import snapshot_age_days


class TestShippedConfig:
    """The real config/ files, because a broken registry breaks every number downstream."""

    def test_the_demo_roles_resolve_to_priced_models(self) -> None:
        registry = load_registry()
        for role in ("frontier", "mid", "cheap"):
            entry = registry.role(role)
            assert entry.price.input > 0
            assert entry.price.output > 0

    def test_the_demo_model_ids_match_anthropics_published_ids(self) -> None:
        registry = load_registry()
        assert registry.roles["frontier"] == "claude-opus-5"
        assert registry.roles["mid"] == "claude-sonnet-5"
        assert registry.roles["cheap"] == "claude-haiku-4-5-20251001"

    def test_the_demo_prices_match_anthropics_published_prices(self) -> None:
        """Verified against platform.claude.com/docs/en/about-claude/pricing on 2026-09-11."""
        registry = load_registry()
        opus = registry.model("claude-opus-5").price
        assert (opus.input, opus.output) == (Decimal("5"), Decimal("25"))
        assert (opus.cache_write_5m, opus.cache_write_1h, opus.cache_read) == (
            Decimal("6.25"),
            Decimal("10"),
            Decimal("0.50"),
        )
        sonnet = registry.model("claude-sonnet-5").price
        assert (sonnet.input, sonnet.output) == (Decimal("2"), Decimal("10"))
        haiku = registry.model("claude-haiku-4-5-20251001").price
        assert (haiku.input, haiku.output) == (Decimal("1"), Decimal("5"))
        assert all(p.batch_discount == Decimal("0.5") for p in (opus, sonnet, haiku))

    def test_the_demo_cache_minimums_match_the_documentation(self) -> None:
        registry = load_registry()
        assert registry.model("claude-opus-5").min_cacheable_tokens == 512
        assert registry.model("claude-sonnet-5").min_cacheable_tokens == 1024
        assert registry.model("claude-haiku-4-5-20251001").min_cacheable_tokens == 4096

    def test_fable_51_reads_at_the_lower_multiplier(self) -> None:
        """Appendix B: 0.025x on Fable 5.1, not the usual 0.1x."""
        price = load_registry().model("claude-fable-5-1").price
        assert price.cache_read == Decimal("0.25")
        assert price.cache_read == price.input * Decimal("0.025")

    def test_every_directly_priced_model_is_verified(self) -> None:
        prices = load_prices()
        unverified = [m for m, e in prices.items() if not e.price.provenance.verified]
        assert unverified == [], f"unverified direct prices: {unverified}"

    def test_every_direct_price_carries_a_source_and_a_date(self) -> None:
        for entry in load_prices().values():
            assert entry.price.provenance.source_url.startswith("https://")
            assert entry.price.provenance.retrieved <= date.today()

    def test_haiku_is_on_the_older_tokenizer_and_opus_on_the_newer(self) -> None:
        registry = load_registry()
        assert registry.model("claude-haiku-4-5-20251001").tokenizer_generation == "previous"
        assert registry.model("claude-opus-5").tokenizer_generation == "newer"

    def test_tiers_sort_cheapest_first(self) -> None:
        registry = load_registry()
        ordered = registry.tiers_by_cost(
            ["claude-opus-5", "claude-haiku-4-5-20251001", "claude-sonnet-5"]
        )
        assert [e.model_id for e in ordered] == [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-5",
            "claude-opus-5",
        ]

    def test_the_scarce_model_is_the_frontier_model(self) -> None:
        registry = load_registry()
        assert registry.is_scarce(registry.roles["frontier"])
        assert not registry.is_scarce(registry.roles["cheap"])


class TestProviders:
    def test_unconfirmed_providers_are_disabled_with_a_reason(self) -> None:
        providers = load_providers()
        for name in ("qwen", "kimi", "zai", "perplexity"):
            provider = providers[name]
            assert not provider.enabled
            assert provider.unconfirmed_reason
            assert provider.base_url is None

    def test_enabled_providers_have_base_urls(self) -> None:
        for provider in load_providers().values():
            if provider.enabled and not provider.test_only:
                assert provider.base_url and provider.base_url.startswith("https://")

    def test_the_simulated_provider_is_marked_test_only(self) -> None:
        assert load_providers()["simulated"].test_only

    def test_an_enabled_provider_without_a_base_url_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "providers.yaml").write_text(
            "providers:\n  broken:\n    adapter: openai_compatible\n    base_url: null\n"
            "    api_key_env: X\n    enabled: true\n    usage_mapping: openai\n"
        )
        with pytest.raises(RegistryError, match="enabled but has no base_url"):
            load_providers(tmp_path / "providers.yaml")


class TestOpenRouterListing:
    def test_per_token_prices_are_converted_to_per_million(self) -> None:
        payload = {
            "_tokop_retrieved": "2026-09-11",
            "data": [
                {
                    "id": "vendor/model",
                    "name": "Vendor Model",
                    "context_length": 128000,
                    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                }
            ],
        }
        entries, taken = parse_openrouter_listing(payload)
        assert entries[0].price.input == Decimal("3")
        assert entries[0].price.output == Decimal("15")
        assert taken == date(2026, 9, 11)

    def test_everything_from_the_listing_is_unverified(self) -> None:
        entries, _ = parse_openrouter_listing(
            {"data": [{"id": "v/m", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
        )
        assert not entries[0].price.provenance.verified

    def test_a_model_with_no_usable_price_is_skipped_not_entered_as_free(self) -> None:
        entries, _ = parse_openrouter_listing(
            {"data": [{"id": "v/free", "pricing": {"prompt": "0", "completion": "0"}}]}
        )
        assert entries == []

    def test_a_payload_without_data_is_refused(self) -> None:
        with pytest.raises(RegistryError, match="no `data` array"):
            parse_openrouter_listing({})

    def test_the_committed_snapshot_parses_and_says_it_is_a_fixture(self) -> None:
        from tokop.paths import repo_root

        payload = json.loads((repo_root() / "fixtures/test/openrouter-models.json").read_text())
        entries, taken = parse_openrouter_listing(payload)
        assert len(entries) >= 3
        assert taken is not None
        assert "HAND-BUILT TEST FIXTURE" in payload["_tokop_source"]
        assert all("hand-built fixture" in (e.price.provenance.note or "") for e in entries)

    def test_config_prices_win_over_the_listing(self) -> None:
        """Both name Claude Opus 5; the direct price is the one that must survive."""
        registry = load_registry()
        assert registry.model("claude-opus-5").price.provenance.verified
        assert not registry.model("anthropic/claude-opus-5").price.provenance.verified

    def test_snapshot_age_is_reported_for_the_ui(self) -> None:
        assert snapshot_age_days(date(2026, 9, 1), date(2026, 9, 11)) == 10
        assert snapshot_age_days(None) is None


class TestRegistryErrors:
    def test_an_unknown_model_lists_the_known_ones(self) -> None:
        with pytest.raises(RegistryError, match="claude-opus-5"):
            load_registry().model("not-a-model")

    def test_an_unknown_role_lists_the_known_roles(self) -> None:
        with pytest.raises(RegistryError, match="frontier"):
            load_registry().role("nonexistent")

    def test_an_unknown_provider_points_at_the_config_file(self) -> None:
        with pytest.raises(RegistryError, match=r"providers\.yaml"):
            load_registry().provider("nope")

    def test_a_role_pointing_at_an_unpriced_model_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "providers.yaml").write_text("providers: {}\n")
        (tmp_path / "prices.yaml").write_text(
            "prices:\n  m1:\n    provider: p\n    input: 1\n    output: 2\n"
            "    provenance:\n      source_url: https://x.invalid\n      retrieved: 2026-09-11\n"
        )
        (tmp_path / "models.yaml").write_text("roles:\n  frontier: missing-model\nscarce: []\n")
        with pytest.raises(RegistryError, match="cannot be costed"):
            load_registry(config_path=tmp_path)

    def test_a_missing_config_file_names_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(RegistryError, match="missing config file"):
            load_registry(config_path=tmp_path)
