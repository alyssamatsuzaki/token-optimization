"""Tokop command line. See SPEC.md section 4."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from tokop import recording_state
from tokop.core.registry import OPENROUTER_MODELS_URL, load_registry
from tokop.optimize.report import DEFAULT_WORKLOAD
from tokop.paths import fixtures_dir, repo_root
from tokop.recorder import PILOT_TASKS

if TYPE_CHECKING:  # pragma: no cover - import kept out of the startup path
    from tokop.optimize.report import ReportPayload

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find the cheapest way to run an AI workload without getting worse, and prove it.",
)


@app.command("recording-state")
def recording_state_cmd(
    check: bool = typer.Option(
        False, "--check", help="Exit nonzero only if the state is unreadable."
    ),
) -> None:
    """Report which demo recording this build has, and why."""
    state = recording_state.describe()
    typer.echo(f"state:   {state.state}")
    typer.echo(f"source:  fixtures/{state.fixture_source}/")
    typer.echo(f"reason:  {state.reason}")
    # Every state is a legitimate outcome; only an unreadable state is a failure.
    known = (recording_state.RECORDED, recording_state.AWAITING_REVIEW, recording_state.UNRECORDED)
    if check and state.state not in known:
        typer.echo(f"unknown recording state: {state.state}", err=True)
        raise typer.Exit(1)


def _wrote(path: Path) -> str:
    """ "wrote <path>", relative to the repository when it is inside it and absolute when not."""
    try:
        return f"wrote {path.relative_to(repo_root())}"
    except ValueError:
        return f"wrote {path}"


def _not_yet(name: str, milestone: str) -> None:
    typer.echo(f"`tokop {name}` is not built yet (arrives in {milestone}).", err=True)
    raise typer.Exit(2)


@app.command("build-test-fixtures")
def build_test_fixtures_cmd(
    ledger_only: bool = typer.Option(
        False,
        "--ledger-only",
        help="Rebuild ledger.db from the committed cassettes; leave manifest and extras alone.",
    ),
    workload: str = typer.Option(DEFAULT_WORKLOAD, "--workload"),
) -> None:
    """Build fixtures/test/ from the deterministic simulated provider. Spends nothing.

    `--ledger-only` is what a fresh checkout needs: ledger.db is generated rather than committed,
    and materialising it is replay, not recording. Without the flag the manifest is rewritten,
    which stamps the rebuilding machine's tokenizer over the one the fixtures were built with
    (DECISIONS.md D26).
    """
    from tokop.recorder import build_test_fixtures
    from tokop.workloads.bundle import load_bundle
    from tokop.workloads.spec import load_workload

    registry = load_registry()
    spec = load_workload(repo_root() / workload)
    bundle = load_bundle(spec)
    # A workload that names its own fixture directory gets it; the demo names none and its
    # simulated set lives where it always has (UPGRADE_V4.md M15).
    root = fixtures_dir() / (spec.fixtures or "test")
    root.mkdir(parents=True, exist_ok=True)
    report = build_test_fixtures(spec, registry, bundle, root, ledger_only=ledger_only)
    for step in report.steps:
        typer.echo("  " + step.summary())
    if report.stopped:
        typer.echo(f"\nSTOPPED: {report.stopped}", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"\n{len(report.steps)} runs, {report.cassette_count} cassettes, "
        f"simulated cost ${report.total_cost:.4f} (no money was spent: origin=simulated)"
    )


@app.command()
def record(
    workload: str = typer.Option(DEFAULT_WORKLOAD, "--workload"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    underpowered: bool = typer.Option(
        False,
        "--underpowered",
        help="Record a split too small to conclude. Stored in the run record and reported.",
    ),
    pilot_tasks: int = typer.Option(PILOT_TASKS, "--pilot-tasks", help="Tasks per pilot step."),
) -> None:
    """Record a workload against live providers. Capped by RECORD_BUDGET_USD.

    Four gates stand between this command and a charge, in this order:

    1. **Consent.** A key and an explicit budget. Tokop never sets either.
    2. **Power.** A test split too small to produce a conclusive verdict is refused, because
       recording one spends the whole budget and returns "inconclusive".
    3. **A projection**, counted before the first call, printed against the cap.
    4. **A pilot** of a few tasks per step, which replaces the projected output length with a
       measured one and aborts if the refined figure exceeds the cap (SPEC.md section 6).

    Then it asks. An interrupted recording resumes: identical requests reuse their cassette, so
    nothing is paid for twice, and the cap is enforced per call rather than per step.
    """
    import asyncio as _asyncio
    from datetime import date

    from tokop.core.budget import BudgetExceeded, SpendGuard
    from tokop.core.registry import load_registry
    from tokop.recorder import (
        Recorder,
        RecordingStopped,
        project_from_pilot,
        record_live,
        require_live_consent,
    )
    from tokop.settings import Mode, get_settings
    from tokop.workloads.bundle import load_bundle
    from tokop.workloads.spec import load_workload

    settings = get_settings()
    if settings.mode != Mode.LIVE:
        typer.echo(
            f"TOKOP_MODE is {settings.mode!r}. Recording makes live API calls, so it only runs "
            "with TOKOP_MODE=live.",
            err=True,
        )
        raise typer.Exit(2)

    # ------------------------------------------------------------------ 1. consent
    try:
        budget = require_live_consent(settings)
    except RecordingStopped as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    spec = load_workload(repo_root() / workload)
    registry = load_registry()
    bundle = load_bundle(spec)
    typer.echo(f"workload: {workload}")
    typer.echo(f"budget:   ${budget} (RECORD_BUDGET_USD)")
    typer.echo("")

    # ------------------------------------------------------------------ 2. power
    estimate = _power_estimate(spec)
    if estimate is None:
        typer.echo(
            "No prior run to size this split from, so nothing here knows whether "
            f"{len(bundle.test)} test tasks can produce a verdict. Build the simulated fixture "
            "set first (`tokop build-test-fixtures`), or pass --underpowered to record a split "
            "nobody has sized.",
            err=True,
        )
        if not underpowered:
            raise typer.Exit(2)
    else:
        typer.echo(f"  power: {estimate.display}")
        short = estimate.required_n is not None and len(bundle.test) < estimate.required_n
        if short and not underpowered:
            typer.echo(
                f"\nThe test split has {len(bundle.test)} tasks and needs "
                f"{estimate.required_n:,}. Recording it would spend the budget and return "
                "'inconclusive':\nthe most expensive outcome available here. Enlarge the split, "
                "or pass --underpowered to record it anyway.",
                err=True,
            )
            raise typer.Exit(2)
        if short:
            typer.echo(
                "  --underpowered: recording a split that cannot conclude. Stored in the run "
                "record and printed in the report."
            )
    typer.echo("")

    # ------------------------------------------------------------------ 3. the projection
    guard = SpendGuard(run_cap=budget, daily_cap=settings.daily_budget_usd)
    recorder = Recorder(
        spec,
        registry,
        bundle,
        registry.snapshot(list(registry.roles.values()), date.today()),
        fixtures_dir() / "demo" / "cassettes",
        provider="anthropic",
        origin="live",
        guard=guard,
    )
    projection = _asyncio.run(recorder.project(budget))
    for line in projection.lines():
        typer.echo(line)
    if projection.exceeds_cap:
        raise typer.Exit(2)
    typer.echo("")

    if not yes and not typer.confirm(f"Spend up to ${budget} recording {spec.id}?"):
        typer.echo("Nothing was spent.")
        raise typer.Exit(1)

    # ------------------------------------------------------------------ 4. the pilot
    typer.echo(f"\npilot: {pilot_tasks} tasks per step")
    try:
        outcome = _asyncio.run(recorder.pilot(pilot_tasks))
    except BudgetExceeded as exc:
        _echo_cap_reached(exc, guard)
        raise typer.Exit(3) from exc
    refined = project_from_pilot(
        outcome.cost, outcome.tasks, outcome.output_tokens, outcome.total_tasks, budget
    )
    typer.echo(refined.describe())
    if not refined.within_budget:
        typer.echo(
            "\nRefusing to continue: the pilot's own answers project a total above the cap. "
            "The pilot's calls are\ncassettes, so they are not lost — raise the cap or shrink "
            "the split and run again.",
            err=True,
        )
        raise typer.Exit(2)

    # ------------------------------------------------------------------ 5. the recording
    typer.echo("")
    try:
        report = record_live(
            spec,
            registry,
            bundle,
            fixtures_dir() / "demo",
            guard=guard,
            provider="anthropic",
            underpowered=underpowered,
        )
    except BudgetExceeded as exc:
        _echo_cap_reached(exc, guard)
        raise typer.Exit(3) from exc
    for step in report.steps:
        typer.echo(f"  {step.summary()}")
    typer.echo(f"\n{report.cassette_count:,} cassettes, ${report.total_cost:.4f} spent")
    if report.stopped:
        typer.echo(f"\nstopped: {report.stopped}", err=True)
        raise typer.Exit(3)
    typer.echo("\nNext: tokop prove, then tokop report --write-readme")


def _short(path: Path) -> str:
    """Repository-relative where that makes sense, absolute where it does not."""
    try:
        return str(path.relative_to(repo_root()))
    except ValueError:
        return str(path)


def _echo_cap_reached(exc: Any, guard: Any) -> None:
    """The cap did its job. Say what was bought, and that none of it is lost.

    Reaching the cap is not a failure of the recording — it is the one outcome the cap exists to
    produce, and the difference between it and a crash is whether the operator knows the money
    already spent bought something they can resume from.
    """
    typer.echo(f"\n{exc}", err=True)
    typer.echo(
        f"\nStopped at the cap after spending ${guard.run_spend:.4f}. Every call that was paid "
        "for is a cassette:\nraise RECORD_BUDGET_USD and run `make record` again, and it will "
        "replay them for nothing and\ncarry on from where it stopped.",
        err=True,
    )


def _power_estimate(spec: Any) -> Any:
    """Size the test split from whatever run this build already has, or None if it has none.

    Absence is checked rather than caught: a repository with no fixtures is a state this command
    has to describe, and wrapping the report build in an ``except`` would swallow the failures
    that mean something is actually broken.
    """
    from tokop.core.stats import PowerEstimate
    from tokop.optimize.report import build_report

    if not any((fixtures_dir() / kind / "manifest.json").exists() for kind in ("demo", "test")):
        return None
    payload = build_report()
    proof = payload["proof"]
    counts = proof["mcnemar"]
    return PowerEstimate.from_counts(
        baseline_only=int(counts["baseline_only"]),
        candidate_only=int(counts["candidate_only"]),
        n=int(proof["candidate"]["n"]),
        margin=float(spec.margin),
        source=f"{proof['baseline']['pipeline']} against {proof['candidate']['pipeline']}",
    )


@app.command()
def annotate(
    workload: str = typer.Option(DEFAULT_WORKLOAD, "--workload"),
    budget: float = typer.Option(
        -1.0, "--budget", help="Override the workload's annotation budget, in dollars."
    ),
    policy: str = typer.Option("", "--policy", help="cost-optimal or uniform."),
    strong_grader: str = typer.Option("", "--strong-grader", help="frontier or human."),
    queue: bool = typer.Option(
        False, "--queue", help="Write a human review queue instead of calling a strong grader."
    ),
    queue_results: str = typer.Option(
        "", "--queue-results", help="Fold a completed review queue back into the annotation set."
    ),
    contract: bool = typer.Option(
        False,
        "--contract",
        help="Judge the checkable-contract pair instead of the proof's two arms (U4).",
    ),
) -> None:
    """Buy the strong labels the judged proof corrects a cheap judge with (UPGRADE_V3.md U1).

    Runs the cheap judge over every answer in both arms, allocates the annotation budget across
    tasks by the workload's policy, draws, and grades the drawn items with the strong grader.
    Writes ``annotations.json`` next to the fixtures.
    """
    from decimal import Decimal as _Decimal

    from tokop.annotate import AnnotationStopped, build_demo_annotations, merge_queue_results
    from tokop.optimize.annotation import AnnotationError, AnnotationSet, annotations_path
    from tokop.recording_state import describe

    if workload != "data/demo/workload.yaml":
        typer.echo(
            f"this build can only annotate the demo workload; --workload {workload!r} is not "
            "supported yet (DECISIONS.md D24).",
            err=True,
        )
        raise typer.Exit(2)

    root = fixtures_dir() / describe().fixture_source
    if queue_results:
        source = repo_root() / queue_results
        if not source.exists():
            typer.echo(f"no review queue at {source}", err=True)
            raise typer.Exit(2)
        try:
            existing = AnnotationSet.read(annotations_path(root, contract=contract))
            merged, filled = merge_queue_results(existing, source.read_text())
        except AnnotationError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        target = annotations_path(root, contract=contract)
        merged.write(target)
        typer.echo(f"folded {filled} reviewed items into {target.name}")
        return

    try:
        run = build_demo_annotations(
            budget_usd=None if budget < 0 else _Decimal(str(budget)),
            policy=policy or None,
            strong_grader=("human" if queue else (strong_grader or None)),
            contract=contract,
        )
    except (AnnotationStopped, AnnotationError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    for step in run.steps:
        typer.echo("  " + step)
    if run.queue is not None:
        typer.echo(f"\n{run.queue.reason}")
        typer.echo(f"wrote {run.queue.path} ({run.queue.entries} answers to review)")
    if run.annotations is not None and run.path is not None:
        annotations = run.annotations
        typer.echo(
            f"\n{annotations.n_annotated} of {annotations.n} items strong-graded "
            f"({annotations.n_annotated / annotations.n:.0%}), "
            f"${annotations.cost['total_usd']} spent in total "
            f"(judge ${annotations.cost['judge_usd']}, "
            f"strong ${annotations.cost['strong_usd']}). "
            f"Grading every item would have cost ${annotations.cost['grading_everything_usd']}."
        )
        typer.echo(f"wrote {run.path.relative_to(repo_root())}")
        if annotations.origin != "recorded":
            typer.echo("\nNOTE: simulated verdicts, not a recording (DECISIONS.md D1).", err=True)


def _echo_calibration(calibration: dict[str, Any]) -> None:
    """Where the operating point's labels came from, and what a label-free one would pick."""
    typer.echo("")
    typer.echo(
        f"CALIBRATION:   {calibration['mode']} — "
        f"{', '.join(f'{t:.2f}' for t in calibration['thresholds'][:-1])} at a "
        f"{calibration['accuracy_floor']:.1%} accuracy floor"
        + (
            f", {calibration['excluded']} of {calibration['n']} tasks excluded"
            if calibration["excluded"]
            else ""
        )
    )
    for alternative in calibration["alternatives"]:
        agreement = [
            value
            for value in alternative["label_agreement_with_gold"].values()
            if value is not None
        ]
        typer.echo(
            f"  {alternative['kind']:15} "
            f"{', '.join(f'{t:.2f}' for t in alternative['thresholds'][:-1])}  "
            f"(gap {alternative['max_threshold_gap']:.2f}, "
            f"{alternative['excluded']} excluded"
            + (
                f", labels agree {min(agreement):.0%}"
                if agreement
                else ", agreement not computable"
            )
            + ")"
        )
    exercise = calibration["fixtures_can_exercise_this"]
    if calibration["alternatives"] and not exercise["answer"]:
        typer.echo(f"  {exercise['reason']}")


def _echo_contract(contract: dict[str, Any]) -> None:
    """What the checkable output contract costs and what it buys (UPGRADE_V3.md U4)."""
    typer.echo("")
    if not contract.get("available"):
        typer.echo(f"CHECKABILITY:  not priced — {contract.get('reason', 'no reason given')}")
        return
    typer.echo(
        f"CHECKABILITY:  {contract['candidate_pipeline']} against "
        f"{contract['baseline_pipeline']}, judged by {contract['judge']['model_id']} and "
        f"corrected by {contract['annotated']} of {contract['n']} strong-graded tasks"
    )
    header = (
        f"  {'pipeline':10}{'judge agrees':>14}{'sample rate':>13}"
        f"{'annotation $':>14}{'accuracy':>10}{'generation $':>14}"
    )
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for arm in contract["arms"]:
        typer.echo(
            f"  {arm['pipeline']:10}"
            f"{arm['judge_agreement_with_strong_grader']:>13.1%}"
            f"{arm['sampling_rate_for_target']:>13.1%}"
            f"{float(arm['annotation_cost_for_target_usd']):>14.4f}"
            f"{arm['accuracy']:>10.1%}"
            f"{float(arm['generation_cost_usd']):>14.4f}"
        )
    typer.echo(
        f"  the contract moves judge agreement {contract['agreement_delta'] * 100:+.1f} points "
        f"and accuracy {contract['accuracy_delta'] * 100:+.1f} points"
    )
    typer.echo(
        f"  annotation saving ${contract['annotation_saving_usd']} against a generation premium "
        f"of ${contract['generation_premium_usd']}: "
        + (
            "it pays for itself on this split"
            if contract["pays_for_itself"]
            else f"net ${contract['net_on_evaluation_split_usd']} — it does not pay here"
        )
        + f" (target standard error {contract['target_standard_error']:.2f})"
    )
    typer.echo(f"  {contract['note']}")


def _echo_judged(judged: dict[str, Any]) -> None:
    """Print the same comparison with the answer key withheld."""
    typer.echo("")
    if not judged.get("available"):
        typer.echo(f"WITHOUT GOLD: unavailable — {judged.get('reason', 'no reason given')}")
        return
    annotation = judged["annotation"]
    judge_only = judged["judge_only"]
    typer.echo(f"WITHOUT GOLD:  {judged['verdict']['display']}")
    typer.echo(f"               {judged['verdict']['sentence']}")
    typer.echo(
        f"  judge alone: {judge_only['delta_accuracy']['point'] * 100:+.1f} points "
        f"[{judge_only['delta_accuracy']['low'] * 100:+.1f}, "
        f"{judge_only['delta_accuracy']['high'] * 100:+.1f}] — "
        f"{judge_only['bias_vs_corrected'] * 100:+.1f} points of judge bias, measured"
    )
    typer.echo(
        f"  annotation:  {annotation['n_annotated']} of {annotation['n']} items "
        f"({annotation['annotated_share']:.0%}) at ${annotation['annotation_cost_usd']}; "
        f"strong-only grading needs {annotation['strong_only_items_for_same_width']} items "
        f"at ${annotation['strong_only_cost_usd']} for the same interval width"
    )
    typer.echo(f"  {annotation['cost_optimal_rate_note']}")
    coverage = judged.get("coverage_check")
    if coverage:
        covers = "covers" if coverage["judged_interval_covers_gold"] else "MISSES"
        typer.echo(
            f"  check:       the gold-graded delta is "
            f"{coverage['gold_delta_accuracy'] * 100:+.1f} points; the gold-free interval "
            f"{covers} it"
        )
    for caveat in judged.get("caveats", []):
        typer.echo(f"  - {caveat}")


@app.command()
def prove(
    workload: str = typer.Option(DEFAULT_WORKLOAD, "--workload"),
    baseline: str = typer.Option("B0", "--baseline"),
    candidate: str = typer.Option("B3", "--candidate"),
    margin: float = typer.Option(0.03, "--margin"),
    grading: str = typer.Option(
        "gold",
        "--grading",
        help="gold reads the dataset's answer key; judged withholds it and uses the judge.",
    ),
    annotation_budget: float = typer.Option(
        -1.0,
        "--annotation-budget",
        help="Replay the judged estimate at a smaller annotation budget, in dollars.",
    ),
    calibration: str = typer.Option(
        "",
        "--calibration",
        help=(
            "Where calibration labels come from: gold, penalized-v1 or majority-vote. "
            "Defaults to the workload's grading mode."
        ),
    ),
) -> None:
    """Run the proof for a candidate pipeline against a baseline.

    Exits 0 when the verdict is non-inferior and 1 otherwise, so it can gate CI. With
    ``--grading judged`` the verdict comes from the gold-free estimate instead, which is what a
    workload that arrives without labels gets.
    """
    from decimal import Decimal as _Decimal

    from tokop.optimize.report import build_report

    if grading not in ("gold", "judged"):
        typer.echo(
            f"--grading must be `gold` or `judged`, not {grading!r}.",
            err=True,
        )
        raise typer.Exit(2)

    # The margin is applied, not echoed: the verdict below is computed against this number.
    from tokop.optimize.pseudolabels import GOLD, PSEUDO_LABEL_REGISTRY

    if calibration and calibration not in {GOLD, *PSEUDO_LABEL_REGISTRY}:
        typer.echo(
            f"--calibration must be one of {', '.join([GOLD, *sorted(PSEUDO_LABEL_REGISTRY)])}, "
            f"not {calibration!r}.",
            err=True,
        )
        raise typer.Exit(2)

    payload = build_report(
        workload_path=workload,
        margin=margin,
        annotation_budget=None if annotation_budget < 0 else _Decimal(str(annotation_budget)),
        calibration=calibration or None,
    )
    proof = payload["proof"]
    if proof["baseline"]["pipeline"] != baseline or proof["candidate"]["pipeline"] != candidate:
        typer.echo(
            f"this build's report compares {proof['baseline']['pipeline']} with "
            f"{proof['candidate']['pipeline']}; --baseline/--candidate are not yet wired to "
            "arbitrary pairs.",
            err=True,
        )
        raise typer.Exit(2)

    typer.echo(f"workload:  {payload['workload']['name']} ({workload})")
    typer.echo(
        f"splits:    {proof['split_sizes']['calibration']} calibration, "
        f"{proof['split_sizes']['test']} test"
    )
    typer.echo(f"margin:    {margin:.0%} (applied)")
    typer.echo("")
    typer.echo(f"VERDICT:   {proof['verdict']['display']}")
    typer.echo(f"           {proof['verdict']['sentence']}")
    typer.echo("")
    typer.echo(f"proof cost ${float(proof['proof_cost']['total_usd']):.4f}", nl=False)
    if proof["repayment_tasks"]:
        typer.echo(f", repaid after {proof['repayment_tasks']:,} tasks")
    else:
        typer.echo("")
    _echo_calibration(payload["calibration"])
    _echo_contract(payload["contract"])
    judged = payload["judged"]
    if workload_has_judge(payload):
        _echo_judged(judged)
    if payload["provenance"]["is_test_data"]:
        typer.echo("")
        typer.echo("NOTE: computed over simulated test data, not a recording.", err=True)

    if grading == "judged":
        if not judged.get("available"):
            typer.echo(
                f"\n--grading judged, but there is no usable judged estimate: "
                f"{judged.get('reason', 'no reason given')}",
                err=True,
            )
            raise typer.Exit(2)
        # The gate reads the judged verdict, which is the one a workload without labels gets.
        if judged["verdict"]["label"] != "non_inferior":
            raise typer.Exit(1)
        return
    if proof["verdict"]["label"] != "non_inferior":
        raise typer.Exit(1)


def workload_has_judge(payload: ReportPayload) -> bool:
    """Whether this report has anything to say about grading without gold."""
    return bool(payload["judged"])


def _echo_scorer_comparison(payload: ReportPayload) -> None:
    """Print every scorer the calibration search evaluated, with what its sampling cost.

    The point of the table is the two columns next to each other: what a scorer bought, and
    what it spent to buy it. A scorer that is cheaper per successful task only because nobody
    charged it for its samples would show a sampling cost of zero next to a k above 1.
    """
    cascade = payload["cascade"]
    rows = cascade.get("scorer_comparison") or []
    if len(rows) < 2:
        return
    choice = cascade["scorer_choice"]

    typer.echo("")
    typer.echo("  scorers evaluated on the calibration split")
    header = (
        f"  {'configuration':26}{'calls':>6}{'accuracy':>10}{'$/success':>12}"
        f"{'scarce':>8}{'sampling $':>12}{'eff. k':>8}{'repays':>8}  verdict"
    )
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for row in rows:
        accuracy = row["accuracy"]
        marker = " *" if row["is_chosen"] else "  "
        correlation = row.get("sample_correlation")
        # The worst tier's effective k: how many independent draws this configuration's k
        # repeats were actually worth (UPGRADE_V3.md U6). "—" for a scorer that reads one call.
        effective = (
            f"{min(v['effective_k'] for v in correlation.values()):.2f}" if correlation else "—"
        )
        typer.echo(
            f"  {row['label'][:24]:24}{marker}{row['calls_per_scored_task']:>4}"
            f"{accuracy['point']:>10.1%}"
            f"{row['cost_per_successful_task']['point']:>12.6f}"
            f"{row['scarce_share']:>8.1%}"
            f"{float(row['sampling_cost_usd']):>12.4f}"
            f"{effective:>8}"
            f"{row['repayment_tasks']!s:>8}  {row['verdict']['label']}"
        )
    typer.echo(f"  * the operating point, chosen on {choice['chosen_on']}")

    withheld = sorted({r["scorer"] for r in rows} - set(choice["adoptable"]))
    if withheld:
        typer.echo(
            f"  {', '.join(withheld)} was measured but this workload does not let the search "
            "adopt it;\n  see DECISIONS.md D27 for why."
        )
    typer.echo(
        f"  recording the repeats this table needed cost "
        f"${choice['search_recording_cost_usd']}, which is not part of the proof cost above."
    )
    if any(row.get("sample_correlation") for row in rows):
        typer.echo(
            "  eff. k is how many *independent* draws each configuration's k repeats were worth,"
            "\n  measured from within-task agreement. Close to k means the samples really are "
            "independent,\n  which on these fixtures is a fact about the simulator (DECISIONS.md "
            "D27)."
        )

    ties = cascade.get("ties")
    if ties and ties["is_tie"]:
        typer.echo("")
        typer.echo("  statistically tied for cheapest, ordered by dollars")
        for row in ties["tied"]:
            marks = []
            if row["is_operating_point"]:
                marks.append("operating point")
            if row["is_fallback"]:
                marks.append("fallback")
            if not row["adoptable"]:
                marks.append("not adoptable here")
            typer.echo(
                f"  {row['label'][:34]:34}{row['cost_per_successful_task']:>12.6f}"
                f"  [{row['cost_low']:.6f}, {row['cost_high']:.6f}]"
                + (f"  ({', '.join(marks)})" if marks else "")
            )
        typer.echo(f"  {ties['note']}")
        typer.echo(f"  {ties['fallback_reason']}")


@app.command("ingest")
def ingest_cmd(
    source: str = typer.Option(..., "--source", help="The file to read."),
    fmt: str = typer.Option("jsonl", "--format", help="jsonl, csv or otel."),
    out: str = typer.Option(..., "--out", help="Directory to write data/<name>/ into."),
    calibration_size: int = typer.Option(
        -1, "--calibration-size", help="Tasks in the calibration split. Default: a third."
    ),
    map_fields: list[str] = typer.Option(
        [], "--map", help="Name a column: --map question=prompt --map gold=expected."
    ),
) -> None:
    """Read a JSONL, CSV or OpenTelemetry export into a workload's dataset.

    SPEC.md section 3 promised this and it was never built, which is why `--workload` refused
    everything but the demo by name until M15.

    The formats differ in one way that matters more than their syntax. A JSONL or CSV export is
    usually a curated set and carries an answer key. OpenTelemetry spans are traffic: a span
    records what was asked and what the model said, never what the right answer was. Tokop
    reads the tasks and leaves the answers empty rather than promoting a recorded completion to
    a gold label, and says what that costs you.
    """
    from tokop.workloads.ingest import ingest
    from tokop.workloads.spec import WorkloadError

    mapping: dict[str, str] = {}
    for pair in map_fields:
        if "=" not in pair:
            typer.echo(f"--map takes field=column, not {pair!r}", err=True)
            raise typer.Exit(2)
        field_, column = pair.split("=", 1)
        mapping[field_.strip()] = column.strip()

    try:
        report = ingest(
            Path(source) if Path(source).is_absolute() else repo_root() / source,
            fmt,
            Path(out) if Path(out).is_absolute() else repo_root() / out,
            mapping=mapping,
            calibration_size=None if calibration_size < 0 else calibration_size,
        )
    except WorkloadError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    for line in report.lines():
        typer.echo(line)
    typer.echo(f"\nwrote {_short(Path(out) / 'dataset.jsonl')}")
    typer.echo(
        "Next: write a workload.yaml beside it — `data/incident-triage/workload.yaml` is a "
        "worked example —\nthen `tokop build-test-fixtures --workload <path>` or `make record` "
        "to give it a response matrix."
    )
    if not report.has_gold:
        raise typer.Exit(3)


@app.command("export")
def export_cmd(
    artifact: str = typer.Option(
        "all", "--artifact", help="prompt-diff, cascade-config, routing-rule, or all."
    ),
    out: str = typer.Option("exports", "--out", help="Directory to write into."),
    workload: str = typer.Option(DEFAULT_WORKLOAD, "--workload"),
) -> None:
    """Export what the proof recommends, as artifacts you apply yourself.

    Deploy means export, not proxy. SPEC.md section 3 excludes any gateway carrying other
    applications' traffic and that exclusion stands, so what ships is a diff, a config file and
    a routing rule you can read before you run them — each generated from the report, so what is
    exported is what was proven.
    """
    from tokop.optimize.export import ARTIFACTS, ExportError, export
    from tokop.optimize.report import build_report
    from tokop.workloads.spec import load_workload

    wanted = list(ARTIFACTS) if artifact == "all" else [artifact]
    payload = build_report()
    spec = load_workload(repo_root() / workload)
    destination = repo_root() / out
    try:
        results = [export(payload, name, destination, spec) for name in wanted]  # type: ignore[arg-type]
    except ExportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    for result in results:
        typer.echo(f"  {_short(result.path)}")
        typer.echo(f"      {result.note}")
    evidence = payload["evidence"]
    typer.echo(f"\nEvidence: {evidence['grade']}. {evidence['headline']}")
    if evidence["grade"] != "recording":
        typer.echo(
            "These artifacts describe a result that is not yet backed by a recording. They are "
            "correct about\nthe traces they were computed over; deploying them is a decision "
            "about how far that generalises."
        )


@app.command()
def power(
    workload: str = typer.Option(DEFAULT_WORKLOAD, "--workload"),
    margin: float = typer.Option(
        0.0, "--margin", help="Non-inferiority margin in accuracy POINTS. 0 uses the workload's."
    ),
    alpha: float = typer.Option(0.05, "--alpha", help="Two-sided significance level."),
) -> None:
    """How many test tasks a conclusive verdict needs, before any of them are recorded.

    Sizing the split is the one calculation that belongs before the money moves. A split too
    small to conclude spends the whole budget and returns "inconclusive", which is the most
    expensive outcome available: nothing is proven and the budget is gone.

    The discordance rate comes from a run that already happened, named in the output. Read the
    caveat with it — a rate measured on simulated arms is the optimistic case.
    """
    from tokop.core.stats import PowerEstimate
    from tokop.optimize.report import build_report

    payload = build_report()
    proof = payload["proof"]
    counts = proof["mcnemar"]
    effective_margin = margin / 100 if margin else float(payload["workload"]["margin"])
    simulated = bool(payload["provenance"]["is_test_data"])
    estimate = PowerEstimate.from_counts(
        baseline_only=int(counts["baseline_only"]),
        candidate_only=int(counts["candidate_only"]),
        n=int(proof["candidate"]["n"]),
        margin=effective_margin,
        alpha=alpha,
        source=f"{proof['baseline']['pipeline']} against {proof['candidate']['pipeline']} on "
        f"the {payload['provenance']['fixture_source']} fixtures",
    )

    typer.echo(f"workload: {workload}")
    typer.echo(f"measured on: {estimate.source}")
    typer.echo("")
    typer.echo(f"  split as recorded            {estimate.observed_n:,} tasks")
    typer.echo(
        f"  the arms disagree on         {estimate.discordant} of them "
        f"({(estimate.p10 + estimate.p01) * 100:.1f}%)"
    )
    typer.echo(
        f"  accuracy difference          {estimate.delta_hat * 100:+.1f} points "
        f"(candidate ahead on {round(estimate.p01 * estimate.observed_n)}, "
        f"behind on {round(estimate.p10 * estimate.observed_n)})"
    )
    typer.echo(f"  margin                       {effective_margin * 100:.0f} points")
    typer.echo(f"  alpha                        {alpha}")
    typer.echo("")
    if estimate.required_n is None:
        typer.echo("  tasks needed                 none would settle it")
    else:
        typer.echo(f"  tasks needed                 {estimate.required_n:,}")
    typer.echo(f"  {estimate.display}")
    typer.echo("")
    if simulated:
        typer.echo(
            "This rate was measured on simulated arms whose errors are drawn independently,\n"
            "which is the optimistic case: two real models tend to fail on the same hard tasks,\n"
            "which makes them more concordant and the split larger. Treat the number as a floor."
        )
    if not estimate.is_sufficient and estimate.required_n is not None:
        typer.echo(
            f"\n`tokop record` refuses a test split below {estimate.required_n:,} tasks. "
            "Override with --underpowered;\nthe override is stored in the run record and "
            "printed in the report."
        )


@app.command()
def report(
    check: bool = typer.Option(False, "--check", help="Recompute and assert everything agrees."),
    write_readme: bool = typer.Option(False, "--write-readme", help="Rewrite the README block."),
    out: str = typer.Option("", "--out", help="Write the full report JSON here."),
) -> None:
    """Recompute every headline metric from fixtures."""
    import json as _json

    from tokop.optimize.report import (
        build_report,
        cached_report,
        clear_cache,
        method_doc_current,
        readme_metrics_current,
        write_method_doc,
        write_readme_metrics,
    )

    payload = build_report()

    if write_readme:
        for path in (write_readme_metrics(payload), write_method_doc(payload)):
            typer.echo(f"wrote the generated block in {path.relative_to(repo_root())}")

    if out:
        destination = repo_root() / out
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_json.dumps(payload.data, indent=2, sort_keys=True, default=str))
        typer.echo(f"wrote {destination.relative_to(repo_root())}")

    if not check:
        headline = payload.headline()
        for key, value in headline.items():
            typer.echo(f"  {key:38} {value}")
        _echo_scorer_comparison(payload)
        return

    failures = 0

    # 1. The computation is deterministic: a second run produces identical headline numbers.
    again = build_report()
    if again.headline() != payload.headline():
        typer.echo("  FAIL  the report is not deterministic across two builds", err=True)
        failures += 1
    else:
        typer.echo("  ok    the report recomputes identically")

    # 2. The API serves the same computation, not a parallel one. This goes through the route
    #    over HTTP rather than calling the cache directly: the thing worth checking is what a
    #    browser receives, and a route that serialized the wrong object would pass either way.
    clear_cache()
    cached_report(False)  # warm it, so the request measures the served path and not a build
    from fastapi.testclient import TestClient

    from tokop.api.app import app as api_app

    with TestClient(api_app) as client:
        response = client.get("/api/report")
    if response.status_code != 200:
        typer.echo(f"  FAIL  GET /api/report returned {response.status_code}", err=True)
        failures += 1
    else:
        from tokop.optimize.report import ReportPayload

        over_http = ReportPayload(_json.loads(response.content)).headline()
        # The fresh headline goes through JSON too, so this compares numbers and not the
        # Decimal-versus-string difference that serialization introduces.
        fresh = _json.loads(_json.dumps(payload.headline(), default=str))
        if over_http != fresh:
            differing = [k for k in fresh if over_http.get(k) != fresh[k]]
            typer.echo(
                f"  FAIL  GET /api/report disagrees on {', '.join(differing)}",
                err=True,
            )
            failures += 1
        else:
            typer.echo("  ok    GET /api/report serves the same numbers")

    # 3. Both generated documents are current. One code path writes them, so a check that
    #    covered only the README would let the method document drift away from the report it
    #    is supposed to be the derivation of.
    for current, message in (method_doc_current(payload), readme_metrics_current(payload)):
        typer.echo(f"  {'ok  ' if current else 'FAIL'}  {message}")
        failures += 0 if current else 1

    # 4. The gold-free estimate is available, and it lands where the labelled one does.
    #    An annotation set drawn against a different operating point judges answers this
    #    cascade did not give, so "unavailable" is a failure here rather than a note.
    judged = payload["judged"]
    if not judged.get("available"):
        typer.echo(f"  FAIL  the judged estimate is unavailable: {judged.get('reason')}", err=True)
        failures += 1
    else:
        coverage = judged["coverage_check"]
        covers = coverage["judged_interval_covers_gold"]
        typer.echo(
            f"  {'ok  ' if covers else 'FAIL'}  the gold-free interval "
            f"[{judged['delta_accuracy']['low'] * 100:+.1f}, "
            f"{judged['delta_accuracy']['high'] * 100:+.1f}] "
            f"{'covers' if covers else 'MISSES'} the gold-graded delta "
            f"{coverage['gold_delta_accuracy'] * 100:+.1f}"
        )
        failures += 0 if covers else 1

    if failures:
        typer.echo(f"\nreport --check FAILED ({failures})", err=True)
        raise typer.Exit(1)
    typer.echo("\nreport --check passed")


@app.command()
def provenance(
    dataset: str = typer.Option("data/demo/dataset.jsonl", "--dataset"),
    check: bool = typer.Option(
        False, "--check", help="Exit nonzero if the provenance block is incomplete."
    ),
) -> None:
    """Say where the task set came from, and whether it can back a certificate (U8).

    Reports; it does not gate. A set that cannot certify is a fact about the set, not a defect
    in the build, and `--check` fails only when the block itself is missing or does not add up.
    """
    from tokop.optimize.report import build_report

    if dataset != "data/demo/dataset.jsonl":
        typer.echo(
            f"this build can only read the demo dataset; --dataset {dataset!r} is not supported "
            "yet (DECISIONS.md D24).",
            err=True,
        )
        raise typer.Exit(2)

    block = build_report()["dataset_provenance"]
    shape = block["shape"]
    typer.echo(f"items:      {block['n']}")
    typer.echo(
        f"origins:    {block['real_traffic_items']} real traffic, "
        f"{block['model_generated_items']} model-generated, "
        f"{block['program_generated_items']} program-generated "
        f"({block['synthetic_share']:.0%} synthetic)"
    )
    typer.echo(
        f"generators: {', '.join(block['generators']) or 'none declared'}"
        + (
            f", decoding budget {block['decoding_budget']}"
            if block["decoding_budget"]
            else ", no decoding budget (no model wrote any of it)"
        )
    )
    typer.echo(
        f"tail:       {shape['templates_present']} of {shape['template_space']} templates "
        f"present ({shape['tail_coverage']:.0%}), {shape['tail_share']:.1%} of items in "
        f"rarely-seen templates, concentration {shape['concentration']:.2f}"
    )
    typer.echo("")
    typer.echo(f"CERTIFIABLE: {'yes' if block['certifiable'] else 'no'}")
    for refusal in block["refusals"]:
        typer.echo(f"  refused:  {refusal}")
    for warning in block["warnings"]:
        typer.echo(f"  warning:  {warning}")
    for note in block["notes"]:
        typer.echo(f"  note:     {note}")

    if check and not block["declared"]:
        typer.echo(
            "\nthe workload declares no `dataset_provenance:` block, so nothing is known about "
            "where its tasks came from.",
            err=True,
        )
        raise typer.Exit(1)


@app.command()
def certificate(
    out: str = typer.Option("", "--out", help="Write the certificate JSON here."),
    margin: float = typer.Option(0.03, "--margin"),
) -> None:
    """Issue a certificate: what was proven, what it rests on, and when it stops being true."""
    from datetime import UTC, datetime

    from tokop.optimize.certificate import issue
    from tokop.optimize.report import build_report

    payload = build_report(margin=margin)
    cert = issue(payload, today=datetime.now(UTC).date())
    typer.echo(f"workload:    {cert.workload}")
    typer.echo(
        f"claim:       {cert.candidate_pipeline} against {cert.baseline_pipeline} — {cert.verdict}"
    )
    typer.echo(
        f"delta:       {cert.delta_point * 100:+.1f} points "
        f"[{cert.delta_low * 100:+.1f}, {cert.delta_high * 100:+.1f}] at a "
        f"{cert.margin:.0%} margin, n = {cert.n}"
    )
    typer.echo(f"grading:     {cert.grading_mode}, calibration {cert.calibration_mode}")
    for snapshot in cert.model_snapshots:
        typer.echo(f"  {snapshot.role:9} {snapshot.snapshot}")
    typer.echo(f"prices:      {cert.price_snapshot_id}")
    typer.echo(f"issued:      {cert.issued} — expires {cert.expires}")
    typer.echo(
        f"canary:      {cert.looks_planned} looks planned, alpha {cert.alpha} spent by "
        f"{cert.spending} ({', '.join(f'{level:.4f}' for level in cert.levels())})"
    )
    typer.echo(f"fingerprint: {cert.fingerprint()}")
    typer.echo("")
    typer.echo(f"CERTIFIABLE: {'yes' if cert.certifiable else 'no'}")
    for refusal in cert.dataset_provenance.get("refusals", []):
        typer.echo(f"  refused:  {refusal}")
    if out:
        path = repo_root() / out
        cert.write(path)
        typer.echo(f"\n{_wrote(path)}")


@app.command()
def canary(
    certificate_path: str = typer.Option(..., "--certificate", help="Path to the certificate."),
    log: str = typer.Option("", "--log", help="Where the canary log lives. Defaults beside it."),
    today: str = typer.Option("", "--today", help="ISO date to check as of. Defaults to now."),
) -> None:
    """Take one look at a standing certificate (UPGRADE_V3.md U5).

    Exits 0 when the certificate still stands and 1 when the look raised, so a scheduled canary
    gates a deploy the way `tokop prove` gates a merge.
    """
    from datetime import UTC, datetime
    from datetime import date as _date

    from tokop.optimize.certificate import (
        CANARY_FRACTION,
        CanaryLog,
        Certificate,
        CertificateError,
        take_look,
    )
    from tokop.optimize.report import build_report, canary_observation, current_identity

    path = repo_root() / certificate_path
    log_path = repo_root() / log if log else path.with_suffix(".canary.json")
    as_of = _date.fromisoformat(today) if today else datetime.now(UTC).date()

    try:
        cert = Certificate.read(path)
        canary_log = CanaryLog.read(log_path, cert.fingerprint())
    except CertificateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    payload = build_report(margin=cert.margin)
    identity = current_identity(payload)
    # Drift is only testable against a recording the certificate was *not* issued from.
    # Re-scoring the same cassettes cannot produce a different answer, and reporting that as a
    # pass would be reporting a tautology as evidence.
    same_recording = identity["model_snapshots"] == cert.identity()["model_snapshots"]
    observed = None
    subset = 0
    if not same_recording:
        subset, delta, standard_error = canary_observation(
            payload,
            fraction=CANARY_FRACTION,
            # Seeded from the certificate so successive looks at the same certificate draw the
            # same stratified subset in the same order — a canary that re-drew freely could
            # look again by re-running rather than by spending alpha.
            seed=int(cert.fingerprint()[:8], 16) + canary_log.taken,
        )
        observed = (delta, standard_error)

    try:
        result = take_look(
            cert, canary_log, today=as_of, identity=identity, observed=observed, subset_size=subset
        )
    except CertificateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    canary_log.write(log_path)

    typer.echo(f"certificate: {path.name} ({cert.fingerprint()}), expires {cert.expires}")
    typer.echo(
        f"look:        {result.look} of {result.of_looks}, alpha {result.alpha_this_look:.4f}"
    )
    if result.drift_tested and result.observed_delta is not None and result.z is not None:
        typer.echo(
            f"drift:       {result.observed_delta * 100:+.1f} points on {result.subset_size} "
            f"stratified tasks against the certificate's {cert.delta_point * 100:+.1f}, "
            f"z = {result.z:+.2f}"
        )
    else:
        typer.echo(f"drift:       not tested — {result.drift_note}")
    typer.echo("")
    if result.raised:
        typer.echo("CANARY RAISED")
        for reason in result.reasons:
            typer.echo(f"  {reason}")
        raise typer.Exit(1)
    typer.echo("CANARY CLEAR: the certificate still stands.")


@app.command()
def lint(
    pipeline: str = typer.Argument("B0", help="Pipeline ID from the demo workload."),
) -> None:
    """Run the prompt lint over a pipeline template."""
    from tokop.optimize.report import build_report

    payload = build_report()
    entry = payload["lint"].get(pipeline)
    if entry is None:
        typer.echo(
            f"no pipeline {pipeline!r}; this workload defines {', '.join(sorted(payload['lint']))}",
            err=True,
        )
        raise typer.Exit(2)
    clear = entry["clear"]
    typer.echo(
        f"{pipeline}: CLEAR {entry['clear_total']}/25  "
        + "  ".join(f"{letter} {score}" for letter, score in clear.items())
    )
    if not entry["findings"]:
        typer.echo("  no findings")
        return
    for finding in entry["findings"]:
        typer.echo(
            f"  {finding['id']} [{finding['group']:5}] {finding['confidence']:9} "
            f"${float(finding['projected_usd_per_1k']):>8.2f}/1k  {finding['title']}"
        )
        typer.echo(f"        {finding['evidence'][:100]}")


@app.command("dataset")
def dataset_cmd(
    write: bool = typer.Option(False, "--write", help="Regenerate and write the dataset."),
) -> None:
    """Show or regenerate the demo dataset."""
    from tokop.workloads.demo.dataset import build, write_dataset_doc
    from tokop.workloads.demo.dataset import write as write_dataset

    bundle = build()
    if write:
        path, handbook = write_dataset(bundle)
        doc = write_dataset_doc(bundle)
        typer.echo(f"wrote {path.relative_to(repo_root())}")
        typer.echo(f"wrote {handbook.relative_to(repo_root())}")
        typer.echo(f"wrote {doc.relative_to(repo_root())}")
    counts: dict[str, int] = {}
    for item in bundle.items:
        counts[item.question_type] = counts.get(item.question_type, 0) + 1
    typer.echo(
        f"{len(bundle.items)} items ({len(bundle.calibration)} calibration, "
        f"{len(bundle.test)} test), seed {bundle.seed}"
    )
    for question_type in sorted(counts):
        typer.echo(f"  {question_type:12} {counts[question_type]:4}")


@app.command("fixtures-check")
def fixtures_check() -> None:
    """Regenerate gold answers from YAML and assert they match the stored dataset.

    Also checks that every cassette carries provider-reported usage and that every run manifest
    records the model IDs, the prices with their provenance, the seed and the date
    (SPEC.md section 10, check 4).
    """
    from tokop.workloads.demo.dataset import verify

    failures = 0
    result = verify()
    for name, ok, detail in result.checks:
        typer.echo(f"  {'ok  ' if ok else 'FAIL'}  {name}: {detail}")
        failures += 0 if ok else 1

    for fixture_name, state, fixture_detail in [*_check_fixture_dirs(), *_check_annotations()]:
        # `state is None` is a note: something worth printing that is not a defect in the
        # repository. The token-counter line is the only one, and it reports a property of the
        # machine running the check, not of the fixtures (DECISIONS.md D26).
        label = "note" if state is None else ("ok  " if state else "FAIL")
        typer.echo(f"  {label}  {fixture_name}: {fixture_detail}")
        failures += 1 if state is False else 0

    if failures:
        typer.echo(f"\nfixtures-check FAILED ({failures} check(s))", err=True)
        raise typer.Exit(1)
    typer.echo("\nfixtures-check passed")


def _check_annotations() -> list[tuple[str, bool | None, str]]:
    """Replay every verdict an annotation set records and assert it matches its cassette.

    An annotation set is a committed artifact holding numbers that reach the screen, so it has
    to be checkable the way the dataset is: regenerate from the source and compare. The source
    here is the judge call itself. A verdict that no longer matches the reply it came from means
    either the parser changed or the file was edited, and both are defects.
    """
    from tokop.optimize.annotation import AnnotationError, AnnotationSet, annotations_path

    checks: list[tuple[str, bool | None, str]] = []
    for kind in ("demo", "test"):
        root = fixtures_dir() / kind
        for contract in (False, True):
            path = annotations_path(root, contract=contract)
            if not path.exists():
                continue
            try:
                annotations = AnnotationSet.read(path)
            except AnnotationError as exc:
                checks.append((f"fixtures/{kind}: {path.name}", False, str(exc)))
                continue
            checks.append(_replay_verdicts(kind, root, path, annotations))
    return checks


def _replay_verdicts(
    kind: str, root: Path, path: Path, annotations: Any
) -> tuple[str, bool | None, str]:
    """One annotation set's verdicts, re-read from the calls they came from."""
    from tokop.adapters.cassette import CassetteStore
    from tokop.workloads.verification import verifier_kind

    store = CassetteStore(root / "cassettes")
    parse = verifier_kind(str(annotations.judge["kind"])).parse
    missing: list[str] = []
    disagreeing: list[str] = []
    replayed = 0
    for item in annotations.items:
        for name, arm in (("baseline", item.baseline), ("candidate", item.candidate)):
            pairs: list[tuple[str, int | None]] = [(arm.judge_cassette_key, arm.judge_correct)]
            if arm.strong_cassette_key is not None:
                pairs.append((arm.strong_cassette_key, arm.strong_correct))
            for key, recorded in pairs:
                cassette = store.get(key)
                if cassette is None:
                    missing.append(f"{item.task_id}.{name}")
                    continue
                replayed += 1
                verdict = parse(cassette.response_text)
                stored = recorded if recorded is not None else verdict.correct
                if verdict.correct != stored:
                    disagreeing.append(f"{item.task_id}.{name}")

    ok = not missing and not disagreeing
    detail = f"{replayed} judge verdicts replayed from cassettes, all matching"
    if missing:
        detail = f"{len(missing)} verdicts name a cassette that is not there: {missing[:3]}"
    elif disagreeing:
        detail = (
            f"{len(disagreeing)} verdicts disagree with the reply they came from: {disagreeing[:3]}"
        )
    return (f"fixtures/{kind}: every verdict in {path.name} matches its call", ok, detail)


def _check_fixture_dirs() -> list[tuple[str, bool | None, str]]:
    """Cassette and manifest checks over whichever fixture set this build has.

    A check reports ``True`` (passed), ``False`` (failed) or ``None`` (a note: true, worth
    saying, and not a defect).
    """
    import json

    from tokop.adapters.cassette import CassetteStore

    checks: list[tuple[str, bool | None, str]] = []
    for kind in ("demo", "test"):
        root = fixtures_dir() / kind
        cassettes = root / "cassettes"
        if not cassettes.exists():
            continue
        store = CassetteStore(cassettes)
        all_cassettes = store.all()
        without_usage = [c.key[:10] for c in all_cassettes if not c.raw_usage and c.error is None]
        checks.append(
            (
                f"fixtures/{kind}: every cassette has provider-reported usage",
                not without_usage,
                f"{len(all_cassettes)} cassettes, {len(without_usage)} without usage"
                + (f": {without_usage[:3]}" if without_usage else ""),
            )
        )

        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            checks.append((f"fixtures/{kind}: manifest present", False, f"no {manifest_path.name}"))
            continue
        manifest = json.loads(manifest_path.read_text())

        # What the manifest says this recording is, against the bytes on disk. Cassette keys
        # hash the *request*, so a key set alone cannot notice an edited answer; the digest
        # covers every cassette and blob. A recording nobody can check is a recording that has
        # to be taken on trust, which is the opposite of the point (UPGRADE_V4.md M13).
        declared = manifest.get("cassette_digest")
        count = manifest.get("cassette_count")
        if not declared:
            checks.append(
                (
                    f"fixtures/{kind}: the manifest carries a cassette digest",
                    False,
                    "it does not; rebuild with `tokop build-test-fixtures`",
                )
            )
        else:
            actual = store.digest()
            checks.append(
                (
                    f"fixtures/{kind}: the manifest matches the cassettes on disk",
                    actual == declared and count == len(all_cassettes),
                    f"{len(all_cassettes)} cassettes, digest {actual[:12]}"
                    + (
                        ""
                        if actual == declared
                        else f" but the manifest claims {str(declared)[:12]} ({count} cassettes)"
                    ),
                )
            )
        runs = manifest.get("runs") or []
        missing: list[str] = []
        for run in runs:
            for field in ("model_ids", "seed", "recorded_at", "prices", "split_hash"):
                if not run.get(field):
                    missing.append(f"{run.get('pipeline', '?')}.{field}")
            for model_id, price in (run.get("prices") or {}).items():
                for field in ("source_url", "retrieved"):
                    if not price.get(field):
                        missing.append(f"{model_id}.{field}")
        checks.append(
            (
                f"fixtures/{kind}: every manifest records models, prices, seed and date",
                not missing and bool(runs),
                f"{len(runs)} runs"
                + (f", missing {missing[:4]}" if missing else ", all fields present"),
            )
        )

        # `o200k_base` is downloaded on first use, so the same repository counts tokens
        # differently on a machine that can reach the vocabulary host than on one that cannot.
        # Everything token-derived moves with it. This check makes that divergence visible
        # instead of leaving it to surface as four unrelated-looking test failures
        # (DECISIONS.md D26).
        from tokop.core.tokenize import base_counter_name

        built_with = manifest.get("base_token_counter")
        live = base_counter_name()
        if not built_with:
            # Fixtures that do not say which counter built them cannot be checked anywhere but
            # the machine that built them. That is a defect in the fixtures.
            checks.append(
                (
                    f"fixtures/{kind}: the manifest records its token counter",
                    False,
                    "it does not; rebuild with `tokop build-test-fixtures`",
                )
            )
        elif built_with == live:
            checks.append(
                (
                    f"fixtures/{kind}: the token counter matches the one that built them",
                    True,
                    f"both are {live}",
                )
            )
        else:
            # Not a failure. The committed numbers rest on the recorded counter and are checked
            # against it; what differs is what *this* machine would estimate for a new prompt.
            # Failing here would make the repository red on every machine with a better
            # tokenizer than the one that built the fixtures, which is backwards.
            checks.append(
                (
                    f"fixtures/{kind}: token counter",
                    None,
                    f"built with {built_with}, this machine has {live}. The committed numbers "
                    f"rest on {built_with} and are checked against it; token estimates computed "
                    "here for a new prompt will differ and will name their own counter.",
                )
            )
    if not checks:
        checks.append(
            (
                "fixture directories",
                False,
                "neither fixtures/demo/ nor fixtures/test/ has cassettes",
            )
        )
    return checks


@app.command("sync-models")
def sync_models(
    url: str = typer.Option(OPENROUTER_MODELS_URL, "--url", help="Model listing endpoint."),
    out: str = typer.Option(
        "fixtures/test/openrouter-models.json", "--out", help="Where to write the snapshot."
    ),
) -> None:
    """Pull model metadata from OpenRouter's public model listing into the registry."""
    from tokop.core.sync import SyncError, fetch_listing, write_snapshot

    destination = repo_root() / out
    try:
        payload = fetch_listing(url)
    except SyncError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    count = write_snapshot(payload, destination)
    typer.echo(f"wrote {count} priced models to {out} (retrieved {payload['_tokop_retrieved']})")
    typer.echo("Everything from the listing loads as verified: false; config/prices.yaml wins.")


@app.command()
def models() -> None:
    """List the model registry with its price provenance."""
    registry = load_registry()
    typer.echo(f"{'model':38} {'in $/Mtok':>10} {'out $/Mtok':>11}  verified  source")
    for model_id in sorted(registry.models):
        entry = registry.models[model_id]
        mark = "yes" if entry.price.provenance.verified else "NO "
        typer.echo(
            f"{model_id:38} {entry.price.input:>10} {entry.price.output:>11}  {mark:8}  "
            f"{entry.price.provenance.source_url}"
        )
    roles = ", ".join(f"{k}={v}" for k, v in sorted(registry.roles.items()))
    typer.echo(f"\nroles: {roles}")
    typer.echo(f"scarce: {', '.join(registry.scarce)}")
    if registry.listing_taken:
        typer.echo(f"listing snapshot: {registry.listing_source} taken {registry.listing_taken}")


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
