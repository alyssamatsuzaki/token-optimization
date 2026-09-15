#!/usr/bin/env python3
"""Generate the catalogue-agent workload: the one that is a graph (UPGRADE_V4.md M16).

Run with `python scripts/make_catalogue_agent.py`, which rewrites `data/catalogue-agent/`.

Like `make_incident_triage.py` this lives in `scripts/` and not in the engine, so the engine
cannot import whatever produced the tasks (DECISIONS.md D44).

**Why this workload exists.** Every workload before it is one model call per task, which can be
made cheaper in exactly two ways: run a smaller model, or write a shorter prompt. The workloads
where the money actually goes are agents — retrieve, draft, check, revise — and the useful
proposal there is usually to delete a step. This is the smallest honest workload of that shape:
a service catalogue too large to want to paste, a question answerable from one or two of its
sections, and a retrieval step that looks like the obvious way to avoid pasting it.

**The catalogue is sized deliberately.** It has to clear the cheap model's 4,096-token cache
minimum with headroom under the real tokenizer as well as under this build's approximation
(DECISIONS.md D26), because the interesting comparison is between per-task retrieved context,
which no cache can hold, and a whole document that a cache can. A catalogue that could not be
cached would decide that comparison by being too small rather than on the merits.

Every answer is derivable from the catalogue, so a wrong answer is a reasoning failure and not
missing knowledge — the same property the other two datasets have.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "catalogue-agent"

SEED = 20260916
SIZE = 150
CALIBRATION = 50

TEAMS = [
    "payments",
    "checkout",
    "fulfilment",
    "identity",
    "platform",
    "data",
    "growth",
    "support-tooling",
]

TIERS = ["platform", "product", "internal"]

REGIONS = ["eu-west-1", "us-east-1", "ap-southeast-2", "us-west-2"]

FAILURES = [
    "connection pool exhaustion under retry storms",
    "a slow downstream turning into unbounded queue growth",
    "cache stampede after a deploy invalidates every key at once",
    "clock skew rejecting otherwise valid tokens",
    "a partial region failover leaving writes split across two primaries",
    "back-pressure arriving as timeouts rather than as refusals",
    "schema drift between the writer and the reader",
    "a hot partition serialising what should be parallel work",
]

NOUNS = [
    "gateway",
    "writer",
    "router",
    "scorer",
    "indexer",
    "resolver",
    "collector",
    "broker",
    "sweeper",
    "reconciler",
    "dispatcher",
    "validator",
]

DOMAINS = [
    "payments",
    "checkout",
    "ledger",
    "fraud",
    "catalogue",
    "session",
    "invoice",
    "shipment",
    "identity",
    "billing",
    "refund",
    "pricing",
    "inventory",
    "notification",
    "audit",
    "search",
]


def services() -> list[dict[str, object]]:
    """The catalogue, built deterministically so the committed file is reproducible."""
    rng = random.Random(SEED)
    names: list[str] = []
    for domain in DOMAINS:
        for noun in rng.sample(NOUNS, 3):
            names.append(f"{domain}-{noun}")
    names = sorted(set(names))[:48]
    rows: list[dict[str, object]] = []
    for index, name in enumerate(names):
        tier = TIERS[index % len(TIERS)]
        rows.append(
            {
                "name": name,
                "team": TEAMS[index % len(TEAMS)],
                "code": f"E{10 + (index * 7) % 80:02d}",
                "tier": tier,
                # Product and internal services mostly do not page at night; platform ones do.
                # The change-freeze override exists precisely to flip the ones that do not.
                "pages": tier == "platform" or index % 5 == 0,
                "ack": [10, 15, 30, 60, 120, 240][index % 6],
                "regions": rng.sample(REGIONS, 2),
                "depends": rng.sample([n for n in names if n != name], 2),
                "failure": FAILURES[index % len(FAILURES)],
            }
        )
    return rows


POLICIES = [
    (
        "Escalation ladder",
        """Every alert has an escalation code. The code selects the ladder: the owning team is
paged first, the tier duty engineer after the acknowledgement window has passed, and the
incident commander after a further thirty minutes with no acknowledgement. An alert that is
acknowledged but not resolved does not climb the ladder; it stays with the owning team.""",
    ),
    (
        "Change freeze",
        """A change freeze is declared before a release train and lifted after it. While one is
in effect, every service in the platform tier pages outside working hours regardless of its own
entry, because a freeze means nobody is standing by to pick up a ticket in the morning. Services
in other tiers keep the paging rule written in their own entry. A freeze changes nothing about
escalation codes, owning teams or acknowledgement windows.""",
    ),
    (
        "Incident folding",
        """An alert that names a service already listed on an open incident is folded into that
incident and does not page again, whatever its own entry says. Folding applies for as long as
the incident is open and stops the moment it is resolved. A folded alert is still recorded
against the owning team.""",
    ),
    (
        "Working hours",
        """Working hours are 09:00 to 18:00 local time for the owning team, Monday to Friday,
excluding public holidays in the team's own location. Anything else is outside working hours.
An alert raised at 17:59 is inside working hours; one raised at 18:01 is not.""",
    ),
]


def catalogue(rows: list[dict[str, object]]) -> str:
    parts = [
        "# Service catalogue",
        "",
        "This catalogue is the source of truth for who owns a service, how an alert on it",
        "escalates, and whether it pages outside working hours. The policy sections apply to",
        "every service and beat an individual entry wherever they say so.",
        "",
    ]
    for heading, body in POLICIES:
        parts += [f"## {heading}", "", " ".join(body.split()), ""]
    for row in rows:
        parts += [
            f"## service: {row['name']}",
            "",
            f"- Owning team: {row['team']}",
            f"- Escalation code: {row['code']}",
            f"- Service tier: {row['tier']}",
            f"- Pages outside working hours: {'yes' if row['pages'] else 'no'}",
            f"- Acknowledge within: {row['ack']} minutes",
            f"- Runs in: {', '.join(row['regions'])}",  # type: ignore[arg-type]
            f"- Depends on: {', '.join(row['depends'])}",  # type: ignore[arg-type]
            f"- Known failure mode: {row['failure']}.",
            f"- Dashboard: grafana/{row['name']}-overview",
            "",
        ]
    return "\n".join(parts)


def build(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    rng = random.Random(SEED + 2)
    items: list[dict[str, object]] = []
    shapes = ["owner", "code", "ack", "paging", "override"]
    for number in range(SIZE):
        row = rows[number % len(rows)]
        shape = shapes[number % len(shapes)]
        name = row["name"]
        entry = f"## service: {name}"
        if shape == "owner":
            question = f"Which team owns {name}?"
            gold, answer_type, sections = str(row["team"]), "enum", [entry]
        elif shape == "code":
            question = f"What escalation code does an alert on {name} carry?"
            gold, answer_type, sections = str(row["code"]), "enum", [entry]
        elif shape == "ack":
            question = (
                f"Within how many minutes must an alert on {name} be acknowledged before it "
                "climbs the ladder?"
            )
            gold, answer_type, sections = str(row["ack"]), "number", [entry]
        elif shape == "paging":
            question = f"Does an alert on {name} page outside working hours?"
            gold = "yes" if row["pages"] else "no"
            answer_type, sections = "yes_no", [entry]
        else:
            question = (
                f"A change freeze is in effect. Does an alert on {name} raised at 03:00 on a "
                "Sunday page?"
            )
            # The override: during a freeze every platform-tier service pages, whatever its own
            # entry says. Answering from the entry alone gets this wrong for exactly the
            # services whose entry says no.
            gold = "yes" if (row["tier"] == "platform" or row["pages"]) else "no"
            answer_type = "yes_no"
            sections = [entry, "## Change freeze"]
        items.append(
            {
                "id": f"ca-{number:04d}",
                "question": question,
                "gold": gold,
                "answer_type": answer_type,
                "question_type": shape,
                # Which sections of the catalogue actually answer this task. Recorded so the
                # simulated provider can be wrong when it was handed a context that does not
                # contain them — which is a measured property of the retrieval that really ran,
                # not a parameter.
                "sections": sections,
            }
        )
    items.sort(key=lambda item: str(item["id"]))
    by_type: dict[str, list[dict[str, object]]] = {}
    for item in items:
        by_type.setdefault(str(item["question_type"]), []).append(item)
    split_rng = random.Random(SEED + 3)
    fraction = CALIBRATION / len(items)
    chosen: set[str] = set()
    for question_type in sorted(by_type):
        bucket = list(by_type[question_type])
        split_rng.shuffle(bucket)
        chosen |= {str(row["id"]) for row in bucket[: round(len(bucket) * fraction)]}
    for item in items:
        item["split"] = "calibration" if item["id"] in chosen else "test"
    return items


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = services()
    text = catalogue(rows)
    (OUT / "catalogue.md").write_text(text)
    items = build(rows)
    (OUT / "dataset.jsonl").write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in items) + "\n"
    )
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item["question_type"])] = counts.get(str(item["question_type"]), 0) + 1
    calibration = sum(1 for item in items if item["split"] == "calibration")
    print(f"{len(rows)} services, catalogue {len(text):,} characters")
    print(f"{len(items)} items ({calibration} calibration, {len(items) - calibration} test)")
    for question_type in sorted(counts):
        print(f"  {question_type:10} {counts[question_type]:4}")


if __name__ == "__main__":
    main()
