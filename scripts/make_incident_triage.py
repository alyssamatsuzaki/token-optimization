#!/usr/bin/env python3
"""Generate the incident-triage workload: a second workload that is not the demo.

Run with `python scripts/make_incident_triage.py`, which rewrites `data/incident-triage/`.

This lives in `scripts/` and not in the engine on purpose. The engine must be able to run this
workload without importing whatever produced it (UPGRADE_V4.md M15, DECISIONS.md D44), and the
surest way to keep that true is for the producer to be a script the engine cannot import.

What it is: a runbook maps alert signatures to an escalation code, an owning team and an
out-of-hours paging rule. Each task shows one alert line and asks one question about it. The
answer mix is deliberately unlike the demo's — codes and team names and yes/no rather than
money and day counts — because a second workload that differs only in its wording tests the
plumbing and nothing else.

Every answer is derivable from the runbook, so a wrong answer is a reasoning failure rather
than missing knowledge. That is the same property the demo's dataset has, and it is what makes
either set a fair test of whether a cheaper model can be trusted.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "incident-triage"

SEED = 20260915
SIZE = 150
CALIBRATION = 50

# (signature, human name, code, team, pages out of hours, typical latency budget in minutes)
SERVICES = [
    ("disk_usage_pct", "disk pressure", "E12", "platform", True, 15),
    ("cert_expiry_days", "certificate expiry", "E31", "security", False, 240),
    ("p99_latency_ms", "latency regression", "E22", "checkout", True, 30),
    ("queue_depth", "queue backlog", "E18", "fulfilment", True, 20),
    ("error_rate_pct", "elevated error rate", "E05", "checkout", True, 10),
    ("replica_lag_s", "replica lag", "E27", "data", False, 60),
    ("oom_kills", "memory exhaustion", "E14", "platform", True, 15),
    ("failed_logins", "credential stuffing", "E33", "security", True, 10),
    ("cache_hit_pct", "cache degradation", "E20", "platform", False, 120),
    ("webhook_retries", "webhook backoff", "E09", "integrations", False, 180),
]

SEVERITY = [
    ("critical", 1, "page immediately, no batching"),
    ("major", 2, "page during the working day, batch out of hours"),
    ("minor", 3, "ticket only, no page"),
]


def runbook() -> str:
    lines = [
        "# Incident runbook",
        "",
        "Every alert carries a signature. Look the signature up here to find its escalation",
        "code, the team that owns it, and whether it pages outside working hours.",
        "",
        "## Signatures",
        "",
    ]
    for signature, name, code, team, pages, budget in SERVICES:
        lines.append(
            f"- `{signature}` ({name}) has escalation code {code}, is owned by the {team} team, "
            f"{'pages' if pages else 'does not page'} outside working hours, and must be "
            f"acknowledged within {budget} minutes."
        )
    lines += [
        "",
        "## Severity bands",
        "",
    ]
    for label, tier, rule in SEVERITY:
        lines.append(f"- A {label} alert is severity {tier}: {rule}.")
    lines += [
        "",
        "## Overrides",
        "",
        "- An alert already covered by an open incident is folded into that incident and does",
        "  not page again, whatever its signature says.",
        "- During a declared change freeze every signature owned by the platform team pages,",
        "  including the ones that normally do not.",
        "",
    ]
    return "\n".join(lines) + "\n"


def task_id(prefix: str, payload: str) -> str:
    digest = hashlib.sha256(payload.encode()).hexdigest()[:10]
    return f"{prefix}-{digest}"


def build() -> list[dict[str, object]]:
    rng = random.Random(SEED)
    items: list[dict[str, object]] = []
    seen: set[str] = set()

    while len(items) < SIZE:
        signature, name, code, team, pages, budget = rng.choice(SERVICES)
        value = rng.randint(2, 98)
        alert = f"ALERT {signature}={value} host=web-{rng.randint(1, 40):02d}"
        shape = rng.choice(["code", "owner", "pages", "freeze", "folded"])

        if shape == "code":
            question = f"{alert}. Which escalation code does the runbook give this signature?"
            gold, answer_type, question_type = code, "enum", "lookup"
        elif shape == "owner":
            question = f"{alert}. Which team owns this signature?"
            gold, answer_type, question_type = team, "string", "lookup"
        elif shape == "pages":
            question = f"{alert}. Does this page outside working hours? Answer yes or no."
            gold = "yes" if pages else "no"
            answer_type, question_type = "yes_no", "rule"
        elif shape == "freeze":
            question = (
                f"{alert}. A change freeze is in effect. Does this page outside working hours? "
                "Answer yes or no."
            )
            # The freeze override makes every platform signature page, including the quiet ones.
            gold = "yes" if (pages or team == "platform") else "no"
            answer_type, question_type = "yes_no", "override"
        else:
            question = (
                f"{alert}. An incident is already open covering this signature. Does this page "
                "outside working hours? Answer yes or no."
            )
            # The folded-into-an-open-incident override beats the signature's own rule.
            gold, answer_type, question_type = "no", "yes_no", "override"

        identifier = task_id(question_type[:2], question)
        if identifier in seen:
            continue
        seen.add(identifier)
        items.append(
            {
                "id": identifier,
                "question": question,
                "gold": gold,
                "answer_type": answer_type,
                "question_type": question_type,
                "template_id": f"tpl_{shape}",
                "sections": ["runbook"],
                "aliases": [],
            }
        )

    # Stratified by question type, so both splits see the same mix of shapes.
    items.sort(key=lambda row: str(row["id"]))
    by_type: dict[str, list[dict[str, object]]] = {}
    for item in items:
        by_type.setdefault(str(item["question_type"]), []).append(item)
    split_rng = random.Random(SEED + 1)
    calibration: list[str] = []
    fraction = CALIBRATION / len(items)
    for question_type in sorted(by_type):
        bucket = list(by_type[question_type])
        split_rng.shuffle(bucket)
        calibration += [str(row["id"]) for row in bucket[: round(len(bucket) * fraction)]]
    chosen = set(calibration)
    for item in items:
        item["split"] = "calibration" if item["id"] in chosen else "test"
    return items


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items = build()
    (OUT / "runbook.md").write_text(runbook())
    (OUT / "dataset.jsonl").write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in items) + "\n"
    )
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item["question_type"])] = counts.get(str(item["question_type"]), 0) + 1
    calibration = sum(1 for item in items if item["split"] == "calibration")
    print(f"{len(items)} items ({calibration} calibration, {len(items) - calibration} test)")
    for question_type in sorted(counts):
        print(f"  {question_type:10} {counts[question_type]:4}")


if __name__ == "__main__":
    main()
