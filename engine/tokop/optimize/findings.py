"""Workload findings: W01 to W06, computed from traces (SPEC.md 7.4).

Where the prompt lint reads text, these read **what actually happened**: the calls a run made,
the tokens they really used, the grades they really earned. That difference is why several of
these carry the ``measured`` confidence label and outrank anything the lint can say.

Ranked the same way as lint findings — projected dollars per 1,000 tasks, weighted by
confidence — and returned in one list with them, so the Optimize screen shows a single ranking
rather than asking the reader to merge two.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tokop.core.pricing import ModelPrice
from tokop.optimize.graph import SINGLE_STEP
from tokop.optimize.lint import MILLION, Finding

#: A run with no latency requirement can use the Batch API at half price. v1 projects this and
#: never executes a batch, so it is labelled projected and says so in the detail.
BATCH_DISCOUNT_NOTE = "v1 projects this; it does not execute batches."

#: Output above this multiple of what the grader needs is worth flagging.
OUTPUT_EXCESS_FACTOR = 2.0


@dataclass(frozen=True)
class CallRow:
    """One call as the findings engine needs it. Mirrors the ledger, decoupled from SQLAlchemy
    so the findings can be computed over recorded fixtures without a database."""

    task_id: str
    model: str
    prompt_hash: str
    tier: str
    attempt: int
    prewarm: bool
    input_uncached: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    output_visible: int
    output_reasoning: int
    cost_usd: Decimal
    latency_ms: float
    reused: bool
    error: str | None = None
    system_text: str = ""
    user_text: str = ""
    static_prefix: str = ""
    #: Which step of the pipeline's graph made this call. Every call a single-call pipeline
    #: makes belongs to the one step it compiles to, so this defaults to that and nothing about
    #: an existing workload changes. It is here because the graph findings of M16b — a tool
    #: called twice with the same arguments, a verifier that never changed an outcome — are
    #: questions about *which step* made a call, and a call row that cannot say is a row none
    #: of them can be computed from (UPGRADE_V4.md M16).
    step: str = SINGLE_STEP

    @property
    def total_input(self) -> int:
        return self.input_uncached + self.cache_write_5m + self.cache_write_1h + self.cache_read

    @property
    def total_output(self) -> int:
        return self.output_visible + self.output_reasoning


@dataclass(frozen=True)
class RunStats:
    """Everything the findings need about one run."""

    pipeline_id: str
    split: str
    model_id: str
    price: ModelPrice
    calls: tuple[CallRow, ...]
    task_count: int
    successes: int
    #: Tokens the grader actually reads. For a short-answer workload this is tens, not hundreds.
    needed_output_tokens: int
    calls_per_1k: int = 1000

    @property
    def real_calls(self) -> tuple[CallRow, ...]:
        return tuple(c for c in self.calls if not c.prewarm)

    @property
    def total_cost(self) -> Decimal:
        return sum((c.cost_usd for c in self.calls), Decimal(0))

    @property
    def accuracy(self) -> float:
        return self.successes / self.task_count if self.task_count else 0.0

    @property
    def cost_per_successful_task(self) -> Decimal | None:
        return self.total_cost / self.successes if self.successes else None

    def per_call(self, attribute: str) -> list[float]:
        return [float(getattr(c, attribute)) for c in self.real_calls]

    def scale_to_1k(self, amount: Decimal) -> Decimal:
        """Scale a per-run figure to 1,000 tasks, which is the unit every finding is ranked in."""
        if not self.task_count:
            return Decimal(0)
        return amount * Decimal(self.calls_per_1k) / Decimal(self.task_count)


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _longest_common_suffix(texts: Sequence[str]) -> str:
    """The longest suffix every text shares.

    A grounding document pasted *after* a varying question is a common suffix, not a common
    prefix — which is exactly the shape that makes it uncacheable, so it has to be detected
    from the other end.
    """
    if not texts:
        return ""
    reversed_common = _longest_common_prefix([t[::-1] for t in texts])
    return reversed_common[::-1]


def _longest_common_prefix(texts: Sequence[str]) -> str:
    """The longest prefix every text shares.

    This is exactly what a provider cache can match on: an exact prefix, abandoned at the first
    difference. Measuring it turns "your prompt order is wrong" into a number.
    """
    if not texts:
        return ""
    shortest = min(texts, key=len)
    for index, char in enumerate(shortest):
        if any(text[index] != char for text in texts):
            return shortest[:index]
    return shortest


def workload_findings(stats: RunStats) -> list[Finding]:
    """Every W rule, over one run's traces."""
    findings: list[Finding] = []
    calls = stats.real_calls
    if not calls:
        return findings

    price = stats.price
    per_call_saving = price.input - price.cache_read

    # ---------------------------------------------------------------- W01
    # An identical block repeated across calls, outside the cached prefix. Measured from the
    # traces: if the same text is present in every call and none of it is being read from cache,
    # it is being paid for at full input price every time.
    uncached_repeat = 0
    sample_texts = [c.system_text + "\n" + c.user_text for c in calls[:50]]
    if sample_texts:
        first = calls[0]
        # Three shapes a repeated block takes: its own block, a shared opening, or — the case
        # that matters most here — a document pasted after a question that varies.
        candidates = [
            first.system_text,
            first.user_text,
            _longest_common_prefix(sample_texts),
            _longest_common_suffix(sample_texts),
        ]
        for candidate in candidates:
            if len(candidate) < 400:
                continue
            repeated = all(candidate in text for text in sample_texts)
            if repeated and candidate not in first.static_prefix:
                uncached_repeat = max(uncached_repeat, len(candidate))
    cache_reads = sum(c.cache_read for c in calls)
    if uncached_repeat and cache_reads == 0:
        repeated_tokens = int(_mean([c.total_input for c in calls]))
        amount = Decimal(repeated_tokens) * per_call_saving * stats.calls_per_1k / MILLION
        findings.append(
            Finding(
                id="W01",
                group="Cache",
                title="An identical block is repeated across calls and never cached",
                detail=(
                    "The same text appears in every call but sits outside any cacheable prefix, "
                    "so it is billed at full input price every time. Moving it in front of a "
                    "cache breakpoint changes nothing about what the model sees and turns the "
                    "largest line on the bill into a tenth of itself."
                ),
                evidence=(
                    f"{len(calls)} calls, {cache_reads:,} cache reads, "
                    f"~{repeated_tokens:,} input tokens per call, a "
                    f"{uncached_repeat:,}-character block identical across every call"
                ),
                projected_usd_per_1k=amount,
                formula=(
                    f"{repeated_tokens:,} tok x (${price.input} - ${price.cache_read})/Mtok x "
                    f"{stats.calls_per_1k:,} calls / 1e6 = ${amount:.2f}"
                ),
                confidence="measured",
                transform="reorder_static_first",
            )
        )

    # ---------------------------------------------------------------- W02
    # A prefix that changes on every call. Measured two ways, because there are two ways to
    # have one: a declared breakpoint whose content varies, and — the more common and more
    # expensive case — requests that diverge *before* they reach the large static block, so
    # the provider's exact-prefix match gives up long before the content worth caching.
    prefixes = Counter(c.static_prefix[:2000] for c in calls if c.static_prefix)
    writes = sum(c.cache_write_5m + c.cache_write_1h for c in calls)
    sample = calls[:50]
    shared = _longest_common_prefix([c.system_text + "\n" + c.user_text for c in sample])
    total_input = int(_mean([float(c.total_input) for c in calls]))
    # Rough tokens-per-character for the request text, used to express the shared prefix in the
    # same unit as the bill.
    chars = _mean([float(len(c.system_text) + len(c.user_text) + 1) for c in sample]) or 1.0
    shared_tokens = int(total_input * (len(shared) / chars)) if chars else 0
    strandable = max(0, total_input - shared_tokens)

    prefix_is_constant = bool(calls[0].static_prefix) and len(prefixes) == 1

    if calls[0].static_prefix and len(prefixes) > max(1, len(calls) // 10):
        wasted = Decimal(writes) * price.cache_write_5m / MILLION
        amount = stats.scale_to_1k(wasted)
        findings.append(
            Finding(
                id="W02",
                group="Cache",
                title="The cached prefix changes on almost every call",
                detail=(
                    "A breakpoint is set but the content in front of it differs between calls, "
                    "so every request writes a new cache entry and reads none of them. That is "
                    "worse than not caching at all: writes cost 1.25x base input."
                ),
                evidence=(
                    f"{len(prefixes)} distinct prefixes across {len(calls)} calls; "
                    f"{writes:,} tokens written, {cache_reads:,} read"
                ),
                projected_usd_per_1k=amount,
                formula=(
                    f"{writes:,} written tok x ${price.cache_write_5m}/Mtok / "
                    f"{stats.task_count} tasks x {stats.calls_per_1k:,} = ${amount:.2f}"
                ),
                confidence="measured",
                transform="move_breakpoint_to_static_end",
            )
        )
    elif prefix_is_constant and cache_reads == 0 and writes > 0:
        # The prefix never changed, so the misses are not a prompt-order problem: the calls all
        # went out before the first response returned.
        wasted = Decimal(writes) * price.cache_write_5m / MILLION
        amount = stats.scale_to_1k(wasted)
        findings.append(
            Finding(
                id="W02",
                group="Cache",
                title="Every call writes the cache and none reads it",
                detail=(
                    "Cache writes happened but no read did, and the prefix was identical across "
                    "every call — so this is not a prompt-order problem. A cache entry is only "
                    "available once the first response begins, so a parallel fan-out with no "
                    "pre-warm call misses on every request and pays the 1.25x write price for "
                    "the privilege."
                ),
                evidence=f"{writes:,} tokens written, {cache_reads:,} read, {len(calls)} calls",
                projected_usd_per_1k=amount,
                formula=(
                    f"{writes:,} written tok x ${price.cache_write_5m}/Mtok / "
                    f"{stats.task_count} tasks x {stats.calls_per_1k:,} = ${amount:.2f}"
                ),
                confidence="measured",
                transform="prewarm_cache",
            )
        )
    elif cache_reads == 0 and strandable > shared_tokens and strandable > 500:
        # Requests diverge early, so everything after the divergence point — including the
        # document — is stranded outside any prefix a provider could match.
        amount = Decimal(strandable) * per_call_saving * stats.calls_per_1k / MILLION
        findings.append(
            Finding(
                id="W02",
                group="Cache",
                title="Requests diverge before reaching the content worth caching",
                detail=(
                    "The provider matches an exact prefix over tools, then system, then "
                    "messages, and stops at the first difference. These requests start "
                    "differing after a few hundred tokens, so the large block that follows can "
                    "never be reached by a cache lookup however many breakpoints are set. "
                    "Ordering the request so the identical content comes first is what makes "
                    "any of the other cache findings actionable."
                ),
                evidence=(
                    f"{len(calls)} calls share only their first ~{shared_tokens:,} tokens; "
                    f"~{strandable:,} tokens per call sit after the divergence point, and "
                    f"{cache_reads:,} tokens were read from cache"
                ),
                projected_usd_per_1k=amount,
                formula=(
                    f"{strandable:,} stranded tok x (${price.input} - ${price.cache_read})"
                    f"/Mtok x {stats.calls_per_1k:,} calls / 1e6 = ${amount:.2f}"
                ),
                confidence="measured",
                transform="reorder_static_first",
            )
        )

    # ---------------------------------------------------------------- W03
    outputs = [c.total_output for c in calls]
    mean_output = _mean([float(o) for o in outputs])
    needed = stats.needed_output_tokens
    if needed and mean_output > needed * OUTPUT_EXCESS_FACTOR:
        excess = mean_output - needed
        amount = Decimal(str(round(excess))) * price.output * stats.calls_per_1k / MILLION
        p95 = statistics.quantiles(outputs, n=20)[-1] if len(outputs) >= 20 else max(outputs)
        findings.append(
            Finding(
                id="W03",
                group="C",
                title="Output is far longer than the grader reads",
                detail=(
                    "The pipeline generates paragraphs where the consumer takes a value. Output "
                    "is the most expensive token bucket on every model here, so an output "
                    "contract with a length cap is usually the second-largest saving available "
                    "after caching — and it makes the answer parseable, which is worth more."
                ),
                evidence=(
                    f"mean {mean_output:.0f} output tokens per call, p95 {p95:.0f}, "
                    f"against {needed:,} the grader reads"
                ),
                projected_usd_per_1k=amount,
                formula=(
                    f"({mean_output:.0f} - {needed:,}) tok x ${price.output}/Mtok x "
                    f"{stats.calls_per_1k:,} / 1e6 = ${amount:.2f}"
                ),
                confidence="measured",
                transform="add_output_contract",
            )
        )

    # ---------------------------------------------------------------- W06
    retries = [c for c in calls if c.attempt > 1]
    failures = [c for c in calls if c.error]
    if retries or failures:
        retry_cost = sum((c.cost_usd for c in retries), Decimal(0))
        amount = stats.scale_to_1k(retry_cost)
        findings.append(
            Finding(
                id="W06",
                group="Cache",
                title="Retries and failed calls",
                detail=(
                    "Every retry is its own call with its own cost, and a call that ultimately "
                    "failed still counts its task as unsuccessful. Both move cost per successful "
                    "task, which is why they are listed rather than smoothed away."
                ),
                evidence=(
                    f"{len(retries)} retried call(s), {len(failures)} failed call(s) out of "
                    f"{len(calls)}"
                ),
                projected_usd_per_1k=amount,
                formula=(
                    f"${retry_cost} spent on retries / {stats.task_count} tasks x "
                    f"{stats.calls_per_1k:,} = ${amount:.2f}"
                ),
                confidence="measured",
                transform=None,
            )
        )

    findings.sort(key=lambda f: (-f.weighted_usd, f.id))
    return findings


def batch_finding(stats: RunStats, latency_sensitive: bool) -> Finding | None:
    """W05: a workload with no latency requirement can halve its bill with the Batch API."""
    if latency_sensitive or stats.price.batch_discount <= 0:
        return None
    saving = stats.total_cost * stats.price.batch_discount
    amount = stats.scale_to_1k(saving)
    return Finding(
        id="W05",
        group="Cache",
        title="No latency requirement: the Batch API would halve this",
        detail=(
            "This workload is marked as having no latency requirement, and the Batch API "
            f"discounts input and output by {stats.price.batch_discount:.0%}. "
            + BATCH_DISCOUNT_NOTE
            + " The discount stacks with prompt caching, so it applies on top of everything "
            "else on this list."
        ),
        evidence=(
            f"${stats.total_cost:.4f} spent on {stats.task_count} tasks with no latency "
            "requirement declared in the workload"
        ),
        projected_usd_per_1k=amount,
        formula=(
            f"${stats.total_cost:.4f} x {stats.price.batch_discount:.0%} / {stats.task_count} "
            f"tasks x {stats.calls_per_1k:,} = ${amount:.2f}"
        ),
        confidence="projected",
        transform=None,
    )


def cheaper_tier_finding(
    frontier: RunStats,
    cheap: RunStats,
    cheap_correct_ids: set[str],
    frontier_correct_ids: set[str],
) -> Finding | None:
    """W04: the frontier model answering tasks a cheaper tier already gets right.

    Projected from the **calibration** split, and the evidence states its n, because this is the
    finding that justifies building a cascade and it must not be quoted from the split the proof
    will later be measured on.
    """
    both_right = cheap_correct_ids & frontier_correct_ids
    if not both_right or frontier.task_count == 0:
        return None
    share = len(both_right) / frontier.task_count
    frontier_per_task = frontier.total_cost / frontier.task_count
    cheap_per_task = cheap.total_cost / cheap.task_count if cheap.task_count else Decimal(0)
    saving_per_task = (frontier_per_task - cheap_per_task) * Decimal(str(share))
    amount = saving_per_task * frontier.calls_per_1k
    return Finding(
        id="W04",
        group="Cache",
        title="The frontier model is answering tasks a cheaper tier already gets right",
        detail=(
            f"On the calibration split, the cheap tier answered {len(both_right)} of "
            f"{frontier.task_count} tasks correctly that the frontier model also got right. "
            "Those tasks do not need the frontier model; they need a scorer confident enough to "
            "stop at the cheap tier. That is what a cascade is. The figure is a ceiling: a real "
            "cascade pays for the cheap attempt on escalated tasks too, and the proof measures "
            "what it actually recovers."
        ),
        evidence=(
            f"{len(both_right)}/{frontier.task_count} calibration tasks ({share:.0%}) answered "
            f"correctly by both tiers; n = {frontier.task_count}"
        ),
        projected_usd_per_1k=amount,
        formula=(
            f"({frontier_per_task:.6f} - {cheap_per_task:.6f}) $/task x {share:.0%} routable x "
            f"{frontier.calls_per_1k:,} = ${amount:.2f}"
        ),
        confidence="projected",
        transform="build_cascade",
    )


def rank(findings: Sequence[Finding]) -> list[Finding]:
    """One ranking across lint and workload findings, by weighted dollars then by rule ID."""
    return sorted(findings, key=lambda f: (-f.weighted_usd, f.id))
