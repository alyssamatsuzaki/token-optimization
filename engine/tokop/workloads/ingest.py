"""Read someone else's tasks into a workload (UPGRADE_V4.md M15).

SPEC.md section 3 promised "the tooling to record any YAML-defined workload whose JSONL or CSV
dataset has gold answers" and it was never built, which is why `tokop prove --workload` refused
everything but the demo by name until M15. This is that tooling, plus OpenTelemetry GenAI spans,
which are the shape production traffic actually arrives in.

The three formats differ in one way that matters more than their syntax:

* **JSONL and CSV** are exports of a set somebody has already curated, so they usually carry an
  answer key. A workload built from them can be graded against gold.
* **OpenTelemetry spans** are traffic. A span records what was asked and what the model said —
  it does not record what the *right* answer was, and no amount of parsing invents one. A
  workload ingested from spans has tasks and no labels, so it needs the judged path (M9) or a
  round of labelling before anything can be proven on it. `ingest` says so rather than writing
  an empty `gold` column and letting a grader discover it later.

The semantic conventions for GenAI spans are still moving, so the attribute names read here are
pinned and named in the ingest report rather than being guessed at run time.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tokop.workloads.grading import AnswerType
from tokop.workloads.item import IngestedItem
from tokop.workloads.spec import WorkloadError

Format = str
FORMATS: tuple[str, ...] = ("jsonl", "csv", "otel")

#: The version of the OpenTelemetry GenAI semantic conventions these attribute names come from.
#: Recorded in the ingest report because the convention is not stable and a file read under one
#: version is not a file read under another.
OTEL_CONVENTION = "opentelemetry semantic conventions 1.27.0 (gen_ai.*)"

#: Column names Tokop will recognise without being told, in preference order.
DEFAULT_MAPPING: dict[str, tuple[str, ...]] = {
    "id": ("id", "task_id", "uuid", "key"),
    "question": ("question", "prompt", "input", "text", "query"),
    "gold": ("gold", "answer", "expected", "label", "target", "output"),
    "answer_type": ("answer_type", "type"),
    "question_type": ("question_type", "category", "intent", "task_type"),
}

#: What the grader is asked to check when a file does not say. `string` is the widest checker:
#: it compares normalised text, so it never reads a wrong answer as right the way a numeric
#: comparison can when a file's "42" is really a category label.
FALLBACK_ANSWER_TYPE: AnswerType = "string"


@dataclass(frozen=True)
class IngestReport:
    """What was read, from where, and what is missing before it can be proven on."""

    source: Path
    fmt: str
    items: int
    calibration: int
    test: int
    answer_types: dict[str, int]
    mapping: dict[str, str]
    has_gold: bool
    convention: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def lines(self) -> list[str]:
        out = [
            f"read {self.items:,} tasks from {self.source} ({self.fmt})",
            f"  split           {self.calibration:,} calibration, {self.test:,} test",
            "  answer types    "
            + (", ".join(f"{k} {v}" for k, v in sorted(self.answer_types.items())) or "none"),
            "  fields          "
            + (", ".join(f"{k} <- {v}" for k, v in sorted(self.mapping.items())) or "none mapped"),
        ]
        if self.convention:
            out.append(f"  convention      {self.convention}")
        for warning in self.warnings:
            out.append(f"  ! {warning}")
        return out


def _pick(row: dict[str, Any], names: tuple[str, ...], mapping: dict[str, str], field_: str) -> Any:
    """The first column that carries this field, remembering which one it was."""
    if field_ in mapping:
        return row.get(mapping[field_])
    for name in names:
        if name in row and row[name] not in (None, ""):
            mapping[field_] = name
            return row[name]
    return None


def _to_item(row: dict[str, Any], index: int, mapping: dict[str, str]) -> IngestedItem:
    question = _pick(row, DEFAULT_MAPPING["question"], mapping, "question")
    if not question:
        raise WorkloadError(
            f"row {index} has no question. Tokop looked for "
            f"{', '.join(DEFAULT_MAPPING['question'])}; name the column with --map question=<col>."
        )
    identifier = _pick(row, DEFAULT_MAPPING["id"], mapping, "id") or f"task-{index:05d}"
    gold = _pick(row, DEFAULT_MAPPING["gold"], mapping, "gold")
    answer_type = _pick(row, DEFAULT_MAPPING["answer_type"], mapping, "answer_type")
    question_type = _pick(row, DEFAULT_MAPPING["question_type"], mapping, "question_type")
    known = set(mapping.values())
    return IngestedItem(
        id=str(identifier),
        question=str(question),
        gold="" if gold is None else str(gold),
        answer_type=str(answer_type or FALLBACK_ANSWER_TYPE),  # type: ignore[arg-type]
        question_type=str(question_type or "unclassified"),
        extra={k: v for k, v in row.items() if k not in known},
    )


def read_jsonl(path: Path, mapping: dict[str, str]) -> list[IngestedItem]:
    items: list[IngestedItem] = []
    for index, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkloadError(f"{path}:{index} is not valid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise WorkloadError(f"{path}:{index} is not a JSON object")
        items.append(_to_item(row, index, mapping))
    return items


def read_csv(path: Path, mapping: dict[str, str]) -> list[IngestedItem]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise WorkloadError(f"{path} has no rows")
    return [_to_item(dict(row), index, mapping) for index, row in enumerate(rows, start=1)]


def _span_attributes(span: dict[str, Any]) -> dict[str, Any]:
    """Attributes as a flat mapping, from either the list or the object encoding."""
    raw = span.get("attributes")
    if isinstance(raw, dict):
        return raw
    flat: dict[str, Any] = {}
    for entry in raw or []:
        if not isinstance(entry, dict) or "key" not in entry:
            continue
        value = entry.get("value")
        if isinstance(value, dict):
            value = next(iter(value.values()), None)
        flat[str(entry["key"])] = value
    return flat


def read_otel(path: Path, mapping: dict[str, str]) -> list[IngestedItem]:
    """Tasks from GenAI spans. They carry no answer key, and none is invented."""
    text = path.read_text()
    spans: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkloadError(f"{path}:{index} is not valid JSON: {exc}") from exc
        spans.extend(payload if isinstance(payload, list) else [payload])

    items: list[IngestedItem] = []
    for index, span in enumerate(spans, start=1):
        attributes = _span_attributes(span)
        question = (
            attributes.get("gen_ai.prompt")
            or attributes.get("gen_ai.input.messages")
            or attributes.get("gen_ai.request.prompt")
        )
        if not question:
            continue
        mapping.setdefault("question", "gen_ai.prompt")
        identifier = span.get("spanId") or span.get("span_id") or f"span-{index:05d}"
        items.append(
            IngestedItem(
                id=str(identifier),
                question=str(question),
                # A span records what the model said, never what it should have said. Writing
                # the completion here would turn "what we got" into "what is correct", which is
                # the one substitution that makes every downstream number meaningless.
                gold="",
                answer_type=FALLBACK_ANSWER_TYPE,
                question_type=str(attributes.get("gen_ai.operation.name") or "unclassified"),
                extra={
                    key: value
                    for key, value in attributes.items()
                    if key.startswith("gen_ai.") and key != "gen_ai.prompt"
                },
            )
        )
    if not items:
        raise WorkloadError(
            f"{path} holds no GenAI spans Tokop could read. It looks for `gen_ai.prompt`, "
            f"`gen_ai.input.messages` or `gen_ai.request.prompt` under {OTEL_CONVENTION}."
        )
    return items


READERS = {"jsonl": read_jsonl, "csv": read_csv, "otel": read_otel}


def split_items(
    items: list[IngestedItem], *, calibration_size: int, seed: int
) -> list[IngestedItem]:
    """Assign splits, stratified by question type, once and for all.

    Written into the dataset rather than recomputed on every load: a task that moved between
    splits would invalidate every result measured before the move (DECISIONS.md D44.4).
    """
    rng = random.Random(seed)
    by_type: dict[str, list[IngestedItem]] = {}
    for item in sorted(items, key=lambda i: i.id):
        by_type.setdefault(item.question_type, []).append(item)
    fraction = calibration_size / len(items) if items else 0.0
    calibration: set[str] = set()
    leftovers: list[str] = []
    for question_type in sorted(by_type):
        bucket = list(by_type[question_type])
        rng.shuffle(bucket)
        take = round(len(bucket) * fraction)
        calibration.update(item.id for item in bucket[:take])
        leftovers += [item.id for item in bucket[take:]]
    # Per-stratum rounding can leave the calibration split short, and on a small set it can
    # leave it empty — a split with nothing to calibrate on is not a split. Top up in a fixed
    # order so the result is reproducible.
    for identifier in leftovers:
        if len(calibration) >= calibration_size:
            break
        calibration.add(identifier)
    return [
        IngestedItem(
            **{
                **{f: getattr(item, f) for f in item.to_dict() if f != "split" and f != "extra"},
                "extra": item.extra,
                "split": "calibration" if item.id in calibration else "test",
            }
        )
        for item in sorted(items, key=lambda i: i.id)
    ]


def ingest(
    source: Path,
    fmt: str,
    out: Path,
    *,
    mapping: dict[str, str] | None = None,
    calibration_size: int | None = None,
    seed: int = 20260915,
) -> IngestReport:
    """Read a file into `out/dataset.jsonl`, and say what it can and cannot support."""
    if fmt not in READERS:
        raise WorkloadError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")
    if not source.exists():
        raise WorkloadError(f"no such file: {source}")

    resolved: dict[str, str] = dict(mapping or {})
    items = READERS[fmt](source, resolved)
    if not items:
        raise WorkloadError(f"{source} produced no tasks")

    seen: set[str] = set()
    duplicates = 0
    unique: list[IngestedItem] = []
    for item in items:
        if item.id in seen:
            duplicates += 1
            continue
        seen.add(item.id)
        unique.append(item)

    target = calibration_size if calibration_size is not None else max(1, len(unique) // 3)
    split = split_items(unique, calibration_size=target, seed=seed)

    out.mkdir(parents=True, exist_ok=True)
    (out / "dataset.jsonl").write_text(
        "\n".join(json.dumps(item.to_dict(), sort_keys=True) for item in split) + "\n"
    )

    without_gold = [item for item in split if not item.gold]
    warnings: list[str] = []
    if duplicates:
        warnings.append(f"{duplicates} rows repeated an id already seen and were dropped")
    if without_gold:
        warnings.append(
            f"{len(without_gold):,} of {len(split):,} tasks have no answer key. "
            "Nothing can be graded against gold until they do: set `grading: judged` and a "
            "`judge:` block, or label them. Tokop will not treat a recorded completion as the "
            "correct answer."
        )
    counts: dict[str, int] = {}
    for item in split:
        counts[item.answer_type] = counts.get(item.answer_type, 0) + 1
    return IngestReport(
        source=source,
        fmt=fmt,
        items=len(split),
        calibration=sum(1 for item in split if item.split == "calibration"),
        test=sum(1 for item in split if item.split != "calibration"),
        answer_types=counts,
        mapping=resolved,
        has_gold=not without_gold,
        convention=OTEL_CONVENTION if fmt == "otel" else "",
        warnings=tuple(warnings),
    )
