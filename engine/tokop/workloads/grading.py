"""Checkers: do these answers match the gold ones (SPEC.md section 6)?

Checkers live here and scorers live in ``optimize/scorers.py``, in separate modules with
separate tests, because they must never be confused. A **checker** sees the gold answer and
decides whether a task succeeded. A **scorer** never sees gold and guesses whether an answer is
right, so that it can be used at run time to route. A scorer that could reach a gold answer
would be measuring nothing, and ``tests/test_scorer_isolation.py`` fails if one ever can.

Grading rules:

* numbers are parsed and compared with a tolerance of 0.01;
* enums and yes/no answers use normalized exact match;
* short strings use normalized exact match plus an alias list;
* **output that cannot be parsed is a failure** — not an error, not a skip. A pipeline that
  returns something unreadable has not answered the question, and pretending otherwise would
  flatter exactly the kind of cheap model this product exists to evaluate.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

#: The kinds of answer the grader knows how to check. It lives here rather than in a generator
#: because it is the grader's vocabulary: `grade()` dispatches on it, and a workload that writes
#: one of these into its dataset is naming a checker, not describing its own domain.
AnswerType = Literal["number", "money", "yes_no", "enum", "string"]

NUMBER_TOLERANCE = Decimal("0.01")

_YES = {"yes", "y", "true", "yep", "correct", "permitted", "allowed"}
_NO = {"no", "n", "false", "nope", "incorrect", "not permitted", "not allowed", "refused"}

#: B0 asks for this; B2 asks for JSON. Both are parsed, so the two pipelines are graded the
#: same way and a difference in accuracy cannot come from the grader preferring one format.
_FINAL_ANSWER = re.compile(r"final\s*answer\s*[:\-]\s*(.+)", re.IGNORECASE)
_NUMBER = re.compile(r"-?\$?\s*\d[\d,]*\.?\d*\s*%?")


@dataclass(frozen=True)
class Grade:
    """The outcome of grading one answer."""

    correct: bool
    parsed: str | None
    reason: str

    @property
    def parse_failed(self) -> bool:
        return self.parsed is None

    def as_dict(self) -> dict[str, Any]:
        return {"correct": self.correct, "parsed": self.parsed, "reason": self.reason}


def normalize(text: str) -> str:
    """Case-fold, strip accents and punctuation, and collapse whitespace."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    folded = folded.casefold().strip()
    folded = re.sub(r"[^\w\s.%-]", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def extract_json_answer(output: str) -> dict[str, Any] | None:
    """Pull the JSON object B2 asks for, tolerating a fenced code block around it.

    Tolerant of the wrapper, strict about the content: a model that returns prose instead of
    JSON has not met the output contract and the caller treats that as a parse failure.
    """
    text = output.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_answer(output: str) -> str | None:
    """The answer a pipeline produced, whichever contract it was asked to follow.

    Tries the JSON contract first, then the ``Final answer:`` line, then — only if the whole
    output is short enough to be an answer rather than an essay — the output itself.
    """
    if not output or not output.strip():
        return None

    payload = extract_json_answer(output)
    if payload is not None:
        if "answer" not in payload:
            # The model returned JSON but not the contract's JSON. Falling through to the
            # bare-answer path would find a number somewhere in the object and grade a
            # contract violation as a success.
            return None
        value = payload["answer"]
        return str(value).strip() if value is not None else None  # noqa: RUF100

    matches: list[str] = _FINAL_ANSWER.findall(output)
    if matches:
        # The last one: a model that restates the line has refined its answer, not replaced it.
        return matches[-1].strip().rstrip(".").strip()

    stripped = output.strip()
    if len(stripped) <= 80 and "\n" not in stripped:
        return stripped
    return None


def parse_number(text: str) -> Decimal | None:
    """First number in the text, ignoring currency symbols, commas and a trailing percent."""
    match = _NUMBER.search(text)
    if not match:
        return None
    cleaned = match.group(0).replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_yes_no(text: str) -> str | None:
    # normalize() keeps "." because it is significant inside numbers, so a sentence-final full
    # stop has to come off here or "Yes." grades as unparseable — a grader bug that would make
    # every pipeline look worse than it is.
    normalized = normalize(text).strip(" .")
    if not normalized:
        return None
    if normalized in _YES:
        return "yes"
    if normalized in _NO:
        return "no"
    first = normalized.split()[0].strip(".")
    if first in _YES:
        return "yes"
    if first in _NO:
        return "no"
    # "not allowed" and friends span two words.
    for phrase in _NO:
        if normalized.startswith(phrase):
            return "no"
    return None


def check_number(answer: str, gold: str) -> Grade:
    value = parse_number(answer)
    if value is None:
        return Grade(False, None, f"no number could be parsed from {answer!r}")
    expected = parse_number(gold)
    if expected is None:
        raise ValueError(f"gold answer {gold!r} is not a number; the dataset is wrong")
    if abs(value - expected) <= NUMBER_TOLERANCE:
        return Grade(True, str(value), f"{value} is within {NUMBER_TOLERANCE} of {expected}")
    return Grade(False, str(value), f"{value} is not within {NUMBER_TOLERANCE} of {expected}")


def check_yes_no(answer: str, gold: str) -> Grade:
    value = parse_yes_no(answer)
    if value is None:
        return Grade(False, None, f"{answer!r} is neither yes nor no")
    expected = parse_yes_no(gold)
    if expected is None:
        raise ValueError(f"gold answer {gold!r} is not a yes/no answer; the dataset is wrong")
    return Grade(value == expected, value, f"parsed {value!r}, gold {expected!r}")


def check_exact(answer: str, gold: str, aliases: tuple[str, ...] = ()) -> Grade:
    value = normalize(answer)
    if not value:
        return Grade(False, None, "the answer was empty after normalization")
    accepted = {normalize(gold), *(normalize(a) for a in aliases)}
    if value in accepted:
        return Grade(True, value, f"{value!r} matched gold or an alias")
    # An answer that wraps the gold value in a short sentence still answered the question.
    if len(value) <= 60:
        for candidate in accepted:
            if candidate and re.search(rf"\b{re.escape(candidate)}\b", value):
                return Grade(True, value, f"{candidate!r} found in a short answer")
    return Grade(False, value, f"{value!r} is not {sorted(accepted)}")


def grade(
    output: str,
    gold: str,
    answer_type: str,
    aliases: tuple[str, ...] = (),
) -> Grade:
    """Grade one pipeline output against its gold answer."""
    answer = extract_answer(output)
    if answer is None:
        return Grade(
            False,
            None,
            "no answer could be parsed from the output; an unparseable answer is a failure",
        )
    if answer_type in ("number", "money"):
        return check_number(answer, gold)
    if answer_type == "yes_no":
        return check_yes_no(answer, gold)
    if answer_type in ("enum", "string"):
        return check_exact(answer, gold, aliases)
    raise ValueError(f"unknown answer type {answer_type!r}")


#: A bare number, once currency, grouping commas and a trailing percent are stripped. Used to
#: decide whether two *candidate* answers should be compared numerically. Deliberately a
#: fullmatch: `parse_number` finds a number anywhere, so it reads "tier_1" as 1 and would merge
#: it with the answer "1".
_BARE_NUMBER = re.compile(r"-?\$?\s*\d[\d,]*\.?\d*\s*%?")


def _looks_numeric(text: str) -> bool:
    return bool(_BARE_NUMBER.fullmatch(text.strip()))


def answers_equivalent(left: str, right: str) -> bool:
    """Whether two model outputs give the same answer (SPEC.md 7.5, the sampling scorer).

    **Both arguments are candidate answers. Neither is a reference.** That is what makes this
    usable by a scorer: it is a relation between two things the model said, and it holds no
    opinion about which — if either — is right. The checker's comparisons take a candidate and a
    gold answer and live above; this one is exported for injection precisely so that
    ``optimize/scorers.py`` never has to import from this module's gold-aware half.

    It reuses the checker's parsing and normalization so that two answers a grader would score
    identically also group together here. Shape is inferred from the answers themselves rather
    than from the task, because the task's answer type is one of the things a scorer is
    structurally forbidden to see.

    An output that cannot be parsed is equivalent to nothing, **including another unparseable
    output**. Two failures are not evidence of agreement, and treating them as agreement would
    make a tier that reliably emits garbage look maximally self-consistent.
    """
    left_answer = extract_answer(left)
    right_answer = extract_answer(right)
    if left_answer is None or right_answer is None:
        return False

    if _looks_numeric(left_answer) and _looks_numeric(right_answer):
        left_number = parse_number(left_answer)
        right_number = parse_number(right_answer)
        if left_number is not None and right_number is not None:
            return abs(left_number - right_number) <= NUMBER_TOLERANCE

    left_bool = parse_yes_no(left_answer)
    right_bool = parse_yes_no(right_answer)
    if left_bool is not None and right_bool is not None:
        return left_bool == right_bool

    left_text = normalize(left_answer)
    right_text = normalize(right_answer)
    return bool(left_text) and left_text == right_text
