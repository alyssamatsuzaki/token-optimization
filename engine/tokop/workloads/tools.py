"""The tools a graph step may call, and the refusal for every tool Tokop has not implemented.

A graph step of kind ``tool`` or ``retrieve`` is the part of an agent that is not a model call:
a lookup, a search, a fetch. It costs no tokens of its own, which is exactly why it is easy to
add and easy to leave in — and why the interesting question about one is not what it costs but
what it *puts into the next step's prompt*. That is where an agent's money goes, and it is what
:mod:`tokop.optimize.graph` exists to let the optimizer reason about.

Two rules shape this module.

**Tokop does not invent tools.** A workload naming a tool with no implementation here is refused
by name, with the implemented ones listed. Standing in for an unknown tool with a plausible
string would produce a graph that runs, a number for every step, and no way to tell which of
those numbers described anything.

**A tool is a program, not a model.** ``grounding-lookup-v1`` is keyword retrieval over the
document the workload already ships: sections split on their headings, scored by how many of the
query's significant words they contain. It has a real failure mode — a section whose wording
does not overlap the question is missed — and that failure is the point. An agent that retrieves
the wrong paragraph and then answers from it confidently is the shape being measured, and a
retriever that could not miss would measure nothing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

from tokop.workloads.spec import WorkloadError

#: Words carried by every question in every workload, which therefore separate no section from
#: any other. Kept short and general: a stopword list tuned to one workload's phrasing would be
#: that workload's retriever rather than a neutral one.
_STOPWORDS = """
a an and are as at be by do does for from has have how if in into is it its of on or should
that the their them there these this to was were what when where which who why will with
would you your
"""

STOPWORDS = frozenset(_STOPWORDS.split())

#: Sections a lookup returns. More than this and the "retrieval" is the document; fewer and a
#: question spanning two entries cannot be answered at all.
#:
#: Six, not three. Three was measured against a document of fifty-two near-identical entries and
#: missed the governing section on a third of the questions — not because the section scored
#: badly but because a question asked in the reader's words scores well against the policy
#: section that taught them those words. A retriever that misses that often would decide the
#: milestone's comparison by being a straw man, and the honest way to give an agent its best
#: case is to let it fetch more. It is also the direction that costs it money, which is the
#: trade-off M16 exists to measure rather than to assume.
DEFAULT_SECTIONS = 6

WORD = re.compile(r"[a-z0-9][a-z0-9_.\-]*")
HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)


class ToolError(WorkloadError):
    """A tool step named something this build cannot run, or ran and could not answer."""


@dataclass(frozen=True)
class ToolContext:
    """What a tool is allowed to see. Deliberately not the task's gold answer.

    A retriever handed the answer would retrieve the section containing it every time, and the
    agent built on it would look like a good agent. Everything downstream of that is a number
    about a leak.
    """

    grounding: str
    question: str


@dataclass(frozen=True)
class ToolResult:
    """What one tool call returned, and enough about it to recognise the same call again."""

    tool: str
    query: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


ToolFn = Callable[[str, ToolContext], str]


@dataclass(frozen=True)
class Tool:
    """One named, deterministic operation a graph step can perform."""

    name: str
    description: str
    run: ToolFn


def split_sections(document: str) -> list[str]:
    """The document as its markdown sections, heading included.

    A document with no headings is one section, which is the honest reading: nothing in it says
    where one part ends and the next begins, so a retriever has nothing to choose between.
    """
    if not document.strip():
        return []
    starts = [match.start() for match in HEADING.finditer(document)]
    if not starts:
        return [document.strip()]
    if starts[0] > 0:
        starts.insert(0, 0)
    bounds = [*starts, len(document)]
    sections = [document[a:b].strip() for a, b in pairwise(bounds)]
    return [section for section in sections if section]


def stem(word: str) -> str:
    """A plural and its singular are the same word.

    One rule, applied to words long enough for it to be a plural rather than a spelling. It is
    not a stemmer and does not try to be; it is here because a catalogue entry says "Pages
    outside working hours" and a question asks "does it page", and a retriever that treats those
    as unrelated words never returns the entry that answers the question.
    """
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def significant_words(text: str) -> list[str]:
    """The words of a query that could distinguish one section from another."""
    return [stem(word) for word in WORD.findall(text.casefold()) if word not in STOPWORDS]


def grounding_lookup(query: str, context: ToolContext, *, sections: int = DEFAULT_SECTIONS) -> str:
    """Return the sections of the grounding document that best match ``query``.

    Scoring is inverse document frequency: a shared word is worth ``log(N / sections holding
    it)``, so a service name that appears in three sections outweighs "alert", which appears in
    all of them. Counting shared words instead — the first version of this — made every question
    about a rare thing lose to whichever section happened to use the most common vocabulary, and
    a retriever that bad would have decided the milestone's comparison by being a straw man.

    Ties go to the shorter section, so "matches because it is long" does not beat "matches
    because it is about this". A query that matches nothing returns nothing, and the step that
    consumes it gets an empty context rather than an arbitrary section — a retriever that always
    returns something is a retriever whose failures are invisible.
    """
    candidates = split_sections(context.grounding)
    if not candidates:
        return ""
    wanted = set(significant_words(query))
    if not wanted:
        return ""
    words = [set(significant_words(section)) for section in candidates]
    total = len(candidates)
    weight = {
        word: math.log(total / sum(1 for present in words if word in present))
        for word in wanted
        if any(word in present for present in words)
    }
    scored = []
    for index, (section, present) in enumerate(zip(candidates, words, strict=True)):
        score = sum(weight[word] for word in wanted & present if word in weight)
        if score > 0:
            # Rounded before sorting: two sections whose scores differ in the sixteenth decimal
            # are the same match, and letting float noise order them would make the retrieval
            # depend on the platform's arithmetic rather than on the document.
            scored.append((-round(score, 9), len(section), index, section))
    scored.sort()
    return "\n\n".join(section for *_, section in scored[:sections])


_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool) -> None:
    if tool.name in _REGISTRY:
        raise ToolError(f"tool {tool.name!r} is already registered")
    _REGISTRY[tool.name] = tool


def get_tool(name: str) -> Tool:
    """The tool by name, or a refusal naming the ones that exist."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ToolError(
            f"no tool {name!r}. This build implements {', '.join(sorted(_REGISTRY)) or 'none'}. "
            "Tokop does not stand in for a tool it has not implemented: a graph whose steps "
            "return plausible strings produces a number for every step and evidence for none."
        ) from None


def registered_tools() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


register_tool(
    Tool(
        name="grounding-lookup-v1",
        description=(
            "Keyword retrieval over the workload's grounding document. Returns the sections "
            "whose text shares the most significant words with the query."
        ),
        run=grounding_lookup,
    )
)
