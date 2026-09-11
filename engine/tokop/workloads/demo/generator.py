"""Generate the demo dataset (SPEC.md section 6).

Questions come from templates with at least four phrasings each, and **code computes every gold
answer from the policy**. Nothing is written twice, so a question and its answer cannot drift
apart, and ``tokop fixtures-check`` can regenerate the whole set and assert it still matches.

Four question types, in roughly a 45/25/20/10 mix:

``lookup``       one fact from one section
``two_hop``      two facts combined, usually a category rule plus a tier rule
``computation``  arithmetic on top of the lookups: fees, refunds, shipping
``exception``    a rule that overrides the general rule

The item records its type and its supporting section IDs for analysis after the fact. **The
router never sees either** — that would be a router that knows the answer's shape before
choosing a model, which is not a router anyone could deploy.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from tokop.workloads.demo.policy import Policy

QuestionType = Literal["lookup", "two_hop", "computation", "exception"]
AnswerType = Literal["number", "money", "yes_no", "enum", "string"]

#: Target mix (SPEC.md section 6). 300 items -> 135 / 75 / 60 / 30.
TYPE_MIX: dict[QuestionType, float] = {
    "lookup": 0.45,
    "two_hop": 0.25,
    "computation": 0.20,
    "exception": 0.10,
}

DEFAULT_SEED = 20260911
DEFAULT_SIZE = 300
DEFAULT_CALIBRATION = 100


@dataclass(frozen=True)
class DemoItem:
    """One generated task."""

    id: str
    question: str
    gold: str
    answer_type: AnswerType
    question_type: QuestionType
    sections: tuple[str, ...]
    template_id: str
    params: dict[str, Any] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "gold": self.gold,
            "answer_type": self.answer_type,
            "question_type": self.question_type,
            "sections": list(self.sections),
            "template_id": self.template_id,
            "params": self.params,
            "aliases": list(self.aliases),
        }

    def router_view(self) -> dict[str, Any]:
        """What a router or scorer is allowed to see: the question and nothing else.

        No gold, no answer type, no question type, no section IDs.
        """
        return {"id": self.id, "question": self.question}


@dataclass(frozen=True)
class Template:
    """A question shape: several phrasings, a parameter space, and a gold function."""

    id: str
    question_type: QuestionType
    answer_type: AnswerType
    phrasings: tuple[str, ...]
    sections: tuple[str, ...]
    params: Callable[[Policy], list[dict[str, Any]]]
    gold: Callable[[Policy, dict[str, Any]], str]
    text_params: Callable[[Policy, dict[str, Any]], dict[str, Any]] | None = None
    aliases: Callable[[Policy, dict[str, Any]], tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        if len(self.phrasings) < 4:
            raise ValueError(
                f"template {self.id} has {len(self.phrasings)} phrasings; SPEC.md section 6 "
                "requires at least four"
            )


_ARTICLE = re.compile(r"\b([Aa]) (?=[AEIOUaeiou])")


def _fix_articles(text: str) -> str:
    """Turn "a Alpine member" into "an Alpine member".

    Templates interpolate names the phrasing cannot know the first letter of, so the article is
    corrected once here rather than duplicated across every phrasing.
    """
    return _ARTICLE.sub(lambda m: ("An " if m.group(1) == "A" else "an ") + "", text)


def leaks_own_answer(item: DemoItem) -> bool:
    """Whether a question already contains its own gold answer.

    It happens naturally: an Alpine member pays no restocking fee, so "what refund do they get
    on a $129.00 item" has the gold answer sitting in the question. Such an item is answerable
    by copying a number, which makes every tier look identical on it and quietly flattens the
    difficulty gap the cascade depends on. They are dropped at generation time.
    """
    if item.answer_type not in ("number", "money"):
        return False
    if item.gold in ("0", "0.00"):
        return False
    return re.search(rf"(?<![\d.]){re.escape(item.gold)}(?![\d])", item.question) is not None


def _money_str(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _returnable_categories(policy: Policy) -> list[str]:
    return [k for k, c in policy.categories.items() if not c.final_sale]


# --------------------------------------------------------------------------- lookup templates


def _lookup_templates() -> list[Template]:
    return [
        Template(
            id="lu_return_window",
            question_type="lookup",
            answer_type="number",
            sections=("sec-returns-windows",),
            phrasings=(
                "How many days is the base return window for {category}?",
                "What is the standard return window on {category}, in days?",
                "A customer asks how long they have to send back {category}. What is the base "
                "window in days, before any tier bonus?",
                "For {category}, how many days does the base return window run?",
                "Base return window for {category} — how many days?",
            ),
            params=lambda p: [{"category": k} for k in p.categories],
            gold=lambda p, a: str(p.category(a["category"]).base_return_days),
            text_params=lambda p, a: {"category": p.category(a["category"]).name.lower()},
        ),
        Template(
            id="lu_restocking_pct",
            question_type="lookup",
            answer_type="number",
            sections=("sec-restocking",),
            phrasings=(
                "What restocking fee percentage applies to {category}?",
                "For {category}, what percent is the restocking fee?",
                "A customer returning {category} that is not defective — what restocking "
                "percentage do we charge?",
                "Restocking fee for {category}, as a percentage?",
                "What percent of the item price is withheld as a restocking fee on {category}?",
            ),
            params=lambda p: [{"category": k} for k in p.categories],
            gold=lambda p, a: str(p.category(a["category"]).restocking_fee_pct),
            text_params=lambda p, a: {"category": p.category(a["category"]).name.lower()},
        ),
        Template(
            id="lu_warranty_years",
            question_type="lookup",
            answer_type="number",
            sections=("sec-warranty",),
            phrasings=(
                "How many years is the warranty on {category}?",
                "What is the warranty period for {category}, in years?",
                "A customer wants to know how long {category} is covered. How many years?",
                "Warranty length for {category} in years?",
                "For how many years do we warrant {category}?",
            ),
            params=lambda p: [{"category": k} for k in p.categories],
            gold=lambda p, a: str(p.category(a["category"]).warranty_years),
            text_params=lambda p, a: {"category": p.category(a["category"]).name.lower()},
        ),
        Template(
            id="lu_transit_days",
            question_type="lookup",
            answer_type="number",
            sections=("sec-shipping-zones",),
            phrasings=(
                "How many business days is transit to {zone}?",
                "What is the transit time to {zone}, in business days?",
                "A customer in {zone} asks how long shipping takes. How many business days?",
                "Transit time for {zone} in business days?",
                "For {zone}, how many business days should we quote for transit?",
            ),
            params=lambda p: [{"zone": k} for k in p.zones],
            gold=lambda p, a: str(p.zones[a["zone"]].transit_days),
            text_params=lambda p, a: {"zone": p.zones[a["zone"]].name},
        ),
        Template(
            id="lu_free_ship_threshold",
            question_type="lookup",
            answer_type="money",
            sections=("sec-free-shipping",),
            phrasings=(
                "What order total does a {tier} member need to reach for free shipping?",
                "For a {tier} member, what is the free-shipping threshold in dollars?",
                "How much must a {tier} customer spend before shipping is free?",
                "Free-shipping threshold for the {tier} tier, in dollars?",
                "At what order total does shipping become free for {tier} members?",
            ),
            params=lambda p: [{"tier": k} for k in p.tiers],
            gold=lambda p, a: _money_str(p.tier(a["tier"]).free_shipping_threshold_usd),
            text_params=lambda p, a: {"tier": p.tier(a["tier"]).name},
        ),
        Template(
            id="lu_tier_bonus",
            question_type="lookup",
            answer_type="number",
            sections=("sec-loyalty",),
            phrasings=(
                "How many extra return days does the {tier} tier add?",
                "What is the return window bonus for a {tier} member, in days?",
                "A {tier} member asks how many additional days their membership gives them on "
                "returns. How many?",
                "Return window bonus for {tier}, in days?",
                "How many days does {tier} membership add to a return window?",
            ),
            params=lambda p: [{"tier": k} for k in p.tiers],
            gold=lambda p, a: str(p.tier(a["tier"]).return_window_bonus_days),
            text_params=lambda p, a: {"tier": p.tier(a["tier"]).name},
        ),
        Template(
            id="lu_annual_fee",
            question_type="lookup",
            answer_type="money",
            sections=("sec-loyalty",),
            phrasings=(
                "What is the annual fee for the {tier} tier?",
                "How much does {tier} membership cost per year, in dollars?",
                "A customer asks what {tier} costs annually. How much?",
                "Annual fee for {tier}, in dollars?",
                "What does a year of {tier} membership cost?",
            ),
            params=lambda p: [{"tier": k} for k in p.tiers],
            gold=lambda p, a: _money_str(p.tier(a["tier"]).annual_fee_usd),
            text_params=lambda p, a: {"tier": p.tier(a["tier"]).name},
        ),
        Template(
            id="lu_final_sale",
            question_type="lookup",
            answer_type="yes_no",
            sections=("sec-returns-windows",),
            phrasings=(
                "Is {category} sold as final sale? Answer yes or no.",
                "Are {category} final-sale items? Yes or no.",
                "A customer asks whether {category} can be returned at all. Is the category "
                "final sale? Answer yes or no.",
                "Final sale for {category}: yes or no?",
                "Would {category} be classed as final sale? Answer yes or no.",
            ),
            params=lambda p: [{"category": k} for k in p.categories],
            gold=lambda p, a: "yes" if p.category(a["category"]).final_sale else "no",
            text_params=lambda p, a: {"category": p.category(a["category"]).name.lower()},
        ),
        Template(
            id="lu_packaging",
            question_type="lookup",
            answer_type="yes_no",
            sections=("sec-packaging",),
            phrasings=(
                "Does {category} need its original packaging for a return? Answer yes or no.",
                "Is original packaging required to return {category}? Yes or no.",
                "A customer threw away the box for {category}. Does our policy require original "
                "packaging for that category? Answer yes or no.",
                "Original packaging required for {category}: yes or no?",
                "Must {category} come back in its original packaging? Answer yes or no.",
            ),
            params=lambda p: [{"category": k} for k in p.categories],
            gold=lambda p, a: (
                "yes" if p.category(a["category"]).requires_original_packaging else "no"
            ),
            text_params=lambda p, a: {"category": p.category(a["category"]).name.lower()},
        ),
        Template(
            id="lu_escalation_hours",
            question_type="lookup",
            answer_type="number",
            sections=("sec-escalation",),
            phrasings=(
                "What is the resolution target in hours for {level}?",
                "How many hours is the resolution target at {level}?",
                "A case sits with {level}. What resolution target in hours applies?",
                "Resolution target for {level}, in hours?",
                "Within how many hours should {level} resolve a case?",
            ),
            params=lambda p: [{"level": lvl["id"]} for lvl in p.escalation["levels"]],
            gold=lambda p, a: str(p.escalation_target_hours(a["level"])),
            text_params=lambda p, a: {"level": p.escalation_name(a["level"]).lower()},
        ),
        Template(
            id="lu_band_fee",
            question_type="lookup",
            answer_type="money",
            sections=("sec-shipping-fees",),
            phrasings=(
                "What is the shipping fee to {zone} for a parcel in the {band} band?",
                "For {zone}, what does the {band} weight band cost to ship?",
                "A parcel {band} is going to {zone}. What is the fee in dollars?",
                "Shipping fee, {zone}, {band} band — how much?",
                "How much do we charge to ship {band} to {zone}?",
            ),
            params=lambda p: [{"zone": z, "band": b.id} for z in p.zones for b in p.weight_bands],
            gold=lambda p, a: _money_str(p.fees[a["zone"]][a["band"]]),
            text_params=lambda p, a: {
                "zone": p.zones[a["zone"]].name,
                "band": next(b.label for b in p.weight_bands if b.id == a["band"]),
            },
        ),
    ]


# -------------------------------------------------------------------------- two-hop templates


def _two_hop_templates() -> list[Template]:
    return [
        Template(
            id="th_window_with_tier",
            question_type="two_hop",
            answer_type="number",
            sections=("sec-returns-windows", "sec-loyalty"),
            phrasings=(
                "A {tier} member bought {category}. How many days do they have to return it?",
                "How long is the return window for {category} for someone in the {tier} tier, "
                "in days?",
                "{tier} customer, {category}. What is their return window in days?",
                "Counting the tier bonus, how many days does a {tier} member get to return "
                "{category}?",
                "Return window in days for {category} bought by a {tier} member?",
            ),
            params=lambda p: [
                {"category": c, "tier": t} for c in _returnable_categories(p) for t in p.tiers
            ],
            gold=lambda p, a: str(p.return_window_days(a["category"], a["tier"])),
            text_params=lambda p, a: {
                "category": p.category(a["category"]).name.lower(),
                "tier": p.tier(a["tier"]).name,
            },
        ),
        Template(
            id="th_fee_with_tier",
            question_type="two_hop",
            answer_type="number",
            sections=("sec-restocking", "sec-loyalty"),
            phrasings=(
                "A {tier} member returns {category} in working order. What restocking "
                "percentage applies?",
                "What percent restocking fee does a {tier} customer pay on {category}?",
                "{tier} tier, {category}, not a defect. Restocking percentage?",
                "Taking the tier into account, what restocking percentage applies when a {tier} "
                "member returns {category}?",
                "Restocking fee percentage for {category} returned by a {tier} member?",
            ),
            params=lambda p: [
                {"category": c, "tier": t} for c in _returnable_categories(p) for t in p.tiers
            ],
            gold=lambda p, a: str(p.restocking_fee_pct(a["category"], a["tier"])),
            text_params=lambda p, a: {
                "category": p.category(a["category"]).name.lower(),
                "tier": p.tier(a["tier"]).name,
            },
        ),
        Template(
            id="th_zone_weight_fee",
            question_type="two_hop",
            answer_type="money",
            sections=("sec-shipping-fees",),
            phrasings=(
                "An order weighing {weight} lb ships to {zone}. What is the shipping fee?",
                "What do we charge to send {weight} lb to {zone}?",
                "{weight} lb parcel, {zone}. Shipping fee in dollars?",
                "Work out the shipping fee for a {weight} lb order going to {zone}.",
                "How much is shipping on {weight} lb to {zone}?",
            ),
            params=lambda p: [
                {"zone": z, "weight": w}
                for z in p.zones
                for w in (1.5, 3.0, 4.5, 8.0, 11.0, 18.0, 22.5, 33.0, 47.0, 64.0)
            ],
            gold=lambda p, a: _money_str(p.shipping_fee_usd(a["zone"], a["weight"])),
            text_params=lambda p, a: {
                "zone": p.zones[a["zone"]].name,
                "weight": f"{a['weight']:g}",
            },
        ),
        Template(
            id="th_free_shipping",
            question_type="two_hop",
            answer_type="yes_no",
            sections=("sec-free-shipping", "sec-loyalty"),
            phrasings=(
                "A {tier} member places an order totalling ${total}. Does it ship free? Answer "
                "yes or no.",
                "Order total ${total}, {tier} tier. Free shipping? Yes or no.",
                "Does a ${total} order from a {tier} customer qualify for free shipping? Answer "
                "yes or no.",
                "Free shipping on ${total} for a {tier} member: yes or no?",
                "{tier} member, ${total} before tax. Is shipping free? Answer yes or no.",
            ),
            params=lambda p: [
                {"tier": t, "total": total}
                for t in p.tiers
                for total in (18, 35, 39, 42, 58, 74, 76, 95, 140)
            ],
            gold=lambda p, a: (
                "yes" if p.free_shipping_applies(a["tier"], Decimal(str(a["total"]))) else "no"
            ),
            text_params=lambda p, a: {
                "tier": p.tier(a["tier"]).name,
                "total": f"{a['total']:g}",
            },
        ),
        Template(
            id="th_escalation_level",
            question_type="two_hop",
            answer_type="enum",
            sections=("sec-escalation",),
            phrasings=(
                "A refund of ${amount} is requested with no safety issue. Which escalation "
                "level handles it? Answer with the level key.",
                "Who handles a ${amount} refund with nothing safety-related about it? Give the "
                "escalation level key.",
                "${amount} refund, routine case. Which escalation level key applies?",
                "Route a ${amount} refund request with no injury or safety failure. Which "
                "level key takes it?",
                "For a refund of ${amount} with no safety concern, name the escalation level "
                "key that handles it.",
            ),
            params=lambda p: [
                {"amount": amount} for amount in (75, 180, 320, 480, 499, 501, 640, 850, 1200)
            ],
            gold=lambda p, a: p.escalation_level_for(refund_usd=Decimal(str(a["amount"]))),
            text_params=lambda p, a: {"amount": f"{a['amount']:g}"},
        ),
    ]


# ---------------------------------------------------------------------- computation templates


def _computation_templates() -> list[Template]:
    prices = (39.95, 74.50, 129.00, 187.40, 248.75, 315.00, 429.99, 560.00, 725.25)
    return [
        Template(
            id="cp_restocking_amount",
            question_type="computation",
            answer_type="money",
            sections=("sec-restocking", "sec-loyalty"),
            phrasings=(
                "A {tier} member returns {category} that cost ${price}. What restocking fee is "
                "charged, in dollars?",
                "Work out the restocking fee in dollars on a ${price} {category} return by a "
                "{tier} member.",
                "{tier} tier, {category}, ${price} paid, not defective. Restocking fee in dollars?",
                "How many dollars of restocking fee come off a ${price} {category} returned by "
                "a {tier} customer?",
                "Calculate the restocking fee for {category} priced ${price}, returned by a "
                "{tier} member.",
            ),
            params=lambda p: [
                {"category": c, "tier": t, "price": price}
                for c in _returnable_categories(p)
                for t in p.tiers
                for price in prices
            ],
            gold=lambda p, a: _money_str(
                p.restocking_fee_usd(a["category"], a["tier"], Decimal(str(a["price"])))
            ),
            text_params=lambda p, a: {
                "category": p.category(a["category"]).name.lower(),
                "tier": p.tier(a["tier"]).name,
                "price": f"{a['price']:.2f}",
            },
        ),
        Template(
            id="cp_refund_amount",
            question_type="computation",
            answer_type="money",
            sections=("sec-restocking", "sec-loyalty"),
            phrasings=(
                "A {tier} member returns {category} bought for ${price}, in working order. What "
                "refund do they receive, in dollars?",
                "Work out the refund on a ${price} {category} returned by a {tier} member when "
                "there is no defect.",
                "{tier} tier, {category} at ${price}, no defect. Refund in dollars?",
                "After the restocking fee, how much does a {tier} customer get back on a "
                "${price} {category} return?",
                "Calculate the refund for {category} priced ${price} returned by a {tier} "
                "member with no defect.",
            ),
            params=lambda p: [
                {"category": c, "tier": t, "price": price}
                for c in _returnable_categories(p)
                for t in p.tiers
                for price in prices
            ],
            gold=lambda p, a: _money_str(
                p.refund_usd(a["category"], a["tier"], Decimal(str(a["price"])))
            ),
            text_params=lambda p, a: {
                "category": p.category(a["category"]).name.lower(),
                "tier": p.tier(a["tier"]).name,
                "price": f"{a['price']:.2f}",
            },
        ),
        Template(
            id="cp_order_shipping",
            question_type="computation",
            answer_type="money",
            sections=("sec-shipping-fees", "sec-free-shipping"),
            phrasings=(
                "A {tier} member orders ${total} of goods weighing {weight} lb, shipping to "
                "{zone}. What shipping is charged, in dollars?",
                "Work out the shipping charged on a ${total} order, {weight} lb, to {zone}, for "
                "a {tier} member.",
                "{tier} tier, ${total} order, {weight} lb, {zone}. Shipping in dollars?",
                "How much shipping does a {tier} customer pay on ${total} weighing {weight} lb "
                "sent to {zone}?",
                "Calculate shipping for a {weight} lb, ${total} order to {zone} placed by a "
                "{tier} member.",
            ),
            params=lambda p: [
                {"tier": t, "zone": z, "weight": w, "total": total}
                for t in p.tiers
                for z in p.zones
                for w in (2.0, 6.5, 14.0, 28.0, 55.0)
                for total in (32, 68, 110)
            ],
            gold=lambda p, a: _money_str(
                p.order_shipping_usd(a["tier"], a["zone"], a["weight"], Decimal(str(a["total"])))
            ),
            text_params=lambda p, a: {
                "tier": p.tier(a["tier"]).name,
                "zone": p.zones[a["zone"]].name,
                "weight": f"{a['weight']:g}",
                "total": f"{a['total']:g}",
            },
        ),
        Template(
            id="cp_seasonal_window",
            question_type="computation",
            answer_type="number",
            sections=("sec-returns-windows", "sec-loyalty", "sec-seasonal"),
            phrasings=(
                "A {tier} member bought {category} during the holiday window. How many days do "
                "they have to return it?",
                "With the holiday extension, how long is a {tier} member's return window on "
                "{category}, in days?",
                "{tier} tier, {category}, purchased inside the holiday window. Total return "
                "window in days?",
                "Add the holiday extension: how many days does a {tier} customer have to "
                "return {category} bought in the holiday window?",
                "Return window in days for {category} bought by a {tier} member during the "
                "holiday purchase window?",
            ),
            params=lambda p: [
                {"category": c, "tier": t} for c in _returnable_categories(p) for t in p.tiers
            ],
            gold=lambda p, a: str(
                p.return_window_days(a["category"], a["tier"], holiday_purchase=True)
            ),
            text_params=lambda p, a: {
                "category": p.category(a["category"]).name.lower(),
                "tier": p.tier(a["tier"]).name,
            },
        ),
        Template(
            id="cp_oversize_shipping",
            question_type="computation",
            answer_type="money",
            sections=("sec-shipping-fees",),
            phrasings=(
                "A {weight} lb order containing {category} ships to {zone} with no free "
                "shipping. What is the shipping fee in dollars?",
                "Work out shipping on {weight} lb of {category} going to {zone}, surcharge "
                "included where it applies.",
                "{category}, {weight} lb, {zone}, paying shipping. Fee in dollars?",
                "How much shipping is due on a {weight} lb {category} order to {zone}?",
                "Calculate the shipping fee, including any surcharge, for {weight} lb of "
                "{category} to {zone}.",
            ),
            params=lambda p: [
                {"category": c, "zone": z, "weight": w}
                for c in ("tents", "backpacks", "sleeping_bags")
                for z in p.zones
                for w in (18.0, 27.5, 40.0, 58.0)
            ],
            gold=lambda p, a: _money_str(p.shipping_fee_usd(a["zone"], a["weight"], a["category"])),
            text_params=lambda p, a: {
                "category": p.category(a["category"]).name.lower(),
                "zone": p.zones[a["zone"]].name,
                "weight": f"{a['weight']:g}",
            },
        ),
    ]


# ------------------------------------------------------------------------ exception templates


def _exception_templates() -> list[Template]:
    return [
        Template(
            id="ex_opened_electronics_window",
            question_type="exception",
            answer_type="number",
            sections=("sec-exceptions",),
            phrasings=(
                "A customer opened the factory seal on navigation electronics. How many days "
                "is their return window?",
                "Seal broken on a {tier} member's navigation electronics. Return window in days?",
                "Navigation electronics with the seal opened — how many days to return?",
                "What return window in days applies once the seal on navigation electronics is "
                "broken?",
                "The seal is broken on a GPS unit. How many days does the customer have?",
            ),
            params=lambda p: [{"tier": t} for t in p.tiers],
            gold=lambda p, a: str(p.opened_electronics_window_days()),
            text_params=lambda p, a: {"tier": p.tier(a["tier"]).name},
        ),
        Template(
            id="ex_opened_electronics_fee",
            question_type="exception",
            answer_type="number",
            sections=("sec-exceptions", "sec-loyalty"),
            phrasings=(
                "A {tier} member returns navigation electronics with the seal broken. What "
                "restocking percentage applies?",
                "Seal opened, navigation electronics, {tier} tier. Restocking fee percentage?",
                "What percent restocking fee is charged on opened navigation electronics "
                "returned by a {tier} member?",
                "{tier} customer, broken seal on a GPS unit. Restocking percentage?",
                "Restocking percentage for opened navigation electronics from a {tier} member?",
            ),
            params=lambda p: [{"tier": t} for t in p.tiers],
            gold=lambda p, a: str(p.restocking_fee_pct("electronics", a["tier"], opened_seal=True)),
            text_params=lambda p, a: {"tier": p.tier(a["tier"]).name},
        ),
        Template(
            id="ex_used_safety_gear",
            question_type="exception",
            answer_type="yes_no",
            sections=("sec-exceptions",),
            phrasings=(
                "A customer used {category} in the field and wants to return it. Is that "
                "allowed? Answer yes or no.",
                "Can {category} be returned after being used in the field? Yes or no.",
                "{category}, used outdoors, return requested. Permitted? Answer yes or no.",
                "Is a field-used {category} return accepted? Answer yes or no.",
                "Return of {category} that has been used in the field: yes or no?",
            ),
            params=lambda p: [
                {"category": c} for c in ("climbing_rope", "avalanche_safety", "tents", "backpacks")
            ],
            gold=lambda p, a: "yes" if p.can_return(a["category"], used_in_field=True) else "no",
            text_params=lambda p, a: {"category": p.category(a["category"]).name.lower()},
        ),
        Template(
            id="ex_worn_footwear_refund",
            question_type="exception",
            answer_type="string",
            sections=("sec-exceptions",),
            phrasings=(
                "Hiking footwear was worn outdoors and is being returned inside the window. How "
                "is the refund paid?",
                "A customer wore their boots outside. What form does their refund take?",
                "Worn-outdoors footwear, still in the window. Refund method?",
                "What refund method applies to hiking footwear worn outdoors?",
                "Boots worn on a trail, returned in time — how is the customer refunded?",
            ),
            params=lambda p: [{"variant": i} for i in range(5)],
            gold=lambda p, a: str(p.exceptions["worn_footwear"]["refund_method"]),
            aliases=lambda p, a: ("store credit", "credit", "store-credit"),
        ),
        Template(
            id="ex_final_sale_defect_days",
            question_type="exception",
            answer_type="number",
            sections=("sec-exceptions",),
            phrasings=(
                "Within how many days of delivery must a defect on a final-sale item be reported?",
                "A final-sale item arrived faulty. How many days does the customer have to "
                "report it?",
                "Defect reporting window for final-sale goods, in days?",
                "How many days after delivery can a defect on a final-sale item still be raised?",
                "Final sale, manufacturing defect — how many days to report it?",
            ),
            params=lambda p: [{"variant": i} for i in range(5)],
            gold=lambda p, a: str(p.exceptions["final_sale"]["defect_report_days"]),
        ),
        Template(
            id="ex_final_sale_window",
            question_type="exception",
            answer_type="number",
            sections=("sec-returns-windows", "sec-loyalty"),
            phrasings=(
                "A {tier} member bought {category}. How many days is their return window?",
                "{tier} tier, {category}. Return window in days?",
                "Does the tier bonus help a {tier} member returning {category}? Give the return "
                "window in days.",
                "Return window in days for {category} bought by a {tier} member?",
                "How long does a {tier} customer have to return {category}, in days?",
            ),
            params=lambda p: [
                {"category": c, "tier": t}
                for c, cat in p.categories.items()
                if cat.final_sale
                for t in p.tiers
            ],
            gold=lambda p, a: str(p.return_window_days(a["category"], a["tier"])),
            text_params=lambda p, a: {
                "category": p.category(a["category"]).name.lower(),
                "tier": p.tier(a["tier"]).name,
            },
        ),
        Template(
            id="ex_safety_escalation",
            question_type="exception",
            answer_type="enum",
            sections=("sec-escalation",),
            phrasings=(
                "A customer reports that a transceiver failed during an avalanche search and "
                "asks for a ${amount} refund. Which escalation level key handles it?",
                "Safety failure reported, refund of ${amount} requested. Give the escalation "
                "level key.",
                "${amount} refund and a safety complaint about equipment failure. Which level "
                "key takes the case?",
                "Route a ${amount} claim involving a safety-equipment failure. Escalation level "
                "key?",
                "A near miss is reported alongside a ${amount} refund request. Name the "
                "escalation level key.",
            ),
            params=lambda p: [{"amount": amount} for amount in (60, 150, 300, 700, 1500)],
            gold=lambda p, a: p.escalation_level_for(
                refund_usd=Decimal(str(a["amount"])), safety=True
            ),
            text_params=lambda p, a: {"amount": f"{a['amount']:g}"},
        ),
    ]


def all_templates() -> list[Template]:
    return (
        _lookup_templates()
        + _two_hop_templates()
        + _computation_templates()
        + _exception_templates()
    )


def _candidates(policy: Policy, template: Template) -> Iterator[tuple[dict[str, Any], int]]:
    """Every (parameter combination, phrasing) pair a template can produce."""
    for params in template.params(policy):
        for phrasing_index in range(len(template.phrasings)):
            yield params, phrasing_index


def _build(policy: Policy, template: Template, params: dict[str, Any], phrasing: int) -> DemoItem:
    text_params = template.text_params(policy, params) if template.text_params else dict(params)
    question = _fix_articles(template.phrasings[phrasing].format(**text_params))
    payload = json.dumps(
        {"t": template.id, "p": params, "ph": phrasing}, sort_keys=True, default=str
    )
    item_id = f"{template.id}-{hashlib.sha256(payload.encode()).hexdigest()[:10]}"
    return DemoItem(
        id=item_id,
        question=question,
        gold=template.gold(policy, params),
        answer_type=template.answer_type,
        question_type=template.question_type,
        sections=template.sections,
        template_id=template.id,
        params=params,
        aliases=template.aliases(policy, params) if template.aliases else (),
    )


def generate(
    policy: Policy, *, size: int = DEFAULT_SIZE, seed: int = DEFAULT_SEED
) -> list[DemoItem]:
    """Generate ``size`` items in the target type mix, deterministically.

    Sampling is stratified by question type first (so the mix is exact, not approximate), then
    spread across the templates of that type so no single template dominates its stratum.
    """
    rng = random.Random(seed)
    templates_by_type: dict[str, list[Template]] = {}
    for template in all_templates():
        templates_by_type.setdefault(template.question_type, []).append(template)

    targets = {qt: round(size * share) for qt, share in TYPE_MIX.items()}
    drift = size - sum(targets.values())
    targets["lookup"] += drift  # absorb rounding in the largest stratum

    items: list[DemoItem] = []
    for question_type, target in targets.items():
        templates = templates_by_type[question_type]
        pool: list[tuple[Template, dict[str, Any], int]] = []
        for template in templates:
            candidates = list(_candidates(policy, template))
            rng.shuffle(candidates)
            pool.extend((template, params, phrasing) for params, phrasing in candidates)
        if len(pool) < target:
            raise ValueError(
                f"only {len(pool)} distinct {question_type} questions are available but "
                f"{target} were asked for; add templates or parameters"
            )
        # Round-robin across templates so one large parameter space cannot swamp the stratum.
        by_template: dict[str, list[tuple[Template, dict[str, Any], int]]] = {}
        for entry in pool:
            by_template.setdefault(entry[0].id, []).append(entry)
        order = sorted(by_template)
        # Deduplicate by question *text*: some templates take parameters that do not reach
        # every phrasing, and two identical questions are not two tasks. Counting them twice
        # would inflate n and make the bootstrap treat one observation as two.
        seen: set[str] = set()
        chosen: list[DemoItem] = []
        index = 0
        while len(chosen) < target:
            progressed = False
            for template_id in order:
                bucket = by_template[template_id]
                if index >= len(bucket):
                    continue
                progressed = True
                candidate = _build(policy, *bucket[index])
                if candidate.question in seen or leaks_own_answer(candidate):
                    continue
                seen.add(candidate.question)
                chosen.append(candidate)
                if len(chosen) == target:
                    break
            if not progressed:
                break
            index += 1
        if len(chosen) < target:
            raise ValueError(
                f"only {len(chosen)} distinct {question_type} questions could be built but "
                f"{target} were asked for; add phrasings or parameters"
            )
        items.extend(chosen)

    items.sort(key=lambda i: i.id)
    rng.shuffle(items)
    return items


@dataclass(frozen=True)
class Split:
    """A stratified calibration/test split with a fixed seed."""

    calibration: tuple[DemoItem, ...]
    test: tuple[DemoItem, ...]
    seed: int

    def hashes(self) -> dict[str, str]:
        def digest(items: tuple[DemoItem, ...]) -> str:
            payload = json.dumps([i.id for i in items], sort_keys=True)
            return hashlib.sha256(payload.encode()).hexdigest()[:16]

        return {"calibration": digest(self.calibration), "test": digest(self.test)}


def split_items(
    items: list[DemoItem], *, calibration_size: int = DEFAULT_CALIBRATION, seed: int = DEFAULT_SEED
) -> Split:
    """Stratify by answer type so both splits see the same mix of question shapes."""
    rng = random.Random(seed + 1)
    by_type: dict[str, list[DemoItem]] = {}
    for item in sorted(items, key=lambda i: i.id):
        by_type.setdefault(item.question_type, []).append(item)

    calibration: list[DemoItem] = []
    test: list[DemoItem] = []
    fraction = calibration_size / len(items)
    for question_type in sorted(by_type):
        bucket = by_type[question_type]
        rng.shuffle(bucket)
        take = round(len(bucket) * fraction)
        calibration.extend(bucket[:take])
        test.extend(bucket[take:])

    # Rounding can leave the calibration split a task or two off; move the difference across.
    while len(calibration) > calibration_size:
        test.append(calibration.pop())
    while len(calibration) < calibration_size and test:
        calibration.append(test.pop())

    calibration.sort(key=lambda i: i.id)
    test.sort(key=lambda i: i.id)
    return Split(tuple(calibration), tuple(test), seed)
