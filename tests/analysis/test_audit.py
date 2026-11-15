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


def deficit_file(tmp_path: Path, labels: dict) -> Path:
    path = tmp_path / "state_deficit.yaml"
    path.write_text(yaml.safe_dump({"reviewer": "r", "bugs": labels}))
    return path


def test_state_deficit_replaces_only_labels_7_and_8_on_failed_trials(tmp_path: Path) -> None:
    path = deficit_file(tmp_path, {"b": {"deficit": True}, "c": {"deficit": False}})
    found = audit.load_state_deficit(path, {"b", "c"})
    assert found["bugs"] == ["b"] and found["judged"] == 2
    data = [
        {"id": "b/A/1", "bug": "b", "pipeline": "A", "solved": False, "label": "incorrect_patch"},
        {"id": "b/A/2", "bug": "b", "pipeline": "A", "solved": False, "label": "invalid_repro"},
        {
            "id": "b/B/1",
            "bug": "b",
            "pipeline": "B",
            "solved": False,
            "label": "regression_failure",
        },
        {"id": "b/B/2", "bug": "b", "pipeline": "B", "solved": True, "label": None},
        {"id": "c/B/1", "bug": "c", "pipeline": "B", "solved": False, "label": "incorrect_patch"},
    ]
    out, changed = audit.apply_state_deficit(data, found["bugs"])
    assert [r["label"] for r in out] == [
        "state_deficit",
        "invalid_repro",
        "state_deficit",
        None,
        "incorrect_patch",
    ]
    assert out[0]["auto_label"] == "incorrect_patch" and changed == {"A": 1, "B": 1}


def test_state_deficit_must_cover_every_bug(tmp_path: Path) -> None:
    with pytest.raises(audit.AuditError, match="without a State Deficit"):
        audit.load_state_deficit(deficit_file(tmp_path, {"b": {"deficit": False}}), {"b", "c"})
    with pytest.raises(audit.AuditError, match="true or false"):
        audit.load_state_deficit(deficit_file(tmp_path, {"b": {"deficit": "yes"}}), {"b"})


def test_cohen_kappa() -> None:
    assert audit.cohen_kappa([("a", "a"), ("b", "b"), ("a", "b"), ("b", "b")]) == 0.5
    assert audit.cohen_kappa([("a", "a"), ("b", "b")]) == 1.0
    assert audit.cohen_kappa([("a", "a"), ("a", "a")]) is None  # one category: undefined
    assert audit.cohen_kappa([]) is None


def test_second_review_matches_the_draw_and_counts_agreement(tmp_path: Path) -> None:
    decisions = {
        "correct": {f"b{i}#1": "ok" for i in range(20)},
        "incorrect": {f"t{i}": "not_a_fix" for i in range(12)},
        "state_deficit": {f"b{i}": "no_deficit" for i in range(30)},
    }
    draw = audit.draw_second_review(decisions, seed=3)
    assert draw == audit.draw_second_review(decisions, seed=3)
    assert {k: len(v) for k, v in draw.items()} == audit.SECOND_REVIEW_SIZES
    parts = {
        part: {ref: {"decision": decisions[part][ref]} for ref in refs}
        for part, refs in draw.items()
    }
    first = draw["correct"][0]
    parts["correct"][first] = {"decision": "not_a_fix"}
    path = tmp_path / "second_review.yaml"
    path.write_text(yaml.safe_dump({"reviewer": "owner", "seed": 3, "parts": parts}))
    found = audit.load_second_review(path, decisions)
    assert (found["n"], found["agree"]) == (20, 19)
    assert found["parts"]["correct"]["disagreements"] == [first]
    path.write_text(yaml.safe_dump({"reviewer": "owner", "seed": 4, "parts": parts}))
    with pytest.raises(audit.AuditError, match="does not match the draw"):
        audit.load_second_review(path, decisions)


def test_committed_state_deficit_covers_every_bug() -> None:
    results = ROOT / "benchmark" / "results" / "final.jsonl"
    bugs = {json.loads(line)["bug"] for line in results.read_text().splitlines()}
    found = audit.load_state_deficit(ROOT / "benchmark" / "audit" / "state_deficit.yaml", bugs)
    assert found["judged"] == 30


def test_committed_second_review_matches_the_draw() -> None:
    audit_dir = ROOT / "benchmark" / "audit"
    found = audit.load_second_review(audit_dir / "second_review.yaml", audit.first_pass(audit_dir))
    assert found["n"] == 20 and set(found["resolutions"]) == {
        ref for x in found["parts"].values() for ref in x["disagreements"]
    }
