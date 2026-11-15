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


def failed_rows(n: int) -> list[dict]:
    return [
        {
            "id": f"b{i:02}/A/1",
            "bug": f"b{i:02}",
            "pipeline": "A",
            "trial": 1,
            "solved": False,
            "patch_sha": None if i == 0 else f"{i:012x}",
        }
        for i in range(n)
    ]


def test_population_excludes_trials_without_a_patch() -> None:
    population = audit.incorrect_population(failed_rows(5) + rows())
    assert population == ["b01/A/1", "b02/A/1", "b03/A/1", "b04/A/1"]


def test_sample_is_reproducible_and_rounds_up() -> None:
    ids = [f"t{i}" for i in range(46)]
    first = audit.draw_sample(ids)
    assert first == audit.draw_sample(list(ids)) and len(first) == 12  # ceil(0.25 * 46)
    assert first == sorted(first) and audit.draw_sample(ids, seed=1) != first


def test_incorrect_sample_must_match_the_draw(tmp_path: Path) -> None:
    data = failed_rows(9)
    expected = audit.draw_sample(audit.incorrect_population(data), 0.25, 7)
    entries = [
        {"trial": t, "decision": "real_fix" if i == 0 else "not_a_fix"}
        for i, t in enumerate(expected)
    ]
    path = tmp_path / "incorrect_sample.yaml"
    path.write_text(yaml.safe_dump({"seed": 7, "fraction": 0.25, "sample": entries}))
    found = audit.load_incorrect_sample(path, data)
    assert (found["population"], found["sampled"], found["false_negatives"]) == (8, 2, 1)
    assert found["real_fix"] == [expected[0]]
    path.write_text(yaml.safe_dump({"seed": 8, "fraction": 0.25, "sample": entries}))
    with pytest.raises(audit.AuditError, match="does not match the draw"):
        audit.load_incorrect_sample(path, data)


def test_committed_incorrect_sample_matches_a_fresh_draw() -> None:
    results = ROOT / "benchmark" / "results" / "final.jsonl"
    data = [json.loads(line) for line in results.read_text().splitlines()]
    found = audit.load_incorrect_sample(
        ROOT / "benchmark" / "audit" / "incorrect_sample.yaml", data
    )
    assert found["sampled"] == 12 and found["population"] == 46
