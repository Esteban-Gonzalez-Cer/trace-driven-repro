"""Build the tiny_lib fixture as a real git repository with two deterministic commits.

c_bug: pyproject.toml, tiny_lib/ from buggy/, tests/test_report.py
c_fix: tiny_lib/report.py from fixed/, plus tests/test_regression.py

Author, committer, dates, and config are fixed, so the commit hashes are the same on every
machine and can live in manifest.yaml. `python make_repo.py` rewrites repo.bundle next to this
file; the harness tests check that the bundle, the hashes, and buggy/ + fixed/ never drift apart.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = HERE / "repo.bundle"

PYPROJECT = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "tiny-lib"
version = "0.0.0"
requires-python = ">=3.10"

[tool.setuptools]
packages = ["tiny_lib"]
"""

_ENV = {
    "GIT_AUTHOR_NAME": "tracerepro fixture",
    "GIT_AUTHOR_EMAIL": "fixture@tracerepro.invalid",
    "GIT_AUTHOR_DATE": "2026-11-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "tracerepro fixture",
    "GIT_COMMITTER_EMAIL": "fixture@tracerepro.invalid",
    "GIT_COMMITTER_DATE": "2026-11-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", ""), **_ENV}
    out = subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    dest.chmod(0o644)


def build_repo(dest: Path) -> tuple[str, str]:
    """Create the fixture repository at `dest`; return (c_bug, c_fix)."""
    dest.mkdir(parents=True)
    _git(dest, "init", "--quiet", "--initial-branch=main")
    (dest / "pyproject.toml").write_text(PYPROJECT)
    for f in sorted((HERE / "buggy" / "tiny_lib").glob("*.py")):
        _copy(f, dest / "tiny_lib" / f.name)
    _copy(HERE / "tests" / "test_report.py", dest / "tests" / "test_report.py")
    _git(dest, "add", "--all")
    _git(dest, "commit", "--quiet", "--no-gpg-sign", "-m", "tiny_lib: report formatting")
    c_bug = _git(dest, "rev-parse", "HEAD")

    for f in sorted((HERE / "fixed" / "tiny_lib").glob("*.py")):
        _copy(f, dest / "tiny_lib" / f.name)
    _copy(HERE / "tests" / "test_regression.py", dest / "tests" / "test_regression.py")
    _git(dest, "add", "--all")
    _git(dest, "commit", "--quiet", "--no-gpg-sign", "-m", "Fix crash on empty report")
    c_fix = _git(dest, "rev-parse", "HEAD")
    return c_bug, c_fix


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "tiny_lib"
        c_bug, c_fix = build_repo(repo)
        _git(repo, "bundle", "create", "--quiet", str(BUNDLE), "main")
    print(f"wrote {BUNDLE.name}\nc_bug: {c_bug}\nc_fix: {c_fix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
