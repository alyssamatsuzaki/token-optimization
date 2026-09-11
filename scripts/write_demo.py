#!/usr/bin/env python
"""Generate docs/DEMO.md from the report.

The demo script quotes a dozen figures. Writing them by hand would guarantee that one of them is
stale the next time the fixtures change, and a demo that quotes a number the screen does not show
is worse than no script at all. So the script is generated, like the README's metrics block.

    engine/.venv/bin/python scripts/write_demo.py
"""

from __future__ import annotations

from pathlib import Path

from tokop.optimize.report import build_report


def main() -> Path:
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

> "This support bot answers {report['workload']['dataset']['size']} policy questions. It costs
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

This is the moment that matters. The interval's lower bound sits at
{proof["delta_accuracy"]["low"] * 100:+.1f} points, right on the 3-point margin — so Tokop says
*inconclusive* and tells you exactly how many more tasks would settle it. It does not round
towards the pleasing answer.

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

**"Why is the verdict inconclusive rather than a pass?"**
Because the interval straddles the margin by a fraction of a point. Reporting that as a pass is
how cost-cutting decisions go wrong. It tells you the remedy instead: about
{proof["verdict"]["additional_tasks_needed"]} more tasks.
"""
    path = Path(__file__).resolve().parent.parent / "docs" / "DEMO.md"
    path.write_text(text)
    return path


if __name__ == "__main__":
    print(f"wrote {main()}")
