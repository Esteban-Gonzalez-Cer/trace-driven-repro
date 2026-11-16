"""Beyond-the-crash tests: only tests that pass with the gold patch are used, and only correct
trials are run. The sandbox runs are replaced by a fake: no Docker."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracerepro.evaluate import extra_tests

GOLD = "gold diff"


def test_only_gold_passing_tests_and_correct_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifests = {
        "bug-a": SimpleNamespace(
            id="bug-a",
            excluded_fix_tests=({"id": "t::good", "reason": "x"}, {"id": "t::bad", "reason": "x"}),
        ),
        "bug-b": SimpleNamespace(id="bug-b", excluded_fix_tests=()),
    }
    monkeypatch.setattr(extra_tests, "load_manifest", lambda p: manifests[Path(p).stem])
    monkeypatch.setattr(extra_tests, "bug_repo", lambda m, w: tmp_path)
    monkeypatch.setattr(extra_tests, "gold_diff", lambda m, g: GOLD)
    calls: list[tuple[str, list[str]]] = []

    def fake_run(manifest: Any, diff: str, tests: list[str], **_: Any) -> dict[str, str]:
        calls.append((diff, tests))
        if diff == GOLD:
            return {"t::good": "passed", "t::bad": "failed"}
        return {"t::good": "passed" if "fine" in diff else "failed"}

    monkeypatch.setattr(extra_tests, "run_tests", fake_run)
    run = tmp_path / "runs" / "b2"
    for n, text in ((1, "fine patch"), (2, "narrow patch")):
        folder = run / "bug-a" / "B" / "m" / f"trial_{n}"
        folder.mkdir(parents=True)
        (folder / "final.diff").write_text(text)
    rows = [
        {"id": "bug-a/B2/1", "bug": "bug-a", "pipeline": "B2", "model": "m", "trial": 1,
         "solved": True},
        {"id": "bug-a/B2/2", "bug": "bug-a", "pipeline": "B2", "model": "m", "trial": 2,
         "solved": True},
        {"id": "bug-a/B2/3", "bug": "bug-a", "pipeline": "B2", "model": "m", "trial": 3,
         "solved": False},
        {"id": "bug-b/B2/1", "bug": "bug-b", "pipeline": "B2", "model": "m", "trial": 1,
         "solved": True},
    ]  # fmt: skip
    out = extra_tests.evaluate(rows, {"B2": run}, tmp_path, client=object())  # type: ignore[arg-type]
    assert out["bugs"]["bug-a"]["tests"] == ["t::good"] and out["bugs"]["bug-b"]["tests"] == []
    assert out["trials"] == {
        "bug-a/B2/1": {"outcomes": {"t::good": "passed"}, "all_passed": True},
        "bug-a/B2/2": {"outcomes": {"t::good": "failed"}, "all_passed": False},
    }
    assert len(calls) == 3  # gold once, two correct trials; nothing for bug-b
    json.dumps(out)
