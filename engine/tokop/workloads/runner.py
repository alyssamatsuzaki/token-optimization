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
from tokop.core.pricing import PriceSnapshot, cost_disagreement
from tokop.core.registry import Registry
from tokop.core.usage import TokenUsage
from tokop.db import Call, GradeRow, Run, Workload, session_scope, store_price_snapshot
from tokop.workloads.demo.generator import DemoItem
from tokop.workloads.grading import grade
from tokop.workloads.spec import PipelineSpec, WorkloadSpec, demo_timestamp


class AnyAdapter(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def count_tokens(self, request: LLMRequest) -> int | None: ...


@dataclass
class TaskResult:
    """One task's outcome, before it is written to the ledger."""

    task_id: str
    calls: list[tuple[LLMRequest, LLMResponse, str, int]] = field(default_factory=list)
    output: str = ""
    resolved_tier: str = "single"
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


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
        handbook: str = "",
        concurrency: int = 4,
        origin: str = "simulated",
        timestamp: str | None = None,
    ) -> None:
        self.workload = workload
        self.registry = registry
        self.snapshot = snapshot
        self.adapter = adapter
        self.provider = provider
        self.handbook = handbook
        self.origin = origin
        self.timestamp = timestamp or demo_timestamp()
        self._semaphore = asyncio.Semaphore(concurrency)
        if not workload.provider_allowed(provider):
            raise AdapterError(
                f"workload {workload.id!r} allows providers "
                f"{workload.allowed_providers} and {provider!r} is not among them"
            )

    def variables(self, item: DemoItem) -> dict[str, str]:
        return {
            "handbook": self.handbook,
            "question": item.question,
            "timestamp": self.timestamp,
        }

    def render(self, pipeline: PipelineSpec, item: DemoItem, model_id: str) -> LLMRequest:
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
        items: Sequence[DemoItem],
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
                result.calls.append((request, outcome, "single", 0))
            tasks.append(result)

        run_id = f"{self.workload.id}-{pipeline_id}-{split}-{model}"
        total = Decimal(0)
        for task in tasks:
            for _, response, _, _ in task.calls:
                total += self.snapshot.cost(model, response.usage).total
        for warm in prewarm_responses:
            total += self.snapshot.cost(model, warm.usage).total

        manifest = self.manifest(pipeline_id, split, [model], items, len(prewarm_responses))
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

    def manifest(
        self,
        pipeline_id: str,
        split: str,
        model_ids: list[str],
        items: Sequence[DemoItem],
        prewarm_calls: int,
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
    items: Sequence[DemoItem],
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
            for request, response, tier, tier_index in task.calls:
                breakdown = snapshot.cost(response.model, response.usage)
                task_cost += breakdown.total
                disagreement = (
                    cost_disagreement(breakdown.total, response.provider_cost_usd)
                    if response.provider_cost_usd is not None
                    else None
                )
                call = Call(
                    run_id=run.id,
                    task_id=task.task_id,
                    tier=tier,
                    tier_index=tier_index,
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
                call.set_usage(response.usage)
                session.add(call)

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
