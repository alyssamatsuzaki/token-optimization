"""Render the policy as a support handbook (SPEC.md section 6).

Deterministic: the same policy always produces byte-identical Markdown. That matters more than
it sounds, because the handbook is the cached prefix. A renderer that reordered a dict would
change the prefix, invalidate the provider cache on every run, and quietly destroy the very
saving the product is measuring.

Section IDs are stable and are what a model is asked to cite, so the scorer can check a quoted
piece of evidence against the section the answer itself points at.

Size: the prefix has to clear every tier's minimum cacheable length by at least 20%. Haiku 4.5
has the highest minimum (4,096 tokens) and counts on the older tokenizer, so it sets the floor.
``handbook_size_report`` measures it and ``tokop fixtures-check`` asserts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tokop.workloads.demo.policy import Policy

SECTION_IDS = (
    "sec-overview",
    "sec-definitions",
    "sec-loyalty",
    "sec-returns-windows",
    "sec-category-notes",
    "sec-restocking",
    "sec-warranty",
    "sec-shipping-zones",
    "sec-shipping-fees",
    "sec-free-shipping",
    "sec-exceptions",
    "sec-seasonal",
    "sec-escalation",
    "sec-packaging",
    "sec-procedure",
    "sec-common-mistakes",
    "sec-tone",
)


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _overview(policy: Policy) -> str:
    c = policy.company
    return f"""## Overview {{#sec-overview}}

{c["name"]} has sold backcountry equipment since {c["founded"]}. This handbook is policy version
{c["policy_version"]}, effective {c["effective_date"]}, and it is the only document support
agents may quote from when answering a customer. Where a customer's situation is not covered
here, the case is escalated rather than decided; see section sec-escalation.

Questions arrive by email at {c["support_email"]} and through the storefront chat. Every answer
must state the rule it relies on. An answer that cannot point at a section of this handbook is
not an answer, it is a guess, and a guess about a refund is a complaint waiting to happen."""


def _loyalty(policy: Policy) -> str:
    rows = "\n".join(
        f"| {t.name} | {_money(t.annual_fee_usd)} | {t.return_window_bonus_days} days | "
        f"{_money(t.free_shipping_threshold_usd)} | "
        f"{'waived' if t.restocking_fee_waived else 'charged'} | "
        f"{'yes' if t.priority_support else 'no'} |"
        for t in policy.tiers.values()
    )
    return f"""## Loyalty tiers {{#sec-loyalty}}

Every customer sits in one of three tiers. The tier changes three things: how long they have to
return something, how much they must spend before shipping is free, and whether a restocking fee
is charged at all.

| Tier | Annual fee | Return window bonus | Free shipping from | Restocking fee | Priority support |
| --- | --- | --- | --- | --- | --- |
{rows}

The return window bonus is added to the category's base window. It is additive, not a multiplier,
and it never applies to a final-sale category, whose window is zero days regardless of tier.

A Alpine member never pays a restocking fee, on any category, in any circumstance covered by this
handbook. This is the single most common source of disputed charges, so check the tier before
quoting a fee."""


def _return_windows(policy: Policy) -> str:
    rows = "\n".join(
        f"| {c.name} | `{c.key}` | {c.base_return_days} days | {'yes' if c.final_sale else 'no'} |"
        for c in policy.categories.values()
    )
    return f"""## Return windows by category {{#sec-returns-windows}}

The base return window runs from the delivery date, not the order date. A return is counted as
started on the day the customer requests a return authorisation, not the day the parcel arrives
back at the warehouse.

| Category | Key | Base return window | Final sale |
| --- | --- | --- | --- |
{rows}

To work out a customer's actual window: take the base window from this table, add the tier bonus
from section sec-loyalty, and then add the seasonal extension from section sec-seasonal if the
purchase falls inside the holiday window and the category is not excluded."""


def _restocking(policy: Policy) -> str:
    rows = "\n".join(f"| {c.name} | {c.restocking_fee_pct}% |" for c in policy.categories.values())
    return f"""## Restocking fees {{#sec-restocking}}

A restocking fee is charged on the item price when a customer returns an item that is not
defective. It is never charged on a defect return, and never charged to an Alpine member.

| Category | Restocking fee |
| --- | --- |
{rows}

The fee is a percentage of the price the customer paid, not of the list price, and it is
deducted from the refund rather than invoiced separately. A refund is therefore the price paid
minus the restocking fee. Round to the nearest cent."""


def _warranty(policy: Policy) -> str:
    rows = "\n".join(
        f"| {c.name} | {c.warranty_years} years | {c.warranty_covers} |"
        for c in policy.categories.values()
    )
    return f"""## Warranty terms {{#sec-warranty}}

The warranty is separate from the return window and runs from the purchase date. A warranty
claim outside the return window is still valid; a return request outside the return window is
not, even if the item is under warranty. The two are commonly confused by customers and must not
be confused by agents.

| Category | Warranty period | What it covers |
| --- | --- | --- |
{rows}

Wear from ordinary use is never a warranty defect. A rope that has been climbed on, a sole that
has been walked down, and a stove that has been run for three seasons are all working as
expected, not failing."""


def _shipping_zones(policy: Policy) -> str:
    rows = "\n".join(
        f"| {z.name} | `{z.key}` | {z.transit_days} business days |" for z in policy.zones.values()
    )
    return f"""## Shipping zones {{#sec-shipping-zones}}

| Zone | Key | Transit time |
| --- | --- | --- |
{rows}

Transit time is counted in business days from despatch, and despatch is the business day after
the order is placed. A Zone 3 order placed on a Monday therefore arrives on the following
Monday, not the preceding Friday."""


def _shipping_fees(policy: Policy) -> str:
    bands = policy.weight_bands
    header = " | ".join(b.label for b in bands)
    divider = " | ".join("---" for _ in bands)
    rows = "\n".join(
        "| "
        + policy.zones[zone].name
        + " | "
        + " | ".join(_money(policy.fees[zone][b.id]) for b in bands)
        + " |"
        for zone in policy.zones
    )
    oversize = ", ".join(policy.categories[k].name for k in policy.oversize_applies_to)
    return f"""## Shipping fees {{#sec-shipping-fees}}

Fees are charged per order, by the total shipped weight, not per item. Weight bands are
inclusive of their upper bound: a parcel weighing exactly 5 lb falls in the "over 2 lb up to
5 lb" band, not the next one up.

| Zone | {header} |
| --- | {divider} |
{rows}

An oversize surcharge of {_money(policy.oversize_surcharge)} is added when the order weighs more
than {policy.oversize_threshold_lb:g} lb and contains an item from {oversize}. The surcharge is
added once per order, not once per item.

Return shipping is paid by the customer unless the return is for a manufacturing defect, in
which case {policy.company["name"]} pays it."""


def _free_shipping(policy: Policy) -> str:
    rows = "\n".join(
        f"| {t.name} | {_money(t.free_shipping_threshold_usd)} |" for t in policy.tiers.values()
    )
    return f"""## Free shipping thresholds {{#sec-free-shipping}}

| Tier | Order total for free shipping |
| --- | --- |
{rows}

The threshold is compared against the order total before tax and after any discount. An order
that reaches the threshold ships free in every zone, including Zone 5, and the oversize
surcharge is waived with it. An Alpine member's threshold is zero, so their shipping is always
free."""


def _exceptions(policy: Policy) -> str:
    parts = []
    for key in ("final_sale", "opened_electronics", "used_climbing_safety", "worn_footwear"):
        exception = policy.exceptions[key]
        names = ", ".join(policy.categories[c].name for c in exception["applies_to_categories"])
        parts.append(
            f"**{exception['id']}** — applies to {names}.\n\n{' '.join(exception['rule'].split())}"
        )
    body = "\n\n".join(parts)
    return f"""## Exceptions {{#sec-exceptions}}

An exception overrides the general rule for the categories it names. Where two exceptions could
apply, the more restrictive one wins.

{body}

An exception never widens a window; it only narrows one or changes how the refund is paid."""


def _seasonal(policy: Policy) -> str:
    s = policy.seasonal
    excluded = ", ".join(policy.categories[c].name for c in s["excluded_categories"])
    return f"""## Seasonal return extension {{#sec-seasonal}}

**{s["name"]}** ({s["id"]}). Orders placed between {s["purchase_window_start"]} and
{s["purchase_window_end"]} inclusive receive {s["extra_days"]} additional days on top of the
window the customer would otherwise have had.

The extension does not apply to {excluded}. It stacks with the loyalty tier bonus: a Summit
member buying a backpack inside the holiday window gets the base window, plus the tier bonus,
plus the extension."""


def _escalation(policy: Policy) -> str:
    rows = "\n".join(
        f"| {level['name']} | `{level['id']}` | {level['handles']} | "
        f"{level['resolution_target_hours']} hours |"
        for level in policy.escalation["levels"]
    )
    threshold = Decimal(str(policy.escalation["manager_threshold_usd"]))
    return f"""## Escalation {{#sec-escalation}}

| Level | Key | Handles | Resolution target |
| --- | --- | --- | --- |
{rows}

A refund above {_money(threshold)} goes to the customer relations manager regardless of how
simple the case looks. Any complaint that mentions an injury, a failure of safety equipment, or
a near miss goes straight to the customer relations manager and is never handled at the front
line, whatever the amount involved.

A case with no customer response for
{policy.escalation["auto_escalate_after_hours"]} hours escalates automatically to the next
level."""


def _packaging(policy: Policy) -> str:
    needs = [c for c in policy.categories.values() if c.requires_original_packaging]
    does_not = [c for c in policy.categories.values() if not c.requires_original_packaging]
    needs_names = ", ".join(c.name for c in needs)
    other_names = ", ".join(c.name for c in does_not)
    return f"""## Original packaging {{#sec-packaging}}

These categories must come back in their original packaging for a return to be accepted:
{needs_names}.

These do not: {other_names}.

Missing packaging on a category that requires it is not an automatic refusal. Offer the customer
the return at the category's restocking fee plus a flat repackaging charge, and if they dispute
it, escalate to the returns specialist rather than arguing the point."""


def _definitions(policy: Policy) -> str:
    return """## Definitions {#sec-definitions}

**Delivery date.** The date the carrier records the parcel as delivered. Every return window is
counted from this date, never from the order date and never from the despatch date.

**Purchase date.** The date the order was placed. Warranty periods and the seasonal extension
are both keyed to this date, which is why a customer can be inside their warranty and outside
their return window at the same time.

**Item price.** What the customer actually paid for the item, after any discount and before
tax. Restocking fees are a percentage of this figure, not of the list price.

**Order total.** The sum of item prices in one order, after discounts and before tax. This is
the figure compared against the free-shipping threshold.

**Shipped weight.** The total weight of the parcel or parcels in one order. Shipping fees are
charged once per order on this total, not per item.

**Defect.** A failure the manufacturer is responsible for, listed per category in section
sec-warranty. Wear from ordinary use is not a defect.

**Return authorisation.** The reference issued when a return is approved. The window is met if
the authorisation is requested inside it, even if the parcel is posted later.

**Final sale.** A category that cannot be returned at all except for a defect reported quickly;
see section sec-exceptions."""


def _category_notes(policy: Policy) -> str:
    notes = {
        "tents": "Poles and stakes must all be present. A tent pitched once is still returnable; "
        "a tent with ground-in dirt or a repaired panel is not.",
        "sleeping_bags": "Bags must be returned uncompressed. A bag stored compressed for months "
        "loses loft, and that loss is not a defect.",
        "backpacks": "The longest window in the catalogue and no restocking fee, because fit is "
        "hard to judge indoors. Load the pack before deciding, and keep the hip belt clean.",
        "footwear": "Fit problems are the most common reason for return. Encourage customers to "
        "walk indoors first: worn outdoors, the refund becomes store credit.",
        "outerwear": "Insulation migration is a defect; a jacket that is simply warmer or colder "
        "than the customer expected is not.",
        "climbing_hardware": "Inspect for anodising scratches and rope grooves. Any hardware "
        "showing load marks is treated as used and is refused.",
        "climbing_rope": "The shortest window in the catalogue and the highest restocking fee. "
        "A rope uncoiled but never loaded may be returned; a rope climbed on may not.",
        "electronics": "Check the seal before quoting anything. An intact seal follows the "
        "category rule; a broken one follows the exception in section sec-exceptions.",
        "stoves": "Fuel canisters are never returnable and never shipped. A stove run once for a "
        "test burn is returnable if it is clean.",
        "water_filters": "A filter that has passed water cannot be returned for hygiene reasons "
        "unless the housing itself is cracked, which is a defect.",
        "avalanche_safety": "Treat every claim as potentially safety-related. A transceiver that "
        "failed in the field goes to the customer relations manager, not the front line.",
        "nutrition": "Final sale. Consumables cannot be restocked, and there is no warranty to "
        "claim against.",
        "clearance_apparel": "Final sale with a zero-day window. Customers often assume the "
        "loyalty bonus rescues this; it does not.",
    }
    rows = "\n\n".join(
        f"**{policy.categories[key].name}.** {note}"
        for key, note in notes.items()
        if key in policy.categories
    )
    return f"""## Category notes {{#sec-category-notes}}

These notes do not change the windows or fees in the tables above. They record what agents most
often get wrong about each category.

{rows}"""


def _procedure(policy: Policy) -> str:
    return f"""## Authorising a return {{#sec-procedure}}

Work through these steps in order. Skipping to the fee is the most common cause of a reversal.

1. **Establish the category.** The category decides the base window, the restocking fee, the
   warranty and whether original packaging is required. Ask what the item is before anything
   else.

2. **Check for a final-sale category.** If the category is final sale, the window is zero days
   and the only route is a defect reported inside the exception's reporting period. Stop here
   and say so plainly rather than quoting a window that does not exist.

3. **Check for an applicable exception.** Section sec-exceptions lists four. Where two could
   apply, the more restrictive one wins. An exception never widens a window.

4. **Establish the tier.** The tier adds days to the window, lowers or removes the restocking
   fee, and sets the free-shipping threshold. An Alpine member never pays a restocking fee.

5. **Check the purchase date against the seasonal window.** If the order falls inside it and the
   category is not excluded, add the extension. The extension stacks with the tier bonus.

6. **Add it up.** Base window, plus tier bonus, plus seasonal extension where it applies. Compare
   against the delivery date, not today's order date.

7. **Work out the refund.** Item price minus the restocking fee, rounded to the nearest cent.
   Return shipping comes off as well unless the return is for a defect, in which case
   {policy.company["name"]} pays it.

8. **Check the escalation triggers.** A refund above the manager threshold, or any mention of
   injury or a safety failure, leaves the front line regardless of how simple the case appears.

**Record the reasoning, not only the outcome.** A case note that says "return approved" tells the
next agent nothing when the customer writes back. A note that says which category you established,
which exception you applied and which date you measured from can be checked in seconds, and it is
what lets a reversal be traced to the step that went wrong rather than to the agent who happened to
send the email.

**When two rules appear to conflict, name both before you choose.** This handbook is written so
that the more restrictive rule wins, and an exception never widens a window. Saying so in the
answer — "the seasonal extension would normally apply, but the category is excluded from it" —
turns an apparent contradiction into a decision the customer can follow. Silently applying the
rule you happened to check first is how the same case gets two different answers from two agents.

**Do not re-open a case to correct a smaller figure than the reversal costs.** If the refund you
quoted is within a cent of the correct amount, honour what you quoted and note the discrepancy.
Correcting it costs a second contact, a second review and the customer's confidence, and buys back
less than any of them."""


def _common_mistakes(policy: Policy) -> str:
    return """## Common mistakes {#sec-common-mistakes}

**Confusing the warranty with the return window.** They start on different dates and run for
different lengths. A five-year warranty does not mean a five-year return window.

**Quoting a restocking fee to an Alpine member.** The fee is waived for them everywhere. Check
the tier before the fee, every time.

**Applying the loyalty bonus to a final-sale category.** Final sale means a zero-day window, and
no bonus and no seasonal extension reaches it.

**Charging the oversize surcharge per item.** It is charged once per order, and only when the
order both exceeds the weight threshold and contains a qualifying item.

**Reading the weight bands as exclusive.** Each band includes its upper bound. A parcel weighing
exactly the boundary weight falls in the lower band.

**Comparing the free-shipping threshold against the total after tax.** It is compared before
tax and after any discount.

**Treating a broken seal as a refusal.** Opened navigation electronics are still returnable, on
a shorter window and at a higher fee. Refusing them outright generates a complaint the returns
specialist then has to undo.

**Deciding a safety complaint at the front line.** Anything involving injury or a failure of
safety equipment goes to the customer relations manager, whatever the refund is worth.

**Measuring the window from the order date.** Every window in this handbook runs from delivery.
An order placed well before it shipped is the case where this goes wrong most often, and it goes
wrong in the customer's favour, which means nobody notices until the pattern shows up in a review.

**Stacking two exceptions.** Where more than one exception could apply, the more restrictive one
wins and the others are not applied on top of it. Exceptions narrow; they do not accumulate.

**Assuming a defect claim needs no evidence.** A defect changes who pays return shipping and can
reopen a window that has closed, so it is the one claim worth a photograph. Ask for one before
the case is decided rather than after the refund is quoted.

**Promising a refund date.** This handbook sets what is owed, not when the payment processor
settles it. Quoting a settlement date the processor has not confirmed converts a resolved case
into a second contact."""


def _tone(policy: Policy) -> str:
    return f"""## Answering a customer {{#sec-tone}}

Lead with the answer. A customer asking how long they have to return a jacket wants the number
of days first and the reasoning second, not a paragraph of context ending in a figure. Agents
who bury the answer generate a second email, and a second email costs more than the first.

Name the rule. Every answer should say which part of this handbook it rests on, in plain words:
"backpacks have a sixty-day window and your Summit membership adds fourteen" is better than
"our system shows seventy-four days", because the customer can check the first and can only
trust the second.

Do not soften a refusal into ambiguity. If a final-sale item cannot come back, say that it
cannot come back and say why. "I'm not sure we can do that" reads as an opening to negotiate,
and the negotiation lands on a colleague.

Do not invent goodwill. Waiving a fee, extending a window or covering return shipping outside
the rules here is a decision for the returns specialist, not the front line. Offer to escalate
instead; section sec-escalation says who takes it.

Quote figures exactly. Round money to the nearest cent and windows to whole days. A refund
quoted as "about ninety dollars" that arrives as $87.30 is a complaint; the same refund quoted
as $87.30 is a resolved case.

Assume the customer has read something contradictory. Older versions of this handbook circulate
on forums, and the tiers changed in policy version {policy.company["policy_version"]}. If a
customer quotes a rule that is not in this document, tell them what the current rule is and
when it took effect, which was {policy.company["effective_date"]}.

Answer the question that was asked before the one you expect. A customer who asks only whether an
item can come back does not need the restocking fee, the shipping arithmetic and the packaging
requirement in the same message. Answer what they asked, then offer the rest: "yes, and I can work
out the refund if you tell me what you paid" leaves them in control of how much detail arrives.

Write so the answer survives being forwarded. Support emails get pasted into group chats and
attached to disputes, usually without the question above them. An answer that names the item
category, the window and the date it runs from still makes sense on its own; one that says "in
that case it's thirty days" does not, and it is the second that comes back as a complaint."""


@dataclass(frozen=True)
class SizeReport:
    """How the handbook measures against each model's minimum cacheable prefix."""

    tokens_by_model: dict[str, int]
    minimums: dict[str, int]
    headroom: dict[str, float]
    characters: int

    @property
    def clears_every_minimum_by_20_percent(self) -> bool:
        return all(h >= 0.20 for h in self.headroom.values())

    def failures(self) -> list[str]:
        return [
            f"{model}: {self.tokens_by_model[model]:,} tokens against a "
            f"{self.minimums[model]:,}-token minimum ({self.headroom[model]:.0%} headroom)"
            for model, head in self.headroom.items()
            if head < 0.20
        ]


def render_handbook(policy: Policy) -> str:
    """The whole handbook, in a fixed section order."""
    sections = [
        _overview(policy),
        _definitions(policy),
        _loyalty(policy),
        _return_windows(policy),
        _category_notes(policy),
        _restocking(policy),
        _warranty(policy),
        _shipping_zones(policy),
        _shipping_fees(policy),
        _free_shipping(policy),
        _exceptions(policy),
        _seasonal(policy),
        _escalation(policy),
        _packaging(policy),
        _procedure(policy),
        _common_mistakes(policy),
        _tone(policy),
    ]
    title = f"# {policy.company['name']} support handbook\n"
    return title + "\n\n" + "\n\n".join(sections) + "\n"


def section_text(handbook: str, section_id: str) -> str:
    """The text of one section, used by the scorer to check a quoted piece of evidence.

    The scorer compares a quote against the section the *answer itself* cites. It never sees a
    gold answer, which is what keeps it a scorer rather than a second grader (SPEC.md 7.5).
    """
    marker = f"{{#{section_id}}}"
    start = handbook.find(marker)
    if start < 0:
        return ""
    line_start = handbook.rfind("\n## ", 0, start)
    body_start = handbook.find("\n", start) + 1
    next_section = handbook.find("\n## ", body_start)
    end = next_section if next_section > 0 else len(handbook)
    heading = handbook[line_start + 1 : start] if line_start >= 0 else ""
    return (heading + handbook[body_start:end]).strip()


def handbook_size_report(
    handbook: str, minimums: dict[str, int], ratios: dict[str, float]
) -> SizeReport:
    """Measure the handbook against each model's minimum cacheable length.

    ``ratios`` converts the base counter's tokens into each model's own count, which is what
    makes Haiku 4.5 the binding constraint: the highest minimum *and* the tokenizer that counts
    lowest.
    """
    from tokop.core.tokenize import get_base_counter

    base_tokens = get_base_counter().count(handbook)
    tokens_by_model = {model: round(base_tokens * ratios[model]) for model in minimums}
    headroom = {
        model: (tokens_by_model[model] - minimum) / minimum if minimum else float("inf")
        for model, minimum in minimums.items()
    }
    return SizeReport(
        tokens_by_model=tokens_by_model,
        minimums=minimums,
        headroom=headroom,
        characters=len(handbook),
    )
