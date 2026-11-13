"""The manual audit's files (spec §7.5): checked against the results they audit, and applied
only to the audit-adjusted numbers."""

import json
from pathlib import Path

import pytest
import yaml

from tracerepro.analysis import audit

ROOT = Path(__file__).resolve().parents[2]


def rows() -> list[dict]:
    return [
        {"id": "b/A/1", "bug": "b", "pipeline": "A", "solved": True},
        {"id": "b/A/2", "bug": "b", "pipeline": "A", "solved": True},
        {"id": "b/B/1", "bug": "b", "pipeline": "B", "solved": True},
        {"id": "b/B/2", "bug": "b", "pipeline": "B", "solved": False},
    ]


def write(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "correct.yaml"
    path.write_text(yaml.safe_dump({"reviewer": "r", "patches": entries}))
    return path


def test_counts_by_decision_and_pipeline(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        [
            {"decision": "ok", "trials": ["b/A/1", "b/B/1"]},
            {"decision": "not_a_fix", "trials": ["b/A/2"]},
        ],
    )
    found = audit.load_correct(path, rows())
    assert found["patches"] == 2 and found["not_a_fix"] == ["b/A/2"]
    assert found["by_decision"]["ok"] == {
        "patches": 1,
        "trials": 2,
        "by_pipeline": {"A": 1, "B": 1},
    }
    adjusted = audit.adjusted_rows(rows(), found["not_a_fix"])
    assert [r["solved"] for r in adjusted] == [True, False, True, False]


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        ([{"decision": "ok", "trials": ["b/A/1", "b/A/2"]}], "not audited"),
        ([{"decision": "ok", "trials": ["b/A/1", "b/A/2", "b/B/1", "b/B/1"]}], "more than once"),
        ([{"decision": "ok", "trials": ["b/A/1", "b/A/2", "b/B/1", "b/B/2"]}], "not graded"),
        ([{"decision": "maybe", "trials": ["b/A/1", "b/A/2", "b/B/1"]}], "unknown decisions"),
    ],
)
def test_mismatches_are_refused(tmp_path: Path, entries: list[dict], match: str) -> None:
    with pytest.raises(audit.AuditError, match=match):
        audit.load_correct(write(tmp_path, entries), rows())


def test_committed_audit_covers_every_correct_trial() -> None:
    results = ROOT / "benchmark" / "results" / "final.jsonl"
    data = [json.loads(line) for line in results.read_text().splitlines()]
    found = audit.load_correct(ROOT / "benchmark" / "audit" / "correct.yaml", data)
    assert sum(x["trials"] for x in found["by_decision"].values()) == sum(r["solved"] for r in data)
