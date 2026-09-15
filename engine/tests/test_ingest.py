"""Reading somebody else's tasks into a workload (UPGRADE_V4.md M15).

SPEC.md section 3 promised JSONL and CSV ingestion and it was never built. The tests that matter
here are less about parsing than about one refusal: an OpenTelemetry span records what a model
*said*, and promoting that to a gold answer would make every number downstream meaningless while
looking completely normal.
"""

from __future__ import annotations

import json

import pytest

from tokop.workloads.ingest import OTEL_CONVENTION, ingest, split_items
from tokop.workloads.item import IngestedItem
from tokop.workloads.spec import WorkloadError

CSV_TEXT = """uuid,prompt,expected,category
a1,Which team owns cert_expiry_days?,security,lookup
a2,Does disk_usage_pct page out of hours?,yes,rule
a3,Which escalation code is queue_depth?,E18,lookup
a4,Does a folded alert page?,no,override
"""

JSONL_ROWS = [
    {"id": "j1", "question": "Who owns oom_kills?", "gold": "platform", "answer_type": "string"},
    {"id": "j2", "question": "Code for error_rate_pct?", "gold": "E05", "answer_type": "enum"},
    {"id": "j2", "question": "A repeat of j2", "gold": "E05", "answer_type": "enum"},
]

SPANS = [
    {
        "spanId": "s-001",
        "attributes": [
            {"key": "gen_ai.prompt", "value": {"stringValue": "Who owns oom_kills?"}},
            {"key": "gen_ai.completion", "value": {"stringValue": "platform"}},
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
        ],
    },
    {"spanId": "s-002", "attributes": {"gen_ai.prompt": "Does cache_hit_pct page?"}},
]


def write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text)
    return path


class TestCsvAndJsonl:
    def test_a_csv_maps_its_columns_without_being_told(self, tmp_path) -> None:
        report = ingest(write(tmp_path, "s.csv", CSV_TEXT), "csv", tmp_path / "out")
        assert report.items == 4
        assert report.has_gold
        assert report.mapping["question"] == "prompt"
        assert report.mapping["gold"] == "expected"
        assert report.mapping["id"] == "uuid"

    def test_an_explicit_mapping_wins(self, tmp_path) -> None:
        report = ingest(
            write(tmp_path, "s.csv", CSV_TEXT),
            "csv",
            tmp_path / "out",
            mapping={"question": "expected", "gold": "prompt"},
        )
        rows = [
            json.loads(line)
            for line in (tmp_path / "out" / "dataset.jsonl").read_text().splitlines()
        ]
        assert {row["question"] for row in rows} == {"security", "yes", "E18", "no"}
        assert report.mapping["question"] == "expected"

    def test_a_repeated_id_is_dropped_and_counted(self, tmp_path) -> None:
        text = "\n".join(json.dumps(row) for row in JSONL_ROWS)
        report = ingest(write(tmp_path, "s.jsonl", text), "jsonl", tmp_path / "out")
        assert report.items == 2
        assert any("repeated an id" in w for w in report.warnings)

    def test_a_row_with_no_question_says_which_column_to_name(self, tmp_path) -> None:
        text = json.dumps({"id": "x", "gold": "y"})
        with pytest.raises(WorkloadError, match="--map question="):
            ingest(write(tmp_path, "s.jsonl", text), "jsonl", tmp_path / "out")

    def test_columns_tokop_has_no_field_for_are_kept(self, tmp_path) -> None:
        text = json.dumps({"id": "x", "question": "q", "gold": "g", "tenant": "acme"})
        ingest(write(tmp_path, "s.jsonl", text), "jsonl", tmp_path / "out")
        row = json.loads((tmp_path / "out" / "dataset.jsonl").read_text().strip())
        assert row["extra"] == {"tenant": "acme"}


class TestSpansAreTrafficNotLabels:
    """The refusal this module exists for."""

    def test_a_completion_never_becomes_a_gold_answer(self, tmp_path) -> None:
        """`gen_ai.completion` is what the model said. It is not what the right answer was.

        Writing it into `gold` would produce a workload on which every pipeline scores 100%
        against itself, and nothing about the result would look wrong.
        """
        text = "\n".join(json.dumps(span) for span in SPANS)
        report = ingest(write(tmp_path, "s.jsonl", text), "otel", tmp_path / "out")
        rows = [
            json.loads(line)
            for line in (tmp_path / "out" / "dataset.jsonl").read_text().splitlines()
        ]
        assert [row["gold"] for row in rows] == ["", ""]
        assert not report.has_gold
        assert any("not treat a recorded completion" in w for w in report.warnings)

    def test_it_reads_both_attribute_encodings(self, tmp_path) -> None:
        text = "\n".join(json.dumps(span) for span in SPANS)
        report = ingest(write(tmp_path, "s.jsonl", text), "otel", tmp_path / "out")
        assert report.items == 2, "one of the two attribute encodings was not read"

    def test_it_names_the_convention_it_read(self, tmp_path) -> None:
        """The GenAI conventions are still moving.

        A file read under one version is not a file read under another, so the version is part
        of the result rather than an implementation detail.
        """
        text = json.dumps(SPANS[0])
        report = ingest(write(tmp_path, "s.jsonl", text), "otel", tmp_path / "out")
        assert report.convention == OTEL_CONVENTION
        assert "1.27.0" in "\n".join(report.lines())

    def test_a_file_with_no_genai_spans_says_what_it_looked_for(self, tmp_path) -> None:
        text = json.dumps({"spanId": "s", "attributes": {"http.method": "GET"}})
        with pytest.raises(WorkloadError, match=r"gen_ai\.prompt"):
            ingest(write(tmp_path, "s.jsonl", text), "otel", tmp_path / "out")


class TestSplits:
    def test_a_small_set_still_gets_a_calibration_split(self) -> None:
        """Per-stratum rounding can leave it empty, and a split with nothing to calibrate on
        is not a split."""
        items = [
            IngestedItem(id=f"t{i}", question="q", gold="g", answer_type="string") for i in range(4)
        ]
        split = split_items(items, calibration_size=1, seed=1)
        assert sum(1 for item in split if item.split == "calibration") == 1

    def test_it_is_reproducible(self) -> None:
        items = [
            IngestedItem(id=f"t{i}", question="q", gold="g", answer_type="string")
            for i in range(30)
        ]
        first = [item.split for item in split_items(items, calibration_size=10, seed=7)]
        again = [item.split for item in split_items(items, calibration_size=10, seed=7)]
        assert first == again

    def test_an_unknown_format_lists_the_known_ones(self, tmp_path) -> None:
        with pytest.raises(WorkloadError, match="jsonl, csv, otel"):
            ingest(write(tmp_path, "s.txt", "x"), "parquet", tmp_path / "out")
