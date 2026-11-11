import os
import subprocess
from pathlib import Path

import pytest

from tracerepro.dataset.repo import RepoError, ensure_clone, materialize

GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "upstream"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "mod.py").write_text("X = 1\n")
    (repo / "tests" / "test_mod.py").write_text("def test(): pass\n")
    # Many projects keep tests out of sdists this way; git archive would drop them.
    (repo / ".gitattributes").write_text("tests export-ignore\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--no-gpg-sign", "-m", "init")
    return repo, git(repo, "rev-parse", "HEAD")


def test_materialize_keeps_export_ignored_files_and_drops_git(
    upstream: tuple[Path, str], tmp_path: Path
) -> None:
    repo, sha = upstream
    git_dir = ensure_clone(str(repo), tmp_path / "cache", "up", [sha])
    dest = tmp_path / "snap"
    materialize(git_dir, sha, dest)
    assert (dest / "pkg" / "mod.py").read_text() == "X = 1\n"
    assert (dest / "tests" / "test_mod.py").is_file()
    assert not (dest / ".git").exists()


def test_materialize_refuses_non_empty_destination(
    upstream: tuple[Path, str], tmp_path: Path
) -> None:
    repo, sha = upstream
    git_dir = ensure_clone(str(repo), tmp_path / "cache", "up", [sha])
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "stale.py").write_text("")
    with pytest.raises(RepoError, match="not empty"):
        materialize(git_dir, sha, dest)


def test_ensure_clone_reports_missing_commit(upstream: tuple[Path, str], tmp_path: Path) -> None:
    repo, _ = upstream
    with pytest.raises(RepoError):
        ensure_clone(str(repo), tmp_path / "cache", "up", ["f" * 40])


def test_tag_version_is_the_newest_reachable_release(upstream: tuple[Path, str]) -> None:
    from tracerepro.dataset.repo import tag_version

    repo, first = upstream
    git(repo, "tag", "v1.2.0")
    git(repo, "tag", "not-a-version")
    (repo / "pkg" / "mod.py").write_text("X = 2\n")
    git(repo, "commit", "--quiet", "--no-gpg-sign", "-am", "second")
    second = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "1.10.0")
    assert tag_version(repo / ".git", first) == "1.2.0"  # the later tag is not reachable
    assert tag_version(repo / ".git", second) == "1.10.0"  # version order, not text order
