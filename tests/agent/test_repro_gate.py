"""The repro gate (plan §6.5), kept after Pipeline A was retired: Pipeline B2 reuses it for its
crash check. Scripted test runs: no Docker, no plan usage."""

import json
from typing import Any

from tracerepro.agent.repro_gate import REPRO_PATH, Target, judge, payload_target, repro_passes
from tracerepro.agent.workbench import TrialRun
from tracerepro.dataset.manifest import PROJECT_ROOT
from tracerepro.sandbox.runner import CaseResult, FailureInfo, PytestRun, RunResult

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
