"""Export and report (plan §8.2): per-trial rows from a run folder, the privacy scan, and every
table and figure rebuilt deterministically from the exported rows alone."""

import json
from pathlib import Path

import pytest

from tracerepro.analysis import export, report, stats

BUGS = {"bug-a": ("library_raise", "2026-01-10"), "bug-b": ("python_failure", "2026-09-01")}
DIFF = (
    "diff --git a/lib/x.py b/lib/x.py\n--- a/lib/x.py\n+++ b/lib/x.py\n@@ -1 +1,2 @@\n-a\n+b\n+c\n"
)


def trial(
    run: Path,
    bug: str,
    pipeline: str,
    n: int,
    solved: bool,
    label: str | None,
    repro: str | None = None,
    tokens: int = 1000,
) -> None:
    folder = run / bug / pipeline / "claude-sonnet-5-5" / f"trial_{n}"
    folder.mkdir(parents=True)
    result = {
        "status": "submitted",
        "tokens_used": tokens,
        "attempts": [{}],
        "context": {"chars": 500},
        "prompt_leaks": [],
    }
    meta = {"trial": n, "agent_seconds": 10.0 * n}
    (folder / "record.json").write_text(json.dumps({"meta": meta, "result": result}))
    verdict: dict = {
        "kind": "correct" if solved else "incorrect",
        "solved": solved,
        "label": label,
        "flags": [],
        "regressions": [],
        "context_coverage": {"fix_shown": True, "fault_localization_miss": False},
    }
    if repro:
        verdict["repro_oracle"] = {"kind": repro}
    (folder / "verdict.json").write_text(json.dumps(verdict))
    (folder / "final.diff").write_text(DIFF if solved or label == "incorrect_patch" else "")
    call = {"ok": True, "cache_hit": False, "cost_equiv_usd": 0.5, "latency_s": 2.0}
    (folder / "calls.jsonl").write_text(json.dumps(call) + "\n")


@pytest.fixture
def run(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "final-x"
    for n, ok in enumerate([True, True, False], 1):  # A solves bug-a, not bug-b
        trial(
            run,
            "bug-a",
            "A",
            n,
            ok,
            None if ok else "invalid_repro",
            repro="valid" if ok else "invalid_repro",
        )
        trial(run, "bug-b", "A", n, False, "incorrect_patch", repro="valid")
    for n in (1, 2, 3):  # B solves both
        trial(run, "bug-a", "B", n, True, None, tokens=500)
        trial(run, "bug-b", "B", n, n != 1, "incorrect_patch" if n == 1 else None, tokens=500)
    aside = run / "bug-a" / "A" / "claude-sonnet-5-5" / "trial_2.aborted-20261112T000000"
    aside.mkdir()
    (aside / "calls.jsonl").write_text(json.dumps({"ok": False, "error": "InfraFailure"}) + "\n")
    (run / "run.json").write_text(json.dumps({"run_id": "final-x", "config": {"trials": 3}}))
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for bug, (kind, date) in BUGS.items():
        (manifests / f"{bug}.yaml").write_text(f"crash_kind: {kind}\nfix_date: '{date}'\n")
    return run


def test_export_rows_and_set_aside_attempts(run: Path, tmp_path: Path) -> None:
    rows, meta = export.export(run)
    assert len(rows) == 12 and meta["trials"] == 12
    assert meta["set_aside"] == {"InfraFailure": 1} and meta["run_id"] == "final-x"
    first = rows[0]
    assert first["id"] == "bug-a/A/1" and first["solved"] and first["repro_oracle"] == "valid"
    assert (first["patch_files"], first["lines_added"], first["lines_removed"]) == (1, 2, 1)
    assert first["calls"] == 1 and first["model_seconds"] == 2.0
    assert rows[2]["patch_sha"] is None  # an invalid repro stops before any patch
    paths = export.write(rows, meta, tmp_path / "out", "final")
    assert [p.name for p in paths] == ["final.jsonl", "final-run.json"]
    assert len((tmp_path / "out" / "final.jsonl").read_text().splitlines()) == 12


@pytest.mark.parametrize(
    "value",
    [
        "/Users/someone/project",
        "/home/someone/.ssh",
        "mail me: a.person@example.com",
        "token ghp_abcdefghijklmnop1234",
    ],
)
def test_privacy_scan_fails_closed(value: str) -> None:
    with pytest.raises(export.PrivateDataError):
        export.scan({"rows": [{"label": value}]})


def test_privacy_scan_allows_upstream_test_paths() -> None:
    export.scan({"payload": "FILE:///C:/Users/test/file.txt"})


def test_export_refuses_private_data(run: Path) -> None:
    (run / "run.json").write_text(json.dumps({"run_id": "x", "config": {"root": "/Users/me"}}))
    assert export.main([str(run), "--out", str(run.parent / "out")]) == 1
    assert not (run.parent / "out").exists()


def test_report_numbers_match_stats(run: Path, tmp_path: Path) -> None:
    rows, meta = export.export(run)
    export.write(rows, meta, tmp_path / "results", "final")
    out = tmp_path / "tables"
    report.write_all(tmp_path / "results", run.parent.parent / "manifests", out)
    n = json.loads((out / "numbers.json").read_text())
    a, b = n["pipelines"]["A"], n["pipelines"]["B"]
    assert a["accuracy"]["k"] == 1 and b["accuracy"]["k"] == 2
    assert a["accuracy"]["ci"] == [round(x, 4) for x in stats.clopper_pearson(1, 2)]
    assert n["comparison"]["a_only"] == [] and n["comparison"]["b_only"] == ["bug-b"]
    assert n["comparison"]["mcnemar_p"] == stats.mcnemar_exact(0, 1)
    assert a["labels"]["invalid_repro"] == 1 and a["labels"]["incorrect_patch"] == 3
    assert a["repro_runs"]["k"] == 5 and a["repro_bugs"]["k"] == 2 and "repro_runs" not in b
    assert b["tokens"]["mean"] == 500.0 and b["consistency"]["k"] == 1
    assert n["groups"]["cohort"] == {
        "post-cutoff": {"A": report._interval(0, 1), "B": report._interval(1, 1)},
        "pre-cutoff": {"A": report._interval(1, 1), "B": report._interval(1, 1)},
    }
    assert n["set_aside"] == {"InfraFailure": 1}
    text = (out / "tables.md").read_text()
    assert "| Bugs solved (2 of 3) | 1/2 (50%" in text and "Solved by B only (c = 1): bug-b" in text
    assert (out / "per_bug.csv").read_text().splitlines()[1].startswith("bug-a,library_raise")
    assert sorted(p.name for p in (out / "figures").iterdir()) == [
        "cost_accuracy.svg",
        "failure_labels.svg",
        "per_bug.svg",
    ]


def test_report_is_deterministic(run: Path, tmp_path: Path) -> None:
    rows, meta = export.export(run)
    export.write(rows, meta, tmp_path / "results", "final")
    outputs = []
    for name in ("one", "two"):
        report.write_all(tmp_path / "results", run.parent.parent / "manifests", tmp_path / name)
        outputs.append(
            {
                p.relative_to(tmp_path / name): p.read_bytes()
                for p in sorted((tmp_path / name).rglob("*"))
                if p.is_file()
            }
        )
    assert outputs[0] == outputs[1] and len(outputs[0]) == 6
    svg = outputs[0][Path("figures/per_bug.svg")].decode()
    assert "<dc:date>" not in svg and "✓" in svg  # no date; marks are text, not colour alone


def test_b2_facts_from_a_record() -> None:
    record = {
        "final_from": 2,
        "attempts": [
            {
                "n": 1,
                "check": "valid",
                "outcome": "check_still_fails",
                "check_after": "still_fails",
            },
            {
                "n": 2,
                "check": "valid",
                "outcome": "clean",
                "check_after": "passes",
                "model": "claude-opus-5-5",
            },
        ],
        "file_request": {
            "requested": ["a.py", "b.py"],
            "granted": ["a.py (whole)"],
            "refused": {"b.py": "no such file in the library"},
        },
    }
    assert export.b2_facts(record) == {
        "check": "valid",
        "check_usable": True,
        "check_caught": True,
        "check_final": "passes",
        "files_requested": 2,
        "files_granted": 1,
        "escalated": True,
    }
    assert export.b2_facts({"attempts": [{"n": 1, "outcome": "clean"}]}) == {}


# --- Phase 5b: two arms of Pipeline B (old B and B2) ---------------------------------------


def b2_trial(run: Path, bug: str, n: int, solved: bool, **facts: object) -> None:
    trial(run, bug, "B", n, solved, None if solved else "incorrect_patch")
    folder = run / bug / "B" / "claude-sonnet-5-5" / f"trial_{n}"
    record = json.loads((folder / "record.json").read_text())
    attempts = [{"n": 1, "check": facts.get("check", "valid"), "check_after": "passes"}]
    if facts.get("caught"):
        attempts = [
            {"n": 1, "check": "valid", "outcome": "check_still_fails"},
            {"n": 2, "check": "valid", "check_after": "passes", "model": "claude-sonnet-5-5"},
        ]
    record["result"] |= {"attempts": attempts, "final_from": len(attempts)}
    if facts.get("files"):
        record["result"]["file_request"] = {"requested": ["a.py"], "granted": ["a.py (whole)"]}
    (folder / "record.json").write_text(json.dumps(record))


@pytest.fixture
def arms(tmp_path: Path) -> tuple[Path, Path]:
    old, new = tmp_path / "runs" / "old", tmp_path / "runs" / "new"
    for n in (1, 2, 3):
        trial(old, "bug-a", "B", n, True, None)
        trial(old, "bug-b", "B", n, False, "incorrect_patch")
        b2_trial(new, "bug-a", n, True, files=n == 1)
        b2_trial(new, "bug-b", n, n != 3, caught=n == 1)
    for run in (old, new):
        (run / "run.json").write_text(json.dumps({"run_id": run.name, "config": {}}))
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for bug, (kind, date) in BUGS.items():
        (manifests / f"{bug}.yaml").write_text(f"crash_kind: {kind}\nfix_date: '{date}'\n")
    return old, new


def test_two_runs_export_as_two_arms(arms: tuple[Path, Path], tmp_path: Path) -> None:
    rows, meta = export.export_arms(list(arms), ["B", "B2"])
    assert len(rows) == 12 and {r["pipeline"] for r in rows} == {"B", "B2"}
    assert "bug-b/B2/1" in {r["id"] for r in rows}
    assert set(meta["arms"]) == {"B", "B2"} and meta["run_id"] == "B: old + B2: new"
    with pytest.raises(ValueError, match="distinct"):
        export.export_arms(list(arms), ["B", "B"])
    out = tmp_path / "res"
    assert (
        export.main([*map(str, arms), "--arms", "B", "B2", "--out", str(out), "--name", "b2"]) == 0
    )
    assert len((out / "b2.jsonl").read_text().splitlines()) == 12


def test_b2_report_orders_the_pair_and_adds_its_measures(
    arms: tuple[Path, Path], tmp_path: Path
) -> None:
    rows, meta = export.export_arms(list(arms), ["B", "B2"])
    results = tmp_path / "results"
    export.write(rows, meta, results, "b2")
    extra = {
        "format": 1,
        "bugs": {"bug-a": {"tests": ["t::x"]}, "bug-b": {"tests": []}},
        "trials": {
            f"bug-a/{arm}/{n}": {"all_passed": not (arm == "B2" and n == 2)}
            for arm in ("B", "B2")
            for n in (1, 2, 3)
        },
    }
    (results / "b2-extra.json").write_text(json.dumps(extra))
    out = tmp_path / "tables"
    report.write_all(results, tmp_path / "manifests", out, "b2", None, ("B2", "B"))
    n = json.loads((out / "numbers.json").read_text())
    c = n["comparison"]
    assert c["pair"] == ["B2", "B"] and c["b2_only"] == ["bug-b"] and c["b_only"] == []
    assert c["paired_difference"]["mean"] > 0  # B2 minus B
    b2 = n["pipelines"]["B2"]["b2"]
    assert "b2" not in n["pipelines"]["B"]
    assert b2["check_caught"]["k"] == 1 and b2["check_caught_then_solved"] == 1
    assert b2["file_requests"]["k"] == 1 and b2["escalated"]["k"] == 1
    assert b2["passes_check_but_incorrect"] == 1
    beyond = n["beyond_the_crash"]
    assert beyond["bugs"] == ["bug-a"]
    assert beyond["pipelines"]["B"]["correct_trials"]["k"] == 3
    assert beyond["pipelines"]["B2"]["failed"] == ["bug-a/B2/2"]
    text = (out / "tables.md").read_text()
    assert "Solved by B2 only (c = 1)" not in text and "Solved by B2 only (b = 1): bug-b" in text
    assert "(B2 minus B)" in text and "Pipeline B2's own measures" in text
    assert "Beyond-the-crash tests" in text
    del extra["trials"]["bug-a/B/1"]
    (results / "b2-extra.json").write_text(json.dumps(extra))
    with pytest.raises(ValueError, match="missing"):
        report.write_all(results, tmp_path / "manifests", out, "b2", None, ("B2", "B"))
