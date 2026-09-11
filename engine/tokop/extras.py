"""The Compare and Brief examples (SPEC.md section 6 step 6, 5.2, 5.3).

Recorded the same way as everything else — through the adapter, into cassettes, costed from a
price snapshot — so the Compare and Brief screens display engine output rather than a fixture
somebody typed. In this build the provider underneath is the deterministic simulator, and every
surface says so.

The answers here are written out rather than generated because they are *illustrations of a
screen*, not measurements: nothing downstream computes a claim from them. The tokens, costs,
latencies and time-to-first-token around them are computed by the same code that prices a real
call.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokop.adapters.base import LLMRequest, blocks_of, user_message
from tokop.adapters.cassette import CassetteStore
from tokop.adapters.factory import simulated_profiles
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.registry import Registry
from tokop.core.tokenize import get_base_counter

COMPARE_SYSTEM = (
    "You are answering a single question as directly as you can. Give the answer first, then "
    "at most two sentences of justification."
)

#: One short reasoning prompt and one extraction prompt, as SPEC.md section 6 asks for.
COMPARE_PROMPTS: dict[str, dict[str, str]] = {
    "reasoning": {
        "title": "Short reasoning",
        "prompt": (
            "A team runs 40,000 support answers a month. Each answer sends a 7,000-token "
            "handbook that never changes, plus a 60-token question, and gets back about 220 "
            "tokens. They are on a model that charges $5 per million input tokens, $25 per "
            "million output, and $0.50 per million for a cache read. What is the single "
            "largest saving available to them, and roughly what is it worth per month?"
        ),
    },
    "extraction": {
        "title": "Extraction",
        "prompt": (
            "Extract every monetary amount and what it refers to from this note, as JSON:\n\n"
            '"Order 88-1042 shipped to Zone 3 at 14 lb, so shipping was $29.95. The customer '
            "returned a $248.75 tent outside the window; we waived the 10% restocking fee as "
            'goodwill but kept the $24.00 oversize surcharge. Net refund issued: $248.75."'
        ),
    },
}

#: What each simulated model replies. Written out because these illustrate a screen; nothing
#: computes a claim from them.
COMPARE_ANSWERS: dict[str, dict[str, str]] = {
    "reasoning": {
        "claude-opus-5": (
            "Move the handbook in front of a cache breakpoint.\n\n"
            "The handbook is 7,000 identical tokens on every one of 40,000 calls. Uncached that "
            "is 280M input tokens a month at $5/M, or $1,400. Read from cache at $0.50/M it is "
            "$140, so the saving is about $1,260 a month, before the one-off write cost. "
            "Everything else on the bill — the 60-token question, the 220-token answer — is "
            "rounding error next to it."
        ),
        "claude-sonnet-5": (
            "Prompt caching on the handbook.\n\n"
            "7,000 tokens x 40,000 calls = 280M input tokens/month. At $5/M that is $1,400; "
            "cached reads at $0.50/M cost $140. Saving roughly $1,260/month. Capping the output "
            "would save a little more but far less than the cache."
        ),
        "claude-haiku-4-5-20251001": (
            "Cache the handbook. 7,000 tokens x 40,000 = 280,000,000 tokens. At $5 per million "
            "that is $1,400 per month, and cache reads at $0.50 per million would be $140, so "
            "you save around $1,260. You could also shorten the answers."
        ),
    },
    "extraction": {
        "claude-opus-5": json.dumps(
            {
                "amounts": [
                    {"value": 29.95, "refers_to": "shipping, Zone 3 at 14 lb"},
                    {"value": 248.75, "refers_to": "tent price"},
                    {"value": 24.00, "refers_to": "oversize surcharge, kept"},
                    {"value": 248.75, "refers_to": "net refund issued"},
                ],
                "waived": "10% restocking fee",
            },
            indent=2,
        ),
        "claude-sonnet-5": json.dumps(
            {
                "amounts": [
                    {"value": 29.95, "refers_to": "shipping"},
                    {"value": 248.75, "refers_to": "tent"},
                    {"value": 24.00, "refers_to": "oversize surcharge"},
                    {"value": 248.75, "refers_to": "net refund"},
                ],
            },
            indent=2,
        ),
        "claude-haiku-4-5-20251001": json.dumps(
            {
                "amounts": [
                    {"value": 29.95, "refers_to": "shipping"},
                    {"value": 248.75, "refers_to": "tent returned"},
                    {"value": 24.0, "refers_to": "oversize surcharge"},
                ]
            },
            indent=2,
        ),
    },
}

SYNTHESIS = {
    "agreement": [
        "All three name prompt caching on the handbook as the largest saving.",
        "All three compute the uncached cost as $1,400 a month and the cached cost as $140.",
    ],
    "disagreement": [
        "Opus 5 and Sonnet 5 note the one-off cache write cost; Haiku 4.5 does not.",
    ],
    "unique": [
        "Opus 5 is the only one to say explicitly that the question and answer tokens are "
        "rounding error next to the prefix.",
    ],
    "likely_errors": [
        "None of the three states that a 5-minute cache write costs 1.25x base input, so the "
        "first call of each window is more expensive than the arithmetic implies.",
    ],
    "final": (
        "Move the handbook in front of a cache breakpoint. It is 280M identical input tokens a "
        "month; cached reads cut that from about $1,400 to about $140, a saving near $1,260 a "
        "month before write costs."
    ),
}

BRIEF_SOURCE_SECTIONS = ("sec-loyalty", "sec-returns-windows", "sec-restocking", "sec-exceptions")

BRIEF_TEXT = """# Returns brief

**Windows.** Base window is by category, counted from delivery. Backpacks 60 days, tents /
sleeping bags / outerwear / stoves 45, footwear / climbing hardware / electronics / water
filters / avalanche 30, climbing rope 14. Nutrition and clearance apparel are final sale, 0.
[sec-returns-windows]

**Tier bonus, added to the base.** Standard +0, Summit +14, Alpine +30. Never applies to a
final-sale category. [sec-loyalty]

**Seasonal.** Orders placed 2026-11-01 to 2026-12-24 get +30 days. Excludes nutrition,
clearance apparel and climbing rope. Stacks with the tier bonus. [sec-seasonal]

**Restocking, on non-defective returns only.** Climbing rope 20%, climbing hardware and
electronics 15%, tents / sleeping bags / stoves / water filters 10%, everything else 0%.
**Alpine members pay none, ever.** [sec-restocking]

**Exceptions.** Opened electronics: 14-day window and 25% fee. Used climbing rope and avalanche
gear: no return at all. Footwear worn outdoors: store credit only. Final sale: defect must be
reported within 7 days. More restrictive exception wins. [sec-exceptions]

**Escalation.** Refunds over $500, or any mention of injury or safety-equipment failure, go to
the customer relations manager. [sec-escalation]
"""


@dataclass
class ExtraExamples:
    """Recorded Compare and Brief examples."""

    compares: list[dict[str, Any]]
    brief: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"compares": self.compares, "brief": self.brief}


def record_extras(
    registry: Registry,
    cassette_dir: Path,
    *,
    origin: str = "simulated",
    taken: date | None = None,
) -> ExtraExamples:
    """Record the Compare and Brief examples through the normal adapter path."""
    snapshot = registry.snapshot(list(registry.roles.values()), taken or date(2026, 9, 11))
    models = [registry.roles[r] for r in ("frontier", "mid", "cheap")]
    store = CassetteStore(cassette_dir)
    counter = get_base_counter()

    def adapter_for(answers: dict[str, str]) -> RecordingAdapter:
        def responder(request: LLMRequest) -> str:
            return answers.get(request.model, "")

        return RecordingAdapter(
            SimulatedAdapter(simulated_profiles(registry, list(registry.models)), responder),
            store,
            origin=origin,
        )

    compares: list[dict[str, Any]] = []
    for key, spec in COMPARE_PROMPTS.items():
        answers = COMPARE_ANSWERS[key]
        adapter = adapter_for(answers)
        columns: list[dict[str, Any]] = []
        for model_id in models:
            request = LLMRequest(
                provider="simulated",
                model=model_id,
                system=blocks_of(COMPARE_SYSTEM),
                messages=[user_message(spec["prompt"])],
                max_tokens=600,
            )
            response = asyncio.run(adapter.complete(request))
            cost = snapshot.cost(model_id, response.usage)
            columns.append(
                {
                    "model_id": model_id,
                    "display_name": registry.model(model_id).display_name,
                    "scarce": registry.is_scarce(model_id),
                    "text": response.text,
                    "usage": {
                        bucket: {"tokens": value, "source": source}
                        for bucket, value, source in response.usage.bucket_items()
                    },
                    "input_tokens": response.usage.total_input,
                    "output_tokens": response.usage.total_output,
                    "cost_usd": str(cost.total),
                    "cost_formula": cost.formula(),
                    "ttft_ms": response.ttft_ms,
                    "latency_ms": response.latency_ms,
                    "origin": origin,
                }
            )
        compares.append(
            {
                "id": key,
                "title": spec["title"],
                "prompt": spec["prompt"],
                "system": COMPARE_SYSTEM,
                "columns": columns,
                "synthesis": SYNTHESIS if key == "reasoning" else None,
                "synthesis_model": models[0] if key == "reasoning" else None,
                "manual_column": {
                    "note": (
                        "Subscription apps have no public API, so they appear as a manual "
                        "column: copy the prompt, paste the answer back. Tokens are estimated "
                        "and the cost shows as 'subscription' rather than a dollar figure."
                    ),
                },
            }
        )

    # The Brief: a cheap model compressing long material, costed like any other call.
    from tokop.workloads.demo.handbook import render_handbook, section_text
    from tokop.workloads.demo.policy import load_policy

    handbook = render_handbook(load_policy())
    source = "\n\n".join(section_text(handbook, s) for s in BRIEF_SOURCE_SECTIONS)
    brief_model = registry.roles["cheap"]
    brief_adapter = adapter_for({brief_model: BRIEF_TEXT})
    brief_request = LLMRequest(
        provider="simulated",
        model=brief_model,
        system=blocks_of(
            "Compress the material into the shortest brief that preserves every rule and "
            "number, with a pointer back to the source section for each. Drop prose."
        ),
        messages=[user_message(source)],
        max_tokens=1200,
    )
    brief_response = asyncio.run(brief_adapter.complete(brief_request))
    brief_cost = snapshot.cost(brief_model, brief_response.usage)
    tokens_before = counter.count(source)
    tokens_after = counter.count(brief_response.text)

    brief = {
        "model_id": brief_model,
        "display_name": registry.model(brief_model).display_name,
        "source_sections": list(BRIEF_SOURCE_SECTIONS),
        "source_preview": source[:1200],
        "source_chars": len(source),
        "brief": brief_response.text,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "reduction": 1 - (tokens_after / tokens_before) if tokens_before else 0.0,
        "cost_usd": str(brief_cost.total),
        "cost_formula": brief_cost.formula(),
        "token_counter": counter.name,
        "lossy_note": (
            "A brief is lossy by construction. It keeps the rules and the numbers and throws "
            "away the prose that explained them, so check the cited section before relying on "
            "an edge case."
        ),
        "purpose_note": (
            "This spends cheap API tokens to save subscription capacity: paste the brief into a "
            "chat app instead of the source. Tokop never automates a chat app."
        ),
        "origin": origin,
    }
    return ExtraExamples(compares=compares, brief=brief)


def write_extras(examples: ExtraExamples, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(examples.to_dict(), indent=2, sort_keys=True, default=str))
    return path


def load_extras(path: Path) -> ExtraExamples | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return ExtraExamples(compares=data.get("compares", []), brief=data.get("brief", {}))


def _decimal(value: str) -> Decimal:
    return Decimal(value)
