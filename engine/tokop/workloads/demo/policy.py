"""Typed access to data/demo/policy.yaml.

The policy is the single source of truth for the demo: the handbook is rendered from it and
every gold answer is computed from it. A question and its answer therefore cannot drift apart,
which is the whole reason SPEC.md section 6 asks for a generator instead of a hand-written
dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from tokop.paths import data_dir


class PolicyError(ValueError):
    """The policy file is missing something the generator needs."""


@dataclass(frozen=True)
class Category:
    key: str
    name: str
    base_return_days: int
    restocking_fee_pct: int
    warranty_years: int
    warranty_covers: str
    final_sale: bool
    requires_original_packaging: bool


@dataclass(frozen=True)
class Tier:
    key: str
    name: str
    return_window_bonus_days: int
    free_shipping_threshold_usd: Decimal
    restocking_fee_waived: bool
    annual_fee_usd: Decimal
    priority_support: bool


@dataclass(frozen=True)
class WeightBand:
    id: str
    label: str
    max_lb: float | None

    def contains(self, weight_lb: float) -> bool:
        return self.max_lb is None or weight_lb <= self.max_lb


@dataclass(frozen=True)
class Zone:
    key: str
    name: str
    transit_days: int


class Policy:
    """The retailer's rules, with the derivations the questions ask about."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.company = raw["company"]
        self.categories = {
            key: Category(
                key=key,
                name=str(value["name"]),
                base_return_days=int(value["base_return_days"]),
                restocking_fee_pct=int(value["restocking_fee_pct"]),
                warranty_years=int(value["warranty_years"]),
                warranty_covers=str(value["warranty_covers"]),
                final_sale=bool(value["final_sale"]),
                requires_original_packaging=bool(value["requires_original_packaging"]),
            )
            for key, value in raw["categories"].items()
        }
        self.tiers = {
            key: Tier(
                key=key,
                name=str(value["name"]),
                return_window_bonus_days=int(value["return_window_bonus_days"]),
                free_shipping_threshold_usd=Decimal(str(value["free_shipping_threshold_usd"])),
                restocking_fee_waived=bool(value["restocking_fee_waived"]),
                annual_fee_usd=Decimal(str(value["annual_fee_usd"])),
                priority_support=bool(value["priority_support"]),
            )
            for key, value in raw["loyalty_tiers"].items()
        }
        shipping = raw["shipping"]
        self.zones = {
            key: Zone(key=key, name=str(v["name"]), transit_days=int(v["transit_days"]))
            for key, v in shipping["zones"].items()
        }
        self.weight_bands = [
            WeightBand(
                id=str(b["id"]),
                label=str(b["label"]),
                max_lb=None if b["max_lb"] is None else float(b["max_lb"]),
            )
            for b in shipping["weight_bands"]
        ]
        self.fees = {
            zone: {band: Decimal(str(fee)) for band, fee in bands.items()}
            for zone, bands in shipping["fees"].items()
        }
        self.oversize_surcharge = Decimal(str(shipping["oversize_surcharge_usd"]))
        self.oversize_applies_to = list(shipping["oversize_applies_to"])
        self.oversize_threshold_lb = float(shipping["oversize_threshold_lb"])
        self.exceptions = raw["exceptions"]
        self.seasonal = raw["seasonal_extension"]
        self.escalation = raw["escalation"]

    # ------------------------------------------------------------------ derivations
    # Each of these is exactly what one question type asks the model to work out, and each is
    # the function that computes that question's gold answer. There is no second implementation.

    def category(self, key: str) -> Category:
        try:
            return self.categories[key]
        except KeyError:
            raise PolicyError(f"no category {key!r}") from None

    def tier(self, key: str) -> Tier:
        try:
            return self.tiers[key]
        except KeyError:
            raise PolicyError(f"no loyalty tier {key!r}") from None

    def return_window_days(
        self, category_key: str, tier_key: str, *, holiday_purchase: bool = False
    ) -> int:
        """Base window, plus the tier bonus, plus the seasonal extension where it applies.

        Final-sale categories have a zero-day window and no bonus reaches them.
        """
        category = self.category(category_key)
        tier = self.tier(tier_key)
        if category.final_sale:
            return 0
        days = category.base_return_days + tier.return_window_bonus_days
        if holiday_purchase and category_key not in self.seasonal["excluded_categories"]:
            days += int(self.seasonal["extra_days"])
        return days

    def opened_electronics_window_days(self) -> int:
        return int(self.exceptions["opened_electronics"]["return_days"])

    def restocking_fee_pct(
        self, category_key: str, tier_key: str, *, opened_seal: bool = False
    ) -> int:
        """The percentage actually charged, after the tier waiver and the opened-seal exception."""
        category = self.category(category_key)
        tier = self.tier(tier_key)
        if tier.restocking_fee_waived:
            return 0
        if (
            opened_seal
            and category_key in self.exceptions["opened_electronics"]["applies_to_categories"]
        ):
            return int(self.exceptions["opened_electronics"]["restocking_fee_pct"])
        return category.restocking_fee_pct

    def restocking_fee_usd(
        self, category_key: str, tier_key: str, price_usd: Decimal, *, opened_seal: bool = False
    ) -> Decimal:
        pct = self.restocking_fee_pct(category_key, tier_key, opened_seal=opened_seal)
        return (price_usd * Decimal(pct) / Decimal(100)).quantize(Decimal("0.01"))

    def refund_usd(
        self, category_key: str, tier_key: str, price_usd: Decimal, *, opened_seal: bool = False
    ) -> Decimal:
        return price_usd - self.restocking_fee_usd(
            category_key, tier_key, price_usd, opened_seal=opened_seal
        )

    def band_for(self, weight_lb: float) -> WeightBand:
        for band in self.weight_bands:
            if band.contains(weight_lb):
                return band
        raise PolicyError(f"no weight band covers {weight_lb} lb")

    def shipping_fee_usd(
        self, zone_key: str, weight_lb: float, category_key: str | None = None
    ) -> Decimal:
        """Band fee for the zone, plus the oversize surcharge where it applies."""
        if zone_key not in self.zones:
            raise PolicyError(f"no shipping zone {zone_key!r}")
        band = self.band_for(weight_lb)
        fee = self.fees[zone_key][band.id]
        if category_key in self.oversize_applies_to and weight_lb > self.oversize_threshold_lb:
            fee += self.oversize_surcharge
        return fee

    def free_shipping_applies(self, tier_key: str, order_total_usd: Decimal) -> bool:
        threshold = self.tier(tier_key).free_shipping_threshold_usd
        return order_total_usd >= threshold

    def order_shipping_usd(
        self,
        tier_key: str,
        zone_key: str,
        weight_lb: float,
        order_total_usd: Decimal,
        category_key: str | None = None,
    ) -> Decimal:
        if self.free_shipping_applies(tier_key, order_total_usd):
            return Decimal("0.00")
        return self.shipping_fee_usd(zone_key, weight_lb, category_key)

    def escalation_level_for(
        self, *, refund_usd: Decimal | None = None, safety: bool = False
    ) -> str:
        """Which escalation level handles a case."""
        if safety:
            return str(self.escalation["safety_complaint_escalates_to"])
        if refund_usd is not None and refund_usd > Decimal(
            str(self.escalation["manager_threshold_usd"])
        ):
            return "tier_3"
        return "tier_1"

    def escalation_name(self, level_id: str) -> str:
        for level in self.escalation["levels"]:
            if level["id"] == level_id:
                return str(level["name"])
        raise PolicyError(f"no escalation level {level_id!r}")

    def escalation_target_hours(self, level_id: str) -> int:
        for level in self.escalation["levels"]:
            if level["id"] == level_id:
                return int(level["resolution_target_hours"])
        raise PolicyError(f"no escalation level {level_id!r}")

    def can_return(
        self, category_key: str, *, used_in_field: bool = False, seal_opened: bool = False
    ) -> bool:
        """Whether a return is possible at all, before the window is considered."""
        category = self.category(category_key)
        if category.final_sale:
            return False
        if (
            used_in_field
            and category_key in self.exceptions["used_climbing_safety"]["applies_to_categories"]
        ):
            return False
        if (
            seal_opened
            and category_key in self.exceptions["opened_electronics"]["applies_to_categories"]
        ):
            return True  # allowed, but on a shorter window and a higher fee
        return True


def load_policy_raw(path: Path | None = None) -> dict[str, Any]:
    target = path or data_dir() / "demo" / "policy.yaml"
    if not target.exists():
        raise PolicyError(f"missing policy file: {target}")
    data = yaml.safe_load(target.read_text())
    if not isinstance(data, dict):
        raise PolicyError(f"{target} must contain a mapping")
    return data


@lru_cache(maxsize=4)
def load_policy(path: Path | None = None) -> Policy:
    return Policy(load_policy_raw(path))
