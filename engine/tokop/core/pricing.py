"""Prices, price snapshots, and cost.

Two rules shape this module. First, a price without provenance is not a price: every
``ModelPrice`` carries the URL it came from, the date it was read, and whether a human
confirmed it, and the UI marks anything unverified (SPEC.md non-negotiable 4). Second, a run's
costs are computed from **that run's own snapshot**, so a price change tomorrow cannot move a
result recorded today (SPEC.md 7.3).

Money is ``Decimal`` end to end. Token counts are exact integers and rates are exact decimals,
so there is no reason to let binary floating point near a dollar figure.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from tokop.core.usage import TokenUsage

PER_MILLION = Decimal(1_000_000)


class PricingError(ValueError):
    """A cost could not be computed from the prices on hand."""


class PriceProvenance(BaseModel):
    """Where a price came from and whether anyone checked it."""

    source_url: str
    retrieved: date
    verified: bool = False
    note: str | None = None

    model_config = {"frozen": True}


class ModelPrice(BaseModel):
    """Dollars per million tokens, by bucket.

    Anthropic derives three of these from base input (5-minute writes at 1.25x, 1-hour writes
    at 2x, reads at 0.1x), but they are stored explicitly rather than derived, because that
    relationship is an Anthropic convention and not a law: a provider that prices cache reads
    differently must be representable without a code change.
    """

    model_id: str
    provider: str
    input: Decimal
    output: Decimal
    cache_write_5m: Decimal = Decimal(0)
    cache_write_1h: Decimal = Decimal(0)
    cache_read: Decimal = Decimal(0)
    batch_discount: Decimal = Decimal(0)
    min_cacheable_tokens: int | None = None
    context_tokens: int | None = None
    provenance: PriceProvenance

    model_config = {"frozen": True}

    @property
    def rates(self) -> dict[str, Decimal]:
        return {
            "input_uncached": self.input,
            "cache_write_5m": self.cache_write_5m,
            "cache_write_1h": self.cache_write_1h,
            "cache_read": self.cache_read,
            "output_visible": self.output,
            "output_reasoning": self.output,
        }


class CostBreakdown(BaseModel):
    """A cost with the arithmetic that produced it, so a hover can show its formula."""

    total: Decimal
    by_bucket: dict[str, Decimal]
    price_snapshot_id: str
    model_id: str
    source: str  # "registry" or "provider"

    model_config = {"frozen": True}

    def formula(self) -> str:
        parts = [f"{bucket} x ${rate}/Mtok" for bucket, rate in self.by_bucket.items() if rate]
        return " + ".join(parts) if parts else "no billable tokens"


def compute_cost(
    usage: TokenUsage,
    price: ModelPrice,
    snapshot_id: str = "unpinned",
    *,
    batch: bool = False,
) -> CostBreakdown:
    """Cost of one call, bucket by bucket.

    v1 never executes batches (SPEC.md 7.4 W05 is projected only), but the discount is applied
    here so a projection and a real cost run through the same arithmetic.
    """
    rates = price.rates
    discount = (Decimal(1) - price.batch_discount) if batch else Decimal(1)
    by_bucket: dict[str, Decimal] = {}
    total = Decimal(0)
    for bucket, rate in rates.items():
        tokens = getattr(usage, bucket)
        if not tokens:
            continue
        amount = (Decimal(tokens) * rate * discount) / PER_MILLION
        by_bucket[bucket] = amount
        total += amount
    for reserved in ("image_in", "audio_in", "image_out"):
        if getattr(usage, reserved):
            raise PricingError(
                f"{reserved} is non-zero but v1 does not price image, video or audio "
                "generation (SPEC.md section 3). Refusing to report a cost that omits it."
            )
    return CostBreakdown(
        total=total,
        by_bucket=by_bucket,
        price_snapshot_id=snapshot_id,
        model_id=price.model_id,
        source="registry",
    )


class PriceSnapshot(BaseModel):
    """The prices one run was costed with, frozen and content-addressed."""

    snapshot_id: str
    taken: date
    prices: dict[str, ModelPrice]

    model_config = {"frozen": True}

    @classmethod
    def of(cls, prices: Iterable[ModelPrice], taken: date) -> PriceSnapshot:
        by_id = {p.model_id: p for p in prices}
        payload = json.dumps(
            {
                mid: {
                    "input": str(p.input),
                    "output": str(p.output),
                    "cache_write_5m": str(p.cache_write_5m),
                    "cache_write_1h": str(p.cache_write_1h),
                    "cache_read": str(p.cache_read),
                    "batch_discount": str(p.batch_discount),
                    "source_url": p.provenance.source_url,
                    "retrieved": p.provenance.retrieved.isoformat(),
                    "verified": p.provenance.verified,
                }
                for mid, p in sorted(by_id.items())
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return cls(snapshot_id=digest, taken=taken, prices=by_id)

    def price_for(self, model_id: str) -> ModelPrice:
        try:
            return self.prices[model_id]
        except KeyError:
            raise PricingError(
                f"no price for {model_id!r} in snapshot {self.snapshot_id}. A run is always "
                "costed from its own snapshot, so the model must have been priced when it ran."
            ) from None

    def cost(self, model_id: str, usage: TokenUsage, *, batch: bool = False) -> CostBreakdown:
        return compute_cost(usage, self.price_for(model_id), self.snapshot_id, batch=batch)

    @property
    def all_verified(self) -> bool:
        return all(p.provenance.verified for p in self.prices.values())

    def unverified_models(self) -> list[str]:
        return sorted(m for m, p in self.prices.items() if not p.provenance.verified)


COST_DISAGREEMENT_THRESHOLD = Decimal("0.02")


def cost_disagreement(registry_cost: Decimal, provider_cost: Decimal) -> Decimal | None:
    """Relative gap between a gateway's authoritative cost and the registry's, if above 2%.

    Returns ``None`` when the two agree closely enough to be the same number, and the signed
    relative difference otherwise, so the caller can flag it rather than silently pick a side
    (SPEC.md 7.2).
    """
    if provider_cost == 0:
        return None if registry_cost == 0 else Decimal(1)
    gap = (registry_cost - provider_cost) / provider_cost
    return gap if abs(gap) > COST_DISAGREEMENT_THRESHOLD else None


def price_from_mapping(model_id: str, provider: str, raw: Mapping[str, Any]) -> ModelPrice:
    """Build a ``ModelPrice`` from a config/prices.yaml entry.

    Anthropic's cache multipliers are applied only where the YAML leaves the field out, and the
    provenance note records that the number was derived rather than read off a page.
    """
    provenance_raw = raw.get("provenance")
    if not isinstance(provenance_raw, Mapping):
        raise PricingError(f"price entry for {model_id!r} has no provenance block")
    provenance = PriceProvenance(
        source_url=str(provenance_raw["source_url"]),
        retrieved=date.fromisoformat(str(provenance_raw["retrieved"])),
        verified=bool(provenance_raw.get("verified", False)),
        note=provenance_raw.get("note"),
    )
    base_input = Decimal(str(raw["input"]))
    derived: list[str] = []

    def field(name: str, multiplier: Decimal | None) -> Decimal:
        if name in raw and raw[name] is not None:
            return Decimal(str(raw[name]))
        if multiplier is None:
            return Decimal(0)
        derived.append(name)
        return base_input * multiplier

    price = ModelPrice(
        model_id=model_id,
        provider=provider,
        input=base_input,
        output=Decimal(str(raw["output"])),
        cache_write_5m=field("cache_write_5m", Decimal("1.25")),
        cache_write_1h=field("cache_write_1h", Decimal("2")),
        cache_read=field("cache_read", Decimal("0.1")),
        batch_discount=Decimal(str(raw.get("batch_discount", 0))),
        min_cacheable_tokens=raw.get("min_cacheable_tokens"),
        context_tokens=raw.get("context_tokens"),
        provenance=provenance,
    )
    if derived:
        note = f"{', '.join(derived)} derived from base input by Anthropic's cache multipliers"
        note = f"{provenance.note}; {note}" if provenance.note else note
        price = price.model_copy(
            update={"provenance": provenance.model_copy(update={"note": note})}
        )
    return price


class PriceRegistryEntry(BaseModel):
    """A model as the UI lists it: price plus where the price came from."""

    model_id: str
    provider: str
    display_name: str
    price: ModelPrice
    roles: list[str] = Field(default_factory=list)
    scarce: bool = False
