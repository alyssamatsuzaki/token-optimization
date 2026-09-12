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
4. B0 and B1 on the test split with the frontier model.

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

from tokop.adapters.cassette import CassetteStore
from tokop.adapters.factory import AnyAdapter, build_live_adapter, simulated_profiles
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.budget import BudgetExceeded, SpendGuard
from tokop.core.pricing import PriceSnapshot
from tokop.core.registry import Registry
from tokop.db import make_engine
from tokop.settings import get_settings
from tokop.workloads.demo.dataset import DatasetBundle
from tokop.workloads.demo.generator import DemoItem
from tokop.workloads.demo.responder import DemoResponder, TierProfile
from tokop.workloads.runner import Runner, RunResult, persist
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
            "cassette_count": self.cassette_count,
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


def accuracy_of(result: RunResult, items: Sequence[DemoItem]) -> float:
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


class Recorder:
    """Executes the recording plan against whatever adapter it is given."""

    def __init__(
        self,
        workload: WorkloadSpec,
        registry: Registry,
        bundle: DatasetBundle,
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
            return RecordingAdapter(live, self.store, origin=self.origin)
        pipeline = self.workload.pipeline(pipeline_id)
        responder = DemoResponder(
            list(self.bundle.items),
            self.bundle.handbook,
            self.tiers,
            pipeline_id,
            pipeline.output_contract,
        )
        inner = SimulatedAdapter(
            simulated_profiles(self.registry, list(self.registry.models)), responder
        )
        return RecordingAdapter(inner, self.store, origin=self.origin)

    def _runner(self, pipeline_id: str) -> Runner:
        adapter = self._adapter(pipeline_id)
        return Runner(
            self.workload,
            self.registry,
            self.snapshot,
            adapter,
            provider=self.provider,
            handbook=self.bundle.handbook,
            concurrency=self.concurrency,
            origin=self.origin,
        )

    async def _step(
        self, pipeline_id: str, split_name: str, tier: str, items: Sequence[DemoItem]
    ) -> StepResult:
        model_id = self.registry.roles[tier]
        runner = self._runner(pipeline_id)
        result = await runner.run_pipeline(pipeline_id, items, model_id=model_id, split=split_name)
        self.guard.preflight(Decimal(0))  # caps are checked per call by the live adapter
        self.guard.record(result.total_cost)
        return StepResult(
            pipeline=pipeline_id,
            split=split_name,
            tier=tier,
            model_id=model_id,
            result=result,
            accuracy=accuracy_of(result, items),
            cost=result.total_cost,
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

        # 1. B2 on calibration with each tier, cheapest first.
        for tier in ("cheap", "mid", "frontier"):
            keep(await self._step("B2", "calibration", tier, calibration))

        # 2. Sanity and difficulty gate, before the expensive part.
        frontier = report.by_key("B2", "calibration", "frontier")
        cheap = report.by_key("B2", "calibration", "cheap")
        if enforce_gate and frontier is not None and cheap is not None:
            if frontier.accuracy < FRONTIER_SANITY_FLOOR:
                report.stopped = (
                    f"The frontier model scored {frontier.accuracy:.1%} on calibration, below the "
                    f"{FRONTIER_SANITY_FLOOR:.0%} floor. Every answer is in the handbook, so this "
                    "is almost always a generator or grader bug rather than a model failure. "
                    "Stopped before spending more."
                )
                return report
            gap = frontier.accuracy - cheap.accuracy
            if gap < DIFFICULTY_GAP_FLOOR:
                report.stopped = (
                    f"The cheap tier scored {cheap.accuracy:.1%} against the frontier's "
                    f"{frontier.accuracy:.1%}, a gap of {gap:.1%}. The set is too easy to show "
                    "escalation: make the computation and exception templates harder, log the "
                    "change, and regenerate. Stopped before spending more."
                )
                return report

        # 3. B2 on test with each tier: completes the response matrix.
        for tier in ("cheap", "mid", "frontier"):
            keep(await self._step("B2", "test", tier, test))

        # 4. B0 and B1 on test with the frontier model.
        for pipeline_id in ("B0", "B1"):
            keep(await self._step(pipeline_id, "test", "frontier", test))

        report.cassette_count = len(self.store)
        return report


def build_test_fixtures(
    workload: WorkloadSpec,
    registry: Registry,
    bundle: DatasetBundle,
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
    from tokop.extras import record_extras, write_extras

    extras = record_extras(registry, fixtures_root / "cassettes", origin="simulated", taken=taken)
    write_extras(extras, fixtures_root / "extras.json")

    manifest = report.manifest()
    manifest["fixture_kind"] = "test"
    manifest["extras"] = {
        "compares": [c["id"] for c in extras.compares],
        "brief_model": extras.brief["model_id"],
    }
    manifest["note"] = (
        "Simulated test data, not a recording. Generated by the deterministic in-process "
        "provider in tokop/adapters/simulated.py because this build has no API credentials "
        "(DECISIONS.md D1). Every number computed from it is real engine output over these "
        "traces; the underlying answers are simulated."
    )
    (fixtures_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return report


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
