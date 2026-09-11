"""Prompt lint: PL01 to PL14, mapped to CLEAR plus a Cache group (SPEC.md 7.4).

Deterministic and local. **No model calls.** Every finding carries its evidence, the spans to
highlight, a projected dollar saving with the formula that produced it, a confidence label, and
the transform that addresses it.

The ranking rule is the point of the whole module: findings are ordered by **projected dollars
per 1,000 tasks, weighted by confidence**, so low-dollar hygiene sinks to the bottom where it
belongs. A linter that puts "you said please" above "your 7,000-token handbook cannot be
cached" is a linter nobody acts on.

One consequence is worth stating because it surprises people: cutting words out of a *cached*
prefix saves about a tenth of what cutting the same words from uncached input saves, because
cache reads cost 0.1x base input. Hygiene findings inside the cacheable prefix are priced at
the read rate, which is why they rank far below a finding that makes the prefix cacheable at
all.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from tokop.adapters.base import LLMRequest
from tokop.core.pricing import ModelPrice
from tokop.core.tokenize import BaseCounter, get_base_counter

Confidence = Literal["measured", "projected", "heuristic"]
Group = Literal["Cache", "C", "L", "E", "A", "R"]

#: Confidence weights used for ranking only. A heuristic finding worth $40 should not outrank a
#: measured finding worth $35, and these make that explicit rather than leaving it to the
#: reader to discount in their head.
CONFIDENCE_WEIGHT: dict[Confidence, Decimal] = {
    "measured": Decimal("1.0"),
    "projected": Decimal("0.7"),
    "heuristic": Decimal("0.4"),
}

MILLION = Decimal(1_000_000)

CLEAR_LETTERS = ("C", "L", "E", "A", "R")

#: Which rules score which CLEAR letter. Cache rules are not part of CLEAR and score nothing.
RULE_LETTER: dict[str, str] = {
    "PL06": "C",
    "PL07": "L",
    "PL08": "L",
    "PL09": "L",
    "PL10": "E",
    "PL11": "E",
    "PL12": "A",
    "PL13": "R",
    "PL14": "A",
}


@dataclass(frozen=True)
class Span:
    """A range to highlight, in one block of the prompt."""

    section: str  # "system" or "user"
    block: int
    start: int
    end: int
    text: str

    def as_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "block": self.block,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


@dataclass(frozen=True)
class Finding:
    """One lint result."""

    id: str
    group: str
    title: str
    detail: str
    evidence: str
    projected_usd_per_1k: Decimal
    formula: str
    confidence: Confidence
    transform: str | None = None
    spans: tuple[Span, ...] = ()

    @property
    def weighted_usd(self) -> Decimal:
        return self.projected_usd_per_1k * CONFIDENCE_WEIGHT[self.confidence]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "group": self.group,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "projected_usd_per_1k": str(self.projected_usd_per_1k),
            "weighted_usd_per_1k": str(self.weighted_usd),
            "formula": self.formula,
            "confidence": self.confidence,
            "transform": self.transform,
            "spans": [s.as_dict() for s in self.spans],
        }


@dataclass
class LintContext:
    """What the lint needs to turn tokens into dollars."""

    price: ModelPrice
    min_cacheable_tokens: int
    calls_per_1k: int = 1000
    #: Measured output tokens per call, when traces exist. Turns PL06 from projected to
    #: measured, which moves it up the ranking for a reason the reader can check.
    observed_output_tokens: float | None = None
    #: What the grader or downstream consumer actually needs. For a short-answer workload this
    #: is tens of tokens, which is why an uncapped essay is the largest output-side waste.
    needed_output_tokens: int | None = None
    counter: BaseCounter = field(default_factory=get_base_counter)
    tool_use_system_prompt_tokens: int = 0

    def tokens(self, text: str) -> int:
        return self.counter.count(text)

    def input_usd(self, tokens: float) -> Decimal:
        return Decimal(str(round(tokens))) * self.price.input * self.calls_per_1k / MILLION

    def cache_read_usd(self, tokens: float) -> Decimal:
        return Decimal(str(round(tokens))) * self.price.cache_read * self.calls_per_1k / MILLION

    def output_usd(self, tokens: float) -> Decimal:
        return Decimal(str(round(tokens))) * self.price.output * self.calls_per_1k / MILLION


# --------------------------------------------------------------------------- detectors

_VOLATILE = (
    (
        re.compile(r"\{\{\s*(timestamp|now|date|time|today|uuid|request_id|counter)\s*\}\}", re.I),
        "template variable",
    ),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b"), "date or timestamp"),
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:UTC|GMT|AM|PM)?\b", re.I), "time of day"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "UUID"),
    (re.compile(r"\b(?:current (?:date|time)|as of now|right now)\b", re.I), "volatile phrase"),
    (re.compile(r"\brequest (?:number|#)\s*\d+\b", re.I), "request counter"),
)

_COURTESY = re.compile(
    r"\b(?:please(?:\s+note)?|thank(?:s| you)|we (?:really )?appreciate|kindly|"
    r"if you (?:don't mind|would)|feel free to|it'?s important that you|"
    r"do a good job|try your best|remember that|keep in mind that|"
    r"i hope|hopefully|perhaps|maybe|it'?s generally a good idea|"
    r"somewhat|a little|a bit|sort of|kind of|as best you can|whenever possible)\b",
    re.I,
)

_VAGUE_VERBS = re.compile(
    r"\b(?:help (?:with|out)|look at|handle|deal with|take care of|work on|"
    r"do (?:something|what you can)|assist with|see (?:to|about))\b",
    re.I,
)

_ROLE = re.compile(r"\byou are (?:an?|the)\s+\w+", re.I)
_TASK = re.compile(r"^\s*(?:task|your task|objective|goal)\s*[:\-]", re.I | re.M)
_SUCCESS = re.compile(
    r"\b(?:success criteria|what good looks like|how to get it right|a good answer|"
    r"you are correct when|done when)\b",
    re.I,
)

#: Curated conflicting pairs. Labelled heuristic: a prompt can legitimately want both in
#: different places, and the lint cannot tell.
CONFLICT_PAIRS: tuple[tuple[str, str, str], ...] = (
    (
        r"\bbe concise\b",
        r"\b(?:in full detail|explain your reasoning in full|at length|be thorough"
        r"|comprehensive)\b",
        "brevity against detail",
    ),
    (r"\bbe brief\b", r"\b(?:elaborate|expand on|in depth)\b", "brevity against depth"),
    (
        r"\bnever\b.{0,40}\bguess\b",
        r"\balways (?:give|provide) an answer\b",
        "refusal against always answering",
    ),
    (
        r"\bonly (?:use|rely on)\b.{0,30}\bprovided\b",
        r"\buse your (?:own )?knowledge\b",
        "grounding against prior knowledge",
    ),
    (r"\bdo not explain\b", r"\bexplain\b", "explain against do not explain"),
)

_OUTPUT_CONTRACT = re.compile(
    r"(?:reply|respond|answer|return|output)\b[^.]{0,60}\b(?:with|as|in)\b[^.]{0,40}"
    r"(?:json|one (?:word|line|number|object)|exactly|the following (?:format|structure)|"
    r"\{.*\}|<[a-z_]+>)",
    re.I | re.S,
)
_LENGTH_CAP = re.compile(
    r"\b(?:no more than|at most|under|fewer than|maximum of|limit(?:ed)? to|within)\s+"
    r"\d+\s*(?:words?|tokens?|sentences?|characters?|lines?|items?)\b|"
    r"\b(?:one|a single|1)\s+(?:word|line|sentence|number|json object)\b",
    re.I,
)

_HARDCODED = (
    (re.compile(r"\b(?:https?://|www\.)\S{6,}"), "URL"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "email address"),
    (re.compile(r"\bv?\d+\.\d+\.\d+\b"), "version number"),
    (re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"), "hard-coded date"),
    (re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?\b"), "hard-coded amount"),
)

_IMPERATIVE_CLAUSE = re.compile(
    r"(?:^|[.\n]\s*|[-*]\s*)((?:please\s+)?(?:you must\s+|you should\s+)?"
    r"(?:analyse|analyze|answer|check|cite|classify|compare|compute|consider|convert|"
    r"create|describe|determine|double-check|evaluate|explain|extract|find|format|"
    r"generate|identify|list|output|produce|rank|rate|read|recommend|report|return|"
    r"review|rewrite|summarise|summarize|translate|validate|verify|write)\b)",
    re.I,
)

#: A phrase that means "this changes on every request", as opposed to a date that happens to
#: appear inside a static document. The distinction matters: the demo handbook contains policy
#: dates, and flagging those as cache-breaking volatility would be a false alarm on the one
#: finding the product most needs to be right about.
_VOLATILE_PHRASE = re.compile(
    r"\{\{\s*(?:timestamp|now|date|time|today|uuid|request_id|counter)\s*\}\}"
    r"|\b(?:current (?:date|time)|as of now|right now|generated at|request (?:number|#)\s*\d+)\b",
    re.I,
)

#: Above this, a block is a grounding document rather than instructions. Rules that critique
#: *authorship* — parameterize this, compose this, structure this — must not be applied to a
#: document the prompt merely carries: the demo handbook's own support email and policy dates
#: are content, not hard-coded specifics somebody should have made variables.
DOCUMENT_TOKENS = 1000

#: Below this, a block is a stamp or header line rather than an instruction. Flagging the
#: rendered value of a {{timestamp}} block as a "hard-coded specific" would be noise; PL01
#: already covers what is wrong with it.
INSTRUCTION_MIN_TOKENS = 40

#: Above this, a block is a document rather than a header line, and a date-shaped string in it
#: is almost certainly content rather than a per-request stamp.
VOLATILE_BLOCK_MAX_TOKENS = 120


def block_is_volatile(text: str, tokens: int) -> bool:
    """Whether a block plausibly differs between requests.

    Two signals, in order of reliability: an unrendered template variable, and — only in a
    block small enough to be a header rather than a document — a date, time or UUID.
    """
    if _VOLATILE_PHRASE.search(text):
        return True
    if tokens > VOLATILE_BLOCK_MAX_TOKENS:
        return False
    return any(pattern.search(text) for pattern, _ in _VOLATILE[1:4])


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9']+")


def _spans_for(section: str, block: int, text: str, pattern: re.Pattern[str]) -> list[Span]:
    return [Span(section, block, m.start(), m.end(), m.group(0)) for m in pattern.finditer(text)]


def _trigrams(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    return {tuple(words[i : i + 3]) for i in range(max(0, len(words) - 2))}


def jaccard(a: str, b: str) -> float:
    """Normalized word-3-gram Jaccard, as SPEC.md 7.4 specifies for PL07."""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


DUPLICATE_THRESHOLD = 0.8
WALL_OF_TEXT_TOKENS = 250
COMPOSE_TOKENS = 500
COMPOSE_JOBS = 3
LARGE_OVERHEAD_TOKENS = 400
FEWSHOT_EXAMPLE_TOKENS = 150


def _blocks(request: LLMRequest) -> list[tuple[str, int, str, str | None]]:
    """(section, index, text, cache_ttl) for every block, in wire order."""
    out: list[tuple[str, int, str, str | None]] = []
    for i, block in enumerate(request.system):
        out.append(("system", i, block.text, block.cache))
    for i, block in enumerate(request.messages[0].blocks if request.messages else []):
        out.append(("user", i, block.text, block.cache))
    return out


def lint(request: LLMRequest, context: LintContext) -> list[Finding]:
    """Run every rule and return findings ranked by weighted dollars."""
    findings: list[Finding] = []
    blocks = _blocks(request)
    prefix = request.static_prefix_text
    prefix_tokens = context.tokens(prefix) if prefix else 0
    full_input_text = "\n\n".join(text for _, _, text, _ in blocks)
    full_input_tokens = context.tokens(full_input_text)
    # Tokens that could be cached if the prompt were ordered for it: everything that is
    # identical across calls. Approximated by the largest block, which for a document-grounded
    # prompt is the document.
    largest = max(blocks, key=lambda b: len(b[2]), default=("system", 0, "", None))
    largest_tokens = context.tokens(largest[2])
    per_call_saving = context.price.input - context.price.cache_read

    def cacheable_saving(tokens: int) -> tuple[Decimal, str]:
        amount = Decimal(tokens) * per_call_saving * context.calls_per_1k / MILLION
        return amount, (
            f"{tokens:,} tok x (${context.price.input}/Mtok input - "
            f"${context.price.cache_read}/Mtok cache read) x {context.calls_per_1k:,} calls "
            f"/ 1e6 = ${amount:.2f}"
        )

    # ---------------------------------------------------------------- Cache group
    # Only blocks that plausibly change between requests count. A policy date inside the
    # handbook is static content, and flagging it would be a false alarm on the finding that
    # matters most.
    volatile_in_prefix: list[Span] = []
    for section, index, text, _ in blocks:
        if prefix and text not in prefix:
            continue  # after the breakpoint: volatility there is correct, not a finding
        if not block_is_volatile(text, context.tokens(text)):
            continue
        volatile_in_prefix.extend(_spans_for(section, index, text, _VOLATILE_PHRASE))
        for pattern, _kind in _VOLATILE[1:4]:
            volatile_in_prefix.extend(_spans_for(section, index, text, pattern))

    if volatile_in_prefix:
        amount, formula = cacheable_saving(prefix_tokens or largest_tokens)
        findings.append(
            Finding(
                id="PL01",
                group="Cache",
                title="Volatile content sits inside the cacheable prefix",
                detail=(
                    "A timestamp, date, UUID or counter inside the prefix changes the prefix on "
                    "every request, so no cache entry is ever read. Move it after the "
                    "breakpoint, where it costs full input price on a handful of tokens instead "
                    "of destroying the cache on thousands."
                ),
                evidence="; ".join(
                    f"{s.section} block {s.block}: {s.text.strip()!r}"
                    for s in volatile_in_prefix[:3]
                ),
                projected_usd_per_1k=amount,
                formula=formula,
                confidence="projected",
                transform="move_volatile_after_breakpoint",
                spans=tuple(volatile_in_prefix),
            )
        )

    # PL02: dynamic content before static content.
    static_index = next(
        (i for i, (_, _, text, _) in enumerate(blocks) if context.tokens(text) > 500), None
    )
    if static_index is not None and static_index > 0:
        before = blocks[:static_index]
        dynamic_before = [
            b
            for b in before
            if block_is_volatile(b[2], context.tokens(b[2])) or context.tokens(b[2]) < 60
        ]
        if dynamic_before:
            amount, formula = cacheable_saving(largest_tokens)
            findings.append(
                Finding(
                    id="PL02",
                    group="Cache",
                    title="Dynamic content is placed before static content",
                    detail=(
                        "The provider cache matches an exact prefix over tools, then system, "
                        "then messages. Anything that varies per request and sits before the "
                        "large static block puts that block behind a moving prefix, so it can "
                        "never be cached. Put the static block first and the variable part last."
                    ),
                    evidence=(
                        f"{len(dynamic_before)} variable block(s) precede a "
                        f"{largest_tokens:,}-token static block "
                        f"({largest[0]} block {largest[1]})"
                    ),
                    projected_usd_per_1k=amount,
                    formula=formula,
                    confidence="projected",
                    transform="reorder_static_first",
                    spans=tuple(
                        Span(s, i, 0, min(len(t), 120), t[:120]) for s, i, t, _ in dynamic_before
                    ),
                )
            )

    # PL03: prefix below the minimum, where caching silently does nothing.
    if prefix and prefix_tokens < context.min_cacheable_tokens:
        wasted = context.input_usd(prefix_tokens) - context.cache_read_usd(prefix_tokens)
        findings.append(
            Finding(
                id="PL03",
                group="Cache",
                title="The cached prefix is below this model's minimum",
                detail=(
                    f"A breakpoint is set but the prefix is {prefix_tokens:,} tokens, under this "
                    f"model's {context.min_cacheable_tokens:,}-token minimum. The request runs "
                    "uncached and returns no error: both cache usage fields read zero. Either "
                    "move more static content in front of the breakpoint or accept that this "
                    "prompt is not cacheable on this model."
                ),
                evidence=(
                    f"prefix {prefix_tokens:,} tok against a "
                    f"{context.min_cacheable_tokens:,} tok minimum"
                ),
                projected_usd_per_1k=wasted,
                formula=(
                    f"{prefix_tokens:,} tok x (${context.price.input} - "
                    f"${context.price.cache_read})/Mtok x {context.calls_per_1k:,} = "
                    f"${wasted:.2f} that looks cached and is not"
                ),
                confidence="heuristic",
                transform="grow_or_drop_prefix",
            )
        )

    # PL04: a breakpoint on a block that changes every request.
    for section, index, text, cache in blocks:
        if cache is None:
            continue
        if block_is_volatile(text, context.tokens(text)):
            amount, formula = cacheable_saving(context.tokens(text))
            findings.append(
                Finding(
                    id="PL04",
                    group="Cache",
                    title="The cache breakpoint is on a block that changes every request",
                    detail=(
                        "Writes happen only at breakpoints. A breakpoint on content that varies "
                        "per request writes a new entry every time and reads none of them, so "
                        "it converts every call into a cache write at 1.25x input price. Move "
                        "the breakpoint to the end of the last block that is identical across "
                        "calls."
                    ),
                    evidence=f"{section} block {index} carries cache={cache} and varies per call",
                    projected_usd_per_1k=amount,
                    formula=formula,
                    confidence="projected",
                    transform="move_breakpoint_to_static_end",
                    spans=(Span(section, index, 0, min(len(text), 120), text[:120]),),
                )
            )

    # PL05: large fixed overhead per call.
    system_tokens = context.tokens(request.system_text)
    tool_tokens = context.tokens(str(request.tools)) if request.tools else 0
    overhead = (
        system_tokens
        + tool_tokens
        + (context.tool_use_system_prompt_tokens if request.tools else 0)
    )
    if overhead > LARGE_OVERHEAD_TOKENS and not prefix:
        amount, formula = cacheable_saving(overhead)
        findings.append(
            Finding(
                id="PL05",
                group="Cache",
                title="Large fixed overhead is paid in full on every call",
                detail=(
                    f"The system prompt, tool definitions and the provider's tool-use system "
                    f"prompt add {overhead:,} tokens to every request. That content is identical "
                    "across calls, so it is exactly what a cache breakpoint exists for."
                ),
                evidence=(
                    f"system {system_tokens:,} tok"
                    + (
                        f" + tools {tool_tokens:,} tok + provider tool-use prompt "
                        f"{context.tool_use_system_prompt_tokens:,} tok"
                        if request.tools
                        else ""
                    )
                ),
                projected_usd_per_1k=amount,
                formula=formula,
                confidence="projected",
                transform="add_breakpoint_after_static",
            )
        )

    # ---------------------------------------------------------------- CLEAR: C
    has_contract = bool(_OUTPUT_CONTRACT.search(full_input_text))
    has_cap = bool(_LENGTH_CAP.search(full_input_text))
    if not (has_contract and has_cap):
        needed = context.needed_output_tokens
        observed = context.observed_output_tokens
        if observed is not None and needed is not None:
            excess = max(0.0, observed - needed)
            amount = context.output_usd(excess)
            confidence: Confidence = "measured"
            formula = (
                f"({observed:.0f} observed - {needed:,} needed) tok x "
                f"${context.price.output}/Mtok x {context.calls_per_1k:,} / 1e6 = ${amount:.2f}"
            )
        else:
            # Without traces, project against the declared cap: an uncapped prompt tends to
            # fill a good fraction of it.
            projected_excess = request.max_tokens * 0.25
            amount = context.output_usd(projected_excess)
            confidence = "projected"
            formula = (
                f"max_tokens {request.max_tokens:,} x 25% x ${context.price.output}/Mtok x "
                f"{context.calls_per_1k:,} / 1e6 = ${amount:.2f}"
            )
        findings.append(
            Finding(
                id="PL06",
                group="C",
                title="No output contract with a length cap",
                detail=(
                    "The prompt does not say what shape the answer must take or how long it may "
                    "be, so the model decides, and it decides generously. An explicit contract "
                    "costs a few input tokens and is paid back many times over on the output "
                    "side. It also makes the answer parseable, which is worth more than the "
                    "tokens."
                ),
                evidence=(
                    ("no output format is specified" if not has_contract else "")
                    + ("; " if not has_contract and not has_cap else "")
                    + ("no length cap is specified" if not has_cap else "")
                )
                + f"; max_tokens is {request.max_tokens:,}",
                projected_usd_per_1k=amount,
                formula=formula,
                confidence=confidence,
                transform="add_output_contract",
            )
        )

    # ---------------------------------------------------------------- CLEAR: L
    # PL07: duplicate instructions.
    instructions: list[tuple[str, int, str]] = []
    for section, index, text, _ in blocks:
        for line in text.splitlines():
            stripped = line.strip().lstrip("-*").strip()
            if 20 <= len(stripped) <= 300:
                instructions.append((section, index, stripped))
    duplicates: list[tuple[str, str, float]] = []
    for i, (_, _, a) in enumerate(instructions):
        for _, _, b in instructions[i + 1 :]:
            score = jaccard(a, b)
            if score >= DUPLICATE_THRESHOLD:
                duplicates.append((a, b, score))
    if duplicates:
        dup_tokens = sum(context.tokens(b) for _, b, _ in duplicates)
        in_prefix = bool(prefix) and all(b in prefix for _, b, _ in duplicates)
        amount = context.cache_read_usd(dup_tokens) if in_prefix else context.input_usd(dup_tokens)
        rate = context.price.cache_read if in_prefix else context.price.input
        findings.append(
            Finding(
                id="PL07",
                group="L",
                title="The same instruction is given twice",
                detail=(
                    "Duplicated rules cost tokens on every call and give the model two things "
                    "to reconcile instead of one. Keep the clearer wording and delete the other."
                    + (
                        " These duplicates sit inside the cached prefix, so they are billed at "
                        "the cache read rate and removing them saves about a tenth of what the "
                        "same cut would save in uncached input."
                        if in_prefix
                        else ""
                    )
                ),
                evidence="; ".join(
                    f"{a[:60]!r} ~ {b[:60]!r} (Jaccard {score:.2f})"
                    for a, b, score in duplicates[:2]
                ),
                projected_usd_per_1k=amount,
                formula=(
                    f"{dup_tokens:,} duplicated tok x ${rate}/Mtok x "
                    f"{context.calls_per_1k:,} / 1e6 = ${amount:.2f}"
                ),
                confidence="projected",
                transform="remove_duplicate_instructions",
                spans=tuple(
                    Span(s, i, 0, len(t), t)
                    for s, i, t in instructions
                    if any(t == b for _, b, _ in duplicates)
                ),
            )
        )

    # PL08: courtesy, hedges, filler.
    courtesy_spans: list[Span] = []
    for section, index, text, _ in blocks:
        courtesy_spans.extend(_spans_for(section, index, text, _COURTESY))
    if courtesy_spans:
        filler_tokens = sum(context.tokens(s.text) for s in courtesy_spans)
        # Removing a phrase usually removes the clause around it; count the phrase only, which
        # under-claims rather than over-claims.
        in_prefix = bool(prefix)
        amount = (
            context.cache_read_usd(filler_tokens) if in_prefix else context.input_usd(filler_tokens)
        )
        rate = context.price.cache_read if in_prefix else context.price.input
        findings.append(
            Finding(
                id="PL08",
                group="L",
                title="Courtesy, hedges and filler",
                detail=(
                    "Politeness and hedging cost tokens on every call and do not improve the "
                    "answer. Hedges ('perhaps consider', 'it's generally a good idea') are worse "
                    "than merely wasteful: they make an instruction optional."
                    + (
                        " This filler sits inside the cached prefix, so it is billed at the "
                        "cache read rate and removing it saves about a tenth of what the same "
                        "cut would save in uncached input."
                        if in_prefix
                        else ""
                    )
                ),
                evidence=f"{len(courtesy_spans)} phrase(s), for example "
                + ", ".join(repr(s.text) for s in courtesy_spans[:4]),
                projected_usd_per_1k=amount,
                formula=(
                    f"{filler_tokens:,} tok of filler x ${rate}/Mtok x "
                    f"{context.calls_per_1k:,} / 1e6 = ${amount:.2f}"
                ),
                confidence="projected",
                transform="strip_courtesy",
                spans=tuple(courtesy_spans),
            )
        )

    # PL09: oversized or redundant few-shot examples.
    # Few-shot examples look like numbered or input/output pairs, not like a document that
    # happens to use the word "example".
    example_marker = re.compile(
        r"^\s*(?:example\s*\d*\s*[:.)\-]|input\s*:|output\s*:|q\s*:|a\s*:)", re.I | re.M
    )
    example_blocks = [
        (s, i, t)
        for s, i, t, _ in blocks
        if len(example_marker.findall(t)) >= 2 and context.tokens(t) > FEWSHOT_EXAMPLE_TOKENS
    ]
    if example_blocks:
        example_tokens = sum(context.tokens(t) for _, _, t in example_blocks)
        amount = context.input_usd(example_tokens * 0.4)
        findings.append(
            Finding(
                id="PL09",
                group="L",
                title="Large few-shot examples",
                detail=(
                    "Examples earn their tokens only while they change the answer. Drop them one "
                    "at a time and measure; on a workload with an output contract they are often "
                    "carrying nothing."
                ),
                evidence=f"{len(example_blocks)} example block(s), {example_tokens:,} tokens",
                projected_usd_per_1k=amount,
                formula=(
                    f"{example_tokens:,} tok x 40% assumed removable x ${context.price.input}"
                    f"/Mtok x {context.calls_per_1k:,} / 1e6 = ${amount:.2f}"
                ),
                confidence="heuristic",
                transform="trim_examples",
                spans=tuple(Span(s, i, 0, min(len(t), 120), t[:120]) for s, i, t in example_blocks),
            )
        )

    # ---------------------------------------------------------------- CLEAR: E
    system_text = request.system_text
    missing = []
    if not _ROLE.search(system_text):
        missing.append("role")
    if not _TASK.search(system_text):
        missing.append("task statement")
    if not _SUCCESS.search(system_text):
        missing.append("success criteria")
    vague = _spans_for("system", 0, system_text, _VAGUE_VERBS)
    if missing or vague:
        findings.append(
            Finding(
                id="PL10",
                group="E",
                title="No explicit role, task or success criteria",
                detail=(
                    "A prompt that says what to avoid more clearly than what to do leaves the "
                    "model guessing at the goal. Name the role, state the task in one line, and "
                    "say what a correct answer looks like."
                ),
                evidence=(
                    ("missing: " + ", ".join(missing) if missing else "")
                    + ("; " if missing and vague else "")
                    + (
                        "vague verbs: " + ", ".join(repr(s.text) for s in vague[:3])
                        if vague
                        else ""
                    )
                ),
                projected_usd_per_1k=Decimal(0),
                formula="quality finding; no direct token saving",
                confidence="heuristic",
                transform="add_role_task_criteria",
                spans=tuple(vague),
            )
        )

    # PL11: conflicting instruction pairs.
    conflicts: list[tuple[str, str, str]] = []
    for left, right, label in CONFLICT_PAIRS:
        left_match = re.search(left, full_input_text, re.I)
        right_match = re.search(right, full_input_text, re.I)
        if left_match and right_match:
            conflicts.append((label, left_match.group(0), right_match.group(0)))
    if conflicts:
        findings.append(
            Finding(
                id="PL11",
                group="E",
                title="Conflicting instructions",
                detail=(
                    "Two instructions cannot both be followed, so the model picks one and the "
                    "choice varies between calls. This is a heuristic: a prompt may legitimately "
                    "want both in different places, and the lint cannot tell."
                ),
                evidence="; ".join(
                    f"{label}: {a!r} against {b!r}" for label, a, b in conflicts[:2]
                ),
                projected_usd_per_1k=Decimal(0),
                formula="quality finding; no direct token saving",
                confidence="heuristic",
                transform="resolve_conflicts",
            )
        )

    # ---------------------------------------------------------------- CLEAR: A
    for section, index, text, _ in blocks:
        block_tokens = context.tokens(text)
        if block_tokens < WALL_OF_TEXT_TOKENS:
            continue
        has_structure = bool(re.search(r"^\s*(?:[-*#]|\d+\.)\s", text, re.M)) or "|" in text
        if not has_structure:
            findings.append(
                Finding(
                    id="PL12",
                    group="A",
                    title="Unstructured wall of text",
                    detail=(
                        "A long block with no headings, list or table is harder for the model to "
                        "navigate and harder for a human to edit. Structure costs almost nothing "
                        "in tokens and makes the rest of the lint actionable."
                    ),
                    evidence=(
                        f"{section} block {index}: {block_tokens:,} tokens, no list or heading"
                    ),
                    projected_usd_per_1k=Decimal(0),
                    formula="quality finding; no direct token saving",
                    confidence="heuristic",
                    transform="add_structure",
                    spans=(Span(section, index, 0, min(len(text), 200), text[:200]),),
                )
            )
            break

    # PL14: compose instead.
    # Instructions only: a grounding document's verbs are not jobs the prompt is asking for.
    instruction_text = "\n\n".join(
        text
        for section, _, text, _ in blocks
        if section == "system" and context.tokens(text) <= DOCUMENT_TOKENS
    )
    jobs = {m.group(1).strip().lower() for m in _IMPERATIVE_CLAUSE.finditer(instruction_text)}
    system_prompt_tokens = context.tokens(instruction_text)
    if system_prompt_tokens > COMPOSE_TOKENS and len(jobs) >= COMPOSE_JOBS:
        findings.append(
            Finding(
                id="PL14",
                group="A",
                title="Consider composing this into steps",
                detail=(
                    f"The system prompt is {system_prompt_tokens:,} tokens and asks for "
                    f"{len(jobs)} distinct jobs. Composition is worth trying **after** a CLEAR "
                    "pass, not instead of one, and it does not automatically use fewer tokens: "
                    "each step pays its own overhead. Split only if the prompt is still brittle "
                    "once it is lean and explicit. Heuristic."
                ),
                evidence=f"{len(jobs)} imperative task clauses: " + ", ".join(sorted(jobs)[:5]),
                projected_usd_per_1k=Decimal(0),
                formula="quality finding; composition does not automatically save tokens",
                confidence="heuristic",
                transform=None,
            )
        )

    # ---------------------------------------------------------------- CLEAR: R
    hardcoded: list[Span] = []
    for section, index, text, _ in blocks:
        block_tokens = context.tokens(text)
        if block_tokens > DOCUMENT_TOKENS:
            continue  # a grounding document, not a template to parameterize
        if block_tokens < INSTRUCTION_MIN_TOKENS:
            continue  # a stamp or a header line, not an instruction to parameterize
        for pattern, _kind in _HARDCODED:
            hardcoded.extend(_spans_for(section, index, text, pattern))
    if hardcoded:
        findings.append(
            Finding(
                id="PL13",
                group="R",
                title="Hard-coded specifics that should be variables",
                detail=(
                    "Values baked into the prompt make it single-use and go stale silently. "
                    "Parameterize them so the prompt can be shared and the value can be checked."
                ),
                evidence=", ".join(repr(s.text) for s in hardcoded[:4]),
                projected_usd_per_1k=Decimal(0),
                formula="quality finding; no direct token saving",
                confidence="heuristic",
                transform="parameterize",
                spans=tuple(hardcoded[:20]),
            )
        )

    findings.sort(key=lambda f: (-f.weighted_usd, f.id))
    del full_input_tokens
    return findings


def clear_score(findings: Sequence[Finding]) -> dict[str, int]:
    """A 1-to-5 score per CLEAR letter, 25 total.

    Displayed, never used for ranking (SPEC.md 7.4). Each letter starts at 5 and loses two
    points for the first rule that fires against it and one for each rule after, floored at 1:
    a letter with one problem is meaningfully worse than a clean one, and a letter with three
    is not fifteen times worse than a letter with one.
    """
    fired: dict[str, int] = dict.fromkeys(CLEAR_LETTERS, 0)
    for finding in findings:
        letter = RULE_LETTER.get(finding.id)
        if letter:
            fired[letter] += 1
    scores = {}
    for letter in CLEAR_LETTERS:
        count = fired[letter]
        scores[letter] = 5 if count == 0 else max(1, 5 - 1 - count)
    return scores


def total_projected_usd(findings: Sequence[Finding]) -> Decimal:
    """Sum of projected savings. Not additive in reality — fixing PL02 subsumes PL01 — so the
    caller must label it as an upper bound, which the UI does."""
    return sum((f.projected_usd_per_1k for f in findings), Decimal(0))
