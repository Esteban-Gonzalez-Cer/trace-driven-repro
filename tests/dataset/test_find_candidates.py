"""Candidate finder and preflight batch rules (plan §7.2), with recorded `gh` output: no network."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tracerepro.dataset.candidate import parse_candidate
from tracerepro.dataset.find_candidates import (
    candidate_yaml,
    crash_hint,
    find,
    keep,
    load_profiles,
    windows,
)
from tracerepro.dataset.preflight import Batch, split_changes


def pr(number: int, files: list[tuple[str, int]], *, title: str = "Fix thing", bot: bool = False,
       body: str = "") -> dict[str, Any]:  # fmt: skip
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/pallets/click/pull/{number}",
        "mergedAt": "2026-08-14T10:00:00Z",
        "mergeCommit": {"oid": f"{number:040x}"},
        "author": {"login": "dependabot[bot]" if bot else "dev", "is_bot": bot},
        "files": [{"path": p, "additions": n, "deletions": 0} for p, n in files],
    }


GOOD = [("src/click/core.py", 5), ("tests/test_core.py", 20), ("CHANGES.rst", 3)]


def test_keep_rules() -> None:
    assert keep(pr(1, GOOD)) == ""
    assert keep(pr(2, GOOD, bot=True)) == "bot author"
    assert keep(pr(3, [("src/click/core.py", 5)])) == "no test module changed"
    assert (
        keep(pr(4, [("tests/test_core.py", 5), ("docs/x.rst", 1)])) == "no library source changed"
    )
    assert keep(pr(5, [*GOOD, ("pyproject.toml", 1)])).startswith("changes deny-listed")
    assert keep(pr(6, [*GOOD, (".github/workflows/t.yaml", 1)])).startswith("changes deny-listed")
    many = [("tests/test_a.py", 1)] + [(f"src/click/m{i}.py", 1) for i in range(4)]
    assert keep(pr(7, many)).startswith("too large")
    assert keep(pr(8, [("tests/test_a.py", 1), ("src/click/core.py", 61)])).startswith("too large")
    # A conftest inside the test folder is a test file, as preflight treats it.
    assert keep(pr(9, [*GOOD, ("tests/conftest.py", 2)])) == ""


def test_the_split_is_preflights_own() -> None:
    tests, docs, denied, source = split_changes(
        ["src/a.py", "tests/test_a.py", "CHANGES.rst", "setup.py", "src/data.json"]
    )
    assert tests == ["tests/test_a.py"] and docs == ["CHANGES.rst"]
    assert denied == ["setup.py"] and source == ["src/a.py", "src/data.json"]


def test_crash_hint_orders_but_does_not_filter() -> None:
    assert crash_hint("Fix TypeError when x is None", "") == 2
    assert crash_hint("Add feature", "it raises a ValueError with a traceback") == 3
    assert crash_hint("Add feature", "") == 0


def run_with(prs: list[dict[str, Any]]) -> Any:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["gh", "pr", "list"] and "--state" in argv
        window = argv[argv.index("--search") + 1]
        # Only the newest window returns the pull requests, so nothing is counted twice.
        newest = windows("2025-11-11")[0]
        found = prs if window == f"merged:{newest[0]}..{newest[1]}" else []
        return subprocess.CompletedProcess(argv, 0, json.dumps(found), "")

    return run


def test_find_orders_by_hint_and_skips_known() -> None:
    prs = [
        pr(10, GOOD, title="Add option"),
        pr(11, GOOD, title="Fix KeyError in parser"),
        pr(12, GOOD, title="Fix crash"),
        pr(13, GOOD, bot=True),
    ]
    profile = load_profiles()["click"]
    kept, dropped = find("click", profile, "2025-11-11", 50, run=run_with(prs),
                         known=({"click-12"}, set()))  # fmt: skip
    assert [p.id for p in kept] == ["click-11", "click-10"]
    assert dropped == {"already screened or written": 1, "bot author": 1}


def test_candidate_files_are_valid(tmp_path: Path) -> None:
    profiles = load_profiles()
    for library in ("click", "requests", "sqlparse"):
        kept, _ = find(library, profiles[library], "2025-11-11", 5,
                       run=run_with([pr(42, GOOD, title="Fix: it's broken")]),
                       known=(set(), set()))  # fmt: skip
        text = candidate_yaml(kept[0], profiles[library])
        cand = parse_candidate(yaml.safe_load(text))
        assert cand.split == "test" and cand.id == f"{library}-42"
        assert cand.c_fix == f"{42:040x}" and cand.fix_date == "2026-08-14"
        if cand.deps.strategy == "compile":
            assert cand.deps.exclude_newer == "2026-08-14"


def test_every_pool_library_has_a_profile() -> None:
    pool = {"click", "requests", "flask", "jinja2", "attrs", "marshmallow", "rich", "werkzeug",
            "starlette", "httpx", "typer", "sqlparse", "tomlkit", "itsdangerous", "markupsafe",
            "more-itertools"}  # fmt: skip
    assert set(load_profiles()) == pool


def cand(cid: str, repo: str, split: str = "test") -> Any:
    return parse_candidate({"id": cid, "repo": repo, "c_fix": "a" * 40, "python_version": "3.11",
                            "deps": {"strategy": "uv-lock", "groups": ["tests"]},
                            "split": split})  # fmt: skip


def test_batch_cap_stop_rule_and_summary() -> None:
    known = {f"click-{i}": "pallets/click" for i in range(10, 15)}
    batch = Batch(known | {"req-1": "psf/requests", "req-2": "psf/requests"})
    batch.add(cand("click-10", "pallets/click"), "preflight_pass", "ok", None)  # re-screened
    assert batch.test_counts["pallets/click"] == 5
    assert batch.skip(cand("click-1", "pallets/click"), cap=6, stop_at=30) == ""
    batch.add(cand("click-1", "pallets/click"), "preflight_pass", "ok", "python_failure")
    assert "per-library cap" in batch.skip(cand("click-2", "pallets/click"), cap=6, stop_at=30)
    assert batch.skip(cand("click-3", "pallets/click", "dev"), cap=6, stop_at=30) == ""
    assert "stop rule" in batch.skip(cand("req-9", "psf/requests"), cap=6, stop_at=8)
    batch.add(cand("req-3", "psf/requests"), "excluded", "Criterion 1: no crash (x)", None)
    summary = batch.summary()
    assert '"preflight_pass": 2' in summary and '"Criterion 1": 1' in summary
    assert '"python_failure": 1' in summary and "(total 8)" in summary


def test_windows_cover_the_range_without_gaps() -> None:
    import datetime as dt
    import itertools

    found = windows("2026-01-01", until=dt.date(2026, 3, 15))
    assert found[0][1] == "2026-03-15" and found[-1][0] == "2026-01-01"
    ordered = sorted(found)
    for (_, end), (start, _) in itertools.pairwise(ordered):
        assert dt.date.fromisoformat(start) == dt.date.fromisoformat(end) + dt.timedelta(days=1)
