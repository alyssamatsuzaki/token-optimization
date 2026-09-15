"""Run a pipeline over a split and write every call, grade and cost to the ledger.

The runner is deliberately boring. It renders each task's request from the pipeline spec, sends
it through whatever adapter the mode built, normalizes the usage, costs it against **the run's
own price snapshot**, grades the output, and writes a row. Everything interesting — findings,
cascades, proofs — is computed later from those rows, which is what makes SPEC.md
non-negotiable 1 achievable: there is exactly one place numbers enter the system.

Two behaviours worth stating:

* **Pre-warming.** A cache entry only becomes available once the first response begins, so
  firing a whole split in parallel makes every first request a miss. The runner sends one
  ``max_tokens: 0`` request per distinct prefix and awaits it before fanning out. Pre-warm calls
  are costed like any other call and appear in the trace.
* **Failures stay visible.** A call that raises is recorded as a failed call with its error, and
  the task is graded as unsuccessful (non-negotiable 8). It is never dropped from the
  denominator, because a pipeline that crashes on 5% of tasks is 5% worse, not 5% smaller.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from tokop.adapters.anthropic import prewarm_request
from tokop.adapters.base import AdapterError, LLMRequest, LLMResponse
from tokop.core.budget import BudgetExceeded
from tokop.core.pricing import PriceSnapshot, cost_disagreement
from tokop.core.registry import Registry
from tokop.core.usage import TokenUsage
from tokop.db import Call, GradeRow, Run, ToolCall, Workload, session_scope, store_price_snapshot
from tokop.optimize.graph import SINGLE_STEP, Graph, GraphError, Step, compile_graph
from tokop.workloads.grading import grade
from tokop.workloads.item import Item
from tokop.workloads.spec import (
    BlockSpec,
    EntailmentSpec,
    JudgeSpec,
    PipelineSpec,
    WorkloadSpec,
    demo_timestamp,
    render_request,
)
from tokop.workloads.tools import ToolContext, ToolResult, get_tool
from tokop.workloads.verification import AnswerView, parse_verdict


class AnyAdapter(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def count_tokens(self, request: LLMRequest) -> int | None: ...


@dataclass(frozen=True)
class TaskCall:
    """One provider call a task made, and where in the pipeline it came from.

    A tuple until M16, when a fifth field had to go on it. ``step`` is that field: every finding
    the graph milestone exists to compute is a question about which step made a call, and a call
    that cannot say which step it belongs to is a call none of them can be computed from.
    """

    request: LLMRequest
    response: LLMResponse
    tier: str = "single"
    tier_index: int = 0
    step: str = SINGLE_STEP


@dataclass
class TaskResult:
    """One task's outcome, before it is written to the ledger."""

    task_id: str
    calls: list[TaskCall] = field(default_factory=list)
    output: str = ""
    resolved_tier: str = "single"
    error: str | None = None
    #: What the tools did. Kept apart from ``calls`` on purpose: a tool call sends no tokens and
    #: costs no money, and folding one into a list whose every consumer averages tokens per call
    #: would quietly move figures that have nothing to do with tools. What a tool *does* cost is
    #: the input tokens its result adds to the next step's prompt, and that lands in that step's
    #: call where it belongs.
    tool_calls: list[StepToolCall] = field(default_factory=list)
    #: Steps that did not run, with the reason. A retry that never fired is the evidence for
    #: "the verifier never changed an outcome", so a skipped step is a result and not a gap.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.error is not None

    def calls_for(self, step: str) -> list[TaskCall]:
        return [call for call in self.calls if call.step == step]


@dataclass(frozen=True)
class StepToolCall:
    """One tool call, tagged with the step that made it."""

    step: str
    result: ToolResult


@dataclass
class JudgeRunResult:
    """Everything one verifier run produced (UPGRADE_V3.md U1)."""

    model_id: str
    calls: list[tuple[AnswerView, LLMRequest, LLMResponse]]
    prewarm: list[tuple[LLMRequest, LLMResponse]]
    total_cost: Decimal

    @property
    def n(self) -> int:
        return len(self.calls)


@dataclass
class RunResult:
    """Everything one run produced."""

    run_id: str
    pipeline_id: str
    split: str
    tasks: list[TaskResult]
    total_cost: Decimal
    model_ids: list[str]
    price_snapshot_id: str
    origin: str
    prewarm_calls: int
    manifest: dict[str, Any]
    #: Pre-warm calls, kept so they can be written to the ledger like any other call. A run
    #: whose call rows did not sum to its headline cost would be a run nobody could audit.
    prewarm: list[tuple[LLMRequest, LLMResponse]] = field(default_factory=list)


def git_sha() -> str:
    """The commit a run was produced at, for the manifest."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 else "unknown"


@dataclass(frozen=True)
class TierProfile:
    """One tier: a model id and the role it plays in a cascade.

    Lives here rather than beside the demo's responder, which is where it started, because a
    tier is not a demo concept — it is what the runner routes between and what a simulated
    provider needs to know to answer at the right rate (UPGRADE_V4.md M15).
    """

    model_id: str
    role: str


def _stop_on_budget(outcomes: Sequence[Any]) -> None:
    """Let a budget refusal end the run instead of becoming a split of failed tasks.

    A provider failure is a task outcome and stays one: it counts as unsuccessful and shows in
    the trace (non-negotiable 8). A cap being reached is not a model failure but the operator's
    own limit, and absorbing it here would record every remaining task as unsuccessful, spend
    the whole budget doing so, and hand the report an accuracy figure that describes a budget.
    """
    for outcome in outcomes:
        if isinstance(outcome, BudgetExceeded):
            raise outcome


class Runner:
    """Executes pipelines. One instance per run."""

    def __init__(
        self,
        workload: WorkloadSpec,
        registry: Registry,
        snapshot: PriceSnapshot,
        adapter: AnyAdapter,
        *,
        provider: str = "anthropic",
        grounding: str = "",
        concurrency: int = 4,
        origin: str = "simulated",
        timestamp: str | None = None,
    ) -> None:
        self.workload = workload
        self.registry = registry
        self.snapshot = snapshot
        self.adapter = adapter
        self.provider = provider
        self.grounding = grounding
        self.origin = origin
        self.timestamp = timestamp or demo_timestamp()
        self._semaphore = asyncio.Semaphore(concurrency)
        if not workload.provider_allowed(provider):
            raise AdapterError(
                f"workload {workload.id!r} allows providers "
                f"{workload.allowed_providers} and {provider!r} is not among them"
            )

    def variables(self, item: Item) -> dict[str, str]:
        return {
            "grounding": self.grounding,
            "question": item.question,
            "timestamp": self.timestamp,
        }

    def render(self, pipeline: PipelineSpec, item: Item, model_id: str) -> LLMRequest:
        return pipeline.render(self.provider, model_id, self.variables(item))

    async def _call(self, request: LLMRequest) -> LLMResponse:
        async with self._semaphore:
            return await self.adapter.complete(request)

    async def prewarm(
        self, requests: Sequence[LLMRequest]
    ) -> tuple[list[LLMRequest], list[LLMResponse]]:
        """Warm each distinct cacheable prefix once, and wait.

        Sent sequentially per prefix and awaited before the fan-out, because the entry does not
        exist until the first response begins.
        """
        seen: dict[tuple[str, str], LLMRequest] = {}
        for request in requests:
            prefix = request.static_prefix_text
            if not prefix:
                continue
            seen.setdefault((request.model, prefix), request)
        sent: list[LLMRequest] = []
        warmed: list[LLMResponse] = []
        for request in seen.values():
            warm = prewarm_request(request)
            sent.append(warm)
            warmed.append(await self.adapter.complete(warm))
        return sent, warmed

    async def run_pipeline(
        self,
        pipeline_id: str,
        items: Sequence[Item],
        *,
        model_id: str | None = None,
        split: str = "test",
        prewarm: bool = True,
        samples: int = 1,
    ) -> RunResult:
        """Run one pipeline over one split.

        ``samples`` asks the model the *same* question that many times, for a scorer that reads
        the disagreement between repeated generations. The requests are identical but for
        ``sample_index``, which exists only so a content-addressed cassette can hold more than
        one of them; sample 0 is the ordinary single call and hashes as one.
        """
        if samples < 1:
            raise AdapterError(f"samples must be at least 1, got {samples}")
        pipeline = self.workload.pipeline(pipeline_id)
        if pipeline.is_graph:
            return await self.run_graph(
                pipeline_id, items, model_id=model_id, split=split, prewarm=prewarm, samples=samples
            )
        model = model_id or self.registry.role(pipeline.model_role).model_id
        requests = [
            self.render(pipeline, item, model).model_copy(update={"sample_index": index})
            for item in items
            for index in range(samples)
        ]

        prewarm_responses: list[LLMResponse] = []
        prewarm_pairs: list[LLMRequest] = []
        if prewarm:
            prewarm_pairs, prewarm_responses = await self.prewarm(requests)

        responses = await asyncio.gather(
            *(self._call(request) for request in requests), return_exceptions=True
        )
        _stop_on_budget(responses)

        tasks: list[TaskResult] = []
        for position, item in enumerate(items):
            window = slice(position * samples, (position + 1) * samples)
            result = TaskResult(task_id=item.id)
            for request, outcome in zip(requests[window], responses[window], strict=True):
                if isinstance(outcome, BaseException):
                    # A failed call stays visible and the task counts as unsuccessful.
                    result.error = f"{type(outcome).__name__}: {outcome}"
                    outcome = LLMResponse(
                        text="",
                        usage=TokenUsage(),
                        model=model,
                        provider=self.provider,
                        latency_ms=0.0,
                        error=result.error,
                    )
                elif request.sample_index == 0:
                    # Sample 0 is the tier's answer for every scorer that does not sample. What
                    # a sampling scorer returns instead is its own decision, made downstream
                    # from all k outputs, so the runner does not presume one here.
                    result.output = outcome.text
                result.calls.append(TaskCall(request=request, response=outcome))
            tasks.append(result)

        run_id = f"{self.workload.id}-{pipeline_id}-{split}-{model}"
        total = Decimal(0)
        for task in tasks:
            for call in task.calls:
                total += self.snapshot.cost(model, call.response.usage).total
        for warm in prewarm_responses:
            total += self.snapshot.cost(model, warm.usage).total

        manifest = self.manifest(
            pipeline_id, split, [model], items, len(prewarm_responses), samples
        )
        return RunResult(
            run_id=run_id,
            pipeline_id=pipeline_id,
            split=split,
            tasks=tasks,
            total_cost=total,
            model_ids=[model],
            price_snapshot_id=self.snapshot.snapshot_id,
            origin=self.origin,
            prewarm_calls=len(prewarm_responses),
            manifest=manifest,
            prewarm=list(zip(prewarm_pairs, prewarm_responses, strict=True)),
        )

    # ------------------------------------------------------------------ graphs (M16)

    def render_step(
        self, pipeline: PipelineSpec, step: Step, model: str, variables: dict[str, str]
    ) -> LLMRequest:
        """One step's request.

        The compiled single step carries no spec and renders through the pipeline itself, with
        an empty ``step``, which is what keeps its cassette key identical to the one every
        committed fixture was recorded under.
        """
        if step.spec is None:
            return pipeline.render(self.provider, model, variables)
        return render_request(
            self.provider,
            model,
            step.spec.system,
            step.spec.user,
            step.max_tokens(pipeline),
            variables,
            step=step.id,
        )

    def step_model(self, pipeline: PipelineSpec, step: Step, model_id: str | None) -> str:
        """Which model runs this step.

        ``model_id`` is how a tier is measured: the recorder runs the whole pipeline at cheap,
        then at mid, then at frontier. It replaces the model of every step that does not name a
        role of its own; a step the workload pinned to a tier stays pinned, because that pinning
        is part of what the pipeline *is* rather than part of what is being varied.
        """
        if step.spec is not None and step.spec.model_role:
            return self.registry.role(step.spec.model_role).model_id
        return model_id or self.registry.role(pipeline.model_role).model_id

    def _warmable(
        self, pipeline: PipelineSpec, graph: Graph, item: Item, model_id: str | None
    ) -> list[LLMRequest]:
        """The step requests whose cacheable prefix does not depend on an earlier step.

        Rendered twice with different stand-ins for the step inputs. A prefix that comes out the
        same both times cannot contain an earlier step's output, so warming it warms the entry
        the real call will hit. A prefix that differs is a prefix no pre-warm can write — the
        text is not known until the step before it has answered — and it is left alone rather
        than warmed with a guess, which would pay for a cache entry nothing goes on to read.
        """
        base = self.variables(item)
        warmable: list[LLMRequest] = []
        for step in graph.generation_steps:
            model = self.step_model(pipeline, step, model_id)
            probes = [
                self.render_step(
                    pipeline, step, model, {**base, **dict.fromkeys(step.inputs, stand_in)}
                )
                for stand_in in ("", "\u0001")
            ]
            prefix = probes[0].static_prefix_text
            if prefix and prefix == probes[1].static_prefix_text:
                warmable.append(probes[0])
        return warmable

    async def _run_step(
        self,
        pipeline: PipelineSpec,
        step: Step,
        item: Item,
        model_id: str | None,
        variables: dict[str, str],
        result: TaskResult,
    ) -> str:
        """Run one step and return the text its dependents will see."""
        if step.is_tool:
            assert step.spec is not None  # compile_graph refuses a tool step with no tool
            tool = get_tool(step.spec.tool)
            query = BlockSpec(text=step.spec.query).render(variables).text
            text = tool.run(query, ToolContext(grounding=self.grounding, question=item.question))
            result.tool_calls.append(
                StepToolCall(
                    step=step.id, result=ToolResult(tool=tool.name, query=query, text=text)
                )
            )
            return text

        model = self.step_model(pipeline, step, model_id)
        request = self.render_step(pipeline, step, model, variables)
        try:
            response = await self._call(request)
        except BudgetExceeded:
            # The operator's cap, not a model failure. It ends the run rather than becoming a
            # task outcome, for the reason `_stop_on_budget` gives.
            raise
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            response = LLMResponse(
                text="",
                usage=TokenUsage(),
                model=model,
                provider=self.provider,
                latency_ms=0.0,
                error=result.error,
            )
        result.calls.append(TaskCall(request=request, response=response, step=step.id))
        return response.text

    async def _run_task_graph(
        self,
        pipeline: PipelineSpec,
        graph: Graph,
        item: Item,
        model_id: str | None,
    ) -> TaskResult:
        """Walk one task through the graph, in the order the compiler fixed."""
        result = TaskResult(task_id=item.id)
        base = self.variables(item)
        outputs: dict[str, str] = {}
        verdicts: dict[str, int] = {}
        for step in graph.steps:
            if result.failed:
                result.skipped.append((step.id, "an earlier step failed"))
                continue
            skip = self._skip_reason(step, verdicts)
            if skip is not None:
                result.skipped.append((step.id, skip))
                outputs[step.id] = outputs[self._retried(step, graph)]
                continue
            variables = {**base, **{name: outputs[name] for name in step.inputs}}
            text = await self._run_step(pipeline, step, item, model_id, variables, result)
            outputs[step.id] = text
            if step.kind == "verify":
                verdicts[step.id] = parse_verdict(text).correct
        if not result.failed:
            result.output = outputs[graph.terminal.id]
        return result

    @staticmethod
    def _skip_reason(step: Step, verdicts: dict[str, int]) -> str | None:
        """Why this step does not run, or ``None`` if it does.

        Only a retry is ever skipped, and only because the verifier it consumes was satisfied.
        That is the whole conditional vocabulary of a graph in M16, and it is deliberately not a
        general expression language: a condition Tokop cannot read is a step whose cost nobody
        can attribute.
        """
        if step.kind != "retry":
            return None
        objections = [name for name in step.inputs if verdicts.get(name) == 0]
        if objections:
            return None
        return f"the {', '.join(n for n in step.inputs if n in verdicts)} step raised no objection"

    @staticmethod
    def _retried(step: Step, graph: Graph) -> str:
        """What a skipped retry falls back to: the output of the step it would have retried."""
        for name in step.inputs:
            if graph.step(name).kind != "verify":
                return name
        raise GraphError(
            f"{graph.pipeline_id} step {step.id!r} retries nothing: every step it consumes is a "
            "verify step, so there is no answer for it to fall back on when the verifier is "
            "satisfied."
        )

    async def run_graph(
        self,
        pipeline_id: str,
        items: Sequence[Item],
        *,
        model_id: str | None = None,
        split: str = "test",
        prewarm: bool = True,
        samples: int = 1,
    ) -> RunResult:
        """Run a multi-step pipeline over one split (UPGRADE_V4.md M16).

        Steps of one task run in order, because a step consumes what the step before it
        produced; tasks run in parallel, because they do not. The cost of a task is the sum over
        its steps, which is what lets the optimizer propose deleting one.
        """
        pipeline = self.workload.pipeline(pipeline_id)
        graph = compile_graph(pipeline)
        if samples > 1:
            raise AdapterError(
                f"{pipeline_id} is a graph of {len(graph.steps)} steps and was asked for "
                f"{samples} samples per task. What k repeats of a multi-call pipeline mean — the "
                "whole graph again, or only its last step — is a question this build has not "
                "answered, and guessing would charge for one and report the other."
            )
        models = sorted({self.step_model(pipeline, step, model_id) for step in graph.steps})

        prewarm_pairs: list[LLMRequest] = []
        prewarm_responses: list[LLMResponse] = []
        if prewarm and items:
            prewarm_pairs, prewarm_responses = await self.prewarm(
                self._warmable(pipeline, graph, items[0], model_id)
            )

        outcomes = await asyncio.gather(
            *(self._run_task_graph(pipeline, graph, item, model_id) for item in items),
            return_exceptions=True,
        )
        _stop_on_budget(outcomes)
        tasks: list[TaskResult] = []
        for outcome in outcomes:
            # A provider failure is already a task outcome by the time it gets here: `_run_step`
            # records it as a failed call and the task grades unsuccessful. Anything still
            # raising is a defect in the graph itself, and it ends the run rather than becoming
            # a split of tasks that silently scored zero.
            if isinstance(outcome, BaseException):
                raise outcome
            tasks.append(outcome)

        total = Decimal(0)
        for task in tasks:
            for call in task.calls:
                total += self.snapshot.cost(call.response.model, call.response.usage).total
        for warm_request, warm in zip(prewarm_pairs, prewarm_responses, strict=True):
            total += self.snapshot.cost(warm_request.model, warm.usage).total

        manifest = self.manifest(pipeline_id, split, models, items, len(prewarm_responses), samples)
        manifest["graph"] = graph.as_dict()
        return RunResult(
            run_id=f"{self.workload.id}-{pipeline_id}-{split}-{'+'.join(models)}",
            pipeline_id=pipeline_id,
            split=split,
            tasks=tasks,
            total_cost=total,
            model_ids=models,
            price_snapshot_id=self.snapshot.snapshot_id,
            origin=self.origin,
            prewarm_calls=len(prewarm_responses),
            manifest=manifest,
            prewarm=list(zip(prewarm_pairs, prewarm_responses, strict=True)),
        )

    async def run_judge(
        self,
        judge: JudgeSpec,
        views: Sequence[AnswerView],
        *,
        model_id: str,
        prewarm: bool = True,
    ) -> JudgeRunResult:
        """Run one verifier over a list of answers (UPGRADE_V3.md U1).

        Deliberately not ``run_pipeline``. A pipeline answers a task; a judge reviews an answer,
        so the unit is an ``AnswerView`` rather than a ``Item`` and there is no grading step
        at the end — reading a verdict out of the reply is ``workloads/verification.py``'s job
        and happens above this. What is the same is everything that costs money: the same
        pre-warming, the same concurrency, the same cassette-backed adapter, so a judge call is
        priced exactly like any other call and shows up in the trace like one.
        """
        requests = [
            judge.render(self.provider, model_id, self.judge_variables(view)) for view in views
        ]
        prewarm_requests: list[LLMRequest] = []
        prewarm_responses: list[LLMResponse] = []
        if prewarm:
            prewarm_requests, prewarm_responses = await self.prewarm(requests)

        outcomes = await asyncio.gather(
            *(self._call(request) for request in requests), return_exceptions=True
        )
        _stop_on_budget(outcomes)
        calls: list[tuple[AnswerView, LLMRequest, LLMResponse]] = []
        for view, request, outcome in zip(views, requests, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # A judge call that failed verified nothing. It stays visible as a failed call
                # and the verdict parser records it as unverified (non-negotiable 8).
                outcome = LLMResponse(
                    text="",
                    usage=TokenUsage(),
                    model=model_id,
                    provider=self.provider,
                    latency_ms=0.0,
                    error=f"{type(outcome).__name__}: {outcome}",
                )
            calls.append((view, request, outcome))

        total = sum(
            (self.snapshot.cost(model_id, response.usage).total for _, _, response in calls),
            Decimal(0),
        ) + sum(
            (self.snapshot.cost(model_id, warm.usage).total for warm in prewarm_responses),
            Decimal(0),
        )
        return JudgeRunResult(
            model_id=model_id,
            calls=calls,
            prewarm=list(zip(prewarm_requests, prewarm_responses, strict=True)),
            total_cost=total,
        )

    async def run_entailment(
        self,
        entailment: EntailmentSpec,
        pairs: Sequence[tuple[str, str, str]],
        *,
        model_id: str,
        prewarm: bool = False,
    ) -> JudgeRunResult:
        """Ask one model whether each (question, A, B) pair entails (UPGRADE_V3.md U6).

        Reuses ``JudgeRunResult`` because the shape is identical — a list of small calls with
        their cost — and inventing a second one would mean two places that have to agree about
        how a call is priced. Pre-warming is off by default: the prompt carries no large static
        prefix, so there is no cache entry to warm and a warm call would be pure cost.
        """
        views = [
            AnswerView(task_id=f"{question[:24]}|{index}", question=question, answer=f"{a}\n{b}")
            for index, (question, a, b) in enumerate(pairs)
        ]
        requests = [
            entailment.render(
                self.provider,
                model_id,
                {
                    "question": question,
                    "answer_a": a,
                    "answer_b": b,
                    "grounding": self.grounding,
                    "timestamp": self.timestamp,
                },
            )
            for question, a, b in pairs
        ]
        prewarm_requests: list[LLMRequest] = []
        prewarm_responses: list[LLMResponse] = []
        if prewarm:
            prewarm_requests, prewarm_responses = await self.prewarm(requests)

        outcomes = await asyncio.gather(
            *(self._call(request) for request in requests), return_exceptions=True
        )
        _stop_on_budget(outcomes)
        calls: list[tuple[AnswerView, LLMRequest, LLMResponse]] = []
        for view, request, outcome in zip(views, requests, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                outcome = LLMResponse(
                    text="",
                    usage=TokenUsage(),
                    model=model_id,
                    provider=self.provider,
                    latency_ms=0.0,
                    error=f"{type(outcome).__name__}: {outcome}",
                )
            calls.append((view, request, outcome))

        total = sum(
            (self.snapshot.cost(model_id, response.usage).total for _, _, response in calls),
            Decimal(0),
        ) + sum(
            (self.snapshot.cost(model_id, warm.usage).total for warm in prewarm_responses),
            Decimal(0),
        )
        return JudgeRunResult(
            model_id=model_id,
            calls=calls,
            prewarm=list(zip(prewarm_requests, prewarm_responses, strict=True)),
            total_cost=total,
        )

    def judge_variables(self, view: AnswerView) -> dict[str, str]:
        """What a judge prompt may render. ``answer`` is the extra one; gold is not among them.

        The workload supplies the grounding document, so the judge checks against the same one
        pipeline was given. ``view.context`` is ignored here on purpose: a judge that graded
        against whatever context was attached to the answer could be handed a favourable one.
        """
        return {
            "grounding": self.grounding,
            "question": view.question,
            "answer": view.answer,
            "timestamp": self.timestamp,
        }

    def manifest(
        self,
        pipeline_id: str,
        split: str,
        model_ids: list[str],
        items: Sequence[Item],
        prewarm_calls: int,
        samples: int = 1,
    ) -> dict[str, Any]:
        """Everything needed to reproduce a run (SPEC.md section 6)."""
        import hashlib
        import json as _json

        split_hash = hashlib.sha256(
            _json.dumps([i.id for i in items], sort_keys=True).encode()
        ).hexdigest()[:16]
        return {
            "workload": self.workload.id,
            "pipeline": pipeline_id,
            "split": split,
            "split_size": len(items),
            "split_hash": split_hash,
            "model_ids": model_ids,
            "provider": self.provider,
            "origin": self.origin,
            "seed": self.workload.dataset.seed,
            "git_sha": git_sha(),
            "recorded_at": datetime.now(UTC).isoformat(),
            "prewarm_calls": prewarm_calls,
            # How many generations per task this run holds. Replay needs it: a matrix recorded
            # at depth k is only reproducible by something that knows to ask for k.
            "samples": samples,
            "price_snapshot_id": self.snapshot.snapshot_id,
            "prices": {
                model_id: {
                    "input": str(price.input),
                    "output": str(price.output),
                    "cache_write_5m": str(price.cache_write_5m),
                    "cache_read": str(price.cache_read),
                    "source_url": price.provenance.source_url,
                    "retrieved": price.provenance.retrieved.isoformat(),
                    "verified": price.provenance.verified,
                }
                for model_id, price in self.snapshot.prices.items()
                if model_id in model_ids
            },
        }


def persist(
    engine: Any,
    workload: WorkloadSpec,
    result: RunResult,
    items: Sequence[Item],
    snapshot: PriceSnapshot,
    *,
    store_content: bool = True,
) -> None:
    """Write a run and its calls and grades to the ledger."""
    by_id = {item.id: item for item in items}
    with session_scope(engine) as session:
        if session.get(Workload, workload.id) is None:
            session.add(
                Workload(id=workload.id, name=workload.name, description=workload.description)
            )
        store_price_snapshot(
            session,
            snapshot.snapshot_id,
            snapshot.taken.isoformat(),
            {m: str(p.input) for m, p in snapshot.prices.items()},
        )

        existing = session.get(Run, result.run_id)
        if existing is not None:
            from tokop.db import delete_run

            delete_run(session, result.run_id)
            session.flush()

        run = Run(
            id=result.run_id,
            workload_id=workload.id,
            pipeline_id=result.pipeline_id,
            split=result.split,
            model_ids=result.model_ids,
            price_snapshot_id=result.price_snapshot_id,
            seed=workload.dataset.seed,
            git_sha=str(result.manifest.get("git_sha", "")),
            origin=result.origin,
            status="complete",
            finished_at=datetime.now(UTC),
            total_cost_usd=str(result.total_cost),
            manifest=result.manifest,
        )
        session.add(run)

        for warm_request, warm_response in result.prewarm:
            breakdown = snapshot.cost(warm_response.model, warm_response.usage)
            warm_call = Call(
                run_id=run.id,
                task_id="__prewarm__",
                tier="prewarm",
                provider=warm_response.provider,
                model=warm_response.model,
                prompt_hash=warm_request.prompt_hash(),
                cassette_key=warm_request.cassette_key(),
                reused=warm_response.reused,
                prewarm=True,
                cost_usd=str(breakdown.total),
                cost_by_bucket={k: str(v) for k, v in breakdown.by_bucket.items()},
                price_snapshot_id=snapshot.snapshot_id,
                latency_ms=warm_response.latency_ms,
                ttft_ms=warm_response.ttft_ms,
                stop_reason=warm_response.stop_reason,
                decision="prewarm",
                request_json=warm_request.canonical() if store_content else None,
                response_text=None,
            )
            warm_call.set_usage(warm_response.usage)
            session.add(warm_call)

        for task in result.tasks:
            item = by_id[task.task_id]
            task_cost = Decimal(0)
            for tool_call in task.tool_calls:
                session.add(
                    ToolCall(
                        run_id=run.id,
                        task_id=task.task_id,
                        step=tool_call.step,
                        tool=tool_call.result.tool,
                        query=tool_call.result.query,
                        result_chars=tool_call.result.chars,
                        result_text=tool_call.result.text if store_content else None,
                    )
                )
            for call in task.calls:
                request, response = call.request, call.response
                tier, tier_index = call.tier, call.tier_index
                breakdown = snapshot.cost(response.model, response.usage)
                task_cost += breakdown.total
                disagreement = (
                    cost_disagreement(breakdown.total, response.provider_cost_usd)
                    if response.provider_cost_usd is not None
                    else None
                )
                row = Call(
                    run_id=run.id,
                    task_id=task.task_id,
                    tier=tier,
                    tier_index=tier_index,
                    step=call.step,
                    attempt=response.attempt,
                    provider=response.provider,
                    model=response.model,
                    prompt_hash=request.prompt_hash(),
                    cassette_key=request.cassette_key(),
                    reused=response.reused,
                    prewarm=request.prewarm,
                    cost_usd=str(breakdown.total),
                    cost_by_bucket={k: str(v) for k, v in breakdown.by_bucket.items()},
                    price_snapshot_id=snapshot.snapshot_id,
                    provider_cost_usd=(
                        str(response.provider_cost_usd)
                        if response.provider_cost_usd is not None
                        else None
                    ),
                    cost_disagreement=str(disagreement) if disagreement is not None else None,
                    latency_ms=response.latency_ms,
                    ttft_ms=response.ttft_ms,
                    stop_reason=response.stop_reason,
                    decision="answered" if tier == task.resolved_tier else "escalated",
                    error=response.error,
                    request_json=request.canonical() if store_content else None,
                    response_text=response.text if store_content else None,
                )
                row.set_usage(response.usage)
                session.add(row)

            outcome = grade(task.output, item.gold, item.answer_type, item.aliases)
            session.add(
                GradeRow(
                    run_id=run.id,
                    task_id=task.task_id,
                    correct=outcome.correct and not task.failed,
                    parsed=outcome.parsed,
                    reason=task.error or outcome.reason,
                    gold=item.gold,
                    answer_type=item.answer_type,
                    question_type=item.question_type,
                    resolved_tier=task.resolved_tier,
                    task_cost_usd=str(task_cost),
                )
            )


def today() -> date:
    return datetime.now(UTC).date()
