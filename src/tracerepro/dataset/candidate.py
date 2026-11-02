"""Preflight input: one `benchmark/candidates/{id}.yaml` per screened bug, plus lock generation.

A candidate names the fix commit and how to pin the test environment. Preflight derives
everything else (c_bug, oracle tests, baseline, gold files, payload) mechanically.

Dependency strategies (`deps.strategy`):

- `uv-lock`: the repo has a committed `uv.lock` (pallets projects). Exported with
  `uv export --frozen --no-emit-project --no-default-groups --group G ... --no-hashes`.
- `compile`: resolve requirement inputs at c_bug (requirements files, `pyproject.toml`, extra
  pins) with `uv pip compile --universal`, capped at `exclude_newer` (normally the fix date) so a
  re-run resolves the same versions.
- `file`: use an existing lock file in this repository (the toy fixture).
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tracerepro.dataset.lockfile import load_lock
from tracerepro.dataset.manifest import (
    _GITHUB_RE,
    _ID_RE,
    _PYTHON_RE,
    _SHA_RE,
    LOCAL_PREFIX,
    PROJECT_ROOT,
)
from tracerepro.dataset.repo import materialize

STRATEGIES = ("uv-lock", "compile", "file")
KNOWN = {
    "id", "repo", "c_fix", "python_version", "issue_url", "fix_pr_url", "fix_date", "deps",
    "pytest_args", "test_paths", "notes", "split",
}  # fmt: skip


class CandidateError(ValueError):
    """A candidate file is malformed, or its lock cannot be produced."""


@dataclass(frozen=True)
class Deps:
    strategy: str
    groups: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()  # compile: requirement files / pyproject.toml, relative to repo
    extra: tuple[str, ...] = ()  # compile: additional requirement specifiers
    exclude_newer: str | None = None  # compile: ISO date
    path: str | None = None  # file: lock path relative to this project


@dataclass(frozen=True)
class Candidate:
    id: str
    repo: str
    c_fix: str
    python_version: str
    deps: Deps
    issue_url: str | None = None
    fix_pr_url: str | None = None
    fix_date: str | None = None
    pytest_args: tuple[str, ...] = ()
    test_paths: tuple[str, ...] = ("tests",)
    notes: str = ""
    split: str = "dev"
    source_path: Path | None = None

    def repo_location(self, root: Path = PROJECT_ROOT) -> str:
        if self.repo.startswith(LOCAL_PREFIX):
            return str((root / self.repo.removeprefix(LOCAL_PREFIX)).resolve())
        return f"https://github.com/{self.repo}.git"


def _strs(data: dict[str, Any], key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CandidateError(f"{key} must be a list of strings")
    return tuple(value)


def parse_candidate(data: Any, source_path: Path | None = None) -> Candidate:
    if not isinstance(data, dict):
        raise CandidateError("candidate must be a mapping")
    unknown = set(data) - KNOWN
    if unknown:
        raise CandidateError(f"unknown fields: {sorted(unknown)}")
    for key in ("id", "repo", "c_fix", "python_version"):
        if not isinstance(data.get(key), str):
            raise CandidateError(f"{key} is required and must be a string")
    if not _ID_RE.match(data["id"]):
        raise CandidateError(f"invalid id {data['id']!r}")
    if not (data["repo"].startswith(LOCAL_PREFIX) or _GITHUB_RE.match(data["repo"])):
        raise CandidateError(f"invalid repo {data['repo']!r}")
    if not _SHA_RE.match(data["c_fix"]):
        raise CandidateError("c_fix must be a full 40-character commit hash")
    if not _PYTHON_RE.match(data["python_version"]):
        raise CandidateError(f"invalid python_version {data['python_version']!r}")

    raw = data.get("deps")
    if not isinstance(raw, dict) or raw.get("strategy") not in STRATEGIES:
        raise CandidateError(f"deps.strategy must be one of {STRATEGIES}")
    exclude_newer = raw.get("exclude_newer")
    if isinstance(exclude_newer, dt.date):
        exclude_newer = exclude_newer.isoformat()
    deps = Deps(
        strategy=raw["strategy"],
        groups=_strs(raw, "groups"),
        inputs=_strs(raw, "inputs"),
        extra=_strs(raw, "extra"),
        exclude_newer=exclude_newer,
        path=raw.get("path"),
    )
    if deps.strategy == "uv-lock" and not deps.groups:
        raise CandidateError("uv-lock needs at least one dependency group")
    if deps.strategy == "compile" and not (deps.inputs or deps.extra):
        raise CandidateError("compile needs inputs or extra requirements")
    if deps.strategy == "compile" and not deps.exclude_newer:
        raise CandidateError("compile needs exclude_newer, so the resolution is reproducible")
    if deps.strategy == "file" and (not deps.path or Path(deps.path).is_absolute()):
        raise CandidateError("file needs a lock path relative to the project root")

    fix_date = data.get("fix_date")
    if isinstance(fix_date, dt.date):
        fix_date = fix_date.isoformat()
    split = data.get("split", "dev")
    if split not in ("dev", "test"):
        raise CandidateError("split must be 'dev' or 'test'")
    return Candidate(
        id=data["id"],
        repo=data["repo"],
        c_fix=data["c_fix"],
        python_version=data["python_version"],
        deps=deps,
        issue_url=data.get("issue_url"),
        fix_pr_url=data.get("fix_pr_url"),
        fix_date=fix_date,
        pytest_args=_strs(data, "pytest_args"),
        test_paths=_strs(data, "test_paths", ("tests",)),
        notes=str(data.get("notes") or "").strip(),
        split=split,
        source_path=source_path,
    )


def load_candidate(path: Path) -> Candidate:
    try:
        return parse_candidate(yaml.safe_load(path.read_text()), source_path=path)
    except (yaml.YAMLError, CandidateError) as exc:
        raise CandidateError(f"{path}: {exc}") from exc


def _uv(args: list[str], cwd: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise CandidateError("uv is not installed")
    result = subprocess.run(
        [uv, *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=600
    )
    if result.returncode != 0:
        raise CandidateError(f"uv {args[0]} failed: {result.stderr.strip()[-2000:]}")


def make_lock(cand: Candidate, git_dir: Path, c_bug: str, dest: Path) -> Path:
    """Write the pinned requirements for `c_bug` to `dest` (or return the `file` lock)."""
    if cand.deps.strategy == "file":
        assert cand.deps.path is not None
        lock = PROJECT_ROOT / cand.deps.path
        load_lock(lock)
        return lock
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        materialize(git_dir, c_bug, tree)
        if cand.deps.strategy == "uv-lock":
            groups = [arg for g in cand.deps.groups for arg in ("--group", g)]
            _uv(
                ["export", "--frozen", "--no-emit-project", "--no-default-groups", *groups,
                 "--no-hashes", "--no-header", "--no-annotate", "--python", cand.python_version,
                 "-o", str(dest)],
                cwd=tree,
            )  # fmt: skip
        else:
            extra = Path(tmp) / "extra.in"
            extra.write_text("".join(f"{r}\n" for r in cand.deps.extra))
            inputs = [str(tree / i) for i in cand.deps.inputs]
            _uv(
                ["pip", "compile", *inputs, *([str(extra)] if cand.deps.extra else []),
                 "--universal", "--python-version", cand.python_version,
                 "--exclude-newer", str(cand.deps.exclude_newer), "--no-header", "--no-annotate",
                 "--no-emit-package", "setuptools", "-o", str(dest)],
                cwd=tree,
            )  # fmt: skip
    load_lock(dest)  # rejects editable, URL or unpinned lines
    return dest
