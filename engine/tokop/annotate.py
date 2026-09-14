"""Buy the strong labels that correct a cheap judge (UPGRADE_V3.md U1, U2).

This is a separate command from `tokop record` for a reason that is not organisational. The
allocation policy reads *disagreement between the two arms*, and the candidate arm is a cascade
whose answer on each task comes from whichever tier the router stopped at. That is only known
once calibration has chosen an operating point. So the order is: record the matrix, choose the
operating point, then spend the annotation budget on the tasks where a strong label actually
buys interval.

What one run does, in order:

1. Replay both arms on the test split and take the answer each one returned.
2. Run the **cheap judge** over every answer in both arms. This is the ``G`` of the estimator,
   and it is the only part that scales with the workload.
3. Project what one strong-graded *item* — both arms, since the estimator is paired — will
   cost, from the judge calls just recorded, repriced at the strong grader's rates.
4. Allocate ``pi(x)`` under the budget and draw.
5. Run the **strong grader** on the drawn items, or write a human review queue instead.
6. Write ``annotations.json`` next to the fixtures: the rates, the draws, every verdict, and
   what all of it cost.

Against live providers this spends real money and is capped by the workload's
``annotation.budget_usd``. Against the simulated provider it spends nothing and marks the set
``origin: simulated``, exactly as the recorder does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokop.adapters.cassette import CassetteStore
from tokop.adapters.factory import AnyAdapter, build_live_adapter, simulated_profiles
from tokop.adapters.recording import RecordingAdapter
from tokop.adapters.simulated import SimulatedAdapter
from tokop.core.pricing import PriceSnapshot
from tokop.core.registry import Registry
from tokop.core.usage import TokenUsage
from tokop.optimize.annotation import (
    AnnotatedArm,
    AnnotatedItem,
    AnnotationError,
    AnnotationSet,
    ItemFeatures,
    QueueReport,
    allocate,
    allocation_policy,
    annotations_path,
)
from tokop.paths import repo_root
from tokop.workloads.demo.dataset import DatasetBundle
from tokop.workloads.demo.judge_responder import DemoJudgeResponder
from tokop.workloads.demo.responder import TierProfile
from tokop.workloads.runner import JudgeRunResult, Runner
from tokop.workloads.spec import JudgeSpec, WorkloadSpec
from tokop.workloads.verification import (
    AnswerView,
    QueueEntry,
    VerifierVerdict,
    render_queue,
    verifier_kind,
)

BASELINE_ARM = "baseline"
CANDIDATE_ARM = "candidate"


class AnnotationStopped(RuntimeError):
    """The annotation run stopped on purpose, before spending more."""


@dataclass
class AnnotationRun:
    """What one `tokop annotate` produced."""

    steps: list[str] = field(default_factory=list)
    annotations: AnnotationSet | None = None
    queue: QueueReport | None = None
    path: Path | None = None


def _answer_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _judge_adapter(
    provider: str,
    registry: Registry,
    bundle: DatasetBundle,
    store: CassetteStore,
    origin: str,
) -> AnyAdapter:
    """The adapter judge calls go through.

    ``simulated`` builds the in-process judge responder and opens no socket; anything else
    builds that provider's real HTTP adapter. Either way the call is wrapped in the cassette
    recorder, so an interrupted annotation resumes and nothing is paid for twice.
    """
    if provider != "simulated":
        from tokop.settings import get_settings

        return RecordingAdapter(
            build_live_adapter(provider, registry, get_settings()), store, origin=origin
        )
    tiers = {role: TierProfile(registry.roles[role], role) for role in ("cheap", "mid", "frontier")}
    responder = DemoJudgeResponder(list(bundle.items), bundle.handbook, tiers)
    inner = SimulatedAdapter(simulated_profiles(registry, list(registry.models)), responder)
    return RecordingAdapter(inner, store, origin=origin)


async def _judge(
    workload: WorkloadSpec,
    judge: JudgeSpec,
    registry: Registry,
    bundle: DatasetBundle,
    snapshot: PriceSnapshot,
    store: CassetteStore,
    views: list[AnswerView],
    model_id: str,
    provider: str,
    origin: str,
) -> JudgeRunResult:
    runner = Runner(
        workload,
        registry,
        snapshot,
        _judge_adapter(provider, registry, bundle, store, origin),
        provider=provider,
        handbook=bundle.handbook,
        origin=origin,
    )
    return await runner.run_judge(judge, views, model_id=model_id)


def _verdicts(
    result: JudgeRunResult, kind: str
) -> dict[tuple[str, str], tuple[VerifierVerdict, str]]:
    """Parse every reply into a verdict, keyed by (task id, arm), with its cassette key."""
    parse = verifier_kind(kind).parse
    out: dict[tuple[str, str], tuple[VerifierVerdict, str]] = {}
    for view, request, response in result.calls:
        out[(view.task_id, view.arm)] = (parse(response.text), request.cassette_key())
    return out


def _projected_item_cost(
    judge_result: JudgeRunResult,
    snapshot: PriceSnapshot,
    strong_model_id: str,
    judge_model_id: str,
) -> tuple[Decimal, Decimal]:
    """(marginal cost of one strong-graded item, fixed cost of the run).

    An item is both arms, since the estimator is paired and half an item buys nothing. The
    fixed part is pre-warming the strong grader's cached prefix: it is paid once however many
    items are drawn, so it comes off the budget before the per-item rate is set rather than
    being smeared across items that have not been chosen yet.

    Both are projected rather than measured, because nothing has been sent to the strong
    grader yet and the budget has to be allocated before it is. The projection takes the cheap
    judge's recorded usage — same prompt, same cached prefix, so the *shape* of the call is
    right — and reprices it at the strong grader's rates.

    Repricing alone is not enough, and the first version of this was 38% low because of it.
    Anthropic's tokenizer changed at Claude 4.7, so Opus 5 counts about 30% more tokens for the
    same text than Haiku 4.5 does; a projection that carries the cheap model's token counts
    across therefore under-prices the strong grader by roughly that much. ``core/ratios.py``
    already fits the per-model ratio from the cassettes for exactly this, so the usage is
    scaled by ``ratio(strong) / ratio(cheap)`` before it is priced. What remains an assumption
    is only the output length, and the annotation set records the projection next to what the
    run actually cost so a reader can see how far off it was.
    """
    if not judge_result.calls:
        raise AnnotationError("no judge calls to project a strong-grader price from")
    from tokop.core.ratios import fitted_estimator

    estimator = fitted_estimator()
    strong_ratio = estimator.ratio_for(strong_model_id)
    judge_ratio = estimator.ratio_for(judge_model_id)
    scale = strong_ratio.ratio / judge_ratio.ratio if judge_ratio.ratio > 0 else 1.0
    total = Decimal(0)
    for _, _, response in judge_result.calls:
        total += snapshot.cost(strong_model_id, _scale_usage(response.usage, scale)).total
    fixed = sum(
        (
            snapshot.cost(strong_model_id, _scale_usage(warm.usage, scale)).total
            for _, warm in judge_result.prewarm
        ),
        Decimal(0),
    )
    per_call = total / len(judge_result.calls)
    return ((per_call * 2).quantize(Decimal("0.00000001")), fixed.quantize(Decimal("0.00000001")))


def _scale_usage(usage: TokenUsage, scale: float) -> TokenUsage:
    """The same call's usage as a differently-tokenizing model would have counted it."""
    if scale == 1.0:
        return usage

    def grow(value: int) -> int:
        return round(value * scale)

    return usage.model_copy(
        update={
            "input_uncached": grow(usage.input_uncached),
            "cache_write_5m": grow(usage.cache_write_5m),
            "cache_write_1h": grow(usage.cache_write_1h),
            "cache_read": grow(usage.cache_read),
            "output_visible": grow(usage.output_visible),
            "output_reasoning": grow(usage.output_reasoning),
        }
    )


def run_annotation(
    *,
    fixtures_root: Path,
    workload: WorkloadSpec,
    registry: Registry,
    bundle: DatasetBundle,
    snapshot: PriceSnapshot,
    arms: dict[str, Any],
    budget_usd: Decimal | None = None,
    policy: str | None = None,
    strong_grader: str | None = None,
    seed: int | None = None,
    provider: str = "simulated",
    origin: str = "simulated",
    queue_path: Path | None = None,
) -> AnnotationRun:
    """Run the annotation plan. ``arms`` comes from :func:`arms_for_annotation`."""
    judge = workload.judge
    if judge is None:
        raise AnnotationStopped(
            f"workload {workload.id!r} defines no `judge:` block, so there is nothing to "
            "annotate. Judged grading needs a verifier prompt and a model role."
        )
    spec = workload.annotation
    if spec is None:
        raise AnnotationStopped(
            f"workload {workload.id!r} defines no `annotation:` block, so no budget has been "
            "authorised. Tokop will not choose one for you."
        )

    budget = spec.budget_usd if budget_usd is None else budget_usd
    chosen_policy = allocation_policy(policy or spec.policy)
    grader_source = strong_grader or spec.strong_grader
    if grader_source not in ("frontier", "human"):
        raise AnnotationStopped(
            f"strong_grader is {grader_source!r}; it must be `frontier` (a model call) or "
            "`human` (a review queue)."
        )
    draw_seed = spec.seed if seed is None else seed

    run = AnnotationRun()
    store = CassetteStore(fixtures_root / "cassettes")
    judge_model = registry.roles[judge.model_role]
    strong_model = registry.roles[judge.strong_model_role]

    views: list[AnswerView] = []
    for task_id in arms["task_ids"]:
        views.append(
            AnswerView(
                task_id=task_id,
                question=arms["questions"][task_id],
                answer=arms["baseline_answers"][task_id],
                arm=BASELINE_ARM,
                tier=arms["baseline_tier"],
            )
        )
        views.append(
            AnswerView(
                task_id=task_id,
                question=arms["questions"][task_id],
                answer=arms["candidate_answers"][task_id],
                arm=CANDIDATE_ARM,
                tier=arms["candidate_tiers"][task_id],
            )
        )

    judged = asyncio.run(
        _judge(
            workload,
            judge,
            registry,
            bundle,
            snapshot,
            store,
            views,
            judge_model,
            provider,
            origin,
        )
    )
    judge_verdicts = _verdicts(judged, judge.kind)
    run.steps.append(
        f"cheap judge  {judge_model:28} {judged.n:4} answers  ${judged.total_cost:9.5f}"
    )

    features = [
        ItemFeatures(
            task_id=task_id,
            baseline_judged=judge_verdicts[(task_id, BASELINE_ARM)][0].correct,
            candidate_judged=judge_verdicts[(task_id, CANDIDATE_ARM)][0].correct,
            baseline_uncertainty=judge_verdicts[(task_id, BASELINE_ARM)][0].uncertainty,
            candidate_uncertainty=judge_verdicts[(task_id, CANDIDATE_ARM)][0].uncertainty,
        )
        for task_id in arms["task_ids"]
    ]
    cost_per_item, fixed_cost = _projected_item_cost(judged, snapshot, strong_model, judge_model)
    # Pre-warming is paid once whatever the sample size, so it comes off the budget before the
    # rate is set. A budget that cannot cover it buys nothing at all, and saying so is better
    # than drawing a sample that was never affordable.
    marginal_budget = budget - fixed_cost
    if marginal_budget <= 0 and grader_source == "frontier":
        raise AnnotationStopped(
            f"the ${budget} annotation budget does not cover pre-warming the strong grader's "
            f"cached prefix, projected at ${fixed_cost}. Raise the budget or shorten the judge "
            "prompt; drawing a sample that cannot be paid for would be worse."
        )
    allocation = allocate(
        features,
        policy=chosen_policy.name,
        budget_usd=max(Decimal(0), marginal_budget),
        cost_per_item_usd=cost_per_item,
        seed=draw_seed,
        floor=spec.min_rate,
    )
    run.steps.append(
        f"allocation   {chosen_policy.name:28} {allocation.n_sampled:4} of {allocation.n} drawn"
        f"  ${allocation.expected_cost_usd:9.5f} expected"
    )
    if allocation.floor_exceeds_budget:
        run.steps.append(
            f"NOTE: the {spec.min_rate:.0%} sampling floor alone costs more than the "
            f"${budget} budget. The floor wins: an item that can never be sampled makes the "
            "estimator's inverse weight infinite."
        )

    drawn = [
        task_id
        for task_id, sampled in zip(allocation.task_ids, allocation.sampled, strict=True)
        if sampled
    ]
    drawn_set = set(drawn)
    drawn_views = [view for view in views if view.task_id in drawn_set]

    strong_verdicts: dict[tuple[str, str], tuple[VerifierVerdict, str | None]] = {}
    strong_cost = Decimal(0)
    if grader_source == "human":
        entries = [
            QueueEntry(
                task_id=view.task_id,
                arm=view.arm,
                question=view.question,
                answer=view.answer,
                judge_correct=judge_verdicts[(view.task_id, view.arm)][0].correct,
                judge_confidence=judge_verdicts[(view.task_id, view.arm)][0].confidence,
                sampling_rate=allocation.rates[allocation.task_ids.index(view.task_id)],
            )
            for view in drawn_views
        ]
        target = queue_path or (fixtures_root / "annotation-queue.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_queue(entries))
        run.queue = QueueReport(
            path=target,
            entries=len(entries),
            reason=(
                "strong_grader is `human`, so Tokop wrote the review queue and stopped. It "
                "does not fill in a human verdict, and the judged estimate refuses to run "
                "until something else does."
            ),
        )
        run.steps.append(f"review queue {target.name!s:28} {len(entries):4} answers to review")
    else:
        strong = asyncio.run(
            _judge(
                workload,
                judge,
                registry,
                bundle,
                snapshot,
                store,
                drawn_views,
                strong_model,
                provider,
                origin,
            )
        )
        strong_cost = strong.total_cost
        for key, value in _verdicts(strong, judge.kind).items():
            strong_verdicts[key] = value
        run.steps.append(
            f"strong grader{strong_model:28} {strong.n:4} answers  ${strong_cost:9.5f}"
        )

    items: list[AnnotatedItem] = []
    for index, task_id in enumerate(allocation.task_ids):
        sampled = allocation.sampled[index]
        arms_out: dict[str, AnnotatedArm] = {}
        for arm, answers in (
            (BASELINE_ARM, arms["baseline_answers"]),
            (CANDIDATE_ARM, arms["candidate_answers"]),
        ):
            verdict, cassette = judge_verdicts[(task_id, arm)]
            strong_pair = strong_verdicts.get((task_id, arm))
            arms_out[arm] = AnnotatedArm(
                tier=(
                    arms["baseline_tier"]
                    if arm == BASELINE_ARM
                    else arms["candidate_tiers"][task_id]
                ),
                answer_hash=_answer_hash(answers[task_id]),
                judge_correct=verdict.correct,
                judge_confidence=verdict.confidence,
                judge_parsed=verdict.parsed,
                judge_cassette_key=cassette,
                strong_correct=(
                    strong_pair[0].correct
                    if strong_pair is not None and strong_pair[0].parsed
                    else None
                ),
                strong_cassette_key=strong_pair[1] if strong_pair is not None else None,
                strong_failed=bool(strong_pair is not None and not strong_pair[0].parsed),
            )
        items.append(
            AnnotatedItem(
                task_id=task_id,
                rate=allocation.rates[index],
                sampled=sampled,
                baseline=arms_out[BASELINE_ARM],
                candidate=arms_out[CANDIDATE_ARM],
            )
        )

    kind = verifier_kind(judge.kind)
    annotations = AnnotationSet(
        workload=workload.id,
        split="test",
        baseline_pipeline=arms["baseline_pipeline"],
        candidate_pipeline=arms["candidate_pipeline"],
        operating_point=dict(arms["operating_point"]),
        judge={
            "kind": judge.kind,
            "model_id": judge_model,
            "model_role": judge.model_role,
            "contract": judge.contract,
            "method": kind.method_note(judge_model),
            "calls": judged.n,
        },
        strong_grader={
            "source": grader_source,
            "model_id": strong_model if grader_source == "frontier" else None,
            "model_role": judge.strong_model_role if grader_source == "frontier" else None,
            "method": (
                kind.method_note(strong_model)
                if grader_source == "frontier"
                else "human review queue"
            ),
        },
        policy={
            **allocation.as_dict(),
            "description": chosen_policy.description,
            "floor": spec.min_rate,
        },
        cost={
            "judge_usd": str(judged.total_cost),
            "strong_usd": str(strong_cost),
            "total_usd": str(judged.total_cost + strong_cost),
            "projected_strong_usd": str(allocation.expected_cost_usd + fixed_cost),
            "prewarm_projected_usd": str(fixed_cost),
            "budget_usd": str(budget),
            "over_budget": strong_cost > budget,
            "grading_everything_usd": str(
                (cost_per_item * Decimal(allocation.n) + fixed_cost).quantize(Decimal("0.00001"))
            ),
        },
        items=items,
        origin=origin,
        generated_at=datetime.now(UTC).isoformat(),
    )
    if annotations.cost["over_budget"]:
        run.steps.append(
            f"NOTE: strong grading cost ${strong_cost} against a ${budget} budget. The budget "
            "bounds *expected* spend — which item is drawn is random and the items differ in "
            "price — so a realised draw lands either side of it. Nothing was dropped to bring "
            "it back under, because an item drawn and then not graded is an item the estimator "
            "must treat as never sampled."
        )
    run.annotations = annotations
    run.path = annotations_path(fixtures_root)
    annotations.write(run.path)
    return run


def merge_queue_results(annotations: AnnotationSet, text: str) -> tuple[AnnotationSet, int]:
    """Fold a completed human review queue back into an annotation set.

    Rows left blank stay unannotated. A drawn item whose reviewer answered only one arm is
    left without a strong label on either: the estimator is paired and half a pair corrects
    nothing.
    """
    from tokop.workloads.verification import read_queue_results

    results = read_queue_results(text)
    filled = 0
    for item in annotations.items:
        base = results.get((item.task_id, BASELINE_ARM))
        cand = results.get((item.task_id, CANDIDATE_ARM))
        if base is None or cand is None:
            continue
        item.baseline.strong_correct = base.correct
        item.candidate.strong_correct = cand.correct
        item.baseline.strong_cassette_key = None
        item.candidate.strong_cassette_key = None
        filled += 1
    annotations.strong_grader = {**annotations.strong_grader, "reviewed_items": filled}
    return annotations, filled


def build_demo_annotations(
    *,
    budget_usd: Decimal | None = None,
    policy: str | None = None,
    strong_grader: str | None = None,
    seed: int | None = None,
    protect_scarce: bool = False,
    taken: date | None = None,
) -> AnnotationRun:
    """Annotate the demo workload from the committed fixtures. Spends nothing."""
    from tokop.core.registry import load_registry
    from tokop.optimize.report import arms_for_annotation
    from tokop.paths import fixtures_dir
    from tokop.recording_state import describe as describe_recording
    from tokop.workloads.demo.dataset import build as build_dataset
    from tokop.workloads.spec import load_workload

    registry = load_registry()
    workload = load_workload(repo_root() / "data/demo/workload.yaml")
    bundle = build_dataset(
        size=workload.dataset.size,
        calibration_size=workload.dataset.calibration_size,
        seed=workload.dataset.seed,
    )
    snapshot = registry.snapshot(list(registry.roles.values()), taken or date(2026, 9, 11))
    root = fixtures_dir() / describe_recording().fixture_source
    arms = arms_for_annotation(protect_scarce=protect_scarce)
    return run_annotation(
        fixtures_root=root,
        workload=workload,
        registry=registry,
        bundle=bundle,
        snapshot=snapshot,
        arms=arms,
        budget_usd=budget_usd,
        policy=policy,
        strong_grader=strong_grader,
        seed=seed,
        provider="simulated",
        origin=str(arms.get("origin", "simulated")),
    )
