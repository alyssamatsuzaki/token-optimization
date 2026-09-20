# The 90-second demo

Every number below is generated from `tokop report` by `scripts/write_demo.py`. Regenerate it
after any change to the fixtures: **do not edit the figures by hand.**

Start at `http://localhost:8000` after `make demo`. No API keys, no network.

---

## 0:00 — The problem, in one line (10 s)

> "This support bot answers 304 policy questions. It costs
> **$0.05194 per successful answer**. I'm going to make it cost **$0.00462** — and prove it
> didn't get worse."

Point at the headline row. Both numbers are already on screen.

## 0:10 — Why it costs that (20 s)

Scroll to **Findings**. They are ranked by projected dollars per 1,000 tasks, not by severity.

> "The top two findings are the same mistake seen from two angles. 204 calls, 0 cache reads, ~8,714 input tokens per call, a 21,818-character block identical…
> That's $39.21 per thousand tasks. And the second:
> 204 calls share only their first ~845 tokens; ~7,869 tokens per call sit after t…"

The point to make: **the ranking is in dollars.** Politeness filler is on this list too, at
$0.20 — three orders of magnitude down, exactly where it
belongs. A linter that puts "you said please" above a 7,000-token uncacheable handbook is a
linter nobody acts on.

Hover any dollar figure: the formula that produced it is in the tooltip.

## 0:30 — Build the candidate (15 s)

Click **Build candidate**. The graph morphs: one frontier-model node becomes a three-tier
cascade, each edge labelled with the share of tasks that flows along it.

> "Three changes. The handbook moves in front of a cache breakpoint so it can be read instead of
> re-sent. The prompt gets an output contract — Current pipeline generates paragraphs where
> the grader reads one number. And a cascade sends each task to the cheapest tier a scorer will
> vouch for."

The cascade summary underneath shows the search: **2,601 threshold settings
evaluated in 0.42 seconds**, on recorded answers, costing nothing.

## 0:45 — Run the proof (25 s)

Click **Run proof**.

> "Cascade on the B2 prompt cuts cost per successful task by 91.1% (interval 89.1 to 92.6%); accuracy difference +1.0 points, 95% CI [-2.5, +4.4], n = 204, inside the 3-point margin."

Read the verdict label aloud: **Non-inferior at a 3-point margin**.

This is the moment that matters. The interval's lower bound is -2.5 points, above the -3.0-point margin, so Tokop can make a conclusive non-inferiority claim about the simulated traces. The evidence label still refuses to present invented answers as production data.

Then the savings waterfall:

| | Cost per successful task | Accuracy | Verdict |
|---|---:|---:|---|
| **B0** Current pipeline | $0.05194 | 95.1% | — it is the baseline |
| **B1** Cache-friendly order | $0.01114 | 95.6% | Non-inferior at a 3-point margin |
| **B2** CLEAR rewrite with an output contract | $0.00672 | 97.1% | Non-inferior at a 3-point margin |
| **B3** Cascade on the B2 prompt | $0.00462 | 96.1% | Non-inferior at a 3-point margin |

> "Prompt order alone took it from $0.05194 to
> $0.01114. Same words, same model, same token cap —
> only the order changed. The output contract took it to
> $0.00672. The cascade took it to
> $0.00462."

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

> "The proof cost **$17.09** to produce and repays after
> **381 tasks**. And these fixtures are simulated — this build had no
> API credentials, so the model answers underneath are generated. The engine, the statistics and
> every number on screen are real."

---

## 1:30 — The part that makes it usable on a real workload (20 s)

Scroll to **Without the answer key**.

> "Everything above rests on 204 tasks with known answers. Almost nobody has that. So: a cheap
> judge — claude-haiku-4-5-20251001 — grades every task, and a strong grader re-grades
> **44 of them (22%)**, chosen where a strong label buys the most
> interval. The judge on its own says **+5.9 points**. Corrected, it says
> **-3.2 points**, interval **-12.4 to +6.0**. The gap
> between those two — **+9.1 points** — is the judge's bias, and Tokop measured it
> rather than assuming it away."

> "This demo happens to have gold answers, so we can check: the gold-graded difference is +1.0 points, and the gold-free interval covers it. A real unlabelled workload never gets to run that check, which is exactly why it is run here."

> "The estimator is unbiased for the strong grader's mean no matter how bad the cheap judge is.
> A bad judge costs interval width, never correctness. That is the whole argument, and it is why
> the wide interval here is honest rather than embarrassing: on this workload the judge is wrong
> often enough that the cost-optimal sampling rate is 100%, and the report
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
0.72 at the cheap tier and 0.89 at the
mid tier.

**"What if the cheap model is wrong and the scorer is confident?"**
Then the task is answered wrong, and it shows up in the accuracy difference — that is what the
proof measures. Breakdown by question type shows where it happens: the candidate's weakest type
is **exception**, at
90% against the baseline's
95% over 20 tasks. That gap is visible on the
screen rather than averaged away.

**"Your judge is an LLM. Why should I trust it?"**
You should not, and the design does not. Run the adversarial judge test: it injects a judge that
marks 20% of one class of correct answers wrong. The naive judge-only interval stops covering the
true accuracy; the corrected interval still covers it, at every committed seed. That test is a
release blocker — `make verify` runs it by name.

**"Why does the verdict pass when the lower bound is close to the margin?"**
Because the unrounded lower bound is above the predeclared -3.0-point margin. Tokop computes the verdict before rounding the displayed interval and does not move the threshold after seeing the result.
