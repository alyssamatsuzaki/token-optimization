#!/usr/bin/env python
"""Generate docs/DEMO.md from the report.

The demo script quotes a dozen figures. Writing them by hand would guarantee that one of them is
stale the next time the fixtures change, and a demo that quotes a number the screen does not show
is worse than no script at all. So the script is generated, like the README's metrics block.

    engine/.venv/bin/python scripts/write_demo.py
    engine/.venv/bin/python scripts/write_demo.py --check   # fail if it is stale

``--check`` is what `make verify` runs: it rebuilds the text and compares, so a fixture change
that moves a number cannot leave the demo script quoting the old one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tokop.optimize.report import build_report

# The cascade search time is a wall-clock measurement of the machine that generated the file,
# so it moves between runs on the same fixtures. It is a real measured number and stays in the
# document; it just is not evidence that the document is stale.
_TIMING = re.compile(r"evaluated in \d+\.\d+ seconds")


def _comparable(text: str) -> str:
    return _TIMING.sub("evaluated in <wall clock> seconds", text)


def _judged_variables(payload: object) -> dict[str, object]:
    """Numbers for the gold-free beat, or a stand-in saying why there are none.

    The demo script is generated so it cannot quote a stale figure; that applies to a section
    the fixtures may not be able to support at all, so the absence is generated too.
    """
    judged = payload["judged"]  # type: ignore[index]
    if not judged.get("available"):
        reason = judged.get("reason", "no reason given")
        return {
            "judged_model": "no judge",
            "judged_annotated": 0,
            "judged_share": 0.0,
            "judge_only_point": 0.0,
            "judged_point": 0.0,
            "judged_low": 0.0,
            "judged_high": 0.0,
            "judge_bias": 0.0,
            "cost_optimal_rate": 0.0,
            "coverage_line": f"This build has no gold-free estimate to show: {reason}",
        }
    annotation = judged["annotation"]
    coverage = judged.get("coverage_check") or {}
    covers = coverage.get("judged_interval_covers_gold")
    coverage_line = (
        "This demo happens to have gold answers, so we can check: the gold-graded difference is "
        f"{coverage.get('gold_delta_accuracy', 0.0) * 100:+.1f} points, and the gold-free "
        f"interval {'covers it' if covers else 'does NOT cover it'}. A real unlabelled workload "
        "never gets to run that check, which is exactly why it is run here."
    )
    return {
        "judged_model": judged["judge"]["model_id"],
        "judged_annotated": annotation["n_annotated"],
        "judged_share": annotation["annotated_share"] * 100,
        "judge_only_point": judged["judge_only"]["delta_accuracy"]["point"] * 100,
        "judged_point": judged["delta_accuracy"]["point"] * 100,
        "judged_low": judged["delta_accuracy"]["low"] * 100,
        "judged_high": judged["delta_accuracy"]["high"] * 100,
        "judge_bias": judged["judge_only"]["bias_vs_corrected"] * 100,
        "cost_optimal_rate": annotation["cost_optimal_rate"] * 100,
        "coverage_line": coverage_line,
    }


def _proof_interpretation(proof: dict) -> tuple[str, str]:
    """Explain the computed verdict without hard-coding the current fixture outcome."""
    verdict = proof["verdict"]
    label = verdict["label"]
    low = proof["delta_accuracy"]["low"] * 100
    margin = verdict["margin"] * 100

    if label == "non_inferior":
        explanation = (
            "This is the moment that matters. The interval's lower bound is "
            f"{low:+.1f} points, above the -{margin:.1f}-point margin, so Tokop can make a "
            "conclusive non-inferiority claim about the simulated traces. The evidence label "
            "still refuses to present invented answers as production data."
        )
        faq = (
            '**"Why does the verdict pass when the lower bound is close to the margin?"**\n'
            f"Because the unrounded lower bound is above the predeclared -{margin:.1f}-point "
            "margin. Tokop computes the verdict before rounding the displayed interval and does "
            "not move the threshold after seeing the result."
        )
        return explanation, faq

    if label == "inconclusive":
        additional = verdict.get("additional_tasks_needed")
        remedy = (
            f"about {additional:,} more tasks"
            if additional is not None
            else "more informative paired evidence"
        )
        explanation = (
            "This is the moment that matters. The interval's lower bound is "
            f"{low:+.1f} points and does not clear the -{margin:.1f}-point margin, so Tokop says "
            f"*inconclusive* and asks for {remedy}. It does not round towards the pleasing answer."
        )
        faq = (
            '**"Why is the verdict inconclusive rather than a pass?"**\n'
            "Because the interval does not clear the predeclared margin. Reporting that as a pass "
            f"is how cost-cutting decisions go wrong; the remedy is {remedy}."
        )
        return explanation, faq

    explanation = (
        "This is the moment that matters. The evidence falls outside the allowed "
        f"-{margin:.1f}-point margin, so Tokop rejects the candidate rather than trading away more "
        "quality than the workload owner allowed."
    )
    faq = (
        '**"Why was the cheaper candidate rejected?"**\n'
        "Because its paired interval does not satisfy the predeclared non-inferiority margin. "
        "Cost savings cannot override that quality gate."
    )
    return explanation, faq


def main(*, check: bool = False) -> Path:
    report = build_report()
    proof = report["proof"]
    cascade = report["cascade"]
    base = proof["baseline"]["cost_per_successful_task"]["point"]
    cand = proof["candidate"]["cost_per_successful_task"]["point"]
    findings = report["findings"]

    def finding(rule: str) -> dict:
        return next(f for f in findings if f["id"] == rule)

    w01, w02, pl08 = finding("W01"), finding("W02"), finding("PL08")
    # The question type where the two pipelines differ most: quoting one where they happen to
    # match makes the sentence pointless, and which type that is changes with the fixtures.
    worst = min(
        proof["by_type"],
        key=lambda b: b["candidate_accuracy"] - b["baseline_accuracy"],
    )
    wf = {s["pipeline"]: s for s in report["waterfall"]}
    judged_vars = _judged_variables(report)
    judged_model = judged_vars["judged_model"]
    judged_annotated = judged_vars["judged_annotated"]
    judged_share = judged_vars["judged_share"]
    judge_only_point = judged_vars["judge_only_point"]
    judged_point = judged_vars["judged_point"]
    judged_low = judged_vars["judged_low"]
    judged_high = judged_vars["judged_high"]
    judge_bias = judged_vars["judge_bias"]
    cost_optimal_rate = judged_vars["cost_optimal_rate"]
    coverage_line = judged_vars["coverage_line"]
    proof_interpretation, proof_faq = _proof_interpretation(proof)
    proof_n = proof["delta_accuracy"]["n"]

    rows = ""
    for pipeline_id in ("B0", "B1", "B2", "B3"):
        step = wf[pipeline_id]
        verdict = step["verdict"]["display"] if step["verdict"] else "— it is the baseline"
        rows += (
            f"| **{pipeline_id}** {step['label']} "
            f"| ${step['cost_per_successful_task']['point']:.5f} "
            f"| {step['accuracy']['point'] * 100:.1f}% | {verdict} |\n"
        )

    text = f"""# The 90-second demo

Every number below is generated from `tokop report` by `scripts/write_demo.py`. Regenerate it
after any change to the fixtures: **do not edit the figures by hand.**

Start at `http://localhost:8000` after `make demo`. No API keys, no network.

---

## 0:00 — The problem, in one line (10 s)

> "This support bot answers {report["workload"]["dataset"]["size"]} policy questions. It costs
> **${base:.5f} per successful answer**. I'm going to make it cost **${cand:.5f}** — and prove it
> didn't get worse."

Point at the headline row. Both numbers are already on screen.

## 0:10 — Why it costs that (20 s)

Scroll to **Findings**. They are ranked by projected dollars per 1,000 tasks, not by severity.

> "The top two findings are the same mistake seen from two angles. {w01["evidence"][:90]}…
> That's ${float(w01["projected_usd_per_1k"]):.2f} per thousand tasks. And the second:
> {w02["evidence"][:80]}…"

The point to make: **the ranking is in dollars.** Politeness filler is on this list too, at
${float(pl08["projected_usd_per_1k"]):.2f} — three orders of magnitude down, exactly where it
belongs. A linter that puts "you said please" above a 7,000-token uncacheable handbook is a
linter nobody acts on.

Hover any dollar figure: the formula that produced it is in the tooltip.

## 0:30 — Build the candidate (15 s)

Click **Build candidate**. The graph morphs: one frontier-model node becomes a three-tier
cascade, each edge labelled with the share of tasks that flows along it.

> "Three changes. The handbook moves in front of a cache breakpoint so it can be read instead of
> re-sent. The prompt gets an output contract — {wf["B0"]["label"]} generates paragraphs where
> the grader reads one number. And a cascade sends each task to the cheapest tier a scorer will
> vouch for."

The cascade summary underneath shows the search: **{cascade["evaluated"]:,} threshold settings
evaluated in {cascade["runtime_seconds"]:.2f} seconds**, on recorded answers, costing nothing.

## 0:45 — Run the proof (25 s)

Click **Run proof**.

> "{proof["verdict"]["sentence"]}"

Read the verdict label aloud: **{proof["verdict"]["display"]}**.

{proof_interpretation}

Then the savings waterfall:

| | Cost per successful task | Accuracy | Verdict |
|---|---:|---:|---|
{rows}
> "Prompt order alone took it from ${wf["B0"]["cost_per_successful_task"]["point"]:.5f} to
> ${wf["B1"]["cost_per_successful_task"]["point"]:.5f}. Same words, same model, same token cap —
> only the order changed. The output contract took it to
> ${wf["B2"]["cost_per_successful_task"]["point"]:.5f}. The cascade took it to
> ${wf["B3"]["cost_per_successful_task"]["point"]:.5f}."

## 1:10 — The one bold chart (10 s)

Scroll to the **cost-quality frontier**. Every threshold setting the search evaluated, scored on
the test split.

> "The crosshair is the operating point. It was chosen on the calibration split before any of
> these test numbers existed — which is the only reason they're worth reading."

## 1:20 — Land it on trust (10 s)

Click **Open trace** on any disagreement.

> "Every number on that screen opens onto this: the prompt hash, the tokens by bucket with the
> source each came from, the raw provider usage payload, the scorer's features, and the grade.
> Nothing is asserted. It's all computed from traces."

Close with the honest part:

> "The proof cost **${float(proof["proof_cost"]["total_usd"]):.2f}** to produce and repays after
> **{proof["repayment_tasks"]:,} tasks**. And these fixtures are simulated — this build had no
> API credentials, so the model answers underneath are generated. The engine, the statistics and
> every number on screen are real."

---

## 1:30 — The part that makes it usable on a real workload (20 s)

Scroll to **Without the answer key**.

> "Everything above rests on {proof_n} tasks with known answers. Almost nobody has that. So: a cheap
> judge — {judged_model} — grades every task, and a strong grader re-grades
> **{judged_annotated} of them ({judged_share:.0f}%)**, chosen where a strong label buys the most
> interval. The judge on its own says **{judge_only_point:+.1f} points**. Corrected, it says
> **{judged_point:+.1f} points**, interval **{judged_low:+.1f} to {judged_high:+.1f}**. The gap
> between those two — **{judge_bias:+.1f} points** — is the judge's bias, and Tokop measured it
> rather than assuming it away."

> "{coverage_line}"

> "The estimator is unbiased for the strong grader's mean no matter how bad the cheap judge is.
> A bad judge costs interval width, never correctness. That is the whole argument, and it is why
> the wide interval here is honest rather than embarrassing: on this workload the judge is wrong
> often enough that the cost-optimal sampling rate is {cost_optimal_rate:.0f}%, and the report
> says so instead of selling a saving that is not there."

---

## If you have 30 seconds more

- **Inspect** — paste any prompt, see cost per 1,000 calls across the registry, press **Apply
  safe fixes** and watch the diff. Note the net input delta is **positive**: the output contract
  costs input tokens and buys back five times as much on the output side. The screen shows both
  halves rather than the flattering one.
- **Settings** — every price shows the page it was read from and the date. The ones from the
  gateway listing are marked unverified, in red.

## What to say if asked

**"Is the cascade just routing easy questions to the cheap model?"**
Effectively yes, and the scorer is the interesting part: it never sees the gold answer. It
predicts correctness from deterministic features — whether the output parsed, whether the model's
own evidence quote actually appears in the section it cited. AUROC on the test split is
{cascade["tiers"][0]["auroc"]:.2f} at the cheap tier and {cascade["tiers"][1]["auroc"]:.2f} at the
mid tier.

**"What if the cheap model is wrong and the scorer is confident?"**
Then the task is answered wrong, and it shows up in the accuracy difference — that is what the
proof measures. Breakdown by question type shows where it happens: the candidate's weakest type
is **{worst["question_type"].replace("_", " ")}**, at
{worst["candidate_accuracy"] * 100:.0f}% against the baseline's
{worst["baseline_accuracy"] * 100:.0f}% over {worst["n"]} tasks. That gap is visible on the
screen rather than averaged away.

**"Your judge is an LLM. Why should I trust it?"**
You should not, and the design does not. Run the adversarial judge test: it injects a judge that
marks 20% of one class of correct answers wrong. The naive judge-only interval stops covering the
true accuracy; the corrected interval still covers it, at every committed seed. That test is a
release blocker — `make verify` runs it by name.

{proof_faq}
"""
    path = Path(__file__).resolve().parent.parent / "docs" / "DEMO.md"
    if check:
        if not path.exists():
            raise SystemExit(f"{path} does not exist; run scripts/write_demo.py")
        if _comparable(path.read_text()) != _comparable(text):
            raise SystemExit(
                f"{path.name} is stale: the report has moved since it was generated. "
                "Run `engine/.venv/bin/python scripts/write_demo.py`."
            )
        return path
    path.write_text(text)
    return path


if __name__ == "__main__":
    checking = "--check" in sys.argv[1:]
    result = main(check=checking)
    print(f"{'checked' if checking else 'wrote'} {result}")
