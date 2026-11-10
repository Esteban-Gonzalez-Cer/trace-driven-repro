"""Patch validator (plan §5.3): one passing and one rejected example per rule."""

import subprocess
from pathlib import Path

import pytest

from tracerepro.sandbox.paths import is_docs_path, is_library_frame, is_test_path
from tracerepro.sandbox.runner import Workspace, create_workspace
from tracerepro.sandbox.validator import (
    DEFAULT_LIMITS,
    ApplyStatus,
    DiffLimits,
    Rule,
    apply_patch,
    parse_diff,
    validate,
)

HERE = Path(__file__).parent
TINY = HERE.parent / "fixtures" / "tiny_lib"


def edit(path: str, old: str = "x = 1", new: str = "x = 2") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def create(path: str, body: str = "x = 1", mode: str = "100644") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode {mode}\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{body}\n"
    )


def rules(diff: str | bytes, limits: DiffLimits = DEFAULT_LIMITS) -> set[Rule]:
    return {v.rule for v in validate(diff, limits).violations}


# --- real gold patches ---------------------------------------------------------------------


def test_tiny_lib_gold_patch_passes() -> None:
    result = validate((TINY / "gold.patch").read_text())
    assert result.ok, result.violations
    assert [f.path for f in result.files] == ["tiny_lib/report.py"]
    assert result.changed_lines == 2


def test_flask_6096_gold_patch_passes() -> None:
    result = validate((HERE / "data" / "flask-6096-gold.patch").read_text())
    assert result.ok, result.violations
    assert result.changed_lines == 11  # app.py only; testing.py adds 2 more


def test_empty_patch_is_valid() -> None:
    result = validate("")
    assert result.ok
    assert result.files == ()


# --- one pass / one reject per rule --------------------------------------------------------


def test_path_traversal() -> None:
    assert rules(edit("src/pkg/mod.py")) == set()
    assert Rule.UNSAFE_PATH in rules(edit("src/../../etc/cron.d/x"))


def test_absolute_path() -> None:
    assert rules(edit("pkg/mod.py")) == set()
    diff = "--- a/pkg/mod.py\n+++ /etc/passwd\n@@ -1 +1 @@\n-a\n+b\n"
    assert rules(diff) == {Rule.MALFORMED}  # not b/-prefixed, so never handed to git apply
    absolute = "--- a//etc/passwd\n+++ b//etc/passwd\n@@ -1 +1 @@\n-a\n+b\n"
    assert Rule.UNSAFE_PATH in rules(absolute)


def test_symlink() -> None:
    assert rules(create("pkg/new.py")) == set()
    assert Rule.SYMLINK in rules(create("pkg/link.py", "/etc/passwd", mode="120000"))
    mode_change = "diff --git a/pkg/a.py b/pkg/a.py\nold mode 100644\nnew mode 120000\n"
    assert Rule.SYMLINK in rules(mode_change)


def test_other_file_modes() -> None:
    assert rules(create("pkg/tool.py", mode="100755")) == set()
    assert Rule.FILE_MODE in rules(create("pkg/sub", mode="160000"))


def test_binary() -> None:
    assert rules(edit("pkg/mod.py").encode()) == set()
    binary = (
        "diff --git a/pkg/blob.bin b/pkg/blob.bin\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "GIT binary patch\n"
        "literal 3\n"
        "Kcmb=I0ssI2000\n"
        "\n"
    )
    assert Rule.BINARY in rules(binary)
    assert Rule.BINARY in rules("diff --git a/x.so b/x.so\nBinary files a/x.so and b/x.so differ\n")
    assert rules(edit("pkg/mod.py", new="x = '\0'").encode()) == {Rule.BINARY}
    assert rules(b"\xff\xfe" + edit("pkg/mod.py").encode()) == {Rule.BINARY}


def test_test_edit() -> None:
    assert rules(edit("src/flask/testing.py")) == set()  # source module, not a test
    assert Rule.TEST_FILE in rules(edit("tests/test_basic.py"))
    assert Rule.TEST_FILE in rules(create("pkg/test_new.py"))
    assert Rule.TEST_FILE in rules(edit("pkg/mod_test.py"))
    assert Rule.TEST_FILE in rules(edit("tests/data/fixture.txt"))


def test_new_conftest() -> None:
    assert rules(create("pkg/contest.py")) == set()
    skip_all = "import pytest\\ncollect_ignore_glob = ['*']"
    assert Rule.CONFIG_FILE in rules(create("conftest.py", skip_all))
    assert Rule.CONFIG_FILE in rules(create("src/pkg/conftest.py"))


def test_sitecustomize_and_interpreter_config() -> None:
    assert rules(edit("pkg/customize.py")) == set()
    assert Rule.CONFIG_FILE in rules(create("src/sitecustomize.py"))
    assert Rule.CONFIG_FILE in rules(create("usercustomize.py"))
    assert Rule.CONFIG_FILE in rules(create("src/evil.pth", "import os"))
    for name in ("pytest.ini", "tox.ini", "setup.cfg"):
        assert Rule.CONFIG_FILE in rules(edit(name))


def test_workflow_edit() -> None:
    assert rules(edit("src/github/client.py")) == set()
    assert Rule.DEPENDENCY_OR_CI in rules(edit(".github/workflows/ci.yml"))


def test_dependency_files() -> None:
    assert rules(edit("pkg/requirements.py")) == set()
    for name in ("pyproject.toml", "setup.py", "requirements.txt", "requirements-dev.txt"):
        assert Rule.DEPENDENCY_OR_CI in rules(edit(name))


def test_oversized_diff_lines() -> None:
    limits = DiffLimits(max_files=3, max_lines=4)
    assert rules(edit("pkg/a.py") + edit("pkg/b.py"), limits) == set()  # 4 lines
    six = edit("pkg/a.py") + edit("pkg/b.py") + edit("pkg/c.py")
    assert Rule.TOO_MANY_LINES in rules(six, limits)


def test_oversized_diff_files() -> None:
    three = edit("pkg/a.py") + edit("pkg/b.py") + edit("pkg/c.py")
    assert rules(three) == set()
    assert Rule.TOO_MANY_FILES in rules(three + edit("pkg/d.py"))


def test_rename_checks_both_paths() -> None:
    rename = (
        "diff --git a/pkg/a.py b/tests/test_a.py\n"
        "similarity index 100%\n"
        "rename from pkg/a.py\n"
        "rename to tests/test_a.py\n"
    )
    assert Rule.TEST_FILE in rules(rename)


def test_malformed() -> None:
    assert rules(edit("pkg/a.py")) == set()
    assert rules(edit("pkg/a.py") + "rm -rf /\n") == {Rule.MALFORMED}
    truncated = "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1,3 +1,3 @@\n x\n-y\n"
    assert rules(truncated) == {Rule.MALFORMED}
    assert rules("@@ -1 +1 @@\n-a\n+b\n") == {Rule.MALFORMED}


# --- parser details ------------------------------------------------------------------------


def test_removed_line_starting_with_dashes_is_not_a_header() -> None:
    diff = (
        "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n"
        "--- this line is removed\n"
        "+++ and this one added\n"
        "-another\n"
    )
    (f,) = parse_diff(diff.replace("-another\n", ""))
    assert (f.removed, f.added) == (1, 1)
    assert rules(diff) == {Rule.MALFORMED}  # a line past the hunk's declared length


def test_plain_unified_diff_without_git_header() -> None:
    diff = "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n"
    (f,) = parse_diff(diff)
    assert f.path == "pkg/a.py"
    assert validate(diff).ok


def test_paths_classification() -> None:
    assert is_test_path("tests/conftest.py")
    assert not is_test_path("src/flask/testing.py")
    assert is_docs_path("CHANGES.rst") and is_docs_path("docs/api.rst")
    assert not is_docs_path("src/pkg/docs.py")
    assert is_library_frame("src/flask/app.py")
    assert not is_library_frame("tests/test_basic.py")
    assert not is_library_frame("/usr/local/lib/python3.11/enum.py")
    assert not is_library_frame("<frozen importlib._bootstrap>")


# --- applying ------------------------------------------------------------------------------


def _tiny_workspace(tmp_path: Path) -> Workspace:
    ws = create_workspace(tmp_path / "ws")
    for f in (TINY / "buggy").rglob("*.py"):
        target = ws.repo / f.relative_to(TINY / "buggy")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.read_bytes())
    return ws


def test_apply_gold_patch(tmp_path: Path) -> None:
    ws = _tiny_workspace(tmp_path)
    result = apply_patch(ws, (TINY / "gold.patch").read_text())
    assert result.status is ApplyStatus.APPLIED, result.message
    fixed = (TINY / "fixed" / "tiny_lib" / "report.py").read_text()
    assert (ws.repo / "tiny_lib" / "report.py").read_text() == fixed


@pytest.mark.parametrize(
    ("diff", "status"),
    [
        (edit("tests/test_report.py"), ApplyStatus.REJECTED),
        (edit("tiny_lib/report.py", old="no such line"), ApplyStatus.FAILED),
        ("", ApplyStatus.APPLIED),
    ],
)
def test_apply_rejects_or_fails_without_touching_files(
    tmp_path: Path, diff: str, status: ApplyStatus
) -> None:
    ws = _tiny_workspace(tmp_path)
    before = (ws.repo / "tiny_lib" / "report.py").read_text()
    assert apply_patch(ws, diff).status is status
    assert (ws.repo / "tiny_lib" / "report.py").read_text() == before


def test_apply_ignores_an_enclosing_git_repository(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    ws = _tiny_workspace(tmp_path)  # tmp_path/ws/repo sits inside the outer repository
    result = apply_patch(ws, (TINY / "gold.patch").read_text())
    assert result.status is ApplyStatus.APPLIED, result.message
    assert "default=0" in (ws.repo / "tiny_lib" / "report.py").read_text()


def test_test_paths_are_judged_case_insensitively() -> None:
    # Red-team round 2: on a case-insensitive disk, Tests/__init__.py lands in tests/.
    for path in ["Tests/__init__.py", "TESTS/helpers.py", "pkg/Conftest.py", "Setup.CFG"]:
        assert rules(create(path)) & {Rule.TEST_FILE, Rule.CONFIG_FILE}, path


def test_patch_size_in_bytes_is_capped() -> None:
    assert rules(create("pkg/mod.py", "x = '" + "a" * 70_000 + "'")) == {Rule.TOO_LARGE}
