"""Find screening candidates: merged pull requests of a pool library that could be a bug fix
with a regression test (development plan §7.2).

    uv run python -m tracerepro.dataset.find_candidates click flask --since 2025-11-11 [--write]

It only reads public GitHub data, through `gh pr list` (read-only). A pull request is kept when,
mechanically:

- it was merged on or after `--since` (default: one year ago; criterion 5 prefers recent fixes);
- its author is not a bot;
- it changes at least one test file and at least one library source file;
- it changes no deny-listed non-test file (test or interpreter config, dependency or CI files:
  criterion 3 would exclude it anyway);
- its library source changes stay within 3 files and 60 changed lines (criterion 3 says "about
  40"; preflight applies the exact limit on the real diff);
- it is not already in the screening log or the candidates folder.

Nothing is decided from the title or description. They only **order** the candidates: those
whose title or description names an exception, a traceback or a crash are listed first
(`crash_hint`), because criterion 1 needs one. The order changes which candidates are screened
first, never whether a screened candidate passes; every screened candidate is logged.

With `--write`, the first `--per-library` kept pull requests become candidate files
(`benchmark/candidates/{library}-{number}.yaml`, `split: test`), with the library's environment
profile from `benchmark/screening_profiles.yaml`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tracerepro.dataset.manifest import PROJECT_ROOT
from tracerepro.dataset.preflight import is_test_module, split_changes
from tracerepro.dataset.screening import read_log

PROFILES = PROJECT_ROOT / "benchmark" / "screening_profiles.yaml"
CANDIDATES = PROJECT_ROOT / "benchmark" / "candidates"
LOG = PROJECT_ROOT / "benchmark" / "screening_log.jsonl"
MAX_SOURCE_FILES = 3
MAX_SOURCE_LINES = 60
FIELDS = "number,title,body,url,mergedAt,mergeCommit,files,author"
_HINT_RE = re.compile(
    r"\b[A-Z][A-Za-z]*(Error|Exception)\b|\btraceback\b|\bcrash|\braises?\b|\bexception\b",
    re.IGNORECASE,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PullRequest:
    library: str
    repo: str
    number: int
    title: str
    url: str
    merged: str  # ISO date
    merge_commit: str
    source_files: tuple[str, ...]
    source_lines: int
    test_files: tuple[str, ...]
    hint: int

    @property
    def id(self) -> str:
        return f"{self.library}-{self.number}"


def load_profiles(path: Path = PROFILES) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: profiles must be a mapping")
    return data


def crash_hint(title: str, body: str) -> int:
    """How many crash words the title and description contain (title counts double)."""
    return 2 * len(_HINT_RE.findall(title)) + len(_HINT_RE.findall(body or ""))


def classify(files: Sequence[dict[str, Any]]) -> tuple[list[str], int, list[str], list[str]]:
    """(source files, their changed lines, test modules, deny-listed files), by preflight's own
    criterion 3 split (`preflight.split_changes`)."""
    by_path = {str(f.get("path", "")): f for f in files}
    tests, _, denied, source = split_changes(by_path)
    lines = sum(int(by_path[p].get("additions", 0)) + int(by_path[p].get("deletions", 0))
                for p in source)  # fmt: skip
    return source, lines, [t for t in tests if is_test_module(t)], denied


def keep(raw: dict[str, Any]) -> str:
    """Why a pull request is dropped, or "" to keep it."""
    author = raw.get("author") or {}
    if author.get("is_bot") or str(author.get("login", "")).endswith("[bot]"):
        return "bot author"
    if not (raw.get("mergeCommit") or {}).get("oid"):
        return "no merge commit"
    source, lines, tests, denied = classify(raw.get("files") or [])
    if denied:
        return f"changes deny-listed files {denied[:2]}"
    if not tests:
        return "no test module changed"
    if not source:
        return "no library source changed"
    if len(source) > MAX_SOURCE_FILES or lines > MAX_SOURCE_LINES:
        return f"too large ({len(source)} files, {lines} lines)"
    return ""


WINDOW_DAYS = 30  # one `gh` call per month: asking for a year's files at once times out


def windows(since: str, until: dt.date | None = None) -> list[tuple[str, str]]:
    """[start, end] date windows of WINDOW_DAYS from `since` to today, newest first."""
    start, end = dt.date.fromisoformat(since), until or dt.date.today()
    found = []
    while start <= end:
        stop = min(start + dt.timedelta(days=WINDOW_DAYS - 1), end)
        found.append((start.isoformat(), stop.isoformat()))
        start = stop + dt.timedelta(days=1)
    return found[::-1]


def list_merged(
    repo: str, since: str, limit: int, run: Runner = subprocess.run
) -> list[dict[str, Any]]:
    """Merged pull requests since `since`, newest first, at most `limit` per month window."""
    found: list[dict[str, Any]] = []
    for start, stop in windows(since):
        result = run(
            ["gh", "pr", "list", "-R", repo, "--state", "merged",
             "--search", f"merged:{start}..{stop}", "--limit", str(limit), "--json", FIELDS],
            capture_output=True, text=True, check=False, timeout=300,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(f"gh pr list {repo} failed: {result.stderr.strip()[:300]}")
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            raise RuntimeError(f"gh pr list {repo}: unexpected output")
        found += data
    return found


def known_ids() -> tuple[set[str], set[str]]:
    """Candidate ids and fix PR urls already screened or written."""
    rows = read_log(LOG) if LOG.is_file() else []
    ids = {str(r.get("id")) for r in rows} | {p.stem for p in CANDIDATES.glob("*.yaml")}
    urls = {str(r.get("fix_pr_url")) for r in rows if r.get("fix_pr_url")}
    return ids, urls


def find(
    library: str,
    profile: dict[str, Any],
    since: str,
    limit: int,
    run: Runner = subprocess.run,
    known: tuple[set[str], set[str]] | None = None,
) -> tuple[list[PullRequest], dict[str, int]]:
    """Kept pull requests (most crash-like first, then newest) and counts of drop reasons."""
    ids, urls = known if known is not None else known_ids()
    kept: list[PullRequest] = []
    dropped: dict[str, int] = {}
    for raw in list_merged(profile["repo"], since, limit, run):
        reason = keep(raw)
        pr_id = f"{library}-{raw['number']}"
        if not reason and (pr_id in ids or raw.get("url") in urls):
            reason = "already screened or written"
        if reason:
            key = reason.split(" (")[0].split(" [")[0]
            dropped[key] = dropped.get(key, 0) + 1
            continue
        source, lines, tests, _ = classify(raw["files"])
        title = str(raw.get("title", ""))
        hint = crash_hint(title, str(raw.get("body") or ""))
        kept.append(PullRequest(
            library, profile["repo"], int(raw["number"]), title, str(raw.get("url", "")),
            str(raw["mergedAt"])[:10], raw["mergeCommit"]["oid"], tuple(source), lines,
            tuple(tests), hint,
        ))  # fmt: skip
    kept.sort(key=lambda p: -p.hint)  # stable: gh lists newest first, kept within each level
    return kept, dropped


def candidate_yaml(pr: PullRequest, profile: dict[str, Any]) -> str:
    deps = dict(profile["deps"])
    if deps.get("strategy") == "compile":
        deps["exclude_newer"] = pr.merged
    data: dict[str, Any] = {
        "id": pr.id,
        "repo": pr.repo,
        "c_fix": pr.merge_commit,
        "python_version": "3.11",
        "fix_pr_url": pr.url,
        "fix_date": pr.merged,
        "deps": deps,
    }
    if profile.get("pytest_args"):
        data["pytest_args"] = list(profile["pytest_args"])
    if profile.get("test_paths"):
        data["test_paths"] = list(profile["test_paths"])
    data["notes"] = pr.title
    data["split"] = "test"
    header = (
        "# Written by tracerepro.dataset.find_candidates (mechanical filter, see its docstring).\n"
        f"# Crash hint {pr.hint}; source {list(pr.source_files)} ({pr.source_lines} lines).\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List (and write) screening candidates.")
    parser.add_argument("libraries", nargs="+", help="library names from screening_profiles.yaml")
    year_ago = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    parser.add_argument("--since", default=year_ago, help="merged on or after (ISO date)")
    parser.add_argument("--limit", type=int, default=60,
                        help="pull requests read per library and month")  # fmt: skip
    parser.add_argument("--per-library", type=int, default=10, help="candidates written each")
    parser.add_argument("--write", action="store_true", help="write candidate files")
    args = parser.parse_args(argv)
    profiles = load_profiles()
    unknown = [lib for lib in args.libraries if lib not in profiles]
    if unknown:
        parser.error(f"no profile for {unknown}; known: {sorted(profiles)}")
    known = known_ids()
    for library in args.libraries:
        kept, dropped = find(library, profiles[library], args.since, args.limit, known=known)
        print(f"{library}: {len(kept)} kept; dropped {json.dumps(dropped, sort_keys=True)}")
        for pr in kept[: args.per_library]:
            lines = f"{pr.source_lines:3} lines"
            print(f"  {pr.id:22} hint={pr.hint} {pr.merged} {lines}  {pr.title[:66]}")
            if args.write:
                (CANDIDATES / f"{pr.id}.yaml").write_text(candidate_yaml(pr, profiles[library]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
