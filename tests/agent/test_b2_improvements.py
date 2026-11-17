"""Phase 5c, two changes to Pipeline B2 found in the second study's failed trials:

- **Definition requests.** A requested file too large to show whole was sent again as the
  outline the model had already seen (jinja2-1702 and -1852: `compiler.py`), so the model edited
  code it could not see. Requests may now name `path::Name` or `path::Class.method`, a large file
  whose outline is already shown is refused with a pointer to that, and a round that showed
  nothing new does not use up the request.
- **Check repair.** A patch that ran clean while its crash check did not reproduce the crash
  was submitted untested (9 B2 trials on 5 bugs). The model now gets one chance to fix the
  check, and the patch is run with the corrected one.

Scripted model and test runs: no Docker, no plan usage."""

import os
from pathlib import Path

import pytest

from tracerepro.agent.crash_check import CHECK_PATH
from tracerepro.context.assembler import _outline_lines, requested_files

from .test_pipeline import BREAKS_LABEL, CLEAN, GOLD, FakeBench, run
from .test_repro_gate import PASSES, REPRO, TEST_ID, crash, patched
from .test_repro_gate import payload as gate_payload

BIG = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(200)) + (
    "class Box:\n    '''A box.'''\n\n    size = 3\n\n    def area(self):\n        return 9\n\n"
    "    def volume(self):\n        return 27\n"
)
REPRO2 = (
    "```python\nfrom tiny_lib import format_report\n\ndef test_crash():\n"
    "    format_report([])\n```\n"
)


def tree(tmp_path: Path) -> Path:
    root = FakeBench(tmp_path, []).source_tree()
    (root / "tiny_lib" / "big.py").write_text(BIG)
    (root / "tiny_lib" / ".secret.py").write_text("TOKEN = 'x'\n")
    outside = tmp_path / "outside.py"
    outside.write_text("def x():\n    return 1\n")
    os.symlink(outside, root / "tiny_lib" / "linked.py")
    return root


def lines_of(granted: object) -> list[str]:
    return [text for _, text in granted.lines]  # type: ignore[attr-defined]


# --- the assembler ------------------------------------------------------------------------


def test_named_definitions_are_shown_whole_and_together(tmp_path: Path) -> None:
    root = tree(tmp_path)
    (granted,), refused = requested_files(
        root, ["tiny_lib/big.py::f3", "tiny_lib/big.py::Box.volume"], {}, definitions=True
    )
    assert refused == {}
    assert granted.path == "tiny_lib/big.py" and granted.shape == "definitions"
    shown = lines_of(granted)
    assert "def f3():" in shown and "    return 3" in shown
    assert "class Box:" in shown and "    def volume(self):" in shown  # with its class header
    assert "def f4():" not in shown and "    def area(self):" not in shown
    assert "definitions of" in granted.render()


def test_a_large_file_whose_outline_was_shown_is_not_sent_again(tmp_path: Path) -> None:
    root = tree(tmp_path)
    keep = _outline_lines(BIG.splitlines())
    assert keep is not None
    outline_shown = {"tiny_lib/big.py": [[n, n] for n in sorted(keep)]}
    granted, refused = requested_files(
        root, ["tiny_lib/big.py"], outline_shown, budget=8_000, definitions=True
    )
    assert granted == [] and "tiny_lib/big.py::Name" in refused["tiny_lib/big.py"]
    # Not shown before: the outline is still worth sending.
    (outline,), _ = requested_files(root, ["tiny_lib/big.py"], {}, budget=8_000, definitions=True)
    assert outline.shape == "outline"


def test_definition_requests_are_checked_like_files(tmp_path: Path) -> None:
    root = tree(tmp_path)
    for bad in (
        "../outside.py::x",
        "tests/test_report.py::test_empty",
        "tiny_lib/.secret.py::TOKEN",
        "tiny_lib/linked.py::x",
        "/etc/passwd::root",
        "tiny_lib/missing.py::f",
        "tiny_lib/big.py::nothing",
        "tiny_lib/big.py::Box.nothing",
        "tiny_lib/big.py::size",  # an attribute, not a definition
    ):
        granted, refused = requested_files(root, [bad], {}, definitions=True)
        assert granted == [] and set(refused) == {bad}, bad
    whole = {"tiny_lib/report.py": [[1, 200]]}
    assert requested_files(root, ["tiny_lib/report.py::_widest"], whole, definitions=True)[1] == {
        "tiny_lib/report.py::_widest": "already shown in full"
    }


def test_definition_requests_have_limits(tmp_path: Path) -> None:
    root = tree(tmp_path)
    files = ["tiny_lib/big.py::f1", "tiny_lib/util.py", "tiny_lib/__init__.py", "tiny_lib/x.py"]
    _, refused = requested_files(root, files, {}, definitions=True)
    assert "at most 3 files" in refused["tiny_lib/x.py"]
    many = [f"tiny_lib/big.py::f{i}" for i in range(11)]
    granted, refused = requested_files(root, many, {}, definitions=True)
    assert set(refused) == {"tiny_lib/big.py::f10"} and len(granted) == 1


def test_without_the_switch_a_definition_is_not_a_path(tmp_path: Path) -> None:
    root = tree(tmp_path)
    granted, refused = requested_files(root, ["tiny_lib/big.py::f1"], {})
    assert granted == [] and "not a library source" in refused["tiny_lib/big.py::f1"]


# --- the B2 loop: request rounds ----------------------------------------------------------


def b2(tmp_path: Path, replies: list, runs: list, **extra: object):  # type: ignore[no-untyped-def]
    config = {"file_requests": True, "request_definitions": True, "prompt_version": "v4"}
    return run(tmp_path, replies, runs, **{**config, **extra})


def test_a_round_that_showed_nothing_does_not_use_up_the_request(tmp_path: Path) -> None:
    replies = ["REQUEST FILES: tiny_lib/missing.py", "REQUEST FILES: tiny_lib/__init__.py", GOLD]
    result, provider, bench = b2(tmp_path, replies, [CLEAN])
    assert [a.outcome for a in result.attempts] == ["clean"]
    assert [r.prompt_version for r in provider.requests] == ["patch_v4", "morefiles_v4", "files_v4"]
    request = result.file_request
    assert request is not None and request["rounds"] == 2
    assert request["requested"] == ["tiny_lib/missing.py", "tiny_lib/__init__.py"]
    assert request["granted"] == ["tiny_lib/__init__.py (whole)"]
    assert "tiny_lib/missing.py" in request["refused"]
    assert "path::ClassName.method" in provider.requests[0].messages[0].content
    assert {"request_v4", "files_v4", "morefiles_v4"} <= set(result.prompts)
    assert len(bench.diffs) == 1  # requests cost no attempt


def test_at_most_two_request_rounds(tmp_path: Path) -> None:
    replies = ["REQUEST FILES: tiny_lib/a.py", "REQUEST FILES: tiny_lib/b.py"]
    replies += ["REQUEST FILES: tiny_lib/__init__.py", GOLD]
    result, provider, _ = b2(tmp_path, replies, [CLEAN])
    assert [a.outcome for a in result.attempts] == ["declined", "clean"]
    assert provider.requests[2].prompt_version == "files_v4"
    assert result.file_request is not None and result.file_request["rounds"] == 2


def test_a_granted_round_ends_the_requests(tmp_path: Path) -> None:
    replies = ["REQUEST FILES: tiny_lib/__init__.py", "REQUEST FILES: tiny_lib/util.py", GOLD]
    result, _, _ = b2(tmp_path, replies, [CLEAN])
    assert [a.outcome for a in result.attempts] == ["declined", "clean"]
    assert result.file_request is not None and result.file_request["rounds"] == 1


# --- the B2 loop: check repair ------------------------------------------------------------


def repair(tmp_path: Path, replies: list, runs: list, **extra: object):  # type: ignore[no-untyped-def]
    config = {
        "crash_check": True,
        "check_repair": True,
        "prompt_version": "v4",
        "payload": gate_payload(),
    }
    return run(tmp_path, replies, runs, **{**config, **extra})


def test_a_repaired_check_confirms_the_patch(tmp_path: Path) -> None:
    result, provider, bench = repair(
        tmp_path, [GOLD + REPRO, REPRO2], [PASSES, CLEAN, crash(), patched()]
    )
    (attempt,) = result.attempts
    assert attempt.outcome == "clean" and attempt.check == "no_repro"
    assert attempt.check_repair == "valid" and attempt.check_after == "passes"
    assert provider.requests[1].prompt_version == "repair_v4"
    assert "<data-check-run-" in provider.requests[1].messages[2].content
    assert result.check is not None and "test_crash" in result.check
    # The patch runs once more, with the corrected check only (the baseline already passed).
    assert bench.diffs[3] == attempt.diff and bench.baselines[3] is False
    assert bench.extra[3] == {CHECK_PATH: result.check.encode()}
    assert "repair_v4" in result.prompts


def test_a_repaired_check_that_still_fails_sends_the_patch_back(tmp_path: Path) -> None:
    result, provider, _ = repair(
        tmp_path,
        [BREAKS_LABEL + REPRO, REPRO2, GOLD + REPRO2],
        [PASSES, CLEAN, crash(), patched(repro_ok=False), patched()],
    )
    first, second = result.attempts
    assert first.outcome == "check_still_fails" and first.label == "incorrect_patch"
    assert first.check_repair == "valid" and first.check_after == "still_fails"
    assert second.outcome == "clean" and second.check_after == "passes"
    assert result.final_from == 2
    feedback = provider.requests[2].messages[4].content
    assert "Your crash check still fails" in feedback and TEST_ID in feedback


def test_no_check_after_repair_submits_the_patch(tmp_path: Path) -> None:
    result, provider, bench = repair(tmp_path, [GOLD + REPRO, "NO CHECK"], [PASSES, CLEAN])
    (attempt,) = result.attempts
    assert attempt.outcome == "clean" and attempt.check_repair == "none"
    assert attempt.check_after is None and len(provider.requests) == 2 and len(bench.diffs) == 2


@pytest.mark.parametrize(
    ("replies", "runs", "extra"),
    [
        ([GOLD], [CLEAN], {}),  # no check sent: nothing to repair
        ([GOLD + REPRO], [crash(), patched()], {}),  # the check reproduced
        ([GOLD + REPRO], [PASSES, CLEAN], {"max_patch_attempts": 1}),  # no attempt left
        ([GOLD + REPRO], [PASSES, CLEAN], {"check_repair": False}),  # switch off
    ],
)
def test_no_repair_when_it_cannot_help(
    tmp_path: Path, replies: list, runs: list, extra: dict
) -> None:
    result, provider, _ = repair(tmp_path, replies, runs, **extra)
    assert len(provider.requests) == 1 and result.attempts[0].check_repair is None
