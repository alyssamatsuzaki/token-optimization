"""What a conversation costs over its turns, not what one request costs (UPGRADE_V4.md M17).

Every price in this build is a price per request. That is the right unit for the demo — one
question, one call — and the wrong unit for the thing most people actually run, which is a
session: a prefix established once and then read on every turn, a history that grows until
something compacts it, and a cache entry that quietly expires while the user is at lunch.

This module is the accounting for that. It takes a sequence of turns and a price and says what
the sequence cost, bucket by bucket, with the cache modelled the way the provider bills it:

* the **static prefix** is the text up to and including the last cache breakpoint, and the cache
  key is that text exactly (SPEC.md Appendix B);
* a prefix below the model's minimum is not cached at all and returns no error, so both cache
  fields read zero and the whole thing is billed as ordinary input;
* the first turn to present a prefix **writes** it, at 1.25x base input for a five-minute entry
  or 2x for an hour; every later turn presenting the same prefix **reads** it at 0.1x;
* an entry older than its lifetime has gone, and the next turn writes it again.

**Three things this deliberately does not claim.**

*Whether a read refreshes the entry's lifetime.* Appendix B does not say, and nothing in this
build has watched a real cache expire, so the default here is that it does not — which produces
more writes and a higher bill, and is the conservative direction for a number someone budgets
against. ``refresh_on_read=True`` is offered for an operator who has checked; ``SessionCost``
records which was used, because the two give different answers on a long session.

*A partial prefix match.* The provider can match a prefix shorter than the breakpoint, up to the
longest previously-cached one. This models an exact match only, which again costs more than
reality rather than less.

*That the base counter counts what the model counts.* It does not: Anthropic's models from Claude
4.7 on produce about 30% more tokens for the same text than the tokenizer this build can run
locally, and the gap decides whether a prefix clears the model's minimum at all — a prefix
counted at 860 tokens is refused by a model with a 1,024-token minimum and cached by the same
model that counts it at 1,118. ``token_ratio`` is that scale factor, declared per model in
``config/prices.yaml`` beside the price; ``SessionCost`` records the counter and the ratio it
used, because a cache conclusion that depends on them has to say so.

*Anything about quality.* A compaction that drops half a conversation may cost less and answer
worse. This module prices; it does not grade. What compaction does to task success is measured
where outcomes are, and is refused where nothing measured it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

import numpy as np

from tokop.adapters.base import Block
from tokop.core.pricing import PER_MILLION, ModelPrice
from tokop.core.stats import DEFAULT_RESAMPLES, DEFAULT_SEED, Interval, StatsError, paired_bootstrap
from tokop.core.tokenize import BaseCounter, get_base_counter

CacheTTL = Literal["5m", "1h"]

#: How long each lifetime lasts. Named rather than spelled out at the two places that need it.
TTL_SECONDS: dict[str, int] = {"5m": 300, "1h": 3600}


class SessionError(ValueError):
    """A session could not be priced."""


@dataclass(frozen=True)
class Turn:
    """One request in a conversation: its blocks, and when it was sent."""

    blocks: tuple[Block, ...]
    at: datetime
    #: Output tokens this turn produced. Provider-reported where a turn came from a trace;
    #: an estimate where it came from a rendered request, and the caller labels it.
    output_tokens: int = 0
    #: What this turn is for, so a report can say which turns were compaction rather than work.
    label: str = ""

    @property
    def static_prefix_text(self) -> str:
        """Text up to and including the last breakpoint — what a cache entry can hold."""
        parts: list[str] = []
        last_break = -1
        for index, block in enumerate(self.blocks):
            parts.append(block.text)
            if block.cache is not None:
                last_break = index
        if last_break < 0:
            return ""
        return "\n\n".join(parts[: last_break + 1])

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks)

    @property
    def ttl(self) -> CacheTTL:
        """The lifetime the last breakpoint asked for. ``5m`` when none is named."""
        for block in reversed(self.blocks):
            if block.cache is not None:
                return block.cache
        return "5m"


@dataclass(frozen=True)
class TurnCost:
    """What one turn cost, and which bucket each of its tokens landed in."""

    index: int
    label: str
    uncached_input: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    output: int
    cost_usd: Decimal
    #: ``write``, ``read``, ``expired`` (the entry had gone and was written again), ``changed``
    #: (a different prefix from the one in hand) or ``uncacheable`` (no breakpoint, or a prefix
    #: below the model's minimum).
    cache_event: str

    @property
    def total_input(self) -> int:
        return self.uncached_input + self.cache_write_5m + self.cache_write_1h + self.cache_read


@dataclass(frozen=True)
class SessionCost:
    """What a whole conversation cost, with the cache behaviour that produced it."""

    #: Tokens, not turns: these are what the per-million rates multiply.
    writes_5m: int
    writes_1h: int
    reads: int
    uncached_input: int
    output: int
    #: Turns, not tokens: how many turns found no usable entry and had to write one.
    misses: int
    #: Turns whose entry had expired by the time they ran. A subset of ``misses``.
    expired_entries: int
    cost_usd: Decimal
    #: Share of turns after the first whose prefix was byte-identical to the turn before. 1.0
    #: means the prefix never moved; anything less is a prefix that is being rewritten, which is
    #: what PL15 to PL17 are about.
    prefix_stability: float
    turns: tuple[TurnCost, ...] = field(default_factory=tuple)
    #: Whether a read was taken to refresh the entry's lifetime. Recorded because the two
    #: assumptions give different answers on a long session and neither was verified here.
    refresh_on_read: bool = False
    counter: str = ""
    #: What the counter's numbers were multiplied by to reach the model's own units. Recorded
    #: because whether a prefix clears the model's minimum depends on it.
    token_ratio: float = 1.0

    @property
    def total_input(self) -> int:
        return self.uncached_input + self.writes_5m + self.writes_1h + self.reads

    def as_dict(self) -> dict[str, object]:
        return {
            "writes_5m": self.writes_5m,
            "writes_1h": self.writes_1h,
            "reads": self.reads,
            "uncached_input": self.uncached_input,
            "output": self.output,
            "misses": self.misses,
            "expired_entries": self.expired_entries,
            "cost_usd": str(self.cost_usd),
            "prefix_stability": self.prefix_stability,
            "refresh_on_read": self.refresh_on_read,
            "counter": self.counter,
            "token_ratio": self.token_ratio,
            "turns": [
                {
                    "index": turn.index,
                    "label": turn.label,
                    "cache_event": turn.cache_event,
                    "input_tokens": turn.total_input,
                    "output_tokens": turn.output,
                    "cost_usd": str(turn.cost_usd),
                }
                for turn in self.turns
            ],
        }


def session_cost(
    turns: Sequence[Turn],
    price: ModelPrice,
    *,
    ttl: CacheTTL = "5m",
    counter: BaseCounter | None = None,
    refresh_on_read: bool = False,
    token_ratio: float = 1.0,
) -> SessionCost:
    """Price a conversation turn by turn, with the cache entry expiring at its lifetime.

    ``ttl`` is the lifetime to use when a turn's own breakpoint does not name one. A turn that
    names one is priced at that one, because the block is what the request would carry.

    ``token_ratio`` scales the local counter into the model's own units. It is 1.0 by default,
    which is the right answer only for a model on the same tokenizer generation as the counter;
    anything else and the caller has to pass the model's ratio or the minimum-cacheable check
    below is answered in the wrong units.
    """
    if not turns:
        raise SessionError("a session with no turns has no cost; this is a caller bug")
    if token_ratio <= 0:
        raise SessionError(f"token_ratio must be positive, got {token_ratio}")
    base = counter or get_base_counter()
    minimum = price.min_cacheable_tokens or 0

    def count_tokens(text: str) -> int:
        return round(base.count(text) * token_ratio)

    entry_text: str | None = None
    entry_written_at: datetime | None = None
    entry_ttl: str = ttl

    priced: list[TurnCost] = []
    stable = 0
    previous_prefix: str | None = None
    for index, turn in enumerate(turns):
        prefix = turn.static_prefix_text
        total = count_tokens(turn.text)
        prefix_tokens = count_tokens(prefix) if prefix else 0
        turn_ttl = turn.ttl if prefix else ttl

        if previous_prefix is not None and prefix == previous_prefix:
            stable += 1
        previous_prefix = prefix

        write_5m = write_1h = read = 0
        if not prefix or prefix_tokens < minimum:
            # No breakpoint, or a prefix the provider will not hold. Billed as plain input, and
            # the entry in hand is untouched: nothing was written and nothing was read.
            event = "uncacheable"
            uncached = total
        else:
            expired = (
                entry_written_at is not None
                and (turn.at - entry_written_at).total_seconds() > TTL_SECONDS[entry_ttl]
            )
            if entry_text == prefix and not expired:
                event = "read"
                read = prefix_tokens
                if refresh_on_read:
                    entry_written_at = turn.at
            else:
                event = (
                    "expired"
                    if (entry_text == prefix and expired)
                    else ("changed" if entry_text is not None else "write")
                )
                if turn_ttl == "1h":
                    write_1h = prefix_tokens
                else:
                    write_5m = prefix_tokens
                entry_text = prefix
                entry_written_at = turn.at
                entry_ttl = turn_ttl
            uncached = max(0, total - prefix_tokens)

        cost = (
            Decimal(uncached) * price.input
            + Decimal(write_5m) * price.cache_write_5m
            + Decimal(write_1h) * price.cache_write_1h
            + Decimal(read) * price.cache_read
            + Decimal(turn.output_tokens) * price.output
        ) / PER_MILLION
        priced.append(
            TurnCost(
                index=index,
                label=turn.label,
                uncached_input=uncached,
                cache_write_5m=write_5m,
                cache_write_1h=write_1h,
                cache_read=read,
                output=turn.output_tokens,
                cost_usd=cost,
                cache_event=event,
            )
        )

    misses = sum(1 for t in priced if t.cache_event in ("write", "changed", "expired"))
    return SessionCost(
        writes_5m=sum(t.cache_write_5m for t in priced),
        writes_1h=sum(t.cache_write_1h for t in priced),
        reads=sum(t.cache_read for t in priced),
        uncached_input=sum(t.uncached_input for t in priced),
        output=sum(t.output for t in priced),
        misses=misses,
        expired_entries=sum(1 for t in priced if t.cache_event == "expired"),
        cost_usd=sum((t.cost_usd for t in priced), Decimal(0)),
        prefix_stability=(
            sum(1 for t in priced[1:] if t.cache_event == "read") / (len(priced) - 1)
            if len(priced) > 1
            else 1.0
        ),
        turns=tuple(priced),
        refresh_on_read=refresh_on_read,
        counter=base.name,
        token_ratio=token_ratio,
    )


def turns_from_requests(
    requests: Sequence[object],
    outputs: Sequence[int],
    started: datetime,
    *,
    gap: timedelta = timedelta(seconds=30),
    labels: Sequence[str] = (),
) -> list[Turn]:
    """Turns from rendered requests, spaced by a declared gap between them.

    The gap is what decides whether an entry is still alive, so it is a parameter rather than a
    constant: a session where a person reads each answer before replying is a different bill
    from one an agent drives in a loop, and the difference is only visible if the caller says
    which it is.
    """
    turns: list[Turn] = []
    for index, request in enumerate(requests):
        blocks = [*getattr(request, "system", [])]
        for message in getattr(request, "messages", []):
            blocks.extend(message.blocks)
        turns.append(
            Turn(
                blocks=tuple(blocks),
                at=started + gap * index,
                output_tokens=outputs[index] if index < len(outputs) else 0,
                label=labels[index] if index < len(labels) else "",
            )
        )
    return turns


def session_cost_ratio_interval(
    baseline_costs: Sequence[Decimal],
    baseline_successes: Sequence[int],
    candidate_costs: Sequence[Decimal],
    candidate_successes: Sequence[int],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
) -> Interval:
    """Candidate cost per successful turn over baseline's, resampling **sessions**.

    The unit matters and is the reason this is not ``cost_ratio_interval``. Turns inside one
    session share a cache entry: the first pays for the write and the rest read it, so their
    costs move together and a bootstrap that resampled turns would treat sixteen correlated
    observations as sixteen independent ones and report an interval far too narrow. The session
    is the independent unit, so the session is what gets resampled — which makes the interval
    wider and the claim smaller, in that order.
    """
    lengths = {
        len(baseline_costs),
        len(baseline_successes),
        len(candidate_costs),
        len(candidate_successes),
    }
    if len(lengths) != 1:
        raise StatsError(f"all four arrays must have the same length, got {sorted(lengths)}")
    if not baseline_costs:
        raise SessionError("a session comparison needs at least one session")
    bc = np.asarray([float(c) for c in baseline_costs], dtype=float)
    bs = np.asarray(baseline_successes, dtype=float)
    cc = np.asarray([float(c) for c in candidate_costs], dtype=float)
    cs = np.asarray(candidate_successes, dtype=float)

    def stat(idx: np.ndarray) -> float | None:
        b_succ, c_succ = bs[idx].sum(), cs[idx].sum()
        if b_succ == 0 or c_succ == 0:
            return None
        base = bc[idx].sum() / b_succ
        if base == 0:
            return None
        return float((cc[idx].sum() / c_succ) / base)

    return paired_bootstrap(
        len(bc),
        stat,
        resamples=resamples,
        seed=seed,
        level=level,
        method="paired bootstrap over sessions, cost ratio",
    )
