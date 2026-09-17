# Cleanup plan

> **Historical proposal.** This audit describes a cleanup pass against an earlier commit and is
> retained for context. Its measurements and status lines are not current project instructions.
> See [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md) for the supported workflow.

Against `CLEANUP.md`, on `claude/nifty-cray-ryedyy` at `acdc73f`. Nothing here adds a feature.
Every change below names the `CLEANUP.md` work item it comes from.

`CLEANUP.md` is the brief for this pass and is not a file in this repository; item numbers below
refer to its section 3.

**Status: awaiting approval. No application code, README or demo script has been edited.**

---

## 0. Baseline, re-measured (CLEANUP.md section 0.1 and 0.2)

Measured on this checkout, at `acdc73f`, with the toolchain installed by `make setup`.

| Thing | CLEANUP.md said | Measured now | |
| --- | --- | --- | --- |
| Tracked files | 7,931 | 7,931 | same |
| `fixtures/` | 7,740 files, 97.6%, 33 MB | 7,740 files, 97.59%, 33 MB | same |
| `.git` | 23 MB | 23 MB (21.01 MiB pack, 13,287 objects) | same |
| `web/` source and e2e | 37 files, 6,356 lines | 37 tracked files under `web/`; 23 of them under `web/src` + `web/e2e`, totalling 6,356 lines | same, split named |
| Largest screen | `Optimize.tsx`, 995 lines | 995 lines | same |
| Optimize at the proof stage | `optimize-proof.png` 1280 × 7280 | the live page is **1280 × 7542**, 8.4 viewport heights at 900 px; the committed screenshot is stale at 7280 | **moved: 7,542** |
| Frontend deps with zero importers | `recharts`, `clsx`, `tailwind-merge` | confirmed: 0 references in `web/src`, `web/e2e`, `web/*.ts`, `web/*.js` | same |
| Process docs at root | 1268 / 829 / 494 / 380 / 57 | `DECISIONS.md` 1,268, `PROGRESS.md` 829, `PLAN.md` 494, `SPEC.md` 380, `CLAUDE.md` 57 | same |
| README | 200 lines, block at 12–38 | 200 lines; `metrics:start` on line 13, `metrics:end` on line 40 | **moved: 13–40** |
| Screenshots committed | 6 | 6 | same |
| Screenshots shown in the README | 0 | 0 | same |
| Nav items | 4; Compare and Spend absent from `docs/DEMO.md` | 4 primary + Settings; neither word appears in `docs/DEMO.md` | same |

### Five things CLEANUP.md states that this checkout does not bear out

1. **`tokop report` is not byte-identical run to run.** Two consecutive runs on an unchanged
   tree differ in exactly one field, `cascade.runtime_seconds` — the wall-clock time of the
   threshold search on whatever machine ran it. `scripts/write_demo.py` already normalises that
   same figure out of its own `--check` for this reason. Standing rule 1 therefore becomes:
   **identical in every field except `cascade.runtime_seconds`**, checked by
   `scripts/report_diff.py` (added under item 0 below), which prints that field separately
   rather than hiding it. All 21,365 other leaf fields matched exactly.

2. **`docs/DEMO.md`'s main spine already sums to 90 seconds.** The six timed beats are
   10 + 20 + 15 + 25 + 10 + 10 = 90 s and their start offsets are internally consistent
   (0:00 → 1:30). The "1:50" in CLEANUP.md counts the seventh beat, headed `## 1:30 — …
   (20 s)`, which carries a timestamp and so reads as part of the run rather than as extra.
   Item 4 is therefore a **labelling and re-pointing** job, not an arithmetic one, and the
   plan below says so.

3. **`docs/DEMO.md` says "scroll" three times, not five** (lines 20, 76 and 101).

4. **Item 3's stated acceptance test already passes — at the recommendation block, not at the
   Verdict section.** Measured live with Playwright at 1280 px, replay mode, ledger built:
   `The recommendation` runs 332–633 px and carries the verdict display (the pill, 567–609),
   both cost-per-successful-task figures (493–514) and the accuracy delta with its 95% CI and n
   (565–587). The headline row restates all three larger, 633–760. So the three things item 3
   names resolve above 900 px **twice**, by 760 px, today. What does *not* is the **Verdict
   section**, at 2222–2623, pushed down by the 1,410 px `Candidate pipeline` block. Item 3 is
   planned against the strict reading, and section "Item 3" below gives the exact arithmetic of
   what the move buys and what it does not.

5. **The Optimize proof screen is 7,542 px, not 7,280.** The committed
   `docs/screenshots/optimize-proof.png` is stale: a green `make verify` regenerates it at
   1280 × 7542, and a live measurement agrees. `docs/screenshots/inspect.png` is stale the same
   way, committed at 1280 × 2027 and regenerating at 1280 × 2255. The e2e suite *writes* the
   screenshots but nothing *asserts* anything about them, so a committed shot can drift from
   what the app renders and the build stays green. Not in scope to fix here, but it is why every
   height in this plan is a live measurement rather than a read off a committed PNG.

### One thing the baseline run found that CLEANUP.md does not mention

**`make setup` followed by `make verify` fails on a fresh clone.** It failed here, on `acdc73f`,
with nothing edited:

```
  playwright e2e (replay)            FAILED
  1) Settings › deleting stored content really deletes it and keeps every metric
     Error: expect(received).toBeTruthy()   // web/e2e/screens.spec.ts:370
  1 failed, 50 passed
VERIFY FAILED: playwright e2e (replay)
```

The cause is not the test. `fixtures/test/ledger.db` is generated, not committed (`.gitignore`),
and nothing in `make setup`, the `Makefile` or the README builds it. Only
`.github/workflows/verify.yml` knows, in a step between `make setup` and `make verify`:

```yaml
- name: Build the fixture ledger
  run: engine/.venv/bin/tokop build-test-fixtures --ledger-only
```

Running that one command — 12 seconds, no network, no key, `origin=simulated`, nothing spent —
takes the suite to green. It also leaves the report untouched: `tokop report` before and after
building the ledger is identical in all 21,365 leaf fields but `cascade.runtime_seconds`.

This matters to this pass because CLEANUP.md's own definition of done is "`make demo` serves on a
clean checkout with no keys and no network", and because a visitor who runs the two commands the
README gives them hits a red build and a `Spend` screen with no ledger behind it.

**Proposed, and flagged for approval because it is a Makefile change CLEANUP.md did not ask
for:** add `tokop build-test-fixtures --ledger-only` as the last line of `make setup`, and name
it in the README's run block. It is idempotent, it spends nothing, it is the exact command CI
already runs, and it does not touch engine code. If you would rather the Makefile stayed as it
is, the fallback is documentation only — one line in the README — and `make setup` keeps
shipping a checkout on which `make verify` fails. Say which; the plan assumes the Makefile line
unless told otherwise. Either way this lands in commit 1, beside item 0, because every later
commit has to be verified on a checkout where `make verify` can pass.

### Baseline artifacts captured

- `/tmp/report-before.json` — `engine/.venv/bin/tokop report --out /tmp/report-before.json`
  (753,242 bytes). Note: the CLI writes the file and *then* raises `ValueError` formatting the
  confirmation line, because it calls `Path.relative_to(repo_root())` on a path outside the
  repo. The JSON is complete and valid; the failure is cosmetic and in `engine/tokop/cli.py`,
  which section 6 puts out of scope, so it is **not** fixed in this pass. Recorded here so the
  next person does not think the capture failed.
- `verify-baseline.log` — the first `make verify`, which failed as above.
- `verify-baseline2.log` — the same run after `tokop build-test-fixtures --ledger-only`, which
  is the green baseline every later commit is compared against.
- `docs/screenshots/` is rewritten by `make verify` (the e2e suite writes them), so a verify run
  always leaves the tree dirty. On the failed run the shots were also *wrong* — no ledger meant
  `inspect.png` came back 1280×2255 instead of 1280×2027 and `optimize-proof.png` 1280×7542
  instead of 1280×7280. They were restored with `git checkout -- docs/screenshots/` before the
  green run. Worth knowing before anyone reads a height off a screenshot taken on a fresh clone.

---

## Item 0 (new, enabling). A green baseline, and a way to check the report did not move

| File | Action | Reason |
| --- | --- | --- |
| `scripts/report_diff.py` | **add** | Standing rule 1 needs a checkable form. Compares two `tokop report --out` payloads field by field and names `cascade.runtime_seconds` separately instead of ignoring it silently. Not wired into `make verify`; it is a tool for this pass and for the report in section 7. |
| `Makefile` | **edit** — one line in `setup` | The finding above: `make setup` does not build `fixtures/test/ledger.db`, so `make verify` fails on a fresh clone. Adds the command CI already runs. **Flagged for approval.** |
| `README.md` | **edit** — one line in `## Run it` | Same finding: say that setup builds the fixture ledger, and that it spends nothing. |

Not a feature, not engine code, no new dependency. One commit, first, so every later commit can
be verified and checked against the baseline.

---

## Item 1. Put a picture at the top of the README

| File | Action | Reason |
| --- | --- | --- |
| `web/e2e/optimize.spec.ts` | **edit** — extend the existing `screenshots for the record` test | Item 1: the shot must regenerate with `make verify` like the others. One extra `page.screenshot()` without `fullPage`, taken at the proof stage, writing `../docs/screenshots/readme-verdict.png`. Viewport-sized: 1280 × 900. |
| `docs/screenshots/readme-verdict.png` | **add** (generated) | Item 1: the one image on the README's first screen. |
| `README.md` | **edit** | Item 1: embed that one image under the product line. |

Depends on item 3 landing first. At 1280 × 900 today the viewport holds the header (0–332),
`The recommendation` (332–633), the headline row (633–760) and the proof-cost row (760–812) —
everything item 1 asks for except the `Verdict` section, which is at 2,222 px. After item 3 the
`Verdict` section starts at 812 px, so the same viewport shot carries its heading and its
verdict Pill too. That is what makes a viewport-sized shot "the verdict block and the headline
row" rather than a crop of the top of the page. **So item 3 is executed before item 1**, and the
commit order below reflects that.

No gallery. The other five screenshots stay where they are and stay out of the README.

---

## Item 2. Rewrite the README's first screen, keep every number

| File | Action | Reason |
| --- | --- | --- |
| `README.md` | **rewrite the first screen** | Item 2: order becomes product line → image → what it does (~4 lines) → the two run commands → the generated metrics block → the rest, unchanged and in its current order. |

Specifics:

- **The metrics block moves by moving its markers, not by copy-paste.**
  `report.py::_replace_block` rewrites whatever sits between `<!-- metrics:start -->` and
  `<!-- metrics:end -->` **wherever those markers are**, so relocating the marker pair is the
  whole change and no engine code moves. Verified by running `tokop report --write-readme`
  afterwards and confirming the block lands in its new position with identical content, and by
  `tokop report --check` inside `make verify`.
- **One hand-written sentence immediately above `<!-- metrics:start -->`**, saying that the
  verdict below is inconclusive on this dataset and that reporting it that way is the product
  working. It sits *outside* the markers, so regeneration cannot remove it. It states no
  figure, so it cannot go stale and it does not violate non-negotiable 1.
- **`Repository` map corrected** to add `scripts/`, `data/incident-triage/`,
  `data/incident-agent/`, and `docs/METHOD.md` and `docs/DEPLOY.md` in the `docs/` line.
- **One line under "What it does"** naming the second workload, `incident-triage`, that shares
  no code with the demo, and naming `engine/tests/test_no_demo_imports.py` as the test that
  enforces it. No counts: a hand-typed count is a metric literal (non-negotiable 1).
- **One line about the committed cassettes** — see item 10.

Nothing in `## Why now`, `## Where Tokop sits`, `## Limitations`, `## Roadmap` or `## Origin`
changes. No figure is hand-edited anywhere.

---

## Item 3. Cut the height of the Optimize screen

**Decision: a tab row at the proof stage, three tabs, argued below.** No second route (section 6
excludes any new screen). No per-section `<details>`: six collapsed accordion rows are ~360 px of
chrome that still has to be opened six times, and the demo beat at 1:30 becomes "find the right
disclosure".

A tab row is not new vocabulary here: `Inspect` already uses `<nav role="tablist">` with
`<button role="tab" aria-selected>` (`web/src/screens/Inspect.tsx:119`), and `Compare` has a
tab strip of its own. The new one copies that markup exactly — no new component, no new
dependency.

**Proof-stage order after the change**

1. header · `The recommendation` · headline row · proof-cost row — unchanged, ends at 812 px
2. **`Verdict`** — moves up to 812 from 2,222; the sentence, the interval plot against the
   margin, McNemar, the proof cost and both accuracies
3. tab row, defaulting to `Result`
   - **`Result`** — cost-quality frontier → savings waterfall → breakdown by question type
   - **`The candidate`** — the candidate graph, the findings ranked by dollars, the cascade
     summary, and the stage controls (`Show the current pipeline`, `New experiment`, `Protect
     scarce models`)
   - **`What backs it`** — tied for cheapest → where these tasks came from → what this was
     measured on → where the thresholds came from → what checkability costs → without the
     answer key → disagreements, each opening its full trace
4. footer — unchanged

**Measured, at 1280 px, in replay mode with the ledger built.** Document offsets, not estimates:

| Block | Today | Height |
| --- | ---: | ---: |
| header | 0–332 | 332 |
| `The recommendation` | 332–633 | 301 |
| headline row | 633–760 | 127 |
| proof cost / scarce share | 760–812 | 52 |
| `Candidate pipeline` | 812–2222 | **1,410** |
| `Verdict` | 2222–2623 | 401 |
| `Tied for cheapest` | 2623–3093 | 471 |
| `Where these tasks came from` | 3093–3360 | 266 |
| `What this was measured on` | 3360–3622 | 262 |
| `Where the thresholds came from` | 3622–3988 | 366 |
| `What checkability costs…` | 3988–4396 | 408 |
| `Without the answer key` | 4396–5240 | 844 |
| `Cost-quality frontier` | 5240–5990 | 750 |
| `Savings waterfall` | 5990–6434 | 444 |
| `Breakdown by question type` | 6434–6731 | 297 |
| `Disagreements` | 6731–7402 | 671 |
| footer | 7402–7542 | 140 |

Baseline stage is 2,381 px and candidate stage 2,362 px; neither changes.

**What the three tabs come to.** `Result` = 812 + 401 (verdict) + ~50 (tab row) + 750 + 444 +
297 + 140 = **≈2,894 px**, 3.2 viewports, against 7,542 and 8.4 today — a 62% cut.
`The candidate` ≈2,813 px. `What backs it` ≈4,691 px, and it is the one a reader opts into.

Why `The candidate` tab exists rather than leaving that section inline: item 3 says what sits
below the fold is "the frontier chart and the savings waterfall", and the candidate graph plus
its thirteen findings is 1,410 px that would sit between them and the verdict. It is also the
one block that has already done its work by the proof stage — the demo points at Findings at
0:10 and at the graph morph at 0:30, both at earlier stages. Left inline, the `Result` tab is
4,304 px instead of 2,894.

**Where the 900 px line actually falls, stated exactly rather than claimed.** After the move:

- The verdict display, both cost figures and the accuracy delta with its interval and n resolve
  by **760 px** — they already do today, in `The recommendation` and again in the headline row,
  and the move does not touch either.
- The `Verdict` section starts at **812 px**, so its heading and its verdict Pill sit above the
  fold. Its *sentence* lands at **≈953 px** and its interval plot at ≈1,107 — about 50 px and
  200 px below the fold respectively.

So item 3's test passes on the three things it names, and the Verdict section's fuller
restatement of them crosses the line by about 50 px. That gap is closable — moving the 52 px
proof-cost row below the `Verdict` section would put the sentence at ≈901 px — but chasing a
threshold by 2 px is the habit this product exists to argue against, so the plan states the
measurement and leaves the row where it is. Say the word if you want the 50 px.

Every figure above is re-measured after the change and carried into `CLEANUP_REPORT.md`.

**At the `baseline` and `candidate` stages nothing changes.** No tab row, the pipeline section
is the whole screen, and `Build candidate` / `Run proof` are where they are today.

| File | Action | Reason |
| --- | --- | --- |
| `web/src/screens/Optimize.tsx` | **edit** | Item 3: move `Verdict` above the pipeline section at the proof stage; add the three-tab row and the `useState` for it. Executed as part of item 8's split rather than twice over. |
| `web/e2e/optimize.spec.ts` | **edit** | Item 3: eight proof-stage tests assert on panels that now sit behind a tab and each gains one click — `ties-table`, `provenance-tail`, `calibration-thresholds`, `contract-*`, `judged-*` (two tests), `fingerprint-mix`, `open-trace-*`. A ninth, `screenshots for the record`, has to say which tab its full-page shot is taken on. **No assertion is weakened or removed** — the same elements, the same API comparisons. Two are added: that the verdict sentence resolves above 900 px at a 1280 px viewport, and that all three tabs are reachable. |

**Nothing is deleted.** `Without the answer key`, `Where the thresholds came from`, `What
checkability costs`, `Where these tasks came from`, `What this was measured on` and the ties
section keep their content and their wording, character for character. The `Inconclusive`
verdict and the `insufficient` grade stay in `The recommendation`, above every tab, where they
are today.

**`docs/screenshots/optimize-proof.png` shrinks, and that is the deliverable.** It is a shot of
the screen as a visitor meets it, so it stays one full-page shot on the default `Result` tab
rather than growing into three. Its height before and after — 7,542 px to ≈2,894 px — is one of
the four figures `CLEANUP_REPORT.md` has to carry. The commit also lands the regenerated
`inspect.png`, which is stale in the repo for the unrelated reason in finding 5 above; that is
noted in the report rather than passed off as this pass's doing.

**The test for this item**, from CLEANUP.md: a reviewer who never scrolls can state the verdict,
the saving and the evidence grade correctly. They already could from `The recommendation`; after
this change the Verdict section is in the same viewport.

---

## Item 4. Make the 90-second demo actually take 90 seconds

`docs/DEMO.md` is generated end to end by `scripts/write_demo.py` — the whole document is one
f-string. So this item edits the generator and regenerates, and touches no figure by hand.

| File | Action | Reason |
| --- | --- | --- |
| `scripts/write_demo.py` | **rewrite the template** | Item 4: the spine keeps its six beats and its 90-second sum, and gains a stated total. The 1:30 beat moves under a heading that says it is extra, losing its timestamp. The scroll directions are rewritten against the new screen. |
| `docs/DEMO.md` | **regenerate** | Generated artifact; `make verify` checks it with `write_demo.py --check`. |

Specifics:

- **The spine is headed with its total**, so the claim in the title is checkable on the page:
  six beats, 10 + 20 + 15 + 25 + 10 + 10 = 90 s.
- **Everything past 1:30 moves under one `## Past the ninety seconds` heading**: the
  unlabelled-workload section (keeping every word and every generated figure), the
  Inspect/Settings extras, and the four Q&A answers. The 20 s / 30 s timings become
  "about 20 seconds" style notes under that heading rather than positions on the clock.
- **Compare and Spend get a place there** — see item 7.
- **The three "scroll to" directions are rewritten** against item 3's screen: the Findings beat
  at 0:10 happens at the baseline stage, where the findings are already on screen beside the
  graph, so "scroll to" becomes "look right"; the frontier at 1:10 is the first thing on the
  `Result` tab under the verdict; `Without the answer key` becomes a click on `What backs it`,
  as does the `Open trace` beat at 1:20.
- **The closing beat is kept**: the proof cost and the repayment volume, both generated.
- **The unlabelled-workload section is kept in full**, including the wide interval and the
  100% cost-optimal sampling rate. It is the part that makes this usable on an ungraded
  workload and it is not softened.

---

## Item 5. Remove the three dead dependencies

| File | Action | Reason |
| --- | --- | --- |
| `web/package.json` | **edit** | Item 5: drop `recharts`, `clsx`, `tailwind-merge`. Zero importers across `web/src`, `web/e2e` and the config files. |
| `web/pnpm-lock.yaml` | **regenerate** | `pnpm install` after the edit. `clsx` is also a dependency *of* `recharts`, so it leaves as a transitive too. |

Checked: `pnpm build`, `pnpm lint`, `pnpm typecheck`, `pnpm e2e`, then `make verify`.
Footprint removed from `node_modules`: `recharts` 5.4 MB, `tailwind-merge` 788 KB, `clsx` 56 KB,
plus recharts' own transitive tree (`d3-*`, `victory-vendor`) — measured exactly in the report.

**The removal test (CLEANUP.md section 4), the only deletion in this pass:**

1. *Does no code, test, script, document or demo beat reference it?* No. `grep -rn` over
   `web/src`, `web/e2e`, `web/*.ts`, `web/*.js`, `web/*.json` returns zero hits outside
   `package.json` and the lockfile for all three. No document or demo beat names them.
2. *Does removing it leave `tokop report` byte-identical?* Yes — they are frontend packages and
   the report is computed in Python over `fixtures/`. Checked anyway, per commit.
3. *Does `make verify` still pass with them gone?* To be shown, not assumed: the commit is not
   pushed until `make verify` is green.
4. *Would none of the three readers be worse off?* None. Nothing renders from them today.

**`@xyflow/react` stays.** Exactly one importer, `web/src/components/PipelineGraph.tsx`, and the
graph morph is the 0:30 beat of the demo. A hand-rolled replacement is new code, which section 6
excludes. Recorded here so nobody relitigates it.

---

## Item 6. Clear the root directory of build scaffolding

| File | Action | Reason |
| --- | --- | --- |
| `PLAN.md` → `docs/build/PLAN.md` | **move** (`git mv`) | Item 6: internal build process. |
| `PROGRESS.md` → `docs/build/PROGRESS.md` | **move** | Item 6: internal build process. |
| `CLAUDE.md` → `docs/build/CLAUDE.md` | **move** | Item 6: internal build process. |

`README.md`, `SPEC.md`, `DECISIONS.md`, `Makefile`, `Dockerfile` and the dotfiles stay.
`DECISIONS.md` stays at root deliberately, per item 6.

References to update, from a grep of each filename across the tree (excluding `.git`,
`node_modules`, `.venv`, `__pycache__`, `fixtures`):

- `README.md` — one line naming `PROGRESS.md` as the build log.
- `SPEC.md` — lines 28, 37, 38, 39, 331 name all three.
- `CLAUDE.md` itself — lines 3 and 27 name `PROGRESS.md`.
- `DECISIONS.md` — lines 372, 373, 375, 565, 861, 1084, 1092, 1209, 1250.
- `PLAN.md` — lines 9, 99, 100, 157, 166, 170, 203, 465, 472.
- `PROGRESS.md` — lines 474, 597.
- `engine/tests/test_session.py`, `test_graph_execution.py`, `test_graph.py`,
  `test_fingerprint.py` and `engine/tokop/optimize/certificate.py:423` cite `PLAN.md` **in
  docstrings and comments only**. These are prose citations, not imports; the change is a path
  string inside a comment and no code moves. Flagged because section 6 puts engine internals out
  of scope: if that is too close to the line, say so and they stay as-is — the repo already
  cites `UPGRADE_V3.md` and `UPGRADE_V4.md`, which were never committed, so a stale prose
  citation has precedent here.
- Scripts and workflows: grepped, **no hits** in `scripts/`, `.github/`, `Dockerfile` or
  `.dockerignore`.

A note is added at the top of each moved file saying where it moved from, and `README.md` points
at `docs/build/` in one line, so nothing becomes unfindable.

`CLAUDE.md` is read by this toolchain from the repository root. Moving it is what item 6 asks
for; the consequence — a fresh session no longer picks it up automatically — is stated here so
the decision is made knowingly, and the pointer line in `README.md` is what replaces it. No root
stub is left behind, because section 7 says the root holds no build-process document but
`DECISIONS.md` and `SPEC.md`.

**One loose end for the reviewer.** `CLEANUP_PLAN.md` and `CLEANUP_REPORT.md` are themselves
process documents, and CLEANUP.md names both without a path. They are written at the root so
they are where a reviewer expects them, which leaves the root holding two files that section 7's
wording would exclude. Say the word and both move to `docs/build/` in the final commit — one
`git mv` and one README line. Left at root by default because a plan that has to be found is a
plan that does not get read.

---

## Item 7. Decide what Compare and Spend are for

**Decision: they stay in the primary nav, and they get a sentence in the README and a place in
the demo's extra material.** Not demoted.

The argument is not taste. `SPEC.md` is the source of truth for this repository (`CLAUDE.md`
line 3), and it specifies the navigation twice:

- `SPEC.md:63` — "Four screens (Optimize, Inspect, Compare, Spend) plus Settings."
- `SPEC.md:103` — "Navigation: Optimize, Inspect, Compare, Spend, then Settings."

Demoting two of them to a secondary entry would contradict a stated requirement in the document
this build is measured against, to fix a problem that the other option fixes completely. Option A
also costs less and is reversible; option B is a structural change to a spec'd surface. Both
screens already do real work against real fixtures and are covered by nine e2e tests.

The actual cost CLEANUP.md names — "a nav item the author's own demo never opens" — is removed by
using them, which is exactly what option A does.

| File | Action | Reason |
| --- | --- | --- |
| `README.md` | **edit** | Item 7: `Compare` and `Spend` each get one sentence that names the question it answers. The existing `## The four screens` prose is tightened, not replaced. |
| `scripts/write_demo.py` | **edit** | Item 7: both get a bullet under `## Past the ninety seconds`, beside the existing Inspect and Settings bullets, saying what question each answers. |
| `docs/DEMO.md` | **regenerate** | Generated. |

Neither screen is deleted, and neither loses its nav slot.

---

## Item 8. Split `Optimize.tsx`

Pure refactor. No behaviour change, no prop redesign, no styling edits, beyond the ordering and
tab row that item 3 specifies. **Item 3 and item 8 are executed as one commit** for the file,
because splitting 995 lines and then reordering the split pieces would put the screen through two
diffs that each look like a rewrite; the plan keeps them separate as decisions and joins them as
one edit, with the e2e suite and the screenshots as the proof on both counts.

New directory `web/src/screens/optimize/`:

| File | Moved from `Optimize.tsx` | Approx. lines |
| --- | --- | --- |
| `Headline.tsx` | the headline metric row and the proof-cost / scarce-share row | ~70 |
| `PipelinePanel.tsx` | `baselineSteps`, `candidateSteps`, `FindingAmount`, the graph, the findings list, the stage controls and the cascade summary | ~250 |
| `VerdictPanel.tsx` | the `Verdict` section | ~50 |
| `FrontierPanel.tsx` | the cost-quality frontier section | ~15 |
| `WaterfallPanel.tsx` | the savings waterfall table | ~60 |
| `ByTypePanel.tsx` | the breakdown by question type | ~45 |
| `DisagreementsPanel.tsx` | the disagreements table | ~60 |
| `TiesPanel.tsx` | `TiesPanel`, verbatim | ~56 |
| `ProvenancePanel.tsx` | `ProvenancePanel`, verbatim | ~58 |
| `FingerprintPanel.tsx` | `FingerprintPanel`, verbatim | ~45 |
| `CalibrationPanel.tsx` | `CalibrationPanel`, verbatim | ~51 |
| `ContractPanel.tsx` | `ContractPanel`, verbatim | ~76 |
| `JudgedPanel.tsx` | `JudgedPanel`, verbatim | ~85 |

`Optimize.tsx` keeps the stage machine, the tab state, the header, the trace drawer and the
experiment modal, the footer, and the page composition. Target ≈180 lines. Every doc comment
moves with the code it documents — the comments on `JudgedPanel`, `ContractPanel`,
`ProvenancePanel`, `FingerprintPanel` and `TiesPanel` are the record of why those panels exist
and they travel intact.

`FrontierPanel.tsx` at ~15 lines is below the bar item 8 sets ("if a section is fifty lines and
used once, leaving it is fine") and is extracted anyway only so the `Result` tab reads as three
panels rather than two panels and an inline block. If review would rather it stayed inline, that
is a one-line change.

Proof: same e2e tests (plus item 3's tab clicks), same screenshots, `pnpm build`, `pnpm lint`,
`pnpm typecheck` green.

---

## Item 9. Move `docs/screenshots/CRITIQUE.md`

| File | Action | Reason |
| --- | --- | --- |
| `docs/screenshots/CRITIQUE.md` → `docs/build/CRITIQUE.md` | **move** (`git mv`) | Item 9: a build-process write-up living in an image folder; it belongs with the other process documents from item 6. |
| `docs/DESIGN.md:211` | **edit** | Item 9: the only reference in the tree. Grepped. |

---

## Item 10. Decide about the fixture bulk

**Decision: leave the cassettes exactly as they are, and add one line to the README explaining
why they are committed.** Option 1.

Three reasons, any one of which is decisive:

1. **A loader shim is new engine code**, and standing rule 3 says "No new engine code". A shim
   that reads from an archive is not a wrapper — it is a second read path through
   `CassetteStore`, the class every number in the repository is computed through.
2. **The one-file-per-call layout is a documented, load-bearing decision, not an accident.**
   `engine/tokop/adapters/cassette.py:89` states it: "One file per call rather than one big
   archive, so that an interrupted recording leaves every completed call intact and so that git
   shows what a re-recording actually changed." Consolidating would take resumability away from
   `make record` — the one command in this repo that spends money — to save disk on a clone.
3. **The saving is smaller than the file count suggests.** The cassettes hold 19.1 MB of JSON
   across 7,067 files; the 33 MB on disk is mostly 4 KB block slack, and git already
   delta-compresses them into a 21.01 MiB pack. A fresh clone is 62 MB today (23 MB `.git` +
   39 MB working tree). An archive would cut the working tree but would make every future
   re-recording a whole-blob rewrite in the pack rather than a per-file delta. For scale: the
   generated, gitignored `fixtures/test/ledger.db` that `make setup` should build is 129 MB on
   its own — four times the whole committed fixture set. Archiving the cassettes would not be
   the thing a visitor notices.

CLEANUP.md's own instruction applies: if any of byte-identical output, green `make verify`, or a
meaningfully smaller clone is in doubt, take the first option. Two of the three are in doubt.

| File | Action | Reason |
| --- | --- | --- |
| `README.md` | **edit** | Item 10: one sentence under `## Run it`, beside the existing "No API keys needed" line, saying the cassettes are committed so anyone can reproduce every number with no API key and no network. No counts: a hand-typed count is a metric literal (non-negotiable 1). |

`fixtures/incident-triage/` is not touched. Nothing under `fixtures/` is deleted, moved or
rewritten.

---

## Execution order and commits

One commit per work item, in CLEANUP.md's ranked order, with one documented exception. Every
commit is green on `make verify` before the next one starts.

| # | Commit | Item |
| --- | --- | --- |
| 1 | `cleanup: a script that says whether the report moved` | 0 |
| 2 | `cleanup: the Optimize screen fits on a screen` | 3 + 8 |
| 3 | `cleanup: a picture at the top of the README` | 1 |
| 4 | `cleanup: the README's first screen` | 2 |
| 5 | `cleanup: ninety seconds that take ninety seconds` | 4 |
| 6 | `cleanup: three dependencies nothing imports` | 5 |
| 7 | `cleanup: the root holds what a visitor reads` | 6 |
| 8 | `cleanup: what Compare and Spend are for` | 7 |
| 9 | `cleanup: the critique moves in with the build notes` | 9 |
| 10 | `cleanup: why the cassettes are committed` | 10 |
| 11 | `cleanup: the report` | section 7 |

**The exception.** Items 3 and 8 run first, ahead of items 1 and 2, because item 1's screenshot
is a shot of the layout item 3 creates and item 2's first screen embeds that screenshot. Running
them in the written order would mean committing a README that points at a picture of a screen
about to change. Items 3 and 8 share one commit because they edit one file: splitting 995 lines
and then reordering the split pieces produces two diffs that each read as a rewrite, and the
same e2e suite and the same screenshots prove both.

Item 7 lands after items 2 and 4 so that its sentences go into settled text rather than being
written twice.

## After each commit

- `engine/.venv/bin/tokop report --out /tmp/report-after.json` then
  `python scripts/report_diff.py /tmp/report-before.json /tmp/report-after.json`
- `make verify`

## At the end (CLEANUP.md section 7)

- `diff` against a fresh report: identical but for `cascade.runtime_seconds`, stated plainly.
- `make verify` green; `pnpm build`, `pnpm lint`, `pnpm typecheck`, `pnpm e2e` green.
- `make demo` on a clean checkout, no keys, no network.
- `CLEANUP_REPORT.md`: what was removed, what was moved, what was kept against expectation and
  why, and before/after for file count, clone size, largest component, and the pixel height of
  the Optimize screen at the proof stage.

## What this plan does not touch

Engine internals. Statistical method. The proof layer. Live recording. Any new screen, chart,
animation, theme, component library or design system. Anything under `engine/tokop/core/`.
Anything under `fixtures/`. `docs/METHOD.md`. The budget values. The `Inconclusive` verdict, the
`insufficient` grade, the wide gold-free interval, and the red unverified price markers in
Settings — none is softened, reworded or moved out of view.

And, from CLEANUP.md section 5, the things that look like waste and are not:

- `web/src/lib/api-types.ts` and `web/src/lib/types.ts` both stay. One is generated from the
  OpenAPI schema, the other wraps it. Item 8 imports from `types.ts` exactly as `Optimize.tsx`
  does today and merges neither.
- `DECISIONS.md` stays at root, at 1,268 lines.
- `data/incident-agent/` stays, with no fixtures, still referenced by the CLI, the API routes and
  three test files.
- `fixtures/incident-triage/` stays untouched — 668 tracked files: 662 cassettes, 4 blobs,
  a manifest and an extras file. (CLEANUP.md says 666 cassettes; the tracked count is 662
  cassette files plus 4 deduplicated blobs, which is the same set counted two ways.)
