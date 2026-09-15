"""Record a workload: build the response matrix every later stage computes over.

The spend order in SPEC.md section 6 is deliberate and is followed here — cheapest first, with
a sanity gate before the expensive part — so that a generator bug or a dataset that is too easy
is discovered after a few dollars rather than after twenty:

1. B2 on the **calibration** split with each tier.
2. **Sanity and difficulty check.** Stop if the frontier scores below 90% (almost always a
   generator or grader bug, since every answer is in the handbook) or if the cheap tier lands
   within 3 points of the frontier (the set is too easy to show escalation).
3. B2 on the **test** split with each tier. With step 1 this completes the response matrix the
   cascade simulator needs.
4. The single-call pipelines on the test split with the frontier model: B0 and B1, and B2c if
   the workload defines one.

The same code drives two very different things. Against live providers it is ``make record``,
capped by ``RECORD_BUDGET_USD``. Against the deterministic simulated provider it builds
``fixtures/test/``, spends nothing, and marks every run ``origin: simulated`` so no surface can
present it as a recording.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokop.adapters.base import AdapterError, LLMRequest, LLMResponse
from tokop.adapters.cassette import CassetteStore
from tokop.adapters.factory import AnyAdapter, build_live_adapter, simulated_profiles
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import Responder, SimulatedAdapter
from tokop.core.budget import BudgetExceeded, SpendGuard, projected_cost
from tokop.core.pricing import PriceSnapshot
from tokop.core.registry import Registry
from tokop.core.tokenize import BaseCounter, get_base_counter
from tokop.db import make_engine
from tokop.optimize.scorers import TaskView
from tokop.settings import get_settings
from tokop.workloads.bundle import Bundle
from tokop.workloads.item import Item
from tokop.workloads.responder import GroundedResponder
from tokop.workloads.runner import Runner, RunResult, TierProfile, persist, today
from tokop.workloads.spec import WorkloadSpec

#: SPEC.md section 6, step 2. Below this on calibration, the frontier is telling you the
#: generator or the grader is broken, not that the model is bad.
FRONTIER_SANITY_FLOOR = 0.90

#: If the cheap tier is this close to the frontier, the set cannot demonstrate escalation.
DIFFICULTY_GAP_FLOOR = 0.03


class RecordingStopped(RuntimeError):
    """The recorder stopped on purpose, before spending more."""


@dataclass
class StepResult:
    """One run in the recording plan."""

    pipeline: str
    split: str
    tier: str
    model_id: str
    result: RunResult
    accuracy: float
    cost: Decimal

    def summary(self) -> str:
        return (
            f"{self.pipeline:3} {self.split:11} {self.tier:8} {self.model_id:28} "
            f"acc {self.accuracy:6.1%}  ${self.cost:9.5f}"
        )


@dataclass
class RecordingReport:
    """Everything a recording produced."""

    steps: list[StepResult] = field(default_factory=list)
    total_cost: Decimal = Decimal(0)
    stopped: str | None = None
    origin: str = "simulated"
    cassette_count: int = 0
    #: A content hash over the cassettes and blobs this recording produced. Committed so the
    #: recording a checkout claims to hold can be checked against the bytes it holds.
    cassette_digest: str = ""
    #: Whether `--underpowered` was used to record a test split too small to produce a verdict.
    #: Stored here so the report can say so: an operator who overrode the refusal has made a
    #: choice about what the recording can prove, and the next reader did not (UPGRADE_V4 M13.1).
    underpowered: bool = False
    #: What recording the entailment judgements cost. Reported apart from the matrix, because
    #: it is the price of being *able* to cluster by meaning rather than of any one run.
    entailment_cost: Decimal = Decimal(0)
    #: How many entailment judgements were recorded. With the cost above it gives the mean
    #: price of one, which is what a scorer that clusters by meaning is charged per call.
    entailment_calls: int = 0

    def by_key(self, pipeline: str, split: str, tier: str) -> StepResult | None:
        for step in self.steps:
            if (step.pipeline, step.split, step.tier) == (pipeline, split, tier):
                return step
        return None

    def manifest(self) -> dict[str, Any]:
        from tokop.core.tokenize import base_counter_name

        return {
            "complete": self.stopped is None,
            "origin": self.origin,
            "stopped": self.stopped,
            # Which tokenizer was loadable when these fixtures were built. It is recorded because
            # it is not a property of the code: `o200k_base` is downloaded on first use, so a
            # machine without egress to the vocabulary host silently falls back to the
            # approximation (DECISIONS.md D3, D26). Everything token-derived moves with it, and a
            # committed artifact that does not say which counter produced it cannot be checked
            # anywhere but the machine that produced it.
            "base_token_counter": base_counter_name(),
            "recorded_at": datetime.now(UTC).isoformat(),
            "total_cost_usd": str(self.total_cost),
            "entailment_recording_cost_usd": str(self.entailment_cost),
            "entailment_calls": self.entailment_calls,
            "cassette_count": self.cassette_count,
            "cassette_digest": self.cassette_digest,
            "underpowered": self.underpowered,
            "runs": [
                {
                    **step.result.manifest,
                    "tier": step.tier,
                    "accuracy": step.accuracy,
                    "cost_usd": str(step.cost),
                    "run_id": step.result.run_id,
                }
                for step in self.steps
            ],
        }


def accuracy_of(result: RunResult, items: Sequence[Item]) -> float:
    """Grade a run's outputs without touching the ledger."""
    from tokop.workloads.grading import grade

    by_id = {item.id: item for item in items}
    if not result.tasks:
        return 0.0
    correct = 0
    for task in result.tasks:
        item = by_id[task.task_id]
        outcome = grade(task.output, item.gold, item.answer_type, item.aliases)
        correct += int(outcome.correct and not task.failed)
    return correct / len(result.tasks)


@dataclass(frozen=True)
class PlannedStep:
    """One run in the recording plan, before it has been made."""

    pipeline: str
    split: str
    tier: str
    samples: int
    items: int

    @property
    def generation_calls(self) -> int:
        return self.items * self.samples


@dataclass(frozen=True)
class PilotOutcome:
    """What a pilot run measured, and what is left to pay for."""

    tasks: int
    cost: Decimal
    output_tokens: tuple[int, ...]
    total_tasks: int

    @property
    def remaining_tasks(self) -> int:
        return max(0, self.total_tasks - self.tasks)


@dataclass(frozen=True)
class StepProjection:
    """What one planned step is expected to cost, before it is run."""

    pipeline: str
    split: str
    tier: str
    model_id: str
    generation_calls: int
    #: One per distinct cacheable prefix, or none at all when the pipeline has no breakpoint —
    #: which is exactly the case the un-cached baseline is there to demonstrate.
    prewarm_calls: int
    prefix_tokens: int
    volatile_tokens: int
    max_output_tokens: int
    counter: str
    exact_input: bool
    ceiling_usd: Decimal
    floor_usd: Decimal

    @property
    def calls(self) -> int:
        return self.generation_calls + self.prewarm_calls

    @property
    def input_tokens(self) -> int:
        """Billable input tokens across the whole step, cache reads included."""
        return (self.prefix_tokens + self.volatile_tokens) * self.generation_calls

    def row(self) -> str:
        mark = " " if self.exact_input else "~"
        return (
            f"  {self.pipeline:4} {self.split:11} {self.tier:8} {self.model_id:28} "
            f"{self.calls:6,} calls  {self.input_tokens:9,} in{mark} "
            f"${self.floor_usd:8.4f}-${self.ceiling_usd:8.4f}"
        )


@dataclass(frozen=True)
class RecordingProjection:
    """What a recording is expected to cost, priced before the first call.

    Two figures, because one would be a guess dressed as a fact. The **ceiling** prices every
    call's output at ``max_tokens``: nothing before a call knows how long an answer will be, and
    a projection that guesses low is how a budget gets passed. The **floor** prices output at
    zero, which is what the input alone costs. The real bill lands between them, nearer the
    floor on a workload with an output contract.

    The input side is not a guess at all: the requests are rendered and counted, with the cache
    modelled the way the recorder actually runs it — the static prefix written once by the
    pre-warm and read by every call after it.
    """

    steps: tuple[StepProjection, ...]
    cap_usd: Decimal | None
    counter: str
    exact_input: bool
    #: Calls made to the provider's token-counting endpoint to build this projection. They are
    #: live requests and they bill nothing: counting tokens returns no completion. Disclosed
    #: rather than hidden, because "before the first call" should mean what it says.
    counting_calls: int = 0

    @property
    def ceiling_usd(self) -> Decimal:
        return sum((step.ceiling_usd for step in self.steps), Decimal(0))

    @property
    def floor_usd(self) -> Decimal:
        return sum((step.floor_usd for step in self.steps), Decimal(0))

    @property
    def calls(self) -> int:
        return sum(step.calls for step in self.steps)

    @property
    def exceeds_cap(self) -> bool:
        """Whether even the floor is past the cap, which makes the recording impossible."""
        return self.cap_usd is not None and self.floor_usd > self.cap_usd

    @property
    def may_stop_early(self) -> bool:
        """Whether the cap sits inside the band, so the recording could stop partway."""
        return self.cap_usd is not None and not self.exceeds_cap and self.ceiling_usd > self.cap_usd

    def lines(self) -> list[str]:
        out = [
            "  pipeline split       tier     model                           "
            "  calls  input tokens   cost range",
        ]
        out += [step.row() for step in self.steps]
        mark = (
            ""
            if self.exact_input
            else (f"\n  ~ input counted with {self.counter}, not the provider's counting endpoint")
        )
        out += [
            "",
            f"  {self.calls:,} calls, ${self.floor_usd:.2f} to ${self.ceiling_usd:.2f}{mark}",
        ]
        if self.counting_calls:
            out.append(
                f"  input counted exactly by the provider, in {self.counting_calls} calls to "
                "its token-counting\n  endpoint, which returns no completion and bills nothing"
            )
        if self.cap_usd is not None:
            out.append(f"  RECORD_BUDGET_USD is ${self.cap_usd}")
            if self.exceeds_cap:
                out.append(
                    "  The input alone costs more than the cap. This recording cannot finish; "
                    "record a smaller split."
                )
            elif self.may_stop_early:
                out.append(
                    "  The cap sits inside that range, so the recording may stop partway. It "
                    "stops cleanly and resumes:\n  every call it paid for is a cassette, and a "
                    "later run replays them for nothing."
                )
        return out


@dataclass
class GuardSink:
    """Charges the spend guard for the calls that actually reach a provider.

    Until M13 the guard was consulted once per *step* — a hundred or two hundred tasks — with a
    projection of zero, under a comment claiming the live adapter checked each call. No adapter
    ever did: ``SpendGuard.preflight`` had no caller anywhere in the engine, so ``B0`` on the
    test split was a single uninterruptible $9.88 purchase and ``RECORD_BUDGET_USD`` could only
    be observed to have been passed, never enforced (DECISIONS.md D42).

    ``RecordingAdapter`` calls both hooks only on a cassette miss, which is the path that costs
    money. A replayed call is therefore neither refused near the cap nor billed to it, and an
    interrupted recording can replay everything it already paid for even when the budget is
    spent — which is the whole point of resuming.
    """

    guard: SpendGuard
    snapshot: PriceSnapshot
    counter: BaseCounter

    def projection(self, request: LLMRequest) -> Decimal:
        """The pessimistic cost of one call: every input token uncached, output at the cap.

        Counted with the base counter rather than the provider's counting endpoint, because a
        guard that made an extra HTTP call before every generation would double the request
        count of a recording to buy precision that a deliberate over-estimate does not need.
        The endpoint is used where exactness is worth a round trip: the projection the operator
        approves, in :func:`project`.
        """
        price = self.snapshot.price_for(request.model)
        tokens = self.counter.count(request.system_text) + sum(
            self.counter.count(message.text) for message in request.messages
        )
        return projected_cost(tokens, request.max_tokens, price.input, price.output)

    def before(self, request: LLMRequest) -> None:
        self.guard.preflight(self.projection(request))

    def note(self, request: LLMRequest, response: LLMResponse) -> Decimal:
        cost = self.snapshot.cost(request.model, response.usage).total
        self.guard.record(cost)
        return cost


class Recorder:
    """Executes the recording plan against whatever adapter it is given."""

    def __init__(
        self,
        workload: WorkloadSpec,
        registry: Registry,
        bundle: Bundle,
        snapshot: PriceSnapshot,
        cassette_dir: Path,
        *,
        provider: str = "simulated",
        origin: str = "simulated",
        guard: SpendGuard | None = None,
        concurrency: int = 8,
    ) -> None:
        self.workload = workload
        self.registry = registry
        self.bundle = bundle
        self.snapshot = snapshot
        self.cassette_dir = cassette_dir
        self.provider = provider
        self.origin = origin
        self.guard = guard or SpendGuard()
        self.concurrency = concurrency
        self.store = CassetteStore(cassette_dir)
        # Every adapter this recorder builds is charged through one sink, so the cap applies to
        # generation, judging and entailment alike rather than to whichever path remembered.
        self.sink = GuardSink(self.guard, snapshot, get_base_counter())
        self.tiers = {
            role: TierProfile(registry.roles[role], role) for role in ("cheap", "mid", "frontier")
        }

    def _adapter(self, pipeline_id: str) -> AnyAdapter:
        """The adapter this recording runs on.

        ``provider="simulated"`` builds the deterministic in-process responder, which opens no
        socket. Any other provider builds the real HTTP adapter for it, which raises with a
        fixable message when the key or the provider config is missing. Either way the call goes
        through :class:`RecordingAdapter`, so an interrupted run resumes from its cassettes.
        """
        if self.provider != "simulated":
            live = build_live_adapter(self.provider, self.registry, get_settings())
            return RecordingAdapter(live, self.store, origin=self.origin, on_spend=self.sink)
        pipeline = self.workload.pipeline(pipeline_id)
        responder: Responder
        if self.workload.simulation is not None:
            # A workload that declares its own simulated rates gets the plain responder, which
            # reads them from the workload rather than from a table in the engine.
            responder = GroundedResponder(
                items=list(self.bundle.items),
                grounding=self.bundle.grounding,
                tiers=self.tiers,
                simulation=self.workload.simulation,
                pipeline_id=pipeline_id,
                output_contract=pipeline.output_contract,
            )
        else:
            # Imported here and not at module scope: this is the *demo's* invented behaviour,
            # and a module that reaches for it on import cannot be used by a workload that has
            # its own (UPGRADE_V4.md M15).
            from tokop.workloads.demo.responder import DemoResponder

            responder = DemoResponder(
                list(self.bundle.items),
                self.bundle.grounding,
                self.tiers,
                pipeline_id,
                pipeline.output_contract,
                pipeline.checkable,
            )
        inner = SimulatedAdapter(
            simulated_profiles(self.registry, list(self.registry.models)), responder
        )
        return RecordingAdapter(inner, self.store, origin=self.origin, on_spend=self.sink)

    def _runner(self, pipeline_id: str) -> Runner:
        adapter = self._adapter(pipeline_id)
        return Runner(
            self.workload,
            self.registry,
            self.snapshot,
            adapter,
            provider=self.provider,
            grounding=self.bundle.grounding,
            concurrency=self.concurrency,
            origin=self.origin,
        )

    async def _step(
        self,
        pipeline_id: str,
        split_name: str,
        tier: str,
        items: Sequence[Item],
        samples: int = 1,
    ) -> StepResult:
        model_id = self.registry.roles[tier]
        runner = self._runner(pipeline_id)
        result = await runner.run_pipeline(
            pipeline_id, items, model_id=model_id, split=split_name, samples=samples
        )
        # The guard is charged per call by `GuardSink`, on the calls that actually reach a
        # provider. Charging the step total here as well would bill every cassette hit a second
        # time and would put the check after the money had already moved.
        return StepResult(
            pipeline=pipeline_id,
            split=split_name,
            tier=tier,
            model_id=model_id,
            result=result,
            accuracy=accuracy_of(result, items),
            cost=result.total_cost,
        )

    def plan(self) -> list[PlannedStep]:
        """The recording plan, in spend order, as data rather than as control flow.

        SPEC.md section 6 fixes the order: cheapest first, with a sanity gate before the
        expensive part. Both :meth:`run` and :func:`project` read this list, so the cost an
        operator approves is a projection of the calls that will actually be made and not of a
        second, parallel description of them that can quietly drift.
        """
        # The matrix runs carry every repeat any cascade may ask for. Recorded once at full
        # depth: a setting that wants fewer samples reads a prefix, so every k is compared over
        # the same draws. B0 and B1 are single-call baselines and are never sampled.
        matrix_samples = self.workload.max_samples()
        steps = [
            PlannedStep("B2", split, tier, matrix_samples, size)
            for split, size in (
                ("calibration", len(self.bundle.calibration)),
                ("test", len(self.bundle.test)),
            )
            for tier in ("cheap", "mid", "frontier")
        ]
        steps += [
            PlannedStep(pipeline_id, "test", "frontier", 1, len(self.bundle.test))
            for pipeline_id in ("B0", "B1", "B2c")
            if pipeline_id in self.workload.pipelines
        ]
        return steps

    async def project(self, cap: Decimal | None = None) -> RecordingProjection:
        """Price the whole plan before the first call, and say how it was counted.

        The input side is counted, not guessed. Where the provider offers a token-counting
        endpoint the count is exact and free — one call per planned step, not one per request,
        because every request in a step renders the same prompt around a different question and
        the difference between them is a handful of tokens against a handbook. Where it does
        not, the base counter's estimate is used and every figure it touched is marked ``~``
        (non-negotiable 2).

        The cache is modelled the way the recorder runs it: the pre-warm writes the static
        prefix once, and every call after that reads it. Pricing a cached prefix as fresh input
        on all two hundred calls would put the projection so far above the bill that nobody
        could use it to choose a budget.
        """
        steps: list[StepProjection] = []
        exact_everywhere = True
        counted_by_provider = 0
        for planned in self.plan():
            items = self.bundle.calibration if planned.split == "calibration" else self.bundle.test
            pipeline = self.workload.pipeline(planned.pipeline)
            model_id = self.registry.roles[planned.tier]
            runner = self._runner(planned.pipeline)
            request = runner.render(pipeline, next(iter(items)), model_id)

            total_tokens, exact = await self._count_input(runner.adapter, request)
            exact_everywhere = exact_everywhere and exact
            counted_by_provider += int(exact)
            prefix_tokens, volatile_tokens = self._split_prefix(request, total_tokens)

            price = self.snapshot.price_for(model_id)
            generation = planned.generation_calls
            prewarm = 1 if request.static_prefix_text else 0
            million = Decimal(1_000_000)
            # One pre-warm writes the entry; the generation calls read it. With no breakpoint
            # in the pipeline the prefix is empty and every token is fresh input, which is
            # exactly what makes the un-cached baseline expensive.
            input_usd = (
                Decimal(prefix_tokens) * price.cache_write_5m * prewarm
                + Decimal(prefix_tokens) * price.cache_read * generation
                + Decimal(volatile_tokens) * price.input * generation
            ) / million
            # The pre-warm sends max_tokens 0 and bills no output, so the ceiling covers the
            # generation calls only.
            output_usd = (Decimal(pipeline.max_tokens) * price.output * generation) / million
            steps.append(
                StepProjection(
                    pipeline=planned.pipeline,
                    split=planned.split,
                    tier=planned.tier,
                    model_id=model_id,
                    generation_calls=generation,
                    prewarm_calls=prewarm,
                    prefix_tokens=prefix_tokens,
                    volatile_tokens=volatile_tokens,
                    max_output_tokens=pipeline.max_tokens,
                    counter=self.sink.counter.name,
                    exact_input=exact,
                    ceiling_usd=input_usd + output_usd,
                    floor_usd=input_usd,
                )
            )
        return RecordingProjection(
            steps=tuple(steps),
            cap_usd=cap,
            counter="the provider's counting endpoint"
            if exact_everywhere
            else self.sink.counter.name,
            exact_input=exact_everywhere,
            counting_calls=counted_by_provider,
        )

    def _split_prefix(self, request: LLMRequest, total_tokens: int) -> tuple[int, int]:
        """How the input splits into a cacheable prefix and the part that changes per task.

        The exact count and the base counter do not speak the same units — Anthropic's models
        from Claude 4.7 on tokenize the same text about 30% longer than earlier ones — so
        subtracting one from the other would book the tokenizer generation gap as volatile text
        and price 2,000 cached tokens per call at the full input rate. The base counter is used
        for the *share* instead, which is a ratio and survives the change of units, and the
        exact count supplies the magnitude.
        """
        base_prefix = self.sink.counter.count(request.static_prefix_text)
        if not base_prefix:
            return 0, total_tokens
        base_total = self.sink.counter.count(request.system_text) + sum(
            self.sink.counter.count(message.text) for message in request.messages
        )
        if base_total <= 0:  # pragma: no cover - a request with no text at all
            return 0, total_tokens
        prefix_tokens = min(total_tokens, round(total_tokens * base_prefix / base_total))
        return prefix_tokens, total_tokens - prefix_tokens

    async def _count_input(self, adapter: AnyAdapter, request: LLMRequest) -> tuple[int, bool]:
        """Exact input tokens from the provider if it will say, the base counter otherwise.

        The endpoint is wired and tested (``adapters/anthropic.py``) but until M13 nothing in
        the engine called it; this is its first caller. A provider that refuses, errors, or has
        no such endpoint falls back to the base counter and the caller marks the figure
        estimated rather than passing an estimate off as a count.
        """
        try:
            counted = await adapter.count_tokens(request)
        except (AdapterError, NotImplementedError):
            counted = None
        if counted is not None:
            return counted, True
        estimated = self.sink.counter.count(request.system_text) + sum(
            self.sink.counter.count(message.text) for message in request.messages
        )
        return estimated, False

    async def pilot(self, tasks: int = 10) -> PilotOutcome:
        """Run a few items of every planned step, to price the rest from real answers.

        SPEC.md section 6 step 1 asks for this and the pre-flight projection does not replace
        it: counting input is exact, but nothing before a call knows how long an answer will
        be, which is the whole width of the projected band. Ten real answers per step turn that
        band into a number.

        The pilot is not a separate purchase. Every call it makes is written to the same
        cassette store the full recording reads, so those tasks are paid for once and the run
        that follows replays them.
        """
        costs = Decimal(0)
        outputs: list[int] = []
        ran = 0
        for planned in self.plan():
            items = self.bundle.calibration if planned.split == "calibration" else self.bundle.test
            sample = list(items)[:tasks]
            step = await self._step(
                planned.pipeline, planned.split, planned.tier, sample, planned.samples
            )
            costs += step.cost
            ran += len(sample) * planned.samples
            outputs += [
                response.usage.output_visible + response.usage.output_reasoning
                for task in step.result.tasks
                for response in (c.response for c in task.calls)
            ]
        return PilotOutcome(
            tasks=ran,
            cost=costs,
            output_tokens=tuple(outputs),
            total_tasks=sum(step.items * step.samples for step in self.plan()),
        )

    async def run(self, *, engine: Any | None = None, enforce_gate: bool = True) -> RecordingReport:
        report = RecordingReport(origin=self.origin)
        calibration = list(self.bundle.calibration)
        test = list(self.bundle.test)

        def keep(step: StepResult) -> None:
            report.steps.append(step)
            report.total_cost += step.cost
            if engine is not None:
                items = calibration if step.split == "calibration" else test
                persist(engine, self.workload, step.result, items, self.snapshot)

        matrix_samples = self.workload.max_samples()
        items_for = {"calibration": calibration, "test": test}
        gate_checked = False

        for planned in self.plan():
            # 2. Sanity and difficulty gate, before the first test step — which is where the
            #    expensive part starts. Runs once, on what calibration has produced by then.
            if planned.split == "test" and not gate_checked:
                gate_checked = True
                if self._gate(report, enforce_gate):
                    return report
            # 1, 3, 4. B2 on calibration with each tier cheapest first; B2 on test with each
            #    tier, which completes the response matrix; then every remaining single-call
            #    pipeline on test with the frontier model. B0 and B1 are the waterfall's first
            #    two steps and B2c is the checkable-contract lever (UPGRADE_V3.md U4), which is
            #    a pipeline like any other and is priced like one.
            keep(
                await self._step(
                    planned.pipeline,
                    planned.split,
                    planned.tier,
                    items_for[planned.split],
                    planned.samples,
                )
            )

        # 5. Entailment for every distinct pair of answers a task's repeats produced, so
        #    `semantic-entropy-v1` can cluster by meaning at replay (UPGRADE_V3.md U6). Recorded
        #    over the *distinct extracted answers* rather than the raw generations: two correct
        #    answers citing different sentences are one meaning, and comparing raw JSON would
        #    make them two.
        if self.workload.entailment is not None and matrix_samples >= 2:
            report.entailment_cost, report.entailment_calls = await self._entailment_step(
                [("calibration", calibration), ("test", test)], matrix_samples
            )

        report.cassette_count = len(self.store)
        return report

    def _gate(self, report: RecordingReport, enforce: bool) -> bool:
        """SPEC.md section 6 step 2. True when the recording should stop here."""
        frontier = report.by_key("B2", "calibration", "frontier")
        cheap = report.by_key("B2", "calibration", "cheap")
        if enforce and frontier is not None and cheap is not None:
            if frontier.accuracy < FRONTIER_SANITY_FLOOR:
                report.stopped = (
                    f"The frontier model scored {frontier.accuracy:.1%} on calibration, below the "
                    f"{FRONTIER_SANITY_FLOOR:.0%} floor. Every answer is in the handbook, so this "
                    "is almost always a generator or grader bug rather than a model failure. "
                    "Stopped before spending more."
                )
                return True
            gap = frontier.accuracy - cheap.accuracy
            if gap < DIFFICULTY_GAP_FLOOR:
                report.stopped = (
                    f"The cheap tier scored {cheap.accuracy:.1%} against the frontier's "
                    f"{frontier.accuracy:.1%}, a gap of {gap:.1%}. The set is too easy to show "
                    "escalation: make the computation and exception templates harder, log the "
                    "change, and regenerate. Stopped before spending more."
                )
                return True
        return False

    async def _entailment_step(
        self, splits: Sequence[tuple[str, Sequence[Item]]], samples: int
    ) -> tuple[Decimal, int]:
        """Record an entailment judgement for every distinct answer pair in the matrix.

        Ordered pairs of *distinct* answers, both directions, because entailment is not
        symmetric. Identical answers are never sent: paying a model to confirm that "60" means
        what "60" means is money for nothing, and on a workload of numbers and yes/no that
        removes most of the bill.

        Every pair a clustering *might* ask about is recorded, which is slightly more than any
        one clustering asks. The excess is a property of recording rather than of running, and
        `entailment_recording_cost_usd` reports it where it was incurred, the way the deeper
        sample matrix already does (DECISIONS.md D27).
        """
        from tokop.optimize.scorers import answers_of
        from tokop.workloads.spec import EntailmentSpec

        spec: EntailmentSpec = self.workload.entailment  # type: ignore[assignment]
        model_id = self.registry.roles[spec.model_role]
        base = self.workload.cascade("B3").base_pipeline
        pairs: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for split_name, items in splits:
            for tier in ("cheap", "mid", "frontier"):
                runner = self._runner(base)
                result = await runner.run_pipeline(
                    base,
                    items,
                    model_id=self.registry.roles[tier],
                    split=split_name,
                    samples=samples,
                    prewarm=False,
                )
                by_id = {item.id: item for item in items}
                for task in result.tasks:
                    outputs = [call.response.text for call in task.calls][:samples]
                    view = TaskView(
                        task_id=task.task_id,
                        question=by_id[task.task_id].question,
                        output=outputs[0] if outputs else "",
                        samples=tuple(outputs),
                    )
                    distinct = sorted({a for a in answers_of(view, samples) if a is not None})
                    for left in distinct:
                        for right in distinct:
                            if left == right:
                                continue
                            key = (view.question, left, right)
                            if key not in seen:
                                seen.add(key)
                                pairs.append(key)

        if not pairs:
            return Decimal(0), 0
        runner = Runner(
            self.workload,
            self.registry,
            self.snapshot,
            self._entailment_adapter(),
            provider=self.provider,
            grounding=self.bundle.grounding,
            concurrency=self.concurrency,
            origin=self.origin,
        )
        judged = await runner.run_entailment(spec, pairs, model_id=model_id)
        return judged.total_cost, len(pairs)

    def _entailment_adapter(self) -> AnyAdapter:
        if self.provider != "simulated":
            live = build_live_adapter(self.provider, self.registry, get_settings())
            return RecordingAdapter(live, self.store, origin=self.origin, on_spend=self.sink)
        from tokop.workloads.demo.entailment_responder import DemoEntailmentResponder

        inner = SimulatedAdapter(
            simulated_profiles(self.registry, list(self.registry.models)),
            DemoEntailmentResponder(self.tiers),
        )
        return RecordingAdapter(inner, self.store, origin=self.origin, on_spend=self.sink)


def build_test_fixtures(
    workload: WorkloadSpec,
    registry: Registry,
    bundle: Bundle,
    fixtures_root: Path,
    *,
    taken: date | None = None,
    ledger_only: bool = False,
) -> RecordingReport:
    """Build ``fixtures/test/`` from the deterministic simulated provider.

    Spends nothing and opens no socket. Every run is marked ``origin: simulated`` and the
    manifest says so, so the UI can label it as test data rather than a recording.

    ``ledger_only`` rebuilds ``ledger.db`` from the committed cassettes and leaves
    ``manifest.json`` and ``extras.json`` alone. The ledger is generated rather than committed —
    it is a SQLite file that would churn on every run — so a fresh checkout has to materialise it
    before anything can read the report. Doing that is *replay*, not recording: it must not
    re-author the fixture record, and in particular must not stamp the rebuilding machine's
    tokenizer over the one the fixtures were actually built with (DECISIONS.md D26).
    """
    snapshot = registry.snapshot(list(registry.roles.values()), taken or date(2026, 9, 11))
    recorder = Recorder(
        workload,
        registry,
        bundle,
        snapshot,
        fixtures_root / "cassettes",
        provider="simulated",
        origin="simulated",
    )
    engine = make_engine(fixtures_root / "ledger.db")
    report = asyncio.run(recorder.run(engine=engine))
    report.cassette_count = len(recorder.store)

    if ledger_only:
        return report

    # The Compare and Brief examples the screens display (SPEC.md section 6 step 6).
    return write_fixture_set(
        report,
        recorder,
        fixtures_root,
        kind="test",
        note=(
            "Simulated test data, not a recording. Generated by the deterministic in-process "
            "provider in tokop/adapters/simulated.py because this build has no API credentials "
            "(DECISIONS.md D1). Every number computed from it is real engine output over these "
            "traces; the underlying answers are simulated."
        ),
        taken=taken,
    )


def write_fixture_set(
    report: RecordingReport,
    recorder: Recorder,
    fixtures_root: Path,
    *,
    kind: str,
    note: str,
    taken: date | None = None,
) -> RecordingReport:
    """Write the manifest and the screen examples that go beside a set of cassettes.

    Shared by the simulated fixture builder and by a live recording so that a recording is
    described by exactly the same manifest fields as the fixtures it replaces — the provenance
    block reads the same keys either way, and a field that only existed on one of them would be
    a field the UI could not rely on.
    """
    from tokop.extras import record_extras, write_extras

    report.cassette_count = len(recorder.store)
    report.cassette_digest = recorder.store.digest()
    extras = record_extras(
        recorder.registry, fixtures_root / "cassettes", origin=recorder.origin, taken=taken
    )
    write_extras(extras, fixtures_root / "extras.json")

    manifest = report.manifest()
    manifest["fixture_kind"] = kind
    manifest["extras"] = {
        "compares": [c["id"] for c in extras.compares],
        "brief_model": extras.brief["model_id"],
    }
    manifest["note"] = note
    (fixtures_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return report


def record_live(
    workload: WorkloadSpec,
    registry: Registry,
    bundle: Bundle,
    fixtures_root: Path,
    *,
    guard: SpendGuard,
    provider: str = "anthropic",
    taken: date | None = None,
    concurrency: int = 8,
    underpowered: bool = False,
) -> RecordingReport:
    """Record against a real provider, into ``fixtures/demo/``.

    Everything that decides whether this should happen — consent, the power of the split, the
    projection, the pilot and the operator's confirmation — happens in the CLI before this is
    called. By the time control arrives here the decision has been made and the only remaining
    question is whether the cap is reached, which ``GuardSink`` answers per call.
    """
    snapshot = registry.snapshot(list(registry.roles.values()), taken or today())
    recorder = Recorder(
        workload,
        registry,
        bundle,
        snapshot,
        fixtures_root / "cassettes",
        provider=provider,
        origin="live",
        guard=guard,
        concurrency=concurrency,
    )
    engine = make_engine(fixtures_root / "ledger.db")
    report = asyncio.run(recorder.run(engine=engine))
    report.underpowered = underpowered
    return write_fixture_set(
        report,
        recorder,
        fixtures_root,
        kind="demo",
        note=(
            f"Recorded against {provider} on "
            f"{report.manifest()['recorded_at']}. Every number computed from it is engine "
            "output over provider responses."
        ),
        taken=taken,
    )


PILOT_TASKS = 10


@dataclass(frozen=True)
class Projection:
    """What a recording is expected to cost, from a pilot."""

    pilot_tasks: int
    pilot_cost: Decimal
    p95_output_tokens: int
    total_tasks: int
    projected_usd: Decimal
    budget_usd: Decimal | None

    @property
    def within_budget(self) -> bool:
        return self.budget_usd is None or self.projected_usd <= self.budget_usd

    def describe(self) -> str:
        lines = [
            f"pilot:      {self.pilot_tasks} tasks per pipeline, ${self.pilot_cost:.4f} spent",
            f"p95 output: {self.p95_output_tokens:,} tokens",
            f"projected:  ${self.projected_usd:.2f} for {self.total_tasks:,} task-runs",
        ]
        if self.budget_usd is not None:
            lines.append(
                f"budget:     ${self.budget_usd} remaining — "
                + ("within budget" if self.within_budget else "OVER BUDGET, refusing to start")
            )
        else:
            lines.append("budget:     RECORD_BUDGET_USD is not set, so nothing may be spent")
        return "\n".join(lines)


def project_from_pilot(
    pilot_cost: Decimal,
    pilot_tasks: int,
    output_tokens: Sequence[int],
    total_tasks: int,
    budget: Decimal | None,
) -> Projection:
    """Project a whole recording from a small pilot.

    The projection scales the pilot's cost by task count and then inflates it by the ratio of
    p95 output to mean output, because the pilot's *mean* under-predicts a run that will contain
    long answers. Erring high is the right direction for something that refuses to start.
    """
    if pilot_tasks <= 0:
        raise RecordingStopped("a pilot must run at least one task")
    tokens = sorted(output_tokens) or [0]
    index = max(0, min(len(tokens) - 1, round(0.95 * (len(tokens) - 1))))
    p95 = tokens[index]
    mean = sum(tokens) / len(tokens) if tokens else 0.0
    inflation = Decimal(str(p95 / mean)) if mean > 0 else Decimal(1)
    per_task = pilot_cost / Decimal(pilot_tasks)
    projected = (per_task * Decimal(total_tasks) * max(inflation, Decimal(1))).quantize(
        Decimal("0.0001")
    )
    return Projection(
        pilot_tasks=pilot_tasks,
        pilot_cost=pilot_cost,
        p95_output_tokens=p95,
        total_tasks=total_tasks,
        projected_usd=projected,
        budget_usd=budget,
    )


def require_live_consent(settings: Any) -> Decimal:
    """Refuse to spend without an explicit budget and a key.

    Setting ``RECORD_BUDGET_USD`` *is* the consent to spend up to that amount (SPEC.md section
    0). Tokop never sets it, never raises it, and will not run a live recording without it.
    """
    if not settings.anthropic_api_key:
        raise RecordingStopped(
            "ANTHROPIC_API_KEY is not set, so no live call can be made. Run "
            "`tokop build-test-fixtures` for a simulated fixture set that costs nothing."
        )
    if settings.record_budget_usd is None:
        raise RecordingStopped(
            "RECORD_BUDGET_USD is not set. Setting it is your consent to spend up to that "
            "amount; Tokop will not set it for you. Read docs/DATASET.md first — it shows the "
            "questions your money would be spent answering."
        )
    if settings.record_budget_usd <= 0:
        raise RecordingStopped(
            f"RECORD_BUDGET_USD is ${settings.record_budget_usd}; nothing to spend."
        )
    return Decimal(settings.record_budget_usd)


def check_budget(guard: SpendGuard, projected: Decimal) -> None:
    """Raise before a recording starts if the projection already exceeds the cap."""
    try:
        guard.preflight(projected)
    except BudgetExceeded as exc:
        raise RecordingStopped(
            f"projected spend of ${projected:.2f} exceeds the remaining budget. {exc}"
        ) from exc
