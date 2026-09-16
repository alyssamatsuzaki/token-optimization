"""Findings about the *shape* of a pipeline: G01 to G05 (UPGRADE_V4.md M16).

The lint reads a prompt and the workload findings read what one call did. These read what a
**graph** did: which step sent what, which step's output nothing consumed, which call returned
what the call before it returned. They exist because the workloads where the money actually goes
are not long prompts, they are agents — and the useful proposal for an agent is usually *delete a
step*, which no rule that sees one call at a time can make.

Three things about how they are priced, because each is a way to overclaim:

* **A step's input rate is measured, not assumed.** A block of text that reaches two steps is
  billed at whatever bucket each step's request actually landed in — uncached input in one,
  a cache read in another. Rather than guess the split per block, each step carries the blended
  rate its own call rows produced: its input dollars over its input tokens. It is a measured
  number, and the formula says so.
* **A tool call has no price here.** Tokop prices provider tokens. It does not know what a
  retrieval or a function call costs, so G02 reports a dollar figure of zero and says that is
  what it means, rather than inventing a price to make the finding rank.
* **A ceiling is labelled a ceiling.** G05 prices every token a frontier step carries from an
  earlier step, which is what compressing that text to nothing would save. Nothing compresses to
  nothing, so it is ``projected`` and the detail says what the real number is a fraction of.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from tokop.core.pricing import ModelPrice
from tokop.optimize.graph import Graph
from tokop.optimize.lint import MILLION, Finding

#: Below this many characters a shared block is not worth a finding: the text is a label, not a
#: document, and the saving would be rounding.
SHARED_BLOCK_MIN_CHARS = 200

#: Share of tasks a pattern has to hold on before it is reported as a property of the pipeline
#: rather than as something that happened a few times.
PREVALENCE = 0.9


def _normalized(text: str) -> str:
    """Whitespace-insensitive comparison, because two identical answers may be wrapped twice."""
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class StepStats:
    """What one step of one run did, aggregated over its tasks."""

    id: str
    kind: str
    model: str
    called: bool
    calls: int
    price: ModelPrice | None
    input_tokens: int
    output_tokens: int
    input_cost_usd: Decimal
    output_cost_usd: Decimal
    #: Per task, in task order. A local step has arguments and no request text; a generation
    #: step has request text and no arguments.
    arguments: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    request_texts: tuple[str, ...] = ()
    #: The tool a local step names, when it names one.
    tool: str = ""
    inputs: tuple[str, ...] = ()

    @property
    def cost_usd(self) -> Decimal:
        return self.input_cost_usd + self.output_cost_usd

    @property
    def blended_input_rate(self) -> Decimal:
        """Dollars per million input tokens this step actually paid.

        Measured rather than looked up: a step whose prefix was read from cache paid a tenth of
        base input for most of its tokens, and pricing its text at the base rate would inflate
        every finding that touches it.
        """
        if not self.input_tokens:
            return Decimal(0)
        return self.input_cost_usd * MILLION / Decimal(self.input_tokens)

    def carries(self, texts: Sequence[str]) -> int:
        """How many tasks had ``texts[i]`` inside this step's own request."""
        if not self.request_texts:
            return 0
        return sum(
            1
            for own, other in zip(self.request_texts, texts, strict=False)
            if other and other in own
        )


@dataclass(frozen=True)
class GraphStats:
    """One run of one graph pipeline, as the findings need it."""

    pipeline_id: str
    graph: Graph
    steps: tuple[StepStats, ...]
    task_count: int
    successes: int
    #: Tokens per character of the text these steps sent, measured from this run. Used to put a
    #: shared block on the same scale as the bill.
    tokens_per_char: float
    calls_per_1k: int = 1000
    notes: tuple[str, ...] = field(default_factory=tuple)

    def step(self, step_id: str) -> StepStats:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"{self.pipeline_id} ran no step {step_id!r}")

    @property
    def total_cost(self) -> Decimal:
        return sum((step.cost_usd for step in self.steps), Decimal(0))

    def scale_to_1k(self, amount: Decimal) -> Decimal:
        if not self.task_count:
            return Decimal(0)
        return amount * Decimal(self.calls_per_1k) / Decimal(self.task_count)

    def tokens_of(self, text: str) -> int:
        return round(len(text) * self.tokens_per_char)

    def reaches_output(self, step_id: str) -> bool:
        """Whether this step's output can reach the step the pipeline answers with.

        Walked over the declared edges rather than guessed from run order: a step that runs
        before the answer and feeds nothing into it cannot have changed it, however late it ran.
        """
        if step_id == self.graph.output_step:
            return True
        seen = {step_id}
        frontier = [step_id]
        while frontier:
            current = frontier.pop()
            for dependent in self.graph.dependents(current):
                if dependent.id == self.graph.output_step:
                    return True
                if dependent.id not in seen:
                    seen.add(dependent.id)
                    frontier.append(dependent.id)
        return False


# --------------------------------------------------------------------------- the rules


def _g01_shared_context(stats: GraphStats) -> Finding | None:
    """G01: the same block of text reaches more than one step, and every step pays for it."""
    best: tuple[Decimal, str, int, list[str]] | None = None
    for source in stats.steps:
        if not source.outputs:
            continue
        sample = _normalized(source.outputs[0])
        if len(sample) < SHARED_BLOCK_MIN_CHARS:
            continue
        carriers = [
            step
            for step in stats.steps
            if step.id != source.id
            and step.request_texts
            and step.carries(source.outputs) >= PREVALENCE * stats.task_count
        ]
        if len(carriers) < 2:
            continue
        # The cheapest carrier has to hold the text; the rest are the copies. Ordering by the
        # rate each one actually paid means the saving is what removing the *expensive* copies
        # would recover, not an average over a choice nobody would make.
        by_rate = sorted(carriers, key=lambda s: s.blended_input_rate)
        tokens = stats.tokens_of(source.outputs[0])
        redundant = sum(
            (Decimal(tokens) * step.blended_input_rate / MILLION for step in by_rate[1:]),
            Decimal(0),
        )
        amount = redundant * Decimal(stats.calls_per_1k)
        if best is None or amount > best[0]:
            best = (amount, source.id, tokens, [step.id for step in by_rate])
    if best is None:
        return None
    amount, source_id, tokens, carrier_ids = best
    rates = ", ".join(
        f"{sid} at ${stats.step(sid).blended_input_rate:.2f}/Mtok" for sid in carrier_ids
    )
    return Finding(
        id="G01",
        group="Graph",
        title=f"The output of {source_id!r} is sent to {len(carrier_ids)} steps",
        detail=(
            "Every step that carries it pays input price for it again. One step can hold the "
            "context and pass forward what the others need — a reference, an extracted field, a "
            "summary — instead of the whole block. The saving below is what removing every copy "
            "but the cheapest recovers, at the rates these steps actually paid."
        ),
        evidence=(
            f"~{tokens:,} tokens from {source_id!r} present in {carrier_ids} on "
            f"{stats.task_count} of {stats.task_count} tasks; {rates}"
        ),
        projected_usd_per_1k=amount,
        formula=(
            f"{tokens:,} tok x ("
            + " + ".join(f"${stats.step(sid).blended_input_rate:.2f}" for sid in carrier_ids[1:])
            + f")/Mtok x {stats.calls_per_1k:,} tasks / 1e6 = ${amount:.2f}"
        ),
        confidence="measured",
    )


def _g02_duplicate_tool_call(stats: GraphStats) -> Finding | None:
    """G02: two local steps calling the same tool with byte-identical arguments."""
    for index, first in enumerate(stats.steps):
        if first.called or not first.arguments:
            continue
        for second in stats.steps[index + 1 :]:
            if second.called or not second.arguments:
                continue
            if (first.tool or first.kind) != (second.tool or second.kind):
                continue
            same = sum(
                1
                for a, b in zip(first.arguments, second.arguments, strict=False)
                if _normalized(a) == _normalized(b)
            )
            if same < PREVALENCE * stats.task_count:
                continue
            name = first.tool or first.kind
            return Finding(
                id="G02",
                group="Graph",
                title=f"{name!r} is called twice with identical arguments",
                detail=(
                    f"Steps {first.id!r} and {second.id!r} call {name!r} with the same arguments "
                    "on every task, so the second call cannot return anything the first did not. "
                    "One call serving both removes a round trip per task.\n\n"
                    "**The dollar figure is zero and that is not a rounding.** Tokop prices "
                    "provider tokens; it has no price for this tool, so it cannot say what the "
                    "duplicate costs. What it can say is that the call is made twice."
                ),
                evidence=(
                    f"{same} of {stats.task_count} tasks called {name!r} twice with identical "
                    f"arguments, e.g. {first.arguments[0][:80]!r}"
                ),
                projected_usd_per_1k=Decimal(0),
                formula="no price is registered for this tool, so no saving is claimed",
                confidence="measured",
            )
    return None


def _g03_idle_verifier(stats: GraphStats) -> Finding | None:
    """G03: a verifier whose verdict has never changed an outcome."""
    for step in stats.steps:
        if step.kind != "verify" or not step.calls:
            continue
        amount = stats.scale_to_1k(step.cost_usd)
        if not stats.reaches_output(step.id):
            why = (
                f"Nothing downstream of {step.id!r} reaches the step this pipeline answers with "
                f"({stats.graph.output_step!r}), so its verdict cannot have changed an outcome. "
                "That is a property of the graph, not a run of luck: no number of further tasks "
                "would make it change one."
            )
            evidence = (
                f"{step.calls} calls to {step.model} costing ${step.cost_usd:.4f} over "
                f"{stats.task_count} tasks; the graph has no path from {step.id!r} to "
                f"{stats.graph.output_step!r}"
            )
        else:
            downstream = [d.id for d in stats.graph.dependents(step.id)]
            unchanged = _unchanged_share(stats, step.id, downstream)
            if unchanged is None or unchanged < PREVALENCE:
                continue
            why = (
                f"{step.id!r} is read by {downstream}, and on {unchanged:.0%} of tasks what those "
                "steps produced was identical to what they had before the verdict arrived. The "
                "verifier is being paid for, and the traces do not show it changing anything."
            )
            evidence = (
                f"{step.calls} calls to {step.model} costing ${step.cost_usd:.4f}; the outcome "
                f"was unchanged on {unchanged:.0%} of {stats.task_count} tasks"
            )
        return Finding(
            id="G03",
            group="Graph",
            title=f"The verifier {step.id!r} has never changed an outcome",
            detail=(
                why + "\n\nDeleting it removes its whole cost. A verifier worth keeping is one "
                "whose verdict something acts on; wire it to a retry, or take it out."
            ),
            evidence=evidence,
            projected_usd_per_1k=amount,
            formula=(
                f"${step.cost_usd:.4f} over {stats.task_count} tasks x {stats.calls_per_1k:,} "
                f"= ${amount:.2f}"
            ),
            confidence="measured",
        )
    return None


def _unchanged_share(stats: GraphStats, step_id: str, downstream: Sequence[str]) -> float | None:
    """Share of tasks where every downstream step returned what its own source already said.

    ``None`` when nothing downstream is a generation step, in which case there is no output to
    compare and the question is answered structurally instead.
    """
    shares: list[float] = []
    for name in downstream:
        try:
            after = stats.step(name)
        except KeyError:  # pragma: no cover - dependents come from the same graph
            continue
        if not after.called or not after.outputs:
            continue
        priors = [
            stats.step(other)
            for other in after.inputs
            if other != step_id and _is_generation(stats, other)
        ]
        if not priors:
            continue
        best = max(
            sum(
                1
                for a, b in zip(prior.outputs, after.outputs, strict=False)
                if _normalized(a) == _normalized(b)
            )
            for prior in priors
        )
        shares.append(best / stats.task_count if stats.task_count else 0.0)
    return min(shares) if shares else None


def _is_generation(stats: GraphStats, step_id: str) -> bool:
    try:
        return stats.step(step_id).called
    except KeyError:  # pragma: no cover - ids come from the graph
        return False


def _g04_pointless_retry(stats: GraphStats) -> Finding | None:
    """G04: a retry that returns what it retried."""
    for step in stats.steps:
        if step.kind != "retry" or not step.calls or not step.outputs:
            continue
        priors = [stats.step(other) for other in step.inputs if _is_generation(stats, other)]
        candidates = [
            (
                sum(
                    1
                    for a, b in zip(prior.outputs, step.outputs, strict=False)
                    if _normalized(a) == _normalized(b)
                ),
                prior,
            )
            for prior in priors
            if prior.outputs
        ]
        if not candidates:
            continue
        same, prior = max(candidates, key=lambda pair: pair[0])
        share = same / stats.task_count if stats.task_count else 0.0
        if share < PREVALENCE:
            continue
        wasted = step.cost_usd * Decimal(str(share))
        amount = stats.scale_to_1k(wasted)
        return Finding(
            id="G04",
            group="Graph",
            title=f"The retry {step.id!r} returns what it retried",
            detail=(
                f"On {share:.0%} of tasks {step.id!r} returned text identical to {prior.id!r}'s. "
                "An unconditional retry pays for a second generation and keeps the first answer. "
                "Fire it on a verdict instead, or delete it.\n\n"
                "**What this share depends on.** It is measured over the answers in this run. A "
                "provider that samples would return something close rather than identical, and "
                "the share would be lower; a deterministic one makes it the whole split. The run "
                "this was computed over says which it was."
            ),
            evidence=(
                f"{same} of {stats.task_count} tasks identical to {prior.id!r}; "
                f"{step.calls} calls to {step.model} costing ${step.cost_usd:.4f}"
            ),
            projected_usd_per_1k=amount,
            formula=(
                f"${step.cost_usd:.4f} x {share:.0%} identical / {stats.task_count} tasks x "
                f"{stats.calls_per_1k:,} = ${amount:.2f}"
            ),
            confidence="measured",
        )
    return None


def _g05_compressible_input(stats: GraphStats) -> Finding | None:
    """G05: a step at the dearest model carrying text an earlier step could have shortened."""
    priced = [s for s in stats.steps if s.called and s.price is not None]
    if not priced:
        return None
    dearest = max(priced, key=lambda s: s.price.input if s.price else Decimal(0))
    if not dearest.request_texts:
        return None
    carried: list[tuple[str, int]] = []
    for source in stats.steps:
        if source.id == dearest.id or not source.outputs:
            continue
        if source.id not in dearest.inputs:
            continue
        if dearest.carries(source.outputs) < PREVALENCE * stats.task_count:
            continue
        carried.append((source.id, stats.tokens_of(source.outputs[0])))
    if not carried:
        return None
    tokens = sum(count for _, count in carried)
    amount = Decimal(tokens) * dearest.blended_input_rate * Decimal(stats.calls_per_1k) / MILLION
    sources = ", ".join(f"{name} (~{count:,} tok)" for name, count in carried)
    return Finding(
        id="G05",
        group="Graph",
        title=f"{dearest.id!r} runs at the dearest model on text an earlier step produced",
        detail=(
            f"{dearest.id!r} calls {dearest.model}, the most expensive model in this pipeline, "
            f"and ~{tokens:,} tokens of its input are the verbatim output of {len(carried)} "
            "earlier step(s). Whatever those steps can return already reduced — the governing "
            "line rather than the document, the extracted field rather than the reply — is paid "
            "for once at the cheap step's price instead of every time at this one's.\n\n"
            "**This is a ceiling.** It prices compressing that text to nothing, which nothing "
            "does. The real saving is the share of it a shorter form can drop."
        ),
        evidence=(
            f"{dearest.calls} calls to {dearest.model} at an effective "
            f"${dearest.blended_input_rate:.2f}/Mtok input; carried from {sources}"
        ),
        projected_usd_per_1k=amount,
        formula=(
            f"{tokens:,} tok x ${dearest.blended_input_rate:.2f}/Mtok x "
            f"{stats.calls_per_1k:,} tasks / 1e6 = ${amount:.2f}"
        ),
        confidence="projected",
    )


def graph_findings(stats: GraphStats) -> list[Finding]:
    """Every G rule, over one graph run's traces, ranked like every other finding."""
    rules = (
        _g01_shared_context,
        _g02_duplicate_tool_call,
        _g03_idle_verifier,
        _g04_pointless_retry,
        _g05_compressible_input,
    )
    findings = [finding for rule in rules if (finding := rule(stats)) is not None]
    findings.sort(key=lambda f: (-f.weighted_usd, f.id))
    return findings


def deletable_step(stats: GraphStats, findings: Sequence[Finding]) -> str | None:
    """The step the top-ranked finding proposes deleting, if it proposes deleting one.

    Only G03 and G04 name a step whose removal is a change Tokop can prove: both are about a
    call whose result nothing acts on, so the pipeline without it answers with the same text.
    G01, G02 and G05 propose rewriting a step rather than removing one, and rewriting is not
    something the proof machinery can evaluate without the rewritten pipeline in hand.
    """
    for finding in findings:
        if finding.id not in ("G03", "G04"):
            continue
        for step in stats.steps:
            if f"{step.id!r}" in finding.title:
                return step.id
    return None
