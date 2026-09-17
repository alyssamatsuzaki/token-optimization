# Tokop design direction

Tokop is an analysis tool for founders and engineers deciding whether to adopt a less expensive
LLM pipeline. The interface should explain the cost-quality tradeoff within a minute and provide
the trace behind every reported result.

The visual reference is a bench instrument on drafting paper: hairline rules, calibrated scales,
and limited decoration. Provenance appears alongside the value it qualifies.

## 1. Palette

Six named values. Nothing outside this list ships.

| Token | Hex | Role |
|---|---|---|
| `paper` | `#F2F3EF` | Green-grey page background. |
| `chalk` | `#FFFFFF` | Raised surface. Used only where content must sit *above* the plane: the trace drawer, the frontier plot field, open menus. Never the default card. |
| `ink` | `#15181A` | Primary text, axis lines, hairline rules at full strength. |
| `graphite` | `#5E6560` | Secondary text, units, and **every estimated / projected / simulated value**. |
| `prussian` | `#12456B` | The measured signal. Measured quantities, the operating point, the model-tier ramp. |
| `vermilion` | `#C0452A` | Scarce-model spend, "worse" verdicts, budget stops. |
| `verdigris` | `#1F6F5C` | "Better" and non-inferior verdicts, realised savings. |

Derived ramps (no new hues):

- **Tier** is encoded by *value*, not hue: `prussian` at 25% / 55% / 100% tint = cheap / mid /
  frontier. A reader can rank tiers without knowing the legend, and colour-blind readers get
  the ordering from lightness alone.
- **Scarcity** is a *mark*, not a hue: scarce-model spend carries a 45° hatch overlay plus
  `vermilion` at the edge. This keeps scarcity readable on top of the tier ramp.
- **Rules** are `ink` at 12% (`#15181A1F`) for hairlines and 20% for section divisions.

### Rationale

Prussian blue and vermilion resemble drafting inks and provide a clear measured/warning pair. The
palette avoids common dashboard combinations and does not require a third accent for result state.

## 2. Type

Self-hosted from npm (`@fontsource`), so the container needs no font CDN and the demo cannot
fail on a blocked request.

- **IBM Plex Sans Variable** — everything the reader reads. Chosen for its engineering-drawing
  provenance and, decisively, for genuine tabular figures.
- **IBM Plex Mono** — *only* for content that is literally code or prompt text: message bodies
  in the trace drawer, JSON tool definitions, prompt diffs, cassette hashes. Never for labels.

Scale (rem, 16px root): `0.75 / 0.8125 / 0.875 / 1 / 1.25 / 1.75 / 2.5`. Weights: 400 body,
500 for column headers and emphasis, 600 for the two headline numbers only.

**Numerals.** `font-variant-numeric: tabular-nums` is on globally. Every figure in the product
sits in a column that can be scanned vertically, so digits must not shift.

## 3. Numbers and provenance

Measured and estimated values must remain visually distinct.

- **Measured** (provider-reported usage, exact token counts): `ink`, weight 500, full opacity.
- **Estimated / projected / simulated**: `graphite`, with a superscript provenance mark, and —
  for projected and simulated only — a 1px dotted underline.
- Provenance marks, defined once in a legend in the page footer and repeated nowhere:
  `▪` provider-reported · `=` exact count · `≈` estimated · `→` projected · `~` simulated.
- Units always ship with the number (`$0.0142 / task`, `1,284 tok`, `p50 1.9 s`). No bare floats.
- Intervals render as `1.2–1.9` ranges inline and as whiskers in charts. A point estimate
  without an interval is a bug in any quality claim.

## 4. Surface and structure

- **No card grid.** Content is organised by hairline rules and whitespace. A raised `chalk`
  surface means "this floats above the page" and appears at most twice per screen.
- **One bold element per screen.** On Optimize it is the cost-quality frontier plot: full-bleed
  within its column, `chalk` field, `prussian` points, a `vermilion` hatch band for the
  scarce-model region, and the operating point drawn as a crosshair with its coordinates
  labelled. Everything else on that screen is grey, black and hairlines.
- **Motion.** Exactly one orchestrated transition: the baseline→candidate graph morph on
  Optimize (420 ms, `cubic-bezier(.2,.7,.3,1)`, nodes fade+translate, edges draw). Everything
  else is ≤120 ms opacity. `prefers-reduced-motion: reduce` replaces the morph with a cut.
- **Density.** Built for 1280px and up, three-column on Optimize. Below 1024px the app switches to
  view-only mode and displays an explanation.

## 5. Copy

Sentence case everywhere. Full names for metrics — "Cost per successful task", never "CPS".
Buttons name the act: "Build candidate", "Run proof", "Apply safe fixes". The state after the
act reuses the same words ("Candidate built", "Proof run"). Empty states name the next action.
Errors say what happened and how to fix it, in that order. Disabled controls carry a one-line
reason in the same size as the label, not a tooltip.

## 6. Wireframes

### Optimize (default route)

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ tokop    Optimize  Inspect  Compare  Spend            Settings   [replay · fixtures]│
├────────────────────────────────────────────────────────────────────────────────────┤
│ Returns-policy support bot · 300 tasks · recorded 2026-09-11 · opus-5/sonnet-5/haiku│
├──────────────────────────────────┬─────────────────────────────────────────────────┤
│ Cost per successful task         │ Accuracy                Scarce-model share       │
│   $0.0142 ▪   (0.0131–0.0154)    │   94.5% (90.8–96.8)       100% of spend          │
├──────────────────────────────────┴─────────────────────────────────────────────────┤
│ CURRENT PIPELINE  B0                      │ FINDINGS            ranked by $/1k tasks│
│  ┌────────┐      ┌──────────┐             │ ── W02  prefix changes every call  →$41 │
│  │ prompt │─────▶│ opus-5   │────▶ answer │    200 calls, 0 cache reads             │
│  │ 7,102t │      │ 200 call │             │ ── PL02 question before handbook   →$41 │
│  └────────┘      └──────────┘             │ ── W03  output 9x what grader needs →$18│
│                   ▓▓▓▓▓▓ 100% of run cost │ ── PL06 no output contract         →$18 │
│                                           │ ── PL07 rule stated twice          → $2 │
│           [ Build candidate ]             │ ── PL11 concise vs. full detail    → $1 │
├───────────────────────────────────────────┴─────────────────────────────────────────┤
│ ▸ after Build candidate: graph morphs, B3 cascade appears, edges labelled 71%/22%/7%│
│           [ Run proof ]                                                             │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ VERDICT   Non-inferior at a 3-point margin                                          │
│ B3 cuts cost per successful task by 78.4% (72.1–83.0); accuracy difference           │
│ −1.0 points, 95% CI [−4.2, +2.1], n = 200, inside the 3-point margin.               │
│                                                                                     │
│        −3pt margin│                                                                 │
│   ├────────●──────┼──────────┤        proof cost $2.41 ▪ · repays after 1,340 tasks │
│  −6        −1     0         +3                                                      │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ COST-QUALITY FRONTIER  ◀ the one bold element                                       │
│  acc │                              ·· ·B0                                          │
│      │                    ·  ·⊕·  ·B2      ⊕ operating point, chosen on calibration │
│      │         ·  ·  ·                        before test results were computed     │
│      └─────────────────────────────── cost/task                                     │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ SAVINGS WATERFALL   B0 ████ $0.0142 │ B1 ██ $0.0071 │ B2 █ $0.0038 │ B3 ▌$0.0031    │
│ BY QUESTION TYPE    table: accuracy / tier share / cost per successful task          │
│ DISAGREEMENTS       12 tasks where B0 and B3 graded differently → opens trace        │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### Inspect

```
┌─────────────────────────────┬───────────────────────────────────────────────────────┐
│ System prompt   [textarea]  │ TOKENS  opus-5 712 =   sonnet-5 712 =   haiku-4.5 548≈│
│ User template   [textarea]  │ COST PER 1,000 CALLS   sorted, cheapest first          │
│ Tools (JSON)    [textarea]  │   haiku-4.5   $0.55 ▪   sonnet-5 $1.42 ▪  opus-5 $3.56│
│ Variables  {{question}}     │ CACHEABILITY  static prefix 7,102 tok — clears haiku's │
│                             │   4,096 minimum by 73%. Volatile content in prefix: 1  │
│ ── highlighted spans ──     │ FINDINGS  PL01 timestamp line 1 · PL02 · PL06 · PL07   │
│                             │ [ Apply safe fixes ]  → diff, net −214 in / −1,800 out │
│                             │ [ Rewrite with a model ]  $0.0021, repays after 3 calls│
└─────────────────────────────┴───────────────────────────────────────────────────────┘
  Brief mode: paste → 18,402 tok ▪ in / 1,905 tok ▪ out · cost $0.0094 · lossy
```

### Compare

```
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ opus-5       │ sonnet-5     │ haiku-4.5    │ + manual     │
│ 412 tok out  │ 388 tok out  │ 451 tok out  │ paste here   │
│ $0.0113 ▪    │ $0.0043 ▪    │ $0.0024 ▪    │ subscription │
│ ttft 0.9 s   │ ttft 0.6 s   │ ttft 0.4 s   │              │
│ ───────────  │ ───────────  │ ───────────  │ ──────────── │
│ response…    │ response…    │ response…    │              │
└──────────────┴──────────────┴──────────────┴──────────────┘
    [ Synthesize ]  agreement / disagreement / unique / likely errors / final
```

### Spend

```
│ SPEND BY DAY   ▁▂▅█▃▂  $12.84 total ▪        BUDGET  daily $5.00 — used $0.00 (0%)  │
│ BY MODEL       opus-5 $9.12 · sonnet-5 $2.31 · haiku-4.5 $1.41                      │
│ SCARCE SHARE   ▓▓▓▓▓▓▓▓░░ 71% of spend went to scarce models                        │
│ COST PER SUCCESSFUL TASK, BY WORKLOAD, OVER TIME    line per workload                │
│ Tokop sees only API calls it made itself. It cannot see Claude.ai or Claude Code     │
│ plan usage.                                                                          │
```

## 7. Design review

Written, then revised. What changed on review:

1. **First draft used a card grid** with uniform `rounded-lg` + `shadow-sm` — the single most
   recognisable generated-UI tell. Replaced with hairline rules and whitespace; `chalk`
   surfaces are now rationed to two per screen and mean something specific.
2. **First draft put tracked-out all-caps labels above each section heading.** The wireframes
   keep small caps as *table column headers only*, where they do structural work, and the
   section headings carry the weight themselves.
3. **First draft used a middle-dot meta string** in the workload header. Kept exactly one
   (the header context line, where it is genuinely a list of peers) and rebuilt the rest as
   labelled pairs.
4. **First draft accented with indigo.** Swapped to prussian/vermilion for the reasons above.
5. **First draft gave every tier its own hue** (blue/green/orange), which collided with the
   better/worse state colours. Tiers now ride a lightness ramp and scarcity became a hatch.
6. **Monospace was proposed for units and metric labels.** Pulled back to prompt/code/hash
   content only.

## 8. Quality floor

Keyboard reachable in DOM order with a visible 2px `prussian` focus ring on `paper`
(contrast 7.4:1). All text meets WCAG AA at its size — `graphite` on `paper` is 5.6:1,
and it is never used below 0.8125rem. `prefers-reduced-motion` removes the graph morph.
Charts never rely on hue alone: every series carries a shape or a direct label.

## 9. Decorative element removed before shipping

Per the spec's last design instruction, one decorative element was removed from each screen
at the end of the build. The removals are recorded in `docs/screenshots/CRITIQUE.md`.
