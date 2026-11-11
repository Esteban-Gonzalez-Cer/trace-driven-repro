"""Host-side patch validator (spec §7.1, plan §5.3). Runs before anything executes.

A patch is a git-style unified diff. It is rejected if it:

- is not valid UTF-8 text, contains NUL bytes, or carries binary hunks;
- uses absolute paths, `..`, backslashes, `.git/`, or paths without the `a/` and `b/` prefixes
  (so the paths checked here are exactly the paths `git apply -p1` writes);
- creates or changes a symlink, submodule, or any mode other than a regular file;
- creates or modifies a test file, test/interpreter configuration, or a dependency/CI file;
- exceeds the file or changed-line limits (spec §6: 3 files, 40 lines).

The parser follows hunk line counts, so a removed line that happens to start with `--` is never
mistaken for a file header. Anything it does not recognise after the first file is malformed.
"""

from __future__ import annotations

import enum
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from tracerepro.sandbox.paths import is_config_path, is_dependency_or_ci_path, is_test_path
from tracerepro.sandbox.runner import Workspace

REGULAR_MODES = frozenset({"100644", "100755"})
SYMLINK_MODE = "120000"

_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HEADER_PREFIXES = (
    "index ",
    "similarity index ",
    "dissimilarity index ",
)
_PATH_PREFIXES = ("rename from ", "rename to ", "copy from ", "copy to ")
_MODE_PREFIXES = ("old mode ", "new mode ", "new file mode ", "deleted file mode ")


class Rule(enum.StrEnum):
    MALFORMED = "malformed"
    BINARY = "binary"
    UNSAFE_PATH = "unsafe_path"
    SYMLINK = "symlink"
    FILE_MODE = "file_mode"
    TEST_FILE = "test_file"
    CONFIG_FILE = "config_file"
    DEPENDENCY_OR_CI = "dependency_or_ci"
    TOO_MANY_FILES = "too_many_files"
    TOO_MANY_LINES = "too_many_lines"
    TOO_LARGE = "too_large"


@dataclass(frozen=True)
class Violation:
    rule: Rule
    path: str
    message: str


@dataclass(frozen=True)
class DiffLimits:
    max_files: int = 3
    max_lines: int = 40
    # 40 lines can still be huge lines (red-team round 2): cap the patch's size in bytes too.
    max_bytes: int = 64_000


DEFAULT_LIMITS = DiffLimits()


@dataclass
class FileDiff:
    old_path: str | None = None  # None for /dev/null (a new file)
    new_path: str | None = None  # None for /dev/null (a deleted file)
    header_paths: tuple[str, ...] = ()  # from `diff --git`, rename and copy lines
    modes: list[str] = field(default_factory=list)
    binary: bool = False
    added: int = 0
    removed: int = 0
    hunks: int = 0
    added_lines: list[int] = field(default_factory=list)  # line numbers in the new file

    @property
    def paths(self) -> list[str]:
        found = [p for p in (self.old_path, self.new_path, *self.header_paths) if p]
        return list(dict.fromkeys(found))

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or (self.header_paths or ("",))[0]


@dataclass(frozen=True)
class Validation:
    files: tuple[FileDiff, ...]
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def changed_lines(self) -> int:
        return sum(f.added + f.removed for f in self.files)


class DiffError(ValueError):
    """The text is not a well-formed git-style unified diff."""


def _strip_prefix(raw: str, prefix: str) -> str:
    raw = raw.split("\t", 1)[0]
    if raw.startswith('"'):
        raise DiffError(f"quoted paths are not supported: {raw}")
    if not raw.startswith(prefix):
        raise DiffError(f"path must start with {prefix!r}: {raw}")
    return raw[len(prefix) :]


def _side(raw: str, prefix: str) -> str | None:
    raw = raw.split("\t", 1)[0]
    return None if raw == "/dev/null" else _strip_prefix(raw, prefix)


def _git_header(line: str) -> tuple[str, ...]:
    rest = line[len("diff --git ") :]
    # Paths may contain spaces, so split on the " b/" that makes both halves equal length when
    # possible; otherwise fall back to the last occurrence (renames).
    candidates = [m.start() for m in re.finditer(r" b/", rest)]
    if not rest.startswith("a/") or not candidates:
        raise DiffError(f"malformed diff header: {line}")
    for idx in candidates:
        a, b = rest[2:idx], rest[idx + 3 :]
        if a == b:
            return (a,)
    idx = candidates[-1]
    return (rest[2:idx], rest[idx + 3 :])


def parse_diff(text: str) -> list[FileDiff]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        if line.startswith("diff --git "):
            cur = FileDiff(header_paths=_git_header(line))
            files.append(cur)
        elif cur is not None and cur.hunks == 0 and line.startswith(_HEADER_PREFIXES):
            parts = line.split()
            if line.startswith("index ") and len(parts) == 3:
                cur.modes.append(parts[2])
        elif cur is not None and cur.hunks == 0 and line.startswith(_MODE_PREFIXES):
            cur.modes.append(line.split()[-1])
        elif cur is not None and line.startswith(_PATH_PREFIXES):
            cur.header_paths += (line.split(" ", 2)[2],)
        elif line.startswith("Binary files ") or line == "GIT binary patch":
            if cur is None:
                raise DiffError("binary marker outside a file section")
            cur.binary = True
            i += 1
            while i < len(lines) and not lines[i].startswith("diff --git "):
                i += 1
            continue
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks or cur.old_path or cur.new_path:
                cur = FileDiff()
                files.append(cur)
            cur.old_path = _side(line[4:], "a/")
            cur.new_path = _side(lines[i + 1].rstrip("\r")[4:], "b/")
            i += 2
            continue
        elif line.startswith("@@"):
            if cur is None or (cur.old_path is None and cur.new_path is None):
                raise DiffError(f"hunk without file headers at line {i + 1}")
            i = _consume_hunk(lines, i, cur)
            continue
        elif files:
            raise DiffError(f"unexpected line {i + 1}: {line[:80]!r}")
        # Text before the first file (a commit message, say) is ignored, as `git apply` does.
        i += 1
    return files


def _consume_hunk(lines: list[str], i: int, cur: FileDiff) -> int:
    match = _HUNK_RE.match(lines[i])
    if match is None:
        raise DiffError(f"malformed hunk header at line {i + 1}")
    old_left = int(match[1]) if match[1] is not None else 1
    new_line = int(match[2])
    new_left = int(match[3]) if match[3] is not None else 1
    cur.hunks += 1
    i += 1
    while old_left > 0 or new_left > 0:
        if i >= len(lines):
            raise DiffError("hunk is truncated")
        body = lines[i].rstrip("\r")
        tag = body[:1]
        if tag in (" ", ""):
            old_left, new_left = old_left - 1, new_left - 1
            new_line += 1
        elif tag == "-":
            old_left -= 1
            cur.removed += 1
        elif tag == "+":
            new_left -= 1
            cur.added += 1
            cur.added_lines.append(new_line)
            new_line += 1
        elif tag != "\\":
            raise DiffError(f"unexpected hunk line {i + 1}: {body[:80]!r}")
        if old_left < 0 or new_left < 0:
            raise DiffError(f"hunk line counts do not match at line {i + 1}")
        i += 1
    while i < len(lines) and lines[i].startswith("\\"):
        i += 1  # "\ No newline at end of file" after the last line
    return i


def _unsafe(path: str) -> str | None:
    if path.startswith("/"):
        return "absolute path"
    if "\\" in path or "\0" in path:
        return "backslash or NUL in path"
    parts = PurePosixPath(path).parts
    if not parts or path.endswith("/"):
        return "empty path"
    if ".." in parts:
        return "path traversal (..)"
    if ".git" in parts:
        return "writes into .git"
    return None


def validate(diff: str | bytes, limits: DiffLimits = DEFAULT_LIMITS) -> Validation:
    if isinstance(diff, bytes):
        if b"\0" in diff:
            return Validation((), (Violation(Rule.BINARY, "", "patch contains NUL bytes"),))
        try:
            diff = diff.decode("utf-8")
        except UnicodeDecodeError:
            return Validation((), (Violation(Rule.BINARY, "", "patch is not valid UTF-8"),))
    if "\0" in diff:
        return Validation((), (Violation(Rule.BINARY, "", "patch contains NUL bytes"),))
    size = len(diff.encode("utf-8"))
    if size > limits.max_bytes:
        message = f"{size} bytes > {limits.max_bytes}"
        return Validation((), (Violation(Rule.TOO_LARGE, "", message),))
    try:
        files = parse_diff(diff)
    except DiffError as exc:
        return Validation((), (Violation(Rule.MALFORMED, "", str(exc)),))

    found: list[Violation] = []
    for f in files:
        if f.binary:
            found.append(Violation(Rule.BINARY, f.path, "binary patch"))
        for mode in f.modes:
            if mode == SYMLINK_MODE:
                found.append(Violation(Rule.SYMLINK, f.path, "creates or changes a symlink"))
            elif mode not in REGULAR_MODES:
                found.append(Violation(Rule.FILE_MODE, f.path, f"file mode {mode}"))
        for path in f.paths:
            # Judged case-insensitively: on a case-insensitive disk (macOS) a new
            # `Tests/__init__.py` lands in `tests/` (red-team round 2).
            folded = path.casefold()
            reason = _unsafe(folded)
            if reason:
                found.append(Violation(Rule.UNSAFE_PATH, path, reason))
            elif is_test_path(folded):
                found.append(Violation(Rule.TEST_FILE, path, "creates or modifies a test file"))
            elif is_config_path(folded):
                found.append(Violation(Rule.CONFIG_FILE, path, "test or interpreter config"))
            elif is_dependency_or_ci_path(folded):
                found.append(Violation(Rule.DEPENDENCY_OR_CI, path, "dependency or CI file"))
    if len(files) > limits.max_files:
        found.append(Violation(Rule.TOO_MANY_FILES, "", f"{len(files)} files > {limits.max_files}"))
    lines = sum(f.added + f.removed for f in files)
    if lines > limits.max_lines:
        found.append(Violation(Rule.TOO_MANY_LINES, "", f"{lines} lines > {limits.max_lines}"))
    return Validation(tuple(files), tuple(dict.fromkeys(found)))


class ApplyStatus(enum.StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"  # validator violation: a Scope Violation in the taxonomy
    FAILED = "failed"  # valid, but does not apply to c_bug


@dataclass(frozen=True)
class ApplyResult:
    status: ApplyStatus
    validation: Validation
    message: str = ""


def apply_patch(ws: Workspace, diff: str, limits: DiffLimits = DEFAULT_LIMITS) -> ApplyResult:
    """Validate `diff`, then apply it to the workspace copy of the repo with `git apply`."""
    validation = validate(diff, limits)
    if not validation.ok:
        return ApplyResult(ApplyStatus.REJECTED, validation, "; ".join(
            f"{v.rule}: {v.path} {v.message}".strip() for v in validation.violations
        ))  # fmt: skip
    if not validation.files:
        return ApplyResult(ApplyStatus.APPLIED, validation, "empty patch")
    repo = ws.repo.resolve()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        # The workspace may sit inside another repository (e.g. work/ in this one); never let
        # git discover it, or paths would be resolved against that repository's root.
        "GIT_CEILING_DIRECTORIES": str(repo.parent),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
        fh.write(diff if diff.endswith("\n") else diff + "\n")
        patch_file = Path(fh.name)
    try:
        for args in (["--check"], []):
            result = subprocess.run(
                ["git", "apply", *args, "-p1", "--whitespace=nowarn", str(patch_file)],
                cwd=repo, env=env, capture_output=True, text=True, check=False, timeout=60,
            )  # fmt: skip
            if result.returncode != 0:
                return ApplyResult(ApplyStatus.FAILED, validation, result.stderr.strip())
    finally:
        patch_file.unlink(missing_ok=True)
    return ApplyResult(ApplyStatus.APPLIED, validation)
