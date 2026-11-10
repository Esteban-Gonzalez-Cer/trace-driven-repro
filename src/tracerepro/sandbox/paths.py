"""Path classification shared by the patch validator, preflight and the evaluator.

All paths are repo-relative POSIX strings, as they appear in a git diff or in the harness
plugin's frame records (which strip the `/repo/` prefix).
"""

from __future__ import annotations

from fnmatch import fnmatchcase as fnmatch
from pathlib import PurePosixPath

TEST_DIRS = frozenset({"tests"})
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")

# Spec §7.1: test or interpreter configuration, at any depth.
CONFIG_NAMES = frozenset(
    {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "sitecustomize.py", "usercustomize.py"}
)
CONFIG_PATTERNS = ("*.pth",)

# Spec §7.1: dependency and CI files.
DEPENDENCY_NAMES = frozenset({"pyproject.toml", "setup.py"})
DEPENDENCY_PATTERNS = ("requirements*.txt",)
CI_DIRS = frozenset({".github"})

# Amendment 3: changelog and documentation files, dropped from the gold diff.
DOCS_DIRS = frozenset({"docs", "doc"})
DOCS_PATTERNS = ("CHANGES*", "CHANGELOG*", "HISTORY*", "NEWS*", "*.rst", "*.md")


def _parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def _name(path: str) -> str:
    return PurePosixPath(path).name


def is_test_path(path: str) -> bool:
    parts = _parts(path)
    return any(p in TEST_DIRS for p in parts[:-1]) or any(
        fnmatch(_name(path), pat) for pat in TEST_FILE_PATTERNS
    )


def is_config_path(path: str) -> bool:
    name = _name(path)
    return name in CONFIG_NAMES or any(fnmatch(name, pat) for pat in CONFIG_PATTERNS)


def is_dependency_or_ci_path(path: str) -> bool:
    parts = _parts(path)
    name = _name(path)
    return (
        name in DEPENDENCY_NAMES
        or any(fnmatch(name, pat) for pat in DEPENDENCY_PATTERNS)
        or (len(parts) > 1 and parts[0] in CI_DIRS)
    )


def is_docs_path(path: str) -> bool:
    parts = _parts(path)
    return (len(parts) > 1 and parts[0] in DOCS_DIRS) or any(
        fnmatch(_name(path), pat) for pat in DOCS_PATTERNS
    )


def is_library_frame(file: str) -> bool:
    """Amendment 2: a frame in the project's own source, not tests, stdlib, deps or synthetic."""
    if not file or file.startswith(("/", "<")):
        return False
    return not is_test_path(file) and "site-packages" not in _parts(file)
