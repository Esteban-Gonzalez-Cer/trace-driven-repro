"""Pipeline B2's crash check (Phase 5b): parsed from the reply, run on the buggy code, then with
the patch; a soft signal that never blocks a patch. Scripted model and test runs: no Docker, no
plan usage."""

from pathlib import Path

from tracerepro.agent.client import Settings
from tracerepro.agent.crash_check import CHECK_PATH, after_patch, parse_check, run_on_bug
from tracerepro.agent.workbench import TrialRun
from tracerepro.sandbox.runner import CaseResult, FailureInfo

from .test_pipeline import BREAKS_LABEL, CLEAN, GOLD, REGRESSED, FakeBench, run
from .test_repro_gate import PASSES, REPRO, TARGET, TEST_ID, crash, patched, run_of
from .test_repro_gate import payload as gate_payload

NEIGHBOUR_ID = f"{CHECK_PATH}::test_one_row"


def b2(tmp_path: Path, replies: list, runs: list[TrialRun], **extra: object):  # type: ignore[no-untyped-def]
    config = {"crash_check": True, "prompt_version": "v3", "payload": gate_payload(), **extra}
    return run(tmp_path, replies, runs, **config)


# --- parsing ------------------------------------------------------------------------------


def test_the_check_is_the_one_python_block_that_is_not_an_edit() -> None:
    assert parse_check(GOLD + "\n" + REPRO) == (
        "from tiny_lib import format_report\n\ndef test_empty_report():\n"
        '    assert "TOTAL" in format_report([])\n'
    )
    fenced_edits = "```\n" + GOLD + "\n```\n" + REPRO
    assert parse_check(fenced_edits) is not None  # an edit block in a fence is not a check
    assert parse_check(GOLD) is None
    assert parse_check(REPRO + REPRO) is None  # two candidates: no check, never a failure
    assert parse_check("```python\n\n```") is None


# --- on the buggy code and with the patch -------------------------------------------------


def test_reproducing_tests_are_recorded(tmp_path: Path) -> None:
    bench = FakeBench(tmp_path, [crash()])
    check = run_on_bug("code", bench, TARGET)
    assert check.usable and check.reproducing == (TEST_ID,)
    assert bench.diffs == [""] and bench.baselines == [False]
    assert bench.extra == [{CHECK_PATH: b"code"}]
    not_reproducing = run_on_bug("code", FakeBench(tmp_path / "x", [PASSES]), TARGET)
    assert not not_reproducing.usable and not_reproducing.gate.verdict == "no_repro"


def test_after_the_patch(tmp_path: Path) -> None:
    check = run_on_bug("code", FakeBench(tmp_path, [crash()]), TARGET)
    assert after_patch(check, patched(), TARGET) is None
    still = after_patch(check, patched(repro_ok=False), TARGET)
    assert still is not None and TEST_ID in still
    # A neighbouring test may fail for its own reasons (an expected value the model guessed)...
    guessed = run_of(
        CaseResult(TEST_ID, "call", "passed"),
        CaseResult(
            NEIGHBOUR_ID,
            "call",
            "failed",
            FailureInfo("AssertionError", "1 != 2", ((CHECK_PATH, "t", 3),)),
        ),
    )
    assert after_patch(check, guessed, TARGET) is None
    # ...but never with the reported error.
    frames = ((CHECK_PATH, "test_one_row", 4), ("tiny_lib/report.py", "_widest", 11))
    reported = run_of(
        CaseResult(TEST_ID, "call", "passed"),
        CaseResult(NEIGHBOUR_ID, "call", "failed", FailureInfo("ValueError", "max()", frames)),
    )
    assert NEIGHBOUR_ID in (after_patch(check, reported, TARGET) or "")
    assert "did not finish" in (after_patch(check, run_of(finished=False), TARGET) or "")


# --- the B2 loop --------------------------------------------------------------------------


def test_a_patch_that_passes_its_check_is_submitted(tmp_path: Path) -> None:
    result, provider, bench = b2(tmp_path, [GOLD + REPRO], [crash(), patched()])
    (attempt,) = result.attempts
    assert attempt.outcome == "clean" and attempt.check == "valid"
    assert attempt.check_after == "passes" and result.check is not None
    assert bench.extra[1] == {CHECK_PATH: result.check.encode()}  # run with the patch too
    assert provider.requests[0].prompt_version == "patch_v3"
    assert set(result.prompts) == {"system_v3", "patch_v3", "feedback_v3"}


def test_a_failing_check_is_fed_back_and_the_retry_escalates(tmp_path: Path) -> None:
    retry = Settings("claude-opus-5-5", "high", 4000)
    result, provider, _ = b2(
        tmp_path,
        [BREAKS_LABEL + REPRO, GOLD + REPRO],
        [crash(), patched(repro_ok=False), patched()],
        retry_settings=retry,
    )
    first, second = result.attempts
    assert first.outcome == "check_still_fails" and first.label == "incorrect_patch"
    assert first.check_after == "still_fails" and second.check_after == "passes"
    assert result.final_from == 2
    feedback = provider.requests[1].messages[2].content
    assert "Your crash check still fails" in feedback and TEST_ID in feedback
    assert provider.requests[0].settings.model == "claude-haiku-5-5"
    assert provider.requests[1].settings == retry and second.model == "claude-opus-5-5"


def test_a_check_that_does_not_reproduce_is_not_used(tmp_path: Path) -> None:
    result, _, bench = b2(tmp_path, [GOLD + REPRO], [PASSES, CLEAN])
    (attempt,) = result.attempts
    assert attempt.outcome == "clean" and attempt.check == "no_repro"
    assert attempt.check_after is None and bench.extra[1] == {}  # the patch is judged as in B


def test_an_unused_check_is_explained_in_the_feedback(tmp_path: Path) -> None:
    result, provider, _ = b2(tmp_path, [BREAKS_LABEL + REPRO, GOLD], [PASSES, REGRESSED, CLEAN])
    assert [a.outcome for a in result.attempts] == ["regressions", "clean"]
    feedback = provider.requests[1].messages[2].content
    assert "Your crash check was not used" in feedback and "does not reproduce" in feedback


def test_no_check_behaves_like_pipeline_b(tmp_path: Path) -> None:
    result, _, bench = b2(tmp_path, [GOLD], [CLEAN])
    (attempt,) = result.attempts
    assert attempt.outcome == "clean" and attempt.check == "none" and result.check is None
    assert bench.extra == [{}] and len(bench.diffs) == 1


def test_switches_off_leave_pipeline_b_unchanged(tmp_path: Path) -> None:
    result, provider, bench = run(tmp_path, [GOLD + REPRO], [CLEAN])
    assert result.attempts[0].check is None and bench.extra == [{}]
    assert provider.requests[0].prompt_version == "patch_v2"
