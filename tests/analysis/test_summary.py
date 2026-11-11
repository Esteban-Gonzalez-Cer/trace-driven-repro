"""Run summary from stored logs (plan §7.3): the 2-of-3 rule, labels, usage, incomplete trials."""

import json
from pathlib import Path

from tracerepro.analysis.summary import render, summarize


def trial(root: Path, bug: str, pipeline: str, n: int, solved: bool, label: str | None,
          tokens: int = 1000, repro: str | None = None) -> None:  # fmt: skip
    folder = root / bug / pipeline / "claude-sonnet-5-5" / f"trial_{n}"
    folder.mkdir(parents=True)
    record = {"meta": {"trial": n}, "result": {"status": "submitted", "tokens_used": tokens}}
    (folder / "record.json").write_text(json.dumps(record))
    verdict: dict = {"kind": "correct" if solved else "incorrect", "solved": solved,
                     "label": label}  # fmt: skip
    if repro:
        verdict["repro_oracle"] = {"kind": repro}
    (folder / "verdict.json").write_text(json.dumps(verdict))
    call = {"ok": True, "cache_hit": False, "cost_equiv_usd": 0.25}
    (folder / "calls.jsonl").write_text(json.dumps(call) + "\n" + json.dumps(call) + "\n")


def test_two_of_three_rule_labels_and_usage(tmp_path: Path) -> None:
    run = tmp_path / "dry-run-x"
    for n, ok in enumerate([True, True, False], 1):
        trial(run, "bug-a", "B", n, ok, None if ok else "incorrect_patch", tokens=100 * n)
    for n, ok in enumerate([True, False, False], 1):
        trial(run, "bug-b", "B", n, ok, None if ok else "regression_failure")
    trial(run, "bug-a", "A", 1, False, "no_repro", repro="no_repro")
    (run / "bug-a" / "A" / "claude-sonnet-5-5" / "trial_2.aborted-20261111T000000").mkdir()
    table = summarize(run).table()
    b = table["pipelines"]["B"]
    assert b["bugs_solved_2_of_3"] == 1 and b["pass_at_k"] == 2 and b["consistent"] == 0
    assert b["trials_solved"] == 3 and b["labels"] == {"incorrect_patch": 1,
                                                      "regression_failure": 2}  # fmt: skip
    assert b["tokens_max"] == 1000 and b["calls"] == 12 and b["cost_equiv_usd"] == 3.0
    assert table["pipelines"]["A"]["repro_oracle"] == {"no_repro": 1}
    assert table["incomplete"] == ["bug-a/A/claude-sonnet-5-5/trial_2.aborted-20261111T000000"]
    text = render(table)
    assert "Pipeline B: 1/2 bugs solved (2 of 3)" in text and "incomplete" in text
