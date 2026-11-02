"""Upstream repository access: a bare clone cache and `.git`-free snapshots of one commit."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


class RepoError(RuntimeError):
    """A git operation failed or a commit is missing."""


def _git(args: list[str], *, env: dict[str, str] | None = None) -> str:
    base = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", *args],
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RepoError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def has_commit(git_dir: Path, sha: str) -> bool:
    try:
        _git(["--git-dir", str(git_dir), "cat-file", "-e", f"{sha}^{{commit}}"])
    except RepoError:
        return False
    return True


def ensure_clone(location: str, cache_dir: Path, name: str, commits: list[str]) -> Path:
    """Return a bare clone of `location` under `cache_dir` that contains every commit in `commits`.

    `location` may be a URL, a local repository, or a git bundle file.
    """
    git_dir = cache_dir / f"{name}.git"
    if not git_dir.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--bare", "--quiet", location, str(git_dir)])
    missing = [c for c in commits if not has_commit(git_dir, c)]
    if missing:
        _git(["--git-dir", str(git_dir), "fetch", "--quiet", location, *missing])
    still_missing = [c for c in commits if not has_commit(git_dir, c)]
    if still_missing:
        raise RepoError(f"commits not found in {location}: {still_missing}")
    return git_dir


def materialize(git_dir: Path, sha: str, dest: Path) -> None:
    """Write the tree of commit `sha` into `dest`, with no `.git`.

    Uses a throwaway index plus `checkout-index` rather than `git archive`, so files marked
    `export-ignore` in `.gitattributes` (often the test suite) are still included.
    """
    if dest.exists() and any(dest.iterdir()):
        raise RepoError(f"destination is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        common = ["--git-dir", str(git_dir), "--work-tree", str(dest)]
        _git([*common, "read-tree", sha], env=env)
        _git(
            ["-c", "core.autocrlf=false", *common, "checkout-index", "--all", "--force"],
            env=env,
        )


def first_parent(git_dir: Path, sha: str) -> str:
    """The first parent of `sha`: the buggy commit for both squash and merge-commit fixes."""
    return _git(["--git-dir", str(git_dir), "rev-parse", f"{sha}^1"]).strip()


def changed_files(git_dir: Path, a: str, b: str) -> dict[str, tuple[str, int, int]]:
    """Path -> (status letter, lines added, lines removed) between two commits, without renames.

    Binary files report -1 for both counts.
    """
    base = ["--git-dir", str(git_dir), "diff", "--no-renames", "--no-ext-diff"]
    statuses = {}
    for line in _git([*base, "--name-status", a, b]).splitlines():
        status, _, path = line.partition("\t")
        statuses[path] = status[:1]
    result = {}
    for line in _git([*base, "--numstat", a, b]).splitlines():
        added, removed, path = line.split("\t", 2)
        counts = (-1, -1) if added == "-" else (int(added), int(removed))
        result[path] = (statuses.get(path, "M"), *counts)
    return result


def show_file(git_dir: Path, sha: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "show", f"{sha}:{path}"],
        env={"PATH": os.environ.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RepoError(f"git show {sha}:{path} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def diff(git_dir: Path, a: str, b: str, paths: list[str], *, context: int = 3) -> str:
    """Unified diff of `paths` between two commits, in the format `git apply -p1` expects."""
    return _git(
        ["--git-dir", str(git_dir), "diff", "--no-renames", "--no-ext-diff", "--no-color",
         "--full-index", f"-U{context}", a, b, "--", *paths]
    )  # fmt: skip


def clone_name(repo: str) -> str:
    """Cache directory name for a manifest/candidate `repo` value."""
    return repo.replace("/", "__").replace(":", "_")
