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
