"""Output contracts (plan §6.3): parsing well-formed and malformed replies, and turning edit
blocks into a diff that the validator accepts and git applies."""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tracerepro.agent.edits import EditResult, apply_edits
from tracerepro.agent.parse import (
    SCOPE_VIOLATION,
    SYNTAX_OR_IMPORT_ERROR,
    ContractFailure,
    Edit,
    Failure,
    parse_edits,
    parse_repro,
)
from tracerepro.sandbox.validator import validate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_lib"

GOOD_PATCH = textwrap.dedent(
    """\
    The crash comes from max() on an empty list of rows.

    FILE: tiny_lib/report.py
    <<<<<<< SEARCH
    def _widest(rows: Sequence[Row]) -> int:
        return max(len(util.label(r)) for r in rows)
    =======
    def _widest(rows: Sequence[Row]) -> int:
        return max((len(util.label(r)) for r in rows), default=0)
    >>>>>>> REPLACE
    """
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE / "buggy", root)
    shutil.copytree(FIXTURE / "tests", root / "tests")
    return root


def edits_of(reply: str) -> list[Edit]:
    parsed = parse_edits(reply)
    assert not isinstance(parsed, ContractFailure), parsed
    return parsed


def failure_of(result: object) -> Failure:
    assert isinstance(result, ContractFailure), result
    return result.kind


# --- repro contract -----------------------------------------------------------------------


def test_repro_takes_the_single_python_block() -> None:
    reply = (
        "Empty rows crash.\n\n```python\nfrom tiny_lib import format_report\n\n"
        "def test_empty():\n    format_report([])\n```\nDone."
    )
    assert (
        parse_repro(reply) == "from tiny_lib import format_report\n\ndef test_empty():\n"
        "    format_report([])\n"
    )


def test_repro_ignores_non_python_blocks() -> None:
    reply = "Run:\n```bash\npytest\n```\n```py\ndef test_x():\n    assert 1\n```\n"
    assert parse_repro(reply) == "def test_x():\n    assert 1\n"


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        ("No code here.", Failure.NO_CODE_BLOCK),
        ("```python\n```", Failure.NO_CODE_BLOCK),
        ("```python\na = 1\n```\n```python\nb = 2\n```", Failure.SEVERAL_CODE_BLOCKS),
        ("```python\ndef test(): pass\n", Failure.UNCLOSED_CODE_BLOCK),
        ("x" * 400_001, Failure.TOO_LONG),
    ],
)
def test_malformed_repros_are_failed_attempts(reply: str, kind: Failure) -> None:
    result = parse_repro(reply)
    assert failure_of(result) is kind
    assert isinstance(result, ContractFailure) and result.label == SYNTAX_OR_IMPORT_ERROR


# --- patch contract -----------------------------------------------------------------------


def test_edit_blocks_are_parsed_verbatim() -> None:
    (edit,) = edits_of(GOOD_PATCH)
    assert edit.path == "tiny_lib/report.py"
    assert edit.search.startswith("def _widest") and edit.search.endswith("for r in rows)\n")
    assert "default=0" in edit.replace


def test_several_blocks_and_fences_around_them() -> None:
    reply = (
        "```\n" + GOOD_PATCH + "\nFILE: `tiny_lib/util.py`\n<<<<<<< SEARCH\n"
        "    return float(value)  # type: ignore[arg-type]\n=======\n"
        "    return float(value or 0)  # type: ignore[arg-type]\n>>>>>>> REPLACE\n```\n"
    )
    edits = edits_of(reply)
    assert [e.path for e in edits] == ["tiny_lib/report.py", "tiny_lib/util.py"]


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        ("I would change max() to use default=0.", Failure.NO_EDIT_BLOCK),
        ("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE", Failure.MALFORMED_EDIT_BLOCK),
        ("FILE: a.py\n<<<<<<< SEARCH\na\n=======\nb\n", Failure.MALFORMED_EDIT_BLOCK),
        ("FILE: a.py\n<<<<<<< SEARCH\na\n", Failure.MALFORMED_EDIT_BLOCK),
        ("FILE: a.py\n=======\n", Failure.MALFORMED_EDIT_BLOCK),
        (
            "FILE: a.py\n<<<<<<< SEARCH\nFILE: b.py\n=======\nb\n>>>>>>> REPLACE",
            Failure.MALFORMED_EDIT_BLOCK,
        ),
    ],
)
def test_malformed_patches_are_failed_attempts(reply: str, kind: Failure) -> None:
    result = parse_edits(reply)
    assert failure_of(result) is kind
    assert isinstance(result, ContractFailure) and result.label == SYNTAX_OR_IMPORT_ERROR


# --- edits to diff ------------------------------------------------------------------------


def test_edits_become_a_diff_the_validator_accepts_and_git_applies(tree: Path) -> None:
    result = apply_edits(tree, edits_of(GOOD_PATCH))
    assert isinstance(result, EditResult), result
    assert result.files == ("tiny_lib/report.py",)
    assert (tree / "tiny_lib/report.py").read_text().count("default=0") == 0  # tree untouched
    assert validate(result.diff).ok
    applied = subprocess.run(
        ["git", "apply", "-p1", "-"], cwd=tree, input=result.diff, text=True, capture_output=True
    )
    assert applied.returncode == 0, applied.stderr
    fixed = (FIXTURE / "fixed" / "tiny_lib" / "report.py").read_text()
    assert (tree / "tiny_lib/report.py").read_text() == fixed


def test_new_files_and_files_without_a_final_newline(tree: Path) -> None:
    (tree / "tiny_lib/flat.py").write_text("A = 1")  # no trailing newline
    edits = [
        Edit("tiny_lib/flat.py", "A = 1", "A = 2"),
        Edit("tiny_lib/extra.py", "", "B = 1\n"),
    ]
    result = apply_edits(tree, edits)
    assert isinstance(result, EditResult), result
    assert "new file mode" in result.diff and "\\ No newline at end of file" in result.diff
    assert validate(result.diff).ok
    applied = subprocess.run(
        ["git", "apply", "-p1", "-"], cwd=tree, input=result.diff, text=True, capture_output=True
    )
    assert applied.returncode == 0, applied.stderr
    assert (tree / "tiny_lib/flat.py").read_text() == "A = 2"
    assert (tree / "tiny_lib/extra.py").read_text() == "B = 1\n"


def test_edits_to_the_same_file_apply_in_order(tree: Path) -> None:
    edits = [
        Edit("tiny_lib/util.py", '    return str(name) if name is not None else "?"\n',
             '    return str(name) if name is not None else "-"\n'),
        Edit("tiny_lib/util.py", 'else "-"', 'else "--"'),
    ]  # fmt: skip
    result = apply_edits(tree, edits)
    assert (
        isinstance(result, EditResult)
        and '+    return str(name) if name is not None else "--"' in result.diff
    )


@pytest.mark.parametrize(
    ("edit", "kind"),
    [
        (Edit("tiny_lib/report.py", "not in the file\n", "x\n"), Failure.NO_MATCH),
        (Edit("tiny_lib/report.py", "util.", "x"), Failure.SEVERAL_MATCHES),
        (Edit("tiny_lib/missing.py", "a\n", "b\n"), Failure.NO_MATCH),
        (Edit("tiny_lib/report.py", "", "overwrite\n"), Failure.NO_MATCH),
        (Edit("tiny_lib/report.py", "Row = ", "Row = "), Failure.NO_CHANGE),
        (Edit("/etc/passwd", "root", "x"), Failure.BAD_PATH),
        (Edit("../outside.py", "a", "b"), Failure.BAD_PATH),
        (Edit("tiny_lib/../../outside.py", "a", "b"), Failure.BAD_PATH),
        (Edit(".git/config", "a", "b"), Failure.BAD_PATH),
        (Edit("tiny_lib\\report.py", "a", "b"), Failure.BAD_PATH),
    ],
)
def test_bad_edits_are_failed_attempts(tree: Path, edit: Edit, kind: Failure) -> None:
    result = apply_edits(tree, [edit])
    assert failure_of(result) is kind
    expected = SCOPE_VIOLATION if kind is Failure.BAD_PATH else SYNTAX_OR_IMPORT_ERROR
    assert isinstance(result, ContractFailure) and result.label == expected


def test_symlinks_are_not_followed(tree: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=abc\n")
    (tree / "tiny_lib/link.py").symlink_to(secret)
    assert (
        failure_of(apply_edits(tree, [Edit("tiny_lib/link.py", "TOKEN", "X")])) is Failure.BAD_PATH
    )


def test_a_test_edit_passes_parsing_but_the_validator_rejects_it(tree: Path) -> None:
    edit = Edit("tests/test_regression.py", "", "def test_skip():\n    pass\n")
    (tree / "tests/test_regression.py").unlink()
    result = apply_edits(tree, [edit])
    assert isinstance(result, EditResult)
    assert not validate(result.diff).ok  # a Scope Violation, ruled by the usual validator
