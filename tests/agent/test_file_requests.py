"""Pipeline B2's file requests (Phase 5b): the model may ask once for up to 3 more library files.
The harness checks every path itself, against the read-only source tree; tests, configuration,
packaging, hidden files, symlinks and anything outside the tree are refused. No Docker, no plan
usage."""

import os
from pathlib import Path

from tracerepro.agent.pipeline import parse_file_request
from tracerepro.context.assembler import merge_shown, requested_files

from .test_pipeline import CLEAN, GOLD, FakeBench, run

SHOWN = {"tiny_lib/report.py": [[1, 25]], "tiny_lib/util.py": [[1, 9]]}


def tree(tmp_path: Path) -> Path:
    root = FakeBench(tmp_path, []).source_tree()
    (root / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    (root / "conftest.py").write_text("collect_ignore = []\n")
    (root / "tiny_lib" / ".secret.py").write_text("TOKEN = 'x'\n")
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    os.symlink(outside, root / "tiny_lib" / "linked.py")
    return root


def test_only_library_source_is_granted(tmp_path: Path) -> None:
    root = tree(tmp_path)
    util_len = len((root / "tiny_lib" / "util.py").read_text().splitlines())
    shown = {"tiny_lib/report.py": SHOWN["tiny_lib/report.py"], "tiny_lib/util.py": [[1, util_len]]}
    granted, refused = requested_files(
        root, ["tiny_lib/__init__.py", "../outside.py", "tests/test_report.py"], shown
    )
    assert [(f.path, f.role, f.shape) for f in granted] == [
        ("tiny_lib/__init__.py", "requested", "whole")
    ]
    assert set(refused) == {"../outside.py", "tests/test_report.py"}
    for bad in (
        "setup.py",
        "conftest.py",
        "tiny_lib/.secret.py",
        "/etc/passwd",
        "tiny_lib\\report.py",
        "tiny_lib/missing.py",
        "tiny_lib/linked.py",
    ):
        assert requested_files(root, [bad], shown)[1], bad
    assert requested_files(root, ["tiny_lib/util.py"], shown)[1] == {
        "tiny_lib/util.py": "already shown in full"
    }
    many = ["tiny_lib/__init__.py"] * 3 + ["tiny_lib/util.py"]
    assert "at most 3" in requested_files(root, many, {})[1]["tiny_lib/util.py"]


def test_a_large_file_is_outlined_or_refused(tmp_path: Path) -> None:
    root = tree(tmp_path)
    (root / "tiny_lib" / "big.py").write_text(
        "".join(f"def f{i}():\n    return {i}\n\n" for i in range(200))
    )
    (granted,), _ = requested_files(root, ["tiny_lib/big.py"], {}, budget=8_000)
    assert granted.shape == "outline" and len(granted.render()) <= 8_000
    assert requested_files(root, ["tiny_lib/big.py"], {}, budget=50)[1] == {
        "tiny_lib/big.py": "too large to show"
    }


def test_shown_lines_merge() -> None:
    from tracerepro.context.assembler import ContextFile

    extra = ContextFile("a.py", "requested", ((3, "x"), (4, "y")), 9)
    assert merge_shown({"a.py": [[1, 2], [7, 9]], "b.py": [[1, 1]]}, [extra]) == {
        "a.py": [[1, 4], [7, 9]],
        "b.py": [[1, 1]],
    }


def test_request_lines_are_parsed() -> None:
    assert parse_file_request("REQUEST FILES: a.py, `b/c.py`") == ["a.py", "`b/c.py`"]
    assert parse_file_request("**REQUEST FILES:** a.py") == ["a.py"]
    assert parse_file_request(GOLD) is None


def b2(tmp_path: Path, replies: list, runs: list, **extra: object):  # type: ignore[no-untyped-def]
    return run(tmp_path, replies, runs, file_requests=True, prompt_version="v3", **extra)


def test_requested_files_arrive_before_the_first_attempt(tmp_path: Path) -> None:
    ask = "I need the package's init.\nREQUEST FILES: tiny_lib/__init__.py, ../etc/passwd"
    result, provider, bench = b2(tmp_path, [ask, GOLD], [CLEAN])
    assert [a.outcome for a in result.attempts] == ["clean"] and result.final_from == 1
    assert result.file_request is not None
    assert result.file_request["granted"] == ["tiny_lib/__init__.py (whole)"]
    assert set(result.file_request["refused"]) == {"../etc/passwd"}
    first, second = provider.requests
    assert "REQUEST FILES:" in first.messages[0].content
    assert second.prompt_version == "files_v3"
    assert "<data-requested-files-" in second.messages[2].content
    assert "Not shown" in second.messages[2].content
    assert "tiny_lib/__init__.py" in result.context["shown_lines"]
    assert len(bench.diffs) == 1  # the request cost no attempt and ran nothing
    assert {"request_v3", "files_v3"} <= set(result.prompts)


def test_only_one_request_is_answered(tmp_path: Path) -> None:
    replies = ["REQUEST FILES: tiny_lib/__init__.py", "REQUEST FILES: tiny_lib/util.py", GOLD]
    result, _, _ = b2(tmp_path, replies, [CLEAN])
    assert [a.outcome for a in result.attempts] == ["declined", "clean"]


def test_a_declined_reply_is_reminded_of_an_unused_request(tmp_path: Path) -> None:
    # Practice round 1 (2026-11-16): flask-5797 declined, was told "No more files can be shown"
    # although its one request was unused, and guessed.
    replies = ["Not a library bug.", "REQUEST FILES: tiny_lib/__init__.py", GOLD]
    result, provider, _ = b2(tmp_path, replies, [CLEAN])
    assert [a.outcome for a in result.attempts] == ["declined", "clean"]
    feedback = provider.requests[1].messages[2].content
    assert "ask for it now" in feedback and "No more files" not in feedback
    assert result.file_request is not None
    replies = ["REQUEST FILES: tiny_lib/__init__.py", "No.", GOLD]
    used, _, _ = b2(tmp_path / "used", replies, [CLEAN])
    assert "No more files" in (used.attempts[0].feedback or "")


def test_a_request_with_a_fix_is_a_fix(tmp_path: Path) -> None:
    result, _, _ = b2(tmp_path, ["REQUEST FILES: tiny_lib/__init__.py\n" + GOLD], [CLEAN])
    assert [a.outcome for a in result.attempts] == ["clean"] and result.file_request is None


def test_switch_off_means_no_request(tmp_path: Path) -> None:
    result, provider, _ = run(tmp_path, ["REQUEST FILES: tiny_lib/__init__.py", GOLD], [CLEAN])
    assert [a.outcome for a in result.attempts] == ["declined", "clean"]
    assert "REQUEST FILES" not in provider.requests[0].messages[0].content
