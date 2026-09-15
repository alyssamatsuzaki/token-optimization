"""Two ways to run the same conversation, and what each one costs (UPGRADE_V4.md M17).

A session is a prefix established once and read on every turn, a history that grows until
something compacts it, and a cache entry that expires while the user is reading. Two policies
over the same tasks:

* **immutable** — the prefix is fixed at the first turn and never rewritten. History accumulates
  after the breakpoint, so it is billed at full input price and grows every turn.
* **compact** — every few turns the history is summarised and the summary is written *into the
  prefix*, which is what a naive compaction does. History stops growing; the cache entry is
  thrown away and paid for again at the write premium each time.

Which wins is a measurement, and this module makes it. Four things it is careful about.

**The bill is counted, not provider-reported, and it says so.** The in-process simulated provider
has no clock, so it cannot expire a cache entry, and expiry is half of what a session is about.
The turns are priced by ``core/session.py`` from the rendered requests with the base counter —
an estimate that names its counter (non-negotiable 2). The provider supplies the answers, which
is what decides correctness; it does not supply the bill.

**The interval resamples sessions, not turns.** Turns inside one session share a cache entry, so
their costs move together; resampling turns would report an interval several times too narrow.

**The quality side is refused, not estimated.** Compaction's real risk is that it drops what a
later turn needed. This build's provider answers from the task and the model alone — its replies
do not depend on the conversation carried with them — so a compacted arm cannot lose accuracy
here, and reporting "no accuracy difference" would be reporting a property of the simulator as a
property of compaction. It is displayed as not measured, with that reason.

**The one bias that is left runs towards compacting, and it is priced rather than mentioned.**
The summary this arm carries is whatever the simulated provider replied, which is far shorter
than the budget the workload set aside for a compaction. A shorter summary is a cheaper prefix to
write and a cheaper one to read, so the compact arm is flattered. Every turn whose prefix carries
a summary is therefore also priced with that summary at its declared budget, at the rate that
turn's prefix actually paid, and the comparison is reported **twice**: as measured, and with the
gap closed. A conclusion that survives only one of the two is reported as not established.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokop.adapters.base import Block, LLMRequest, Message
from tokop.adapters.factory import simulated_profiles
from tokop.adapters.simulated import GENERATION_RATIO as TOKENIZER_RATIO
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.pricing import ModelPrice, PriceSnapshot
from tokop.core.registry import Registry, load_registry
from tokop.core.session import SessionCost, Turn, session_cost, session_cost_ratio_interval
from tokop.core.stats import Interval
from tokop.core.tokenize import get_base_counter
from tokop.optimize.lint import Finding, LintContext, session_lint
from tokop.paths import repo_root
from tokop.workloads.bundle import Bundle, load_bundle
from tokop.workloads.grading import grade
from tokop.workloads.item import Item
from tokop.workloads.responder import GroundedResponder
from tokop.workloads.runner import TierProfile
from tokop.workloads.spec import PipelineSpec, SessionSpec, WorkloadSpec, load_workload

PRICES_TAKEN = date(2026, 9, 11)
IMMUTABLE = "immutable"
COMPACT = "compact"
POLICIES = (IMMUTABLE, COMPACT)

#: Why the accuracy side of this comparison is not reported as a number. Written once and shown
#: wherever the comparison is, because a refusal that appears in one place and not another is a
#: refusal a reader can miss.
QUALITY_REFUSAL = (
    "Not measured. Compaction's real risk is dropping what a later turn needed, and measuring "
    "that needs a provider whose answer depends on the conversation it was given. This build's "
    "provider answers from the task and the model alone, so a compacted arm cannot lose accuracy "
    "here and any figure would describe the simulator rather than compaction. The cost side "
    "below is measured; this side is refused until a recording exists."
)


class SessionReportError(RuntimeError):
    """A session comparison could not be run."""


@dataclass(frozen=True)
class SessionRun:
    """One conversation under one policy."""

    index: int
    policy: str
    task_ids: tuple[str, ...]
    correct: tuple[int, ...]
    cost: SessionCost
    #: The turns as they were sent, kept so the session lint can read what moved between them.
    turns: tuple[Turn, ...]
    #: Turns that were compaction rather than work, and what they cost.
    compaction_turns: int
    compaction_usd: Decimal
    #: History tokens a compaction removed from the turns that followed it.
    dropped_tokens: int
    #: Prefix tokens written again because a compaction moved the prefix.
    reestablished_tokens: int
    #: What this session would have cost if every compaction had produced a summary at its
    #: declared budget rather than the short reply the in-process provider gives. Zero for the
    #: immutable arm, which carries no summary. Priced at the rate each turn's prefix paid.
    budget_gap_usd: Decimal = Decimal(0)

    @property
    def adjusted_cost(self) -> Decimal:
        return self.cost.cost_usd + self.budget_gap_usd

    @property
    def successes(self) -> int:
        return sum(self.correct)


@dataclass(frozen=True)
class SessionArm:
    """Every conversation under one policy."""

    policy: str
    runs: tuple[SessionRun, ...]
    #: PL15 to PL17 over this arm's first conversation. One session is enough: the rules are
    #: about what moves between turns, and every session here runs the same pipeline.
    findings: tuple[Finding, ...] = ()

    @property
    def total_cost(self) -> Decimal:
        return sum((run.cost.cost_usd for run in self.runs), Decimal(0))

    @property
    def successes(self) -> int:
        return sum(run.successes for run in self.runs)

    @property
    def turns(self) -> int:
        return sum(len(run.task_ids) for run in self.runs)

    @property
    def cost_per_success(self) -> Decimal | None:
        return self.total_cost / self.successes if self.successes else None

    @property
    def adjusted_cost(self) -> Decimal:
        return sum((run.adjusted_cost for run in self.runs), Decimal(0))

    @property
    def prefix_stability(self) -> float:
        values = [run.cost.prefix_stability for run in self.runs]
        return sum(values) / len(values) if values else 0.0

    @property
    def expired_entries(self) -> int:
        return sum(run.cost.expired_entries for run in self.runs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "sessions": len(self.runs),
            "turns": self.turns,
            "successes": self.successes,
            "total_cost_usd": str(self.total_cost),
            "cost_per_successful_turn_usd": (
                str(self.cost_per_success) if self.cost_per_success is not None else None
            ),
            "prefix_stability": self.prefix_stability,
            "cache_writes_tokens": sum(r.cost.writes_5m + r.cost.writes_1h for r in self.runs),
            "cache_reads_tokens": sum(r.cost.reads for r in self.runs),
            "uncached_input_tokens": sum(r.cost.uncached_input for r in self.runs),
            "expired_entries": self.expired_entries,
            "compaction_turns": sum(r.compaction_turns for r in self.runs),
            "compaction_usd": str(sum((r.compaction_usd for r in self.runs), Decimal(0))),
            "dropped_history_tokens": sum(r.dropped_tokens for r in self.runs),
            "reestablished_prefix_tokens": sum(r.reestablished_tokens for r in self.runs),
            "summary_budget_gap_usd": str(sum((r.budget_gap_usd for r in self.runs), Decimal(0))),
            "total_cost_at_budget_usd": str(self.adjusted_cost),
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class GapResult:
    """The same comparison at one pace between turns."""

    gap_seconds: int
    arms: dict[str, SessionArm]
    cost_ratio: Interval
    cost_ratio_at_budget: Interval

    @property
    def winner(self) -> str:
        if self.cost_ratio.low > 1 and self.cost_ratio_at_budget.low > 1:
            return IMMUTABLE
        if self.cost_ratio.high < 1 and self.cost_ratio_at_budget.high < 1:
            return COMPACT
        return "neither"

    def as_dict(self) -> dict[str, Any]:
        return {
            "gap_seconds": self.gap_seconds,
            "winner": self.winner,
            "cost_ratio_compact_over_immutable": self.cost_ratio.as_dict(),
            "cost_ratio_compact_over_immutable_at_summary_budget": (
                self.cost_ratio_at_budget.as_dict()
            ),
            "expired_entries": {
                name: arm.expired_entries for name, arm in sorted(self.arms.items())
            },
            "arms": {name: arm.as_dict() for name, arm in sorted(self.arms.items())},
        }


@dataclass(frozen=True)
class SessionReport:
    """The comparison, with what it can say and what it refuses to."""

    workload: WorkloadSpec
    spec: SessionSpec
    model_id: str
    arms: dict[str, SessionArm]
    cost_ratio: Interval
    #: The same ratio with the short-summary bias closed: every compaction priced as though it
    #: had produced a summary at its declared budget.
    cost_ratio_at_budget: Interval
    counter: str
    token_ratio: float
    #: The same comparison at every pace the workload asked for, headline gap included. The gap
    #: is what decides whether an entry survives to the next turn, so it can decide the winner,
    #: and a report that showed one pace would be presenting a parameter as a finding.
    sensitivity: tuple[GapResult, ...] = ()
    quality_note: str = QUALITY_REFUSAL

    @property
    def winner(self) -> str:
        """Which policy won, on both pricings or on neither.

        A conclusion that holds as measured and not once the short-summary bias is closed is a
        conclusion about the simulator's reply length, so it is reported as neither.
        """
        if self.arms[IMMUTABLE].cost_per_success is None:
            return "neither"
        if self.cost_ratio.low > 1 and self.cost_ratio_at_budget.low > 1:
            return IMMUTABLE
        if self.cost_ratio.high < 1 and self.cost_ratio_at_budget.high < 1:
            return COMPACT
        return "neither"

    def sentence(self) -> str:
        arm = self.arms[COMPACT]
        immutable = self.arms[IMMUTABLE]
        low, high = self.cost_ratio.low, self.cost_ratio.high
        if self.winner == IMMUTABLE:
            head = (
                f"Holding the prefix immutable beats compacting: compacting costs "
                f"{self.cost_ratio.point:.2f}x as much per successful turn, 95% CI "
                f"[{low:.2f}, {high:.2f}]"
            )
        elif self.winner == COMPACT:
            head = (
                f"Compacting beats holding the prefix immutable: it costs "
                f"{self.cost_ratio.point:.2f}x as much per successful turn, 95% CI "
                f"[{low:.2f}, {high:.2f}]"
            )
        else:
            head = (
                f"Not established: compacting costs {self.cost_ratio.point:.2f}x as much per "
                f"successful turn, 95% CI [{low:.2f}, {high:.2f}] as measured and "
                f"[{self.cost_ratio_at_budget.low:.2f}, {self.cost_ratio_at_budget.high:.2f}] "
                "once a compaction is priced at the summary budget it was given, and the two do "
                "not agree on a winner"
            )
        return (
            f"{head}, over {len(immutable.runs)} sessions of up to {self.spec.turns} turns "
            f"({immutable.turns} turns) at {self.spec.gap_seconds}s between them. Compacting "
            f"dropped {sum(r.dropped_tokens for r in arm.runs):,} tokens of history and wrote "
            f"{sum(r.reestablished_tokens for r in arm.runs):,} prefix tokens again to do it."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "workload": {"id": self.workload.id, "name": self.workload.name},
            "origin": "simulated",
            "model_id": self.model_id,
            "session": {
                "pipeline": self.spec.pipeline,
                "turns": self.spec.turns,
                "gap_seconds": self.spec.gap_seconds,
                "ttl": self.spec.ttl,
                "compact_every": self.spec.compact_every,
            },
            "basis": (
                f"counted with {self.counter}, scaled by {self.token_ratio} into the model's own "
                "units, and not provider-reported: the in-process provider has no clock and "
                "cannot expire a cache entry"
            ),
            "arms": {name: arm.as_dict() for name, arm in sorted(self.arms.items())},
            "cost_ratio_compact_over_immutable": self.cost_ratio.as_dict(),
            "cost_ratio_compact_over_immutable_at_summary_budget": (
                self.cost_ratio_at_budget.as_dict()
            ),
            "winner": self.winner,
            "sentence": self.sentence(),
            "quality": {"measured": False, "note": self.quality_note},
            "sensitivity_to_pace": [result.as_dict() for result in self.sensitivity],
        }


# --------------------------------------------------------------------------- running them


def _adapter(workload: WorkloadSpec, bundle: Bundle, registry: Registry) -> SimulatedAdapter:
    if workload.simulation is None:
        raise SessionReportError(
            f"{workload.id} declares no `simulation:` block, so nothing can answer its turns."
        )
    tiers = {role: TierProfile(registry.roles[role], role) for role in ("cheap", "mid", "frontier")}
    responder = GroundedResponder(
        items=list(bundle.items),
        grounding=bundle.grounding,
        tiers=tiers,
        simulation=workload.simulation,
        # Seeded on the workload, not the pipeline: both arms run the same pipeline, and a seed
        # that moved between them would put the simulator's noise into the comparison.
        pipeline_id=workload.id,
        output_contract="json_answer",
    )
    return SimulatedAdapter(simulated_profiles(registry, list(registry.models)), responder)


def _sessions(items: list[Item], turns: int) -> list[list[Item]]:
    """Consecutive tasks, in fixed-size conversations.

    A trailing group of one is folded into the group before it rather than run as a session of
    one, which is a single call and is what every other part of this build already prices.
    """
    groups = [items[start : start + turns] for start in range(0, len(items), turns)]
    if len(groups) > 1 and len(groups[-1]) < 2:
        groups[-2].extend(groups.pop())
    return groups


def _render_turn(
    pipeline: PipelineSpec,
    provider: str,
    model: str,
    variables: dict[str, str],
    history: list[Message],
) -> LLMRequest:
    """One turn: the pipeline's own blocks, with the conversation so far in front of it.

    History goes in ``messages`` and never in ``system``, so it sits after the breakpoint and
    cannot move the prefix. That is the whole difference between the two arms: the compact arm
    puts its summary in ``system``, where it can.
    """
    return LLMRequest(
        provider=provider,
        model=model,
        system=[b.render(variables) for b in pipeline.system],
        messages=[
            *history,
            Message(role="user", blocks=[b.render(variables) for b in pipeline.user]),
        ],
        max_tokens=pipeline.max_tokens,
    )


def _budget_gap(
    cost: SessionCost,
    summary_tokens: list[int],
    price: ModelPrice,
    spec: SessionSpec,
    token_ratio: float,
) -> Decimal:
    """What a compaction summary at its declared budget would have added to this session.

    The in-process provider replies in tens of tokens where a real compaction is given hundreds,
    and the summary sits inside the prefix, so the short reply makes every write and every read
    after it cheaper than it would be. This prices the difference at the rate each turn's prefix
    actually paid — a write at the write premium, a read at a tenth of input — rather than
    asserting the bias is small.
    """
    budget = round(spec.compaction_max_tokens * token_ratio)
    total = Decimal(0)
    for turn, carried in zip(cost.turns, summary_tokens, strict=True):
        gap = max(0, budget - carried)
        if not gap or not carried:
            continue
        if turn.cache_write_1h:
            rate = price.cache_write_1h
        elif turn.cache_write_5m:
            rate = price.cache_write_5m
        elif turn.cache_read:
            rate = price.cache_read
        else:
            rate = price.input
        total += Decimal(gap) * rate / Decimal(1_000_000)
    return total


async def _run_session(
    workload: WorkloadSpec,
    spec: SessionSpec,
    bundle: Bundle,
    registry: Registry,
    price: ModelPrice,
    adapter: SimulatedAdapter,
    items: list[Item],
    *,
    index: int,
    policy: str,
    token_ratio: float,
    provider: str = "simulated",
) -> SessionRun:
    """Run one conversation under one policy and price it."""
    pipeline = workload.pipeline(spec.pipeline)
    model = registry.role(pipeline.model_role).model_id
    compaction_model = registry.role(spec.compaction_model_role).model_id
    counter = get_base_counter()
    started = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
    gap = timedelta(seconds=spec.gap_seconds)

    history: list[Message] = []
    summary = "nothing yet."
    turns: list[Turn] = []
    #: Tokens of summary sitting inside each turn's prefix, aligned with ``turns``. Kept so the
    #: short-summary bias can be priced rather than described.
    summary_tokens: list[int] = []
    correct: list[int] = []
    task_ids: list[str] = []
    compaction_turns = 0
    dropped = 0
    clock = started

    for position, item in enumerate(items):
        if (
            policy == COMPACT
            and spec.compact_every
            and position
            and position % spec.compact_every == 0
            and history
        ):
            transcript = "\n".join(message.text for message in history)
            request = LLMRequest(
                provider=provider,
                model=compaction_model,
                messages=[
                    Message(
                        role="user",
                        blocks=[
                            b.render({"history": transcript, "grounding": bundle.grounding})
                            for b in spec.compaction
                        ],
                    )
                ],
                max_tokens=spec.compaction_max_tokens,
            )
            response = await adapter.complete(request)
            dropped += counter.count(transcript)
            summary = response.text
            history = []
            compaction_turns += 1
            turns.append(
                Turn(
                    blocks=tuple(b for m in request.messages for b in m.blocks),
                    at=clock,
                    output_tokens=counter.count(response.text),
                    label="compaction",
                )
            )
            summary_tokens.append(0)
            clock += gap

        variables = {
            "grounding": bundle.grounding,
            "question": item.question,
            "summary": summary,
            "timestamp": started.strftime("Current date and time: %Y-%m-%d %H:%M UTC"),
        }
        request = _render_turn(pipeline, provider, model, variables, history)
        response = await adapter.complete(request)
        outcome = grade(response.text, item.gold, item.answer_type, item.aliases)
        correct.append(int(outcome.correct))
        task_ids.append(item.id)
        turns.append(
            Turn(
                blocks=(*request.system, *(b for m in request.messages for b in m.blocks)),
                at=clock,
                output_tokens=counter.count(response.text),
                label=f"turn {position + 1}",
            )
        )
        summary_tokens.append(
            round(counter.count(summary) * token_ratio) if policy == COMPACT else 0
        )
        clock += gap
        history = [
            *history,
            Message(role="user", blocks=[Block(text=item.question)]),
            Message(role="assistant", blocks=[Block(text=response.text)]),
        ]

    cost = session_cost(turns, price, ttl=spec.ttl, counter=counter, token_ratio=token_ratio)
    compaction_usd = sum((t.cost_usd for t in cost.turns if t.label == "compaction"), Decimal(0))
    budget_gap = _budget_gap(cost, summary_tokens, price, spec, token_ratio)
    # Prefix tokens written again because a compaction moved the prefix. The first turn's write
    # is what establishing the prefix costs under either policy and is not compaction's doing.
    writes = [t for t in cost.turns if t.cache_event in ("write", "changed", "expired")]
    reestablished = sum(
        t.cache_write_5m + t.cache_write_1h for t in writes if t.cache_event == "changed"
    )
    return SessionRun(
        index=index,
        policy=policy,
        task_ids=tuple(task_ids),
        correct=tuple(correct),
        cost=cost,
        turns=tuple(turns),
        compaction_turns=compaction_turns,
        compaction_usd=compaction_usd,
        dropped_tokens=dropped,
        reestablished_tokens=reestablished,
        budget_gap_usd=budget_gap,
    )


def _compare(
    workload: WorkloadSpec,
    spec: SessionSpec,
    bundle: Bundle,
    registry: Registry,
    price: ModelPrice,
    groups: list[list[Item]],
    *,
    gap_seconds: int,
    token_ratio: float,
) -> GapResult:
    """Run both policies over the same sessions at one pace, and price them twice."""
    paced = spec.model_copy(update={"gap_seconds": gap_seconds})
    arms: dict[str, SessionArm] = {}
    for policy in POLICIES:
        # A fresh adapter per arm: the simulated cache is an adapter-level dict, and reusing one
        # would let the first arm's entries answer the second arm's turns.
        adapter = _adapter(workload, bundle, registry)
        runs = [
            asyncio.run(
                _run_session(
                    workload,
                    paced,
                    bundle,
                    registry,
                    price,
                    adapter,
                    items,
                    index=index,
                    policy=policy,
                    token_ratio=token_ratio,
                )
            )
            for index, items in enumerate(groups)
        ]
        context = LintContext(
            price=price,
            min_cacheable_tokens=price.min_cacheable_tokens or 0,
            calls_per_1k=1000,
        )
        arms[policy] = SessionArm(
            policy=policy,
            runs=tuple(runs),
            findings=tuple(session_lint(runs[0].turns, context)) if runs else (),
        )

    immutable, compact = arms[IMMUTABLE], arms[COMPACT]
    successes = [run.successes for run in immutable.runs]
    return GapResult(
        gap_seconds=gap_seconds,
        arms=arms,
        cost_ratio=session_cost_ratio_interval(
            [run.cost.cost_usd for run in immutable.runs],
            successes,
            [run.cost.cost_usd for run in compact.runs],
            [run.successes for run in compact.runs],
        ),
        cost_ratio_at_budget=session_cost_ratio_interval(
            [run.adjusted_cost for run in immutable.runs],
            successes,
            [run.adjusted_cost for run in compact.runs],
            [run.successes for run in compact.runs],
        ),
    )


def build_session_report(workload_path: str | Path) -> SessionReport:
    """Run both policies over the same sessions, at every pace the workload asked for."""
    path = Path(workload_path)
    workload = load_workload(path if path.is_absolute() else repo_root() / path)
    spec = workload.session
    if spec is None:
        raise SessionReportError(
            f"{workload.id} declares no `session:` block, so it has no conversation to price. "
            "Every other number in this build is per request, which is what `tokop report` does."
        )
    registry = load_registry()
    bundle = load_bundle(workload)
    snapshot: PriceSnapshot = registry.snapshot(list(registry.roles.values()), PRICES_TAKEN)
    pipeline = workload.pipeline(spec.pipeline)
    model_id = registry.role(pipeline.model_role).model_id
    price = snapshot.price_for(model_id)
    # The model's own tokenizer generation, declared beside its price. Without it the minimum
    # cacheable prefix is checked in the wrong units and a cacheable session reads as uncacheable.
    token_ratio = TOKENIZER_RATIO[registry.models[model_id].tokenizer_generation or "newer"]

    groups = _sessions(list(bundle.test), spec.turns)
    paces = sorted({spec.gap_seconds, *spec.gap_sweep_seconds})
    sensitivity = tuple(
        _compare(
            workload,
            spec,
            bundle,
            registry,
            price,
            groups,
            gap_seconds=gap,
            token_ratio=token_ratio,
        )
        for gap in paces
    )
    headline = next(r for r in sensitivity if r.gap_seconds == spec.gap_seconds)
    return SessionReport(
        workload=workload,
        spec=spec,
        model_id=model_id,
        arms=headline.arms,
        cost_ratio=headline.cost_ratio,
        cost_ratio_at_budget=headline.cost_ratio_at_budget,
        counter=get_base_counter().name,
        token_ratio=token_ratio,
        sensitivity=sensitivity,
    )
