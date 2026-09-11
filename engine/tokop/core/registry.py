"""The model and price registry (SPEC.md 7.3).

Two sources feed it, in this order of authority:

1. ``config/prices.yaml`` — official direct-provider prices, read off the provider's own page,
   with the URL and the date. These always win.
2. The OpenRouter public model listing — broad coverage of models Tokop has no direct price
   for. Everything from here is ``verified: false`` until someone checks it against the
   provider's own page, and the UI marks it.

The listing is pulled by ``tokop sync-models``. Tests and replay read a committed snapshot
instead, and the snapshot carries the date it was taken so nothing pretends to be current.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from tokop.core.pricing import ModelPrice, PriceProvenance, PriceSnapshot, price_from_mapping
from tokop.paths import config_dir, repo_root

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class RegistryError(ValueError):
    """The registry could not be assembled from the files on disk."""


class ProviderConfig(BaseModel):
    """One entry from config/providers.yaml."""

    name: str
    adapter: str
    base_url: str | None
    api_key_env: str | None
    enabled: bool
    usage_mapping: str
    docs: str | None = None
    unconfirmed_reason: str | None = None
    test_only: bool = False


class ModelEntry(BaseModel):
    """A model as the registry knows it."""

    model_id: str
    provider: str
    display_name: str
    price: ModelPrice
    max_output_tokens: int | None = None
    tool_use_system_prompt_tokens: int | None = None
    tokenizer_generation: str | None = None

    @property
    def min_cacheable_tokens(self) -> int | None:
        return self.price.min_cacheable_tokens


class Registry(BaseModel):
    """Everything Tokop knows about models, prices and providers."""

    models: dict[str, ModelEntry]
    providers: dict[str, ProviderConfig]
    roles: dict[str, str]
    scarce: list[str]
    concurrency: dict[str, int]
    listing_taken: date | None = None
    listing_source: str | None = None

    def model(self, model_id: str) -> ModelEntry:
        try:
            return self.models[model_id]
        except KeyError:
            raise RegistryError(
                f"no model {model_id!r} in the registry. Known models: "
                f"{', '.join(sorted(self.models))}"
            ) from None

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            raise RegistryError(f"no provider {name!r} in config/providers.yaml") from None

    def role(self, name: str) -> ModelEntry:
        try:
            return self.model(self.roles[name])
        except KeyError:
            raise RegistryError(
                f"no role {name!r} in config/models.yaml. Known roles: "
                f"{', '.join(sorted(self.roles))}"
            ) from None

    def is_scarce(self, model_id: str) -> bool:
        return model_id in self.scarce

    def snapshot(self, model_ids: Iterable[str], taken: date | None = None) -> PriceSnapshot:
        """Freeze the prices for a set of models, so a run's costs cannot move later."""
        prices = [self.model(m).price for m in model_ids]
        return PriceSnapshot.of(prices, taken or date.today())

    def tiers_by_cost(self, model_ids: Iterable[str]) -> list[ModelEntry]:
        """Models ordered cheapest first, by blended input+output price.

        A cascade's tier order is defined by cost (SPEC.md 7.5), and a single blended figure is
        used rather than input alone because a cheap-input, expensive-output model is not
        actually a cheap tier for a workload that generates.
        """
        entries = [self.model(m) for m in model_ids]
        return sorted(entries, key=lambda e: e.price.input + e.price.output)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RegistryError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise RegistryError(f"{path} must contain a mapping at the top level")
    return data


def load_providers(path: Path | None = None) -> dict[str, ProviderConfig]:
    raw = _load_yaml(path or config_dir() / "providers.yaml")
    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, dict):
        raise RegistryError("providers.yaml needs a top-level `providers:` mapping")
    out: dict[str, ProviderConfig] = {}
    for name, entry in providers_raw.items():
        if not isinstance(entry, dict):
            raise RegistryError(f"provider {name!r} must be a mapping")
        out[name] = ProviderConfig(name=name, **entry)
    for name, provider in out.items():
        if provider.enabled and provider.base_url is None and not provider.test_only:
            raise RegistryError(
                f"provider {name!r} is enabled but has no base_url. Either give it one or set "
                "`enabled: false` with an `unconfirmed_reason`."
            )
    return out


def load_prices(path: Path | None = None) -> dict[str, ModelEntry]:
    raw = _load_yaml(path or config_dir() / "prices.yaml")
    prices_raw = raw.get("prices")
    if not isinstance(prices_raw, dict):
        raise RegistryError("prices.yaml needs a top-level `prices:` mapping")
    out: dict[str, ModelEntry] = {}
    for model_id, entry in prices_raw.items():
        if not isinstance(entry, dict):
            raise RegistryError(f"price entry {model_id!r} must be a mapping")
        provider = str(entry.get("provider") or "")
        if not provider:
            raise RegistryError(f"price entry {model_id!r} has no provider")
        out[model_id] = ModelEntry(
            model_id=model_id,
            provider=provider,
            display_name=str(entry.get("display_name") or model_id),
            price=price_from_mapping(model_id, provider, entry),
            max_output_tokens=entry.get("max_output_tokens"),
            tool_use_system_prompt_tokens=entry.get("tool_use_system_prompt_tokens"),
            tokenizer_generation=entry.get("tokenizer_generation"),
        )
    return out


def parse_openrouter_listing(payload: dict[str, Any]) -> tuple[list[ModelEntry], date | None]:
    """Turn an OpenRouter ``/models`` payload into registry entries.

    OpenRouter reports prices per **token** as strings; the registry works per million, so
    every rate is multiplied by 1e6. A model with no usable price is skipped rather than
    entered at zero, which would make it look free.

    Everything from here is ``verified: false``: OpenRouter is a gateway reporting what it
    charges, not the provider's own published price, and the two can differ.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        raise RegistryError("OpenRouter listing has no `data` array")
    taken_raw = payload.get("_tokop_retrieved")
    taken = date.fromisoformat(str(taken_raw)) if taken_raw else None
    note = str(
        payload.get("_tokop_note")
        or "from the OpenRouter gateway listing, not the provider's own price page"
    )
    million = Decimal(1_000_000)
    entries: list[ModelEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        pricing = item.get("pricing")
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        try:
            prompt_rate = Decimal(str(pricing.get("prompt", "0"))) * million
            completion_rate = Decimal(str(pricing.get("completion", "0"))) * million
        except (ArithmeticError, ValueError):
            continue
        if prompt_rate <= 0 and completion_rate <= 0:
            continue
        cache_read_raw = pricing.get("input_cache_read")
        cache_write_raw = pricing.get("input_cache_write")
        provider = model_id.split("/", 1)[0] if "/" in model_id else "openrouter"
        price = ModelPrice(
            model_id=model_id,
            provider=provider,
            input=prompt_rate,
            output=completion_rate,
            cache_read=Decimal(str(cache_read_raw)) * million if cache_read_raw else Decimal(0),
            cache_write_5m=(
                Decimal(str(cache_write_raw)) * million if cache_write_raw else Decimal(0)
            ),
            context_tokens=item.get("context_length"),
            provenance=PriceProvenance(
                source_url=OPENROUTER_MODELS_URL,
                retrieved=taken or date.today(),
                verified=False,
                note=note,
            ),
        )
        entries.append(
            ModelEntry(
                model_id=model_id,
                provider=provider,
                display_name=str(item.get("name") or model_id),
                price=price,
                max_output_tokens=(item.get("top_provider") or {}).get("max_completion_tokens"),
            )
        )
    return entries, taken


def load_registry(
    *,
    config_path: Path | None = None,
    listing_path: Path | None = None,
) -> Registry:
    """Assemble the registry from config and, if present, a model listing snapshot."""
    cfg_dir = config_path or config_dir()
    models_cfg = _load_yaml(cfg_dir / "models.yaml")
    providers = load_providers(cfg_dir / "providers.yaml")
    models = load_prices(cfg_dir / "prices.yaml")

    listing_taken: date | None = None
    listing_source: str | None = None
    snapshot_rel = models_cfg.get("model_listing_snapshot")
    path = listing_path or (repo_root() / str(snapshot_rel) if snapshot_rel else None)
    if path is not None and path.exists():
        entries, listing_taken = parse_openrouter_listing(json.loads(path.read_text()))
        listing_source = str(path.relative_to(repo_root())) if path.is_absolute() else str(path)
        for entry in entries:
            # config/prices.yaml always wins: it is the provider's own published price.
            models.setdefault(entry.model_id, entry)

    roles = models_cfg.get("roles") or {}
    scarce = models_cfg.get("scarce") or []
    concurrency = models_cfg.get("concurrency") or {}
    if not isinstance(roles, dict) or not isinstance(scarce, list):
        raise RegistryError("models.yaml needs a `roles:` mapping and a `scarce:` list")

    for role_name, model_id in roles.items():
        if model_id not in models:
            raise RegistryError(
                f"role {role_name!r} points at {model_id!r}, which has no price in "
                "config/prices.yaml. A model with no price cannot be costed."
            )
    for model_id in scarce:
        if model_id not in models:
            raise RegistryError(f"scarce model {model_id!r} is not in the registry")

    return Registry(
        models=models,
        providers=providers,
        roles={str(k): str(v) for k, v in roles.items()},
        scarce=[str(m) for m in scarce],
        concurrency={str(k): int(v) for k, v in (concurrency or {}).items()},
        listing_taken=listing_taken,
        listing_source=listing_source,
    )
