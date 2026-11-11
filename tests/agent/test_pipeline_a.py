"""Pipeline A (plan §6.5) and its repro gate, with a scripted model and scripted test runs: no
Docker, no plan usage."""

import json
from pathlib import Path
from typing import Any

from tracerepro.agent.client import LLMClient, Settings
from tracerepro.agent.pipeline import Bug, PipelineConfig, run_pipeline_a
from tracerepro.agent.providers.fake import FakeProvider
from tracerepro.agent.repro_gate import REPRO_PATH, Target, judge, payload_target, repro_passes
from tracerepro.agent.workbench import TrialRun
from tracerepro.dataset.manifest import PROJECT_ROOT
from tracerepro.sandbox.runner import CaseResult, FailureInfo, PytestRun, RunResult

from .test_pipeline import CLEAN, GOLD, FakeBench

TEST_ID = f"{REPRO_PATH}::test_empty_report"
REPRO = '```python\nfrom tiny_lib import format_report\n\ndef test_empty_report():\n' \
    '    assert "TOTAL" in format_report([])\n```\n'  # fmt: skip
TARGET = Target("ValueError", "tiny_lib/report.py", "_widest")


def payload() -> dict[str, Any]:
    frames = [
        {"filename": "tiny_lib/report.py", "function": "format_report", "lineno": 21,
         "in_app": True},
        {"filename": "tiny_lib/report.py", "function": "_widest", "lineno": 11, "in_app": True},
    ]  # fmt: skip
    value = {"type": "ValueError", "module": "builtins", "value": "max() arg is an empty sequence",
             "stacktrace": {"frames": frames}}  # fmt: skip
    return {"exception": {"values": [value]}}


def run_of(*cases: CaseResult, finished: bool = True) -> TrialRun:
    result = RunResult(1, "pytest output\n1 failed", 1.0, False, False, completed=True)
    run = PytestRun(result, 1 if finished else None, tuple(cases))
    return TrialRun(applied=True, finished=finished, output_tail="1 failed", run=run)


def crash(
    exc: str = "ValueError", file: str = "tiny_lib/report.py", fn: str = "_widest"
) -> TrialRun:
    frames = (("repro/test_repro.py", "test_empty_report", 4),
              ("tiny_lib/report.py", "format_report", 21), (file, fn, 11))  # fmt: skip
    failure = FailureInfo(exc, "max() arg is an empty sequence", frames)
    return run_of(CaseResult(TEST_ID, "call", "failed", failure, "E   ValueError: max() arg"))


PASSES = run_of(CaseResult(TEST_ID, "call", "passed"))
BROKEN = run_of(CaseResult(REPRO_PATH, "collect", "error", None, "ImportError: no module x"))
ASSERTS = run_of(
    CaseResult(TEST_ID, "call", "failed",
               FailureInfo("AssertionError", "assert 1 == 2", (("repro/test_repro.py", "t", 3),)))
)  # fmt: skip


def patched(repro_ok: bool = True, regressions: tuple[str, ...] = ()) -> TrialRun:
    case = CaseResult(TEST_ID, "call", "passed" if repro_ok else "failed")
    trial = run_of(case)
    return TrialRun(applied=True, regressions=regressions, passed=3, output_tail="ok",
                    run=trial.run)  # fmt: skip


def run_a(tmp_path: Path, replies: list[Any], runs: list[TrialRun]) -> tuple[Any, Any, Any]:
    provider = FakeProvider.replying(replies)
    client = LLMClient(provider, log_path=tmp_path / "calls.jsonl")
    bench = FakeBench(tmp_path, runs)
    config = PipelineConfig(settings=Settings("claude-haiku-5-5", "low", 4000))
    return run_pipeline_a(Bug("tiny_lib-001", payload()), client, bench, config), provider, bench


# --- the gate -----------------------------------------------------------------------------


def test_the_target_comes_from_the_payload() -> None:
    assert payload_target(payload()) == TARGET
    real = json.loads((PROJECT_ROOT / "benchmark/payloads/marshmallow-3082.json").read_text())
    target = payload_target(real)
    assert target.exception == "marshmallow.exceptions.ValidationError"
    assert (target.file, target.function) == ("src/marshmallow/schema.py", "_do_load")


def test_gate_verdicts() -> None:
    assert judge(crash(), TARGET).verdict == "valid"
    assert judge(PASSES, TARGET).verdict == "no_repro"
    wrong_type = judge(crash(exc="TypeError"), TARGET)
    assert wrong_type.verdict == "invalid_repro" and "TypeError" in wrong_type.detail
    wrong_frame = judge(crash(file="tiny_lib/util.py", fn="label"), TARGET)
    assert wrong_frame.verdict == "invalid_repro" and "util.py" in wrong_frame.detail
    in_test = judge(ASSERTS, TARGET)
    assert in_test.verdict == "invalid_repro" and "raised in the test itself" in in_test.detail
    broken = judge(BROKEN, TARGET)
    assert broken.verdict == "broken" and broken.label == "syntax_or_import_error"
    assert judge(run_of(finished=False), TARGET).verdict == "invalid_repro"
    assert judge(run_of(), TARGET).verdict == "broken"  # no test collected


def test_repro_must_pass_after_the_patch() -> None:
    assert repro_passes(patched()) is None
    assert "still fails" in (repro_passes(patched(repro_ok=False)) or "")


# --- the pipeline -------------------------------------------------------------------------


def test_valid_repro_then_clean_patch(tmp_path: Path) -> None:
    result, provider, bench = run_a(tmp_path, [REPRO, GOLD], [crash(), patched()])
    assert result.status == "submitted" and result.pipeline == "A"
    assert [(a.stage, a.outcome) for a in result.attempts] == [
        ("repro", "valid"),
        ("patch", "clean"),
    ]
    assert result.repro is not None and "format_report([])" in result.repro
    # The gate ran the test alone on the unchanged code; the patch ran it with the baseline.
    assert bench.diffs[0] == "" and bench.baselines == [False, True]
    assert set(bench.extra[0]) == set(bench.extra[1]) == {REPRO_PATH}
    patch_request = provider.requests[1]
    assert patch_request.prompt_version == "patch_v2"
    last = patch_request.messages[-1].content
    assert "<data-repro-test-" in last and "<data-repro-result-" in last
    assert "as required" in last
    assert set(result.prompts) >= {"repro_v2", "repro_feedback_v2", "patch_v2"}


def test_a_repro_that_passes_twice_stops_the_run(tmp_path: Path) -> None:
    result, provider, bench = run_a(tmp_path, [REPRO, REPRO], [PASSES, PASSES])
    assert result.status == "no_repro" and result.final_diff is None
    assert len(provider.requests) == 2 and len(bench.diffs) == 2  # no patch was asked for
    retry = provider.requests[1]
    assert retry.prompt_version == "repro_feedback_v2"
    assert "passes on the current code" in retry.messages[-1].content


def test_wrong_crash_then_valid_repro(tmp_path: Path) -> None:
    result, _, _ = run_a(tmp_path, [REPRO, REPRO, GOLD], [crash(exc="TypeError"), crash(),
                                                         patched()])  # fmt: skip
    assert [a.outcome for a in result.attempts] == ["invalid_repro", "valid", "clean"]
    assert result.attempts[0].label == "invalid_repro" and result.status == "submitted"


def test_unusable_repros_end_as_invalid_repro(tmp_path: Path) -> None:
    result, _, bench = run_a(tmp_path, ["I need more files.", REPRO], [BROKEN])
    assert [a.outcome for a in result.attempts] == ["declined", "broken"]
    assert [a.label for a in result.attempts] == ["declined", "syntax_or_import_error"]
    assert result.status == "invalid_repro" and len(bench.diffs) == 1


def test_a_patch_must_make_the_repro_pass(tmp_path: Path) -> None:
    replies = [REPRO, GOLD, GOLD]
    result, provider, _ = run_a(tmp_path, replies, [crash(), patched(repro_ok=False), patched()])
    assert [a.outcome for a in result.attempts] == ["valid", "repro_still_fails", "clean"]
    assert "still fails" in provider.requests[2].messages[-1].content
    assert result.final_from == 2  # the patch stage's own numbering


def test_when_no_patch_makes_the_repro_pass_the_last_applied_is_submitted(
    tmp_path: Path,
) -> None:
    replies = [REPRO, GOLD, GOLD]
    runs = [crash(), patched(repro_ok=False), patched(repro_ok=False)]
    result, _, _ = run_a(tmp_path, replies, runs)
    assert result.status == "submitted" and result.final_from == 2
    assert CLEAN.clean  # (sanity: the shared fixture is still a clean run)
