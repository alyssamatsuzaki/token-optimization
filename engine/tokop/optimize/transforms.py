"""Deterministic prompt rewrites (SPEC.md 5.2, "Apply safe fixes").

Every transform here is a pure function of the prompt text. No model is called, nothing is
paraphrased, and nothing is deleted that a reader would not delete themselves — which is what
makes them safe to apply without reading the diff first, and why the model-assisted rewrite is
a separate, costed action.

Two of them make a prompt **longer**: adding an output contract and adding a cache breakpoint
both add input tokens. They pay for themselves on the output side and the cache side
respectively, so each reports its net input delta next to the saving it buys, and the UI shows
both rather than implying every fix shrinks the prompt.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from tokop.adapters.base import Block, CacheTTL, LLMRequest, Message
from tokop.core.pricing import ModelPrice
from tokop.core.tokenize import BaseCounter, get_base_counter
from tokop.optimize.lint import _COURTESY, DOCUMENT_TOKENS, MILLION, block_is_volatile, jaccard

TransformId = Literal[
    "move_volatile_after_breakpoint",
    "reorder_static_first",
    "add_breakpoint_after_static",
    "move_breakpoint_to_static_end",
    "strip_courtesy",
    "remove_duplicate_instructions",
    "add_output_contract",
]

DUPLICATE_THRESHOLD = 0.8

#: The contract "Apply safe fixes" inserts. Requested through instructions rather than a
#: provider structured-output feature, so the same request works on every provider and can
#: still be pre-warmed (SPEC.md section 6, B2).
OUTPUT_CONTRACT = (
    "Reply with one JSON object and nothing else:\n"
    '{"answer": "...", "evidence": "<verbatim quote from the material above>", '
    '"section": "<section id>"}\n'
    "- answer: the value only, with no units, symbols or sentence around it.\n"
    "- Use no more than 40 words in total."
)


@dataclass
class Change:
    """One edit, with enough context to render a diff."""

    kind: Literal["removed", "added", "moved", "reordered"]
    section: str
    block: int
    before: str
    after: str
    note: str = ""


@dataclass
class TransformResult:
    """A rewritten request, with the arithmetic of what it changed."""

    transform: TransformId
    title: str
    applied: bool
    request: LLMRequest
    changes: list[Change] = field(default_factory=list)
    input_tokens_before: int = 0
    input_tokens_after: int = 0
    cacheable_before: int = 0
    cacheable_after: int = 0
    max_tokens_before: int = 0
    max_tokens_after: int = 0
    #: True when the prefix previously contained content that changed between requests. Such a
    #: prefix is never *read*, whatever its size, so the saving is the whole prefix rather than
    #: the change in its size — which for this fix is slightly negative.
    prefix_was_volatile: bool = False
    note: str = ""

    @property
    def input_delta(self) -> int:
        """Net change in input tokens. Positive means the prompt got longer."""
        return self.input_tokens_after - self.input_tokens_before

    @property
    def cacheable_delta(self) -> int:
        return self.cacheable_after - self.cacheable_before

    def savings_per_1k(self, price: ModelPrice, calls: int = 1000) -> dict[str, Decimal]:
        """What this fix is worth per 1,000 calls, split by where the saving comes from.

        Three separate effects, reported separately because they behave differently: uncached
        input tokens removed, tokens moved into the cache (billed at the read rate instead of
        full input), and the output cap.
        """
        # A prefix that used to change every request was never read, so making it stable turns
        # the *whole* prefix into a cache read. Measuring the size delta instead would report
        # this fix as worth nothing, or less than nothing.
        newly_cacheable = (
            Decimal(self.cacheable_after)
            if self.prefix_was_volatile
            else Decimal(max(0, self.cacheable_delta))
        )
        cache_saving = newly_cacheable * (price.input - price.cache_read) * calls / MILLION
        # Tokens removed outright, excluding anything that merely moved into the cache.
        removed = Decimal(max(0, -self.input_delta))
        input_saving = removed * price.input * calls / MILLION
        added = Decimal(max(0, self.input_delta))
        input_cost = added * price.input * calls / MILLION
        output_saving = (
            Decimal(max(0, self.max_tokens_before - self.max_tokens_after))
            * price.output
            * calls
            / MILLION
        )
        return {
            "cache_usd": cache_saving,
            "input_usd": input_saving,
            "input_cost_usd": input_cost,
            "output_usd_upper_bound": output_saving,
            "net_usd": cache_saving + input_saving + output_saving - input_cost,
        }


def _counter() -> BaseCounter:
    return get_base_counter()


def _tokens(request: LLMRequest, counter: BaseCounter) -> int:
    total = counter.count(request.system_text)
    for message in request.messages:
        total += counter.count(message.text)
    return total


def _cacheable(request: LLMRequest, counter: BaseCounter) -> int:
    prefix = request.static_prefix_text
    return counter.count(prefix) if prefix else 0


def _prefix_is_volatile(request: LLMRequest, counter: BaseCounter) -> bool:
    """Whether anything in front of the breakpoint changes between requests."""
    prefix = request.static_prefix_text
    if not prefix:
        return False
    return any(
        block.text in prefix and block_is_volatile(block.text, counter.count(block.text))
        for block in request.system
    )


def _result(
    transform: TransformId,
    title: str,
    before: LLMRequest,
    after: LLMRequest,
    changes: list[Change],
    note: str = "",
) -> TransformResult:
    counter = _counter()
    return TransformResult(
        transform=transform,
        title=title,
        applied=bool(changes),
        request=after,
        changes=changes,
        input_tokens_before=_tokens(before, counter),
        input_tokens_after=_tokens(after, counter),
        cacheable_before=_cacheable(before, counter),
        cacheable_after=_cacheable(after, counter),
        max_tokens_before=before.max_tokens,
        max_tokens_after=after.max_tokens,
        prefix_was_volatile=_prefix_is_volatile(before, counter),
        note=note,
    )


# --------------------------------------------------------------------------- transforms


def move_volatile_after_breakpoint(request: LLMRequest) -> TransformResult:
    """Move per-request content to the end of the system prompt, behind the breakpoint.

    A timestamp costs a handful of tokens wherever it sits. In front of the breakpoint it also
    costs the entire cache, every call.
    """
    counter = _counter()
    volatile: list[Block] = []
    stable: list[Block] = []
    changes: list[Change] = []
    for index, block in enumerate(request.system):
        if block_is_volatile(block.text, counter.count(block.text)):
            volatile.append(Block(text=block.text))
            changes.append(
                Change(
                    kind="moved",
                    section="system",
                    block=index,
                    before=block.text[:160],
                    after=block.text[:160],
                    note="moved behind the cache breakpoint",
                )
            )
        else:
            stable.append(block)
    prefix = request.static_prefix_text
    already_behind = bool(prefix) and not any(b.text in prefix for b in volatile)
    if not changes or not stable or already_behind:
        return _result(
            "move_volatile_after_breakpoint",
            "Move volatile content behind the breakpoint",
            request,
            request,
            [],
            "no volatile content sits in front of the breakpoint",
        )

    # The breakpoint goes on the last stable block; volatile content follows it.
    rebuilt = [Block(text=b.text) for b in stable]
    ttl: CacheTTL = next((b.cache for b in request.system if b.cache is not None), "5m")
    rebuilt[-1] = Block(text=rebuilt[-1].text, cache=ttl)
    after = request.model_copy(update={"system": [*rebuilt, *volatile]})
    return _result(
        "move_volatile_after_breakpoint",
        "Move volatile content behind the breakpoint",
        request,
        after,
        changes,
    )


def reorder_static_first(request: LLMRequest) -> TransformResult:
    """Move the large static block into the system prompt, ahead of everything that varies.

    The provider matches an exact prefix over tools, then system, then messages. A document
    placed after the question can never be reached by that match, whatever breakpoints are set.
    """
    counter = _counter()
    changes: list[Change] = []
    documents: list[Block] = []
    remaining_user: list[Block] = []
    for index, block in enumerate(request.messages[0].blocks if request.messages else []):
        if counter.count(block.text) > DOCUMENT_TOKENS:
            documents.append(Block(text=block.text))
            changes.append(
                Change(
                    kind="moved",
                    section="user",
                    block=index,
                    before=block.text[:160],
                    after=block.text[:160],
                    note="moved into the system prompt, in front of the breakpoint",
                )
            )
        else:
            remaining_user.append(block)
    if not changes:
        return _result(
            "reorder_static_first",
            "Put the static document first",
            request,
            request,
            [],
            "no large static block sits after variable content",
        )

    system = [Block(text=b.text) for b in request.system]
    stable = [b for b in system if not block_is_volatile(b.text, counter.count(b.text))]
    volatile = [b for b in system if block_is_volatile(b.text, counter.count(b.text))]
    ordered = [*stable, *documents]
    ordered[-1] = Block(text=ordered[-1].text, cache="5m")
    after = request.model_copy(
        update={
            "system": [*ordered, *volatile],
            "messages": [Message(role="user", blocks=remaining_user or [Block(text="")])],
        }
    )
    return _result("reorder_static_first", "Put the static document first", request, after, changes)


def add_breakpoint_after_static(request: LLMRequest) -> TransformResult:
    """Put a cache breakpoint at the end of the last block that is identical across calls."""
    counter = _counter()
    if any(b.cache for b in request.system):
        return _result(
            "add_breakpoint_after_static",
            "Add a cache breakpoint",
            request,
            request,
            [],
            "a breakpoint is already set",
        )
    last_stable = -1
    for index, block in enumerate(request.system):
        if not block_is_volatile(block.text, counter.count(block.text)):
            last_stable = index
    if last_stable < 0:
        return _result(
            "add_breakpoint_after_static",
            "Add a cache breakpoint",
            request,
            request,
            [],
            "every system block varies between calls, so there is nothing to cache",
        )
    system = [
        Block(text=b.text, cache="5m" if i == last_stable else b.cache)
        for i, b in enumerate(request.system)
    ]
    changes = [
        Change(
            kind="added",
            section="system",
            block=last_stable,
            before="",
            after="cache_control: ephemeral (5m)",
            note="breakpoint placed at the end of the static prefix",
        )
    ]
    return _result(
        "add_breakpoint_after_static",
        "Add a cache breakpoint",
        request,
        request.model_copy(update={"system": system}),
        changes,
    )


def move_breakpoint_to_static_end(request: LLMRequest) -> TransformResult:
    """Take the breakpoint off a block that changes and put it on the last stable one."""
    stripped = request.model_copy(update={"system": [Block(text=b.text) for b in request.system]})
    result = add_breakpoint_after_static(stripped)
    return _result(
        "move_breakpoint_to_static_end",
        "Move the breakpoint onto stable content",
        request,
        result.request,
        result.changes or [],
        result.note,
    )


def strip_courtesy(request: LLMRequest) -> TransformResult:
    """Remove politeness, hedges and filler phrases.

    Only the matched phrases are removed, never the clause around them: a rewrite that changed
    meaning would not be safe to apply unread.
    """
    changes: list[Change] = []
    system: list[Block] = []
    for index, block in enumerate(request.system):
        matches = list(_COURTESY.finditer(block.text))
        if not matches:
            system.append(block)
            continue
        cleaned = _COURTESY.sub("", block.text)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n[ \t]*[.,!]\s*", "\n", cleaned)
        cleaned = re.sub(r"^[ \t]*[-*][ \t]*$", "", cleaned, flags=re.M)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        changes.append(
            Change(
                kind="removed",
                section="system",
                block=index,
                before=", ".join(m.group(0) for m in matches[:6]),
                after="",
                note=f"{len(matches)} filler phrase(s) removed",
            )
        )
        system.append(Block(text=cleaned.strip(), cache=block.cache))
    if not changes:
        return _result("strip_courtesy", "Remove courtesy and filler", request, request, [])
    return _result(
        "strip_courtesy",
        "Remove courtesy and filler",
        request,
        request.model_copy(update={"system": system}),
        changes,
    )


def remove_duplicate_instructions(request: LLMRequest) -> TransformResult:
    """Drop the later of two instructions that say the same thing.

    Lexical duplicates only, at the same 0.8 threshold PL07 uses. A paraphrase is left alone:
    deciding that two differently worded rules mean the same thing is a judgement, not a
    deterministic rewrite (DECISIONS.md D18).
    """
    changes: list[Change] = []
    system: list[Block] = []
    for index, block in enumerate(request.system):
        lines = block.text.splitlines()
        kept: list[str] = []
        kept_instructions: list[str] = []
        for line in lines:
            candidate = line.strip().lstrip("-*").strip()
            if len(candidate) < 20:
                kept.append(line)
                continue
            duplicate = next(
                (k for k in kept_instructions if jaccard(candidate, k) >= DUPLICATE_THRESHOLD),
                None,
            )
            if duplicate is not None:
                changes.append(
                    Change(
                        kind="removed",
                        section="system",
                        block=index,
                        before=line.strip(),
                        after="",
                        note=f"duplicates: {duplicate[:60]!r}",
                    )
                )
                continue
            kept_instructions.append(candidate)
            kept.append(line)
        system.append(Block(text="\n".join(kept), cache=block.cache))
    if not changes:
        return _result(
            "remove_duplicate_instructions",
            "Remove duplicated instructions",
            request,
            request,
            [],
            "no lexically duplicated instruction was found",
        )
    return _result(
        "remove_duplicate_instructions",
        "Remove duplicated instructions",
        request,
        request.model_copy(update={"system": system}),
        changes,
    )


def add_output_contract(request: LLMRequest, max_tokens: int = 200) -> TransformResult:
    """Append an output contract and lower the token cap.

    **This fix makes the prompt longer.** It adds about 60 input tokens and buys back far more
    on the output side, where tokens cost five times as much. The diff shows both.
    """
    if OUTPUT_CONTRACT.splitlines()[0] in request.system_text:
        return _result(
            "add_output_contract",
            "Add an output contract",
            request,
            request,
            [],
            "an output contract is already present",
        )
    system = [Block(text=b.text, cache=b.cache) for b in request.system]
    contract = Block(text=OUTPUT_CONTRACT)
    # In front of any breakpoint, so the contract is cached with the rest of the instructions.
    insert_at = next((i + 1 for i, b in enumerate(system) if b.cache is not None), len(system))
    if insert_at <= len(system) and system and system[insert_at - 1].cache is not None:
        cached = system[insert_at - 1]
        system[insert_at - 1] = Block(text=cached.text, cache=None)
        system.insert(insert_at, Block(text=OUTPUT_CONTRACT, cache=cached.cache))
    else:
        system.insert(0, contract)
    changes = [
        Change(
            kind="added",
            section="system",
            block=insert_at,
            before="",
            after=OUTPUT_CONTRACT,
            note=f"max_tokens lowered from {request.max_tokens:,} to {max_tokens:,}",
        )
    ]
    after = request.model_copy(update={"system": system, "max_tokens": max_tokens})
    return _result("add_output_contract", "Add an output contract", request, after, changes)


SAFE_FIXES: dict[str, object] = {
    "move_volatile_after_breakpoint": move_volatile_after_breakpoint,
    "reorder_static_first": reorder_static_first,
    "add_breakpoint_after_static": add_breakpoint_after_static,
    "move_breakpoint_to_static_end": move_breakpoint_to_static_end,
    "strip_courtesy": strip_courtesy,
    "remove_duplicate_instructions": remove_duplicate_instructions,
    "add_output_contract": add_output_contract,
}

#: The order safe fixes are applied in. Structure before words: reordering the prompt changes
#: which tokens are billed at the cache read rate, and that changes what the hygiene fixes are
#: worth, so doing it the other way round would report the wrong numbers.
SAFE_FIX_ORDER: tuple[TransformId, ...] = (
    "reorder_static_first",
    "move_volatile_after_breakpoint",
    "add_breakpoint_after_static",
    "remove_duplicate_instructions",
    "strip_courtesy",
    "add_output_contract",
)


@dataclass
class SafeFixReport:
    """Everything "Apply safe fixes" did, and what it is worth."""

    before: LLMRequest
    after: LLMRequest
    steps: list[TransformResult]
    input_tokens_before: int
    input_tokens_after: int
    cacheable_before: int
    cacheable_after: int
    max_tokens_before: int
    max_tokens_after: int
    prefix_was_volatile: bool = False

    @property
    def applied(self) -> list[TransformResult]:
        return [s for s in self.steps if s.applied]

    @property
    def input_delta(self) -> int:
        return self.input_tokens_after - self.input_tokens_before

    def savings_per_1k(self, price: ModelPrice, calls: int = 1000) -> dict[str, Decimal]:
        newly_cacheable = Decimal(max(0, self.cacheable_after - self.cacheable_before))
        cache_saving = newly_cacheable * (price.input - price.cache_read) * calls / MILLION
        uncached_before = Decimal(max(0, self.input_tokens_before - self.cacheable_before))
        uncached_after = Decimal(max(0, self.input_tokens_after - self.cacheable_after))
        input_delta = uncached_before - uncached_after
        input_saving = input_delta * price.input * calls / MILLION
        output_saving = (
            Decimal(max(0, self.max_tokens_before - self.max_tokens_after))
            * price.output
            * calls
            / MILLION
        )
        return {
            "cache_usd": cache_saving,
            "input_usd": input_saving,
            "output_usd_upper_bound": output_saving,
            "net_usd": cache_saving + input_saving + output_saving,
        }


def apply_safe_fixes(
    request: LLMRequest, order: Sequence[TransformId] = SAFE_FIX_ORDER
) -> SafeFixReport:
    """Apply every deterministic fix that applies, in dependency order."""
    counter = _counter()
    current = request
    steps: list[TransformResult] = []
    for transform_id in order:
        fn = SAFE_FIXES[transform_id]
        result = fn(current)  # type: ignore[operator]
        steps.append(result)
        if result.applied:
            current = result.request
    return SafeFixReport(
        before=request,
        after=current,
        steps=steps,
        input_tokens_before=_tokens(request, counter),
        input_tokens_after=_tokens(current, counter),
        cacheable_before=_cacheable(request, counter),
        cacheable_after=_cacheable(current, counter),
        max_tokens_before=request.max_tokens,
        max_tokens_after=current.max_tokens,
        prefix_was_volatile=_prefix_is_volatile(request, counter),
    )
