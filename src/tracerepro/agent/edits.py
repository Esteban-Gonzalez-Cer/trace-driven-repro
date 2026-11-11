"""Turn parsed edit blocks into a real git diff (development plan §6.3).

The model never writes a unified diff (models get line numbers wrong). It sends search-and-
replace edits; the harness applies them to a scratch copy of the files they name, then lets git
produce the diff. That diff is what the validator checks and what the evaluator re-applies, so
the usual rules (no test files, no config, size limits) apply unchanged.

Rules: paths are relative, inside the tree, with no `..`; every SEARCH text must occur exactly
once in the file as it stands after the earlier edits to it; an empty SEARCH creates a new
file, only if it does not exist yet. Breaking a rule is a `ContractFailure`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tracerepro.agent.parse import ContractFailure, Edit, Failure

MAX_FILE_BYTES = 5_000_000


@dataclass(frozen=True)
class EditResult:
    diff: str
    files: tuple[str, ...]


def _safe(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        bool(path)
        and not p.is_absolute()
        and ".." not in p.parts
        and "\\" not in path
        and (".git" not in p.parts)
    )


def _read(root: Path, path: str) -> str | None:
    full = root / path
    if full.is_symlink() or not full.is_file():
        return None
    if full.stat().st_size > MAX_FILE_BYTES:
        return None
    try:
        return full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _git(args: list[str], cwd: Path) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(cwd.parent),
    }
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def apply_edits(root: Path, edits: Sequence[Edit]) -> EditResult | ContractFailure:
    """Apply `edits` to copies of the files under `root` (which is left untouched) and return
    the git diff from the original files to the edited ones."""
    original: dict[str, str | None] = {}
    current: dict[str, str] = {}
    for n, edit in enumerate(edits, 1):
        if not _safe(edit.path):
            return ContractFailure(Failure.BAD_PATH, f"edit {n}: unsafe path {edit.path!r}")
        if edit.path not in current:
            text = _read(root, edit.path)
            if text is None and (root / edit.path).exists():
                return ContractFailure(
                    Failure.BAD_PATH, f"edit {n}: {edit.path} is not a readable text file"
                )
            original[edit.path] = text
            current[edit.path] = text if text is not None else ""
        if not edit.search:
            if original[edit.path] is not None or current[edit.path]:
                return ContractFailure(
                    Failure.NO_MATCH, f"edit {n}: empty SEARCH, but {edit.path} already exists"
                )
            current[edit.path] = edit.replace
            continue
        if original[edit.path] is None and not current[edit.path]:
            return ContractFailure(Failure.NO_MATCH, f"edit {n}: {edit.path} does not exist")
        count = current[edit.path].count(edit.search)
        if count == 0:
            return ContractFailure(
                Failure.NO_MATCH, f"edit {n}: SEARCH text not found in {edit.path}"
            )
        if count > 1:
            return ContractFailure(
                Failure.SEVERAL_MATCHES,
                f"edit {n}: SEARCH text occurs {count} times in {edit.path}",
            )
        current[edit.path] = current[edit.path].replace(edit.search, edit.replace, 1)
    changed = sorted(p for p in current if current[p] != (original[p] or ""))
    new_files = sorted(p for p in changed if original[p] is None)
    if not changed:
        return ContractFailure(Failure.NO_CHANGE, "the edits change nothing")
    with tempfile.TemporaryDirectory(prefix="tracerepro-edits-") as tmp:
        scratch = Path(tmp) / "tree"
        scratch.mkdir()
        _git(["init", "-q"], scratch)
        for path in changed:
            if original[path] is not None:
                target = scratch / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original[path] or "", encoding="utf-8")
        old = [p for p in changed if original[p] is not None]
        if old:
            _git(["add", "--", *old], scratch)
        for path in changed:
            target = scratch / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(current[path], encoding="utf-8")
        if new_files:
            _git(["add", "-N", "--", *new_files], scratch)
        diff = _git(
            ["diff", "--no-color", "--no-ext-diff", "--no-renames", "-U3", "--", *changed], scratch
        )
    return EditResult(diff, tuple(changed))
