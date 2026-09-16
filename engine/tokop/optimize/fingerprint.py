"""What a workload *is*, as a distribution, and whether traffic has moved away from it (M18).

A certificate says a cheaper pipeline was non-inferior on a held-out split. It binds to the
models, the prices, the grading mode and the dataset's provenance, and until M18 that was all:
nothing in it noticed when the traffic it is quoted about stopped looking like the split it was
measured on. A cascade proven on a task mix that is 30% hard overrides is not a claim about a
month where overrides are 70% of the queue, and nothing was checking.

A **fingerprint** is that mix, measured three ways:

* **task-type mix** — the share of each ``question_type``, which is what the proof's own
  breakdown is grouped by, so a divergence here lands on a table the reader already has;
* **input-length quantiles** — p10, p50, p90 and p99 of the question in tokens, because length
  is what moves the bill even when the mix does not;
* **a difficulty proxy** — the share of tasks in the types the certificate found hardest, plus
  the concentration of the mix. It is a proxy and the name says so: nothing here has a
  difficulty label, and inventing one would make the drift check a check on an invention.

**Distribution drift is not the canary's drift, and the two never share a word.**
``CanaryResult.drift_tested`` means *outcome* drift: a re-scored subset whose accuracy difference
moved. This is *coverage*: traffic that has moved into a region the certified split barely
covered. A certificate can pass every outcome look and still be quoted about tasks it never saw,
which is precisely the failure that would otherwise look like a passing check.

The divergence measure is **total variation distance** over the type mix — half the sum of the
absolute differences in share — because it reads as a share of traffic: a TVD of 0.2 means a
fifth of the queue is in a different type from the one the certificate would predict. Beside it
sits the **uncovered region**: the types that carry real traffic now and carried almost none
then, named individually, because "the distribution moved" is not something anybody can act on
and "overrides went from 12% to 48% of the queue" is.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from tokop.core.tokenize import BaseCounter, get_base_counter
from tokop.workloads.item import Item

FINGERPRINT_SCHEMA = "tokop.fingerprint.v1"

#: Quantiles reported for input length. p99 is included because a context that overflows is a
#: failure rather than a cost, and it never shows up in a median.
QUANTILES: tuple[float, ...] = (0.10, 0.50, 0.90, 0.99)

#: Total variation distance above which the mix is a different mix. A fifth of the queue sitting
#: in a different type from the certified one is a change anybody running the workload would
#: describe as a change; below that, the certified split still covers what is arriving.
DRIFT_TVD = 0.20

#: A type is *uncovered* when it carries at least this share of recent traffic and the certified
#: split gave it less than ``UNCOVERED_FLOOR``. Both are needed: a type that grew from 1% to 3%
#: is noise, and a type that was always 40% is not a gap.
UNCOVERED_SHARE = 0.10
UNCOVERED_FLOOR = 0.02

#: Ratio between a recent length quantile and the certified one past which the lengths have
#: moved. Applied both ways, so a queue that got shorter raises as readily as one that grew.
LENGTH_RATIO = 1.5


class FingerprintError(ValueError):
    """A fingerprint could not be taken or compared."""


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile. Written out rather than imported for one call."""
    if not values:
        raise FingerprintError("no values to take a quantile of")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


@dataclass(frozen=True)
class Fingerprint:
    """The shape of a set of tasks: what kinds, how long, and how concentrated."""

    task_type_mix: dict[str, float]
    length_quantiles: dict[str, float]
    difficulty: dict[str, float]
    counter: str
    n: int
    schema: str = FINGERPRINT_SCHEMA

    def share(self, task_type: str) -> float:
        return self.task_type_mix.get(task_type, 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_type_mix": dict(self.task_type_mix),
            "length_quantiles": dict(self.length_quantiles),
            "difficulty": dict(self.difficulty),
            "counter": self.counter,
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Fingerprint:
        if str(raw.get("schema")) != FINGERPRINT_SCHEMA:
            raise FingerprintError(
                f"fingerprint schema is {raw.get('schema')!r}, not {FINGERPRINT_SCHEMA!r}."
            )
        return cls(
            task_type_mix={str(k): float(v) for k, v in (raw["task_type_mix"] or {}).items()},
            length_quantiles={str(k): float(v) for k, v in (raw["length_quantiles"] or {}).items()},
            difficulty={str(k): float(v) for k, v in (raw["difficulty"] or {}).items()},
            counter=str(raw.get("counter", "")),
            n=int(raw["n"]),
        )


def fingerprint(items: Sequence[Item], counter: BaseCounter | None = None) -> Fingerprint:
    """The fingerprint of a set of tasks.

    The counter is recorded rather than assumed: length quantiles move with the tokenizer, and a
    fingerprint compared against one taken with a different counter would read as length drift
    when nothing about the traffic had changed. :func:`divergence` refuses that comparison.
    """
    if not items:
        raise FingerprintError(
            "a fingerprint of no tasks is not a fingerprint. A workload with no items cannot be "
            "certified and cannot be checked for drift."
        )
    count = counter or get_base_counter()
    total = len(items)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.question_type] = counts.get(item.question_type, 0) + 1
    mix = {name: counts[name] / total for name in sorted(counts)}

    lengths = [float(count.count(item.question)) for item in items]
    quantiles = {f"p{int(q * 100)}": _quantile(lengths, q) for q in QUANTILES}

    # Two numbers, both computable from the items alone, and neither of them a difficulty label.
    # Concentration is the normalised Herfindahl index of the type mix: 0 when every type is
    # equally represented, 1 when one type is everything. `rarest_share` is how much of the set
    # sits in its smallest type, which is the region a stratified canary samples least of.
    herfindahl = sum(share**2 for share in mix.values())
    kinds = len(mix)
    concentration = (herfindahl - 1 / kinds) / (1 - 1 / kinds) if kinds > 1 else 1.0
    difficulty = {
        "type_concentration": round(concentration, 6),
        "rarest_type_share": round(min(mix.values()), 6),
        "mean_question_tokens": round(sum(lengths) / total, 4),
    }
    return Fingerprint(
        task_type_mix={k: round(v, 6) for k, v in mix.items()},
        length_quantiles={k: round(v, 4) for k, v in quantiles.items()},
        difficulty=difficulty,
        counter=count.name,
        n=total,
    )


@dataclass(frozen=True)
class Divergence:
    """How far recent traffic has moved from what a certificate was measured on."""

    total_variation: float
    #: Types carrying real traffic now that the certified split barely covered, with both shares.
    uncovered: tuple[tuple[str, float, float], ...]
    #: Length quantiles that moved by more than ``LENGTH_RATIO``, with both values.
    length_moves: tuple[tuple[str, float, float], ...]
    certified_n: int
    recent_n: int
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def drifted(self) -> bool:
        """Whether the certified split still covers what is arriving."""
        return bool(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_variation": self.total_variation,
            "drifted": self.drifted,
            "uncovered_region": [
                {"task_type": name, "certified_share": was, "recent_share": now}
                for name, was, now in self.uncovered
            ],
            "length_moves": [
                {"quantile": name, "certified": was, "recent": now}
                for name, was, now in self.length_moves
            ],
            "certified_n": self.certified_n,
            "recent_n": self.recent_n,
            "reasons": list(self.reasons),
        }


def divergence(certified: Fingerprint, recent: Fingerprint) -> Divergence:
    """Compare recent traffic to the distribution a certificate was measured on.

    Every reason names the region it is about. "The distribution moved" is not something anybody
    can act on; "overrides went from 12% to 48% of the queue, and the certified split had 12
    of them" is.
    """
    if certified.counter != recent.counter:
        raise FingerprintError(
            f"the certified fingerprint was counted with {certified.counter!r} and this one with "
            f"{recent.counter!r}. Length quantiles move with the tokenizer, so comparing them "
            "would report a counter change as traffic drift. Re-take one with the other's "
            "counter, or compare the type mix alone."
        )
    types = sorted(set(certified.task_type_mix) | set(recent.task_type_mix))
    tvd = sum(abs(recent.share(name) - certified.share(name)) for name in types) / 2

    uncovered = tuple(
        (name, certified.share(name), recent.share(name))
        for name in types
        if recent.share(name) >= UNCOVERED_SHARE and certified.share(name) < UNCOVERED_FLOOR
    )
    length_moves = tuple(
        (name, was, recent.length_quantiles.get(name, 0.0))
        for name, was in sorted(certified.length_quantiles.items())
        if was > 0
        and not (1 / LENGTH_RATIO <= recent.length_quantiles.get(name, 0.0) / was <= LENGTH_RATIO)
    )

    reasons: list[str] = []
    if tvd >= DRIFT_TVD:
        moved = sorted(
            types, key=lambda name: abs(recent.share(name) - certified.share(name)), reverse=True
        )[:3]
        detail = "; ".join(
            f"{name} {certified.share(name):.0%} -> {recent.share(name):.0%}" for name in moved
        )
        reasons.append(
            f"the task mix moved by {tvd:.0%} of traffic (total variation distance {tvd:.2f}, "
            f"threshold {DRIFT_TVD:.2f}): {detail}. The certificate was measured on the first "
            "column and is being quoted about the second."
        )
    for name, was, now in uncovered:
        reasons.append(
            f"{name!r} is {now:.0%} of recent traffic and was {was:.0%} of the certified split "
            f"({round(was * certified.n)} of {certified.n} tasks). That region is uncovered: "
            "nothing in the proof measured it, so nothing in the proof applies to it."
        )
    for name, was, now in length_moves:
        reasons.append(
            f"input length at {name} moved from {was:,.0f} to {now:,.0f} tokens, a factor of "
            f"{(now / was) if was else float('inf'):.2f}. Cost per task moves with it whether or "
            "not accuracy does."
        )
    return Divergence(
        total_variation=round(tvd, 6),
        uncovered=uncovered,
        length_moves=length_moves,
        certified_n=certified.n,
        recent_n=recent.n,
        reasons=tuple(reasons),
    )
