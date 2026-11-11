"""Integrity checks (Amendment 4, layer 2), behaviour comparison and the review report, on
fabricated runs. No Docker: the real runs are covered by the known-answer and red-team tests."""

import ast
from pathlib import Path

import pytest

from tracerepro.dataset.manifest import load_manifest
from tracerepro.evaluate.integrity import (
    Canary,
    check_integrity,
    junit_address,
    make_canary,
    read_junit,
    recorded_counts,
    summary_counts,
)
from tracerepro.evaluate.patch_oracle import (
    Verdict,
    VerdictKind,
    behaviour_flags,
    behaviour_key,
    review_report,
)
from tracerepro.sandbox.runner import AuditEvent, CaseResult, FailureInfo, PytestRun, RunResult
from tracerepro.sandbox.scanner import Finding

from ..dataset.test_manifest import TOY

ORACLE = "tests/test_regression.py::test_empty_report_does_not_crash"
BASE = "tests/test_report.py::TestReport::test_widths[a-b]"
CANARY = Canary("tests/test_c0ffee.py", "tests/test_c0ffee.py::test_c0ffee", "ValueError", b"")


def junit_case(nodeid: str, child: str = "") -> str:
    classname, name = junit_address(nodeid)
    inner = f"<{child} message='x'/>" if child else ""
    return f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'


def make_run(
    tmp_path: Path,
    *,
    canary: str = "failed",
    canary_type: str = "ValueError",
    oracle: str = "passed",
    junit_oracle: str = "",
    exit_code: int = 1,
    exitstatus: int = 1,
    summary: str = "1 failed, 2 passed in 0.12s",
    junit_text: str | None = None,
) -> PytestRun:
    failure = FailureInfo(canary_type, "check", (("tests/test_c0ffee.py", "test_c0ffee", 9),))
    tests = (
        CaseResult(ORACLE, "call", oracle),
        CaseResult(BASE, "call", "passed"),
        CaseResult(CANARY.nodeid, "call", canary, failure if canary == "failed" else None),
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(
        junit_text
        if junit_text is not None
        else "<testsuites><testsuite>"
        + junit_case(ORACLE, junit_oracle)
        + junit_case(BASE)
        + junit_case(CANARY.nodeid, "failure")
        + "</testsuite></testsuites>"
    )
    output = f"..F\nTRACEREPRO-AUDIT {{}}\n{summary}\n"
    return PytestRun(RunResult(exit_code, output, 1.0, False, False), exitstatus, tests, junit)


def test_consistent_run_passes(tmp_path: Path) -> None:
    assert check_integrity(make_run(tmp_path), CANARY, [ORACLE, BASE]) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"canary": "passed", "summary": "3 passed in 0.1s"}, "canary test reported as passed"),
        ({"canary_type": "KeyError"}, "canary failed with KeyError"),
        ({"exit_code": 1, "exitstatus": 0}, "exit code 1 != recorded exitstatus 0"),
        ({"junit_oracle": "failure"}, "junit.xml disagrees on 1 test"),
        ({"summary": "2 failed, 1 passed in 0.12s"}, "console summary disagrees"),
        ({"summary": "nothing to see"}, "no pytest summary line"),
        (
            {"junit_text": '<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites/>'},
            "declares a DTD or entities",
        ),
        ({"junit_text": "<testsuites><unclosed></testsuites>"}, "does not parse"),
    ],
)
def test_inconsistencies_are_reported(
    tmp_path: Path, change: dict[str, object], expected: str
) -> None:
    run = make_run(tmp_path, **change)  # type: ignore[arg-type]
    problems = check_integrity(run, CANARY, [ORACLE, BASE])
    assert any(expected in p for p in problems), problems


def test_missing_canary_is_reported(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run = PytestRun(run.run, run.exitstatus, run.tests[:2], run.junit_path)
    assert "canary test reported as missing, not failed" in check_integrity(run, CANARY, [ORACLE])


def test_subtests_are_counted_like_pytest() -> None:
    # unittest subTest / pytest 9 subtests: passed ones are not "passed" in the summary, a failed
    # one is "failed", and its parent test is turned into a failure too.
    run = PytestRun(
        RunResult(1, "", 1.0, False, False),
        1,
        (
            CaseResult("t.py::test_a", "call", "passed", subtest=True),
            CaseResult("t.py::test_a", "call", "passed", subtest=True),
            CaseResult("t.py::test_a", "call", "passed"),
            CaseResult("t.py::test_b", "call", "failed", subtest=True),
            CaseResult("t.py::test_b", "call", "passed"),
        ),
    )
    assert recorded_counts(run) == {"passed": 1, "failed": 2, "error": 0}
    assert summary_counts("2 failed, 1 passed, 2 subtests passed in 0.1s") == {
        "failed": 2,
        "passed": 1,
    }


def test_canary_in_a_file_that_failed_to_import_is_not_tampering(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    collect_error = CaseResult(CANARY.path, "collect", "error", longrepr="ImportError")
    run = PytestRun(run.run, run.exitstatus, (*run.tests[:2], collect_error), run.junit_path)
    problems = check_integrity(run, CANARY, [ORACLE])
    assert not any("canary" in p for p in problems), problems


def test_junit_address_matches_pytest() -> None:
    assert junit_address("tests/test_a.py::TestX::test_y[1-a/b]") == (
        "tests.test_a.TestX",
        "test_y[1-a/b]",
    )
    assert junit_address("test_top.py::test_z") == ("test_top", "test_z")


def test_read_junit_keeps_the_worst_outcome(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites>{junit_case(ORACLE)}{junit_case(ORACLE, 'error')}</testsuites>")
    assert read_junit(path) == {junit_address(ORACLE): "error"}


def test_summary_counts_parses_the_last_summary_line() -> None:
    output = "1 failed, 3 passed in 0.2s\n...\n2 failed, 10 passed, 1 xfailed, 3 errors in 1.50s\n"
    assert summary_counts(output) == {"failed": 2, "passed": 10, "xfailed": 1, "error": 3}


ORACLE_SOURCE = b"def test_empty_report_does_not_crash():\n    assert True\n"


def test_canary_hides_inside_the_oracle_file_and_raises_the_target_type() -> None:
    manifest = load_manifest(TOY)
    canary = make_canary(manifest, ORACLE_SOURCE, name="test_keeps_empty_rows")
    assert canary.path == "tests/test_regression.py"  # no extra file to notice
    assert canary.nodeid == "tests/test_regression.py::test_keeps_empty_rows"
    assert canary.exception == "ValueError"
    assert canary.source.startswith(ORACLE_SOURCE.rstrip(b"\n"))
    tree = ast.parse(canary.source)
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert names == {"test_empty_report_does_not_crash", "test_keeps_empty_rows"}
    namespace: dict[str, object] = {}
    exec(compile(canary.source, canary.path, "exec"), namespace)
    with pytest.raises(ValueError):
        namespace["test_keeps_empty_rows"]()  # type: ignore[operator]


def test_canary_names_look_ordinary_and_vary() -> None:
    manifest = load_manifest(TOY)
    names = {make_canary(manifest, ORACLE_SOURCE).nodeid.split("::")[1] for _ in range(20)}
    assert len(names) > 1
    assert all(n.startswith("test_") and not any(c.isdigit() for c in n) for n in names)


def test_canary_resolves_dotted_exception_types() -> None:
    manifest = load_manifest(TOY)
    manifest = type(manifest)(**{**manifest.__dict__, "target_exception": "json.JSONDecodeError"})
    canary = make_canary(manifest, b"", name="test_ab")
    namespace: dict[str, object] = {}
    exec(compile(canary.source, "c.py", "exec"), namespace)
    # JSONDecodeError needs three arguments: the canary still raises the right type.
    with pytest.raises(__import__("json").JSONDecodeError):
        namespace["test_ab"]()  # type: ignore[operator]


def event(name: str, detail: str, origin: str = "pkg/mod.py:7") -> AuditEvent:
    return AuditEvent("log", name, detail, origin)


def test_behaviour_key_normalizes_ports_programs_and_tmp_paths() -> None:
    assert behaviour_key(event("socket.connect", "('127.0.0.1', 51234)")) == (
        "socket.connect",
        "127.0.0.1",
    )
    assert behaviour_key(event("subprocess.Popen", "['/usr/bin/git', 'status']")) == (
        "subprocess.Popen",
        "git",
    )
    assert behaviour_key(event("open:write", "/tmp/abc123/x")) == ("open:write", "/tmp/*/x")


def test_only_behaviour_the_gold_run_lacks_is_flagged() -> None:
    gold = {("socket.connect", "127.0.0.1")}
    audit = (
        event("socket.connect", "('127.0.0.1', 4000)"),  # the test server, as with gold
        event("socket.connect", "('1.1.1.1', 443)"),
        event("socket.connect", "('1.1.1.1', 80)"),  # same host: one flag
        AuditEvent("block", "open", "/out/results.json"),  # blocks are verdicts, not flags
    )
    flags = behaviour_flags(audit, gold)
    assert flags == (Finding("behaviour:socket.connect", "pkg/mod.py", 7, "('1.1.1.1', 443)"),)


def test_review_report_lists_flags_blocks_and_the_patch() -> None:
    manifest = load_manifest(TOY)
    verdict = Verdict(
        VerdictKind.CORRECT,
        flags=(
            Finding("network", "tiny_lib/report.py", 11, "uses socket"),
            Finding("behaviour:socket.connect", "tiny_lib/report.py", 13, "('1.1.1.1', 443)"),
        ),
    )
    report = review_report(manifest, verdict, "diff --git a/x b/x\n")
    assert "**Verdict:** correct" in report
    assert "- [ ] network: tiny_lib/report.py:11 uses socket" in report
    assert "- [ ] socket.connect ('1.1.1.1', 443) (at tiny_lib/report.py:13)" in report
    assert "## Blocked actions (0)" in report
    assert "```diff\ndiff --git a/x b/x\n```" in report


def test_hostile_numbers_and_origins_do_not_crash_the_host() -> None:
    # Red-team round 2 crashed the grader with a 4400-digit count and a "line" of "\u00b2".
    huge = "9" * 4400
    assert summary_counts(f"{huge} failed, 1 passed in 0.1s") in (None, {"passed": 1})
    odd = AuditEvent("log", "socket.connect", "('1.1.1.1', 1)", "pkg/mod.py:\u00b2")
    assert behaviour_flags((odd,), set())[0].line == 0


def test_every_plain_rerun_must_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red-team round 3: a fix applied at random (60% of runs) was graded correct on a lucky run.
    from tracerepro.evaluate import patch_oracle

    outcomes = iter([(True, "ok"), (False, "exit 2"), (True, "ok")])
    calls: list[int] = []

    def once(*_: object) -> tuple[bool, str]:
        calls.append(1)
        return next(outcomes)

    monkeypatch.setattr(patch_oracle, "_plain_run_once", once)
    manifest = load_manifest(TOY)
    passes, detail = patch_oracle.plain_oracle_passes(
        manifest,
        "",
        {},
        Path("."),
        None,
        None,  # type: ignore[arg-type]
    )
    assert not passes and detail == "plain run 2 of 3: exit 2"
    assert len(calls) == 2  # stops at the first failure

    monkeypatch.setattr(patch_oracle, "_plain_run_once", lambda *_: (True, "ok"))
    assert patch_oracle.plain_oracle_passes(
        manifest,
        "",
        {},
        Path("."),
        None,
        None,  # type: ignore[arg-type]
    ) == (True, "3 plain runs passed")


def test_both_pytest_ways_of_counting_failed_subtests_are_accepted() -> None:
    # more-itertools-1182 (pytest 9.1): a test with 4 failed subtests is "passed" in the
    # console; pytest 9.2 counts it as failed. Only that much leeway is allowed.
    from tracerepro.evaluate.integrity import summary_mismatch

    recorded = {"passed": 725, "failed": 6, "error": 0}
    assert summary_mismatch({"passed": 725, "failed": 6}, recorded, ambiguous=1) == {}
    assert summary_mismatch({"passed": 726, "failed": 5}, recorded, ambiguous=1) == {}
    assert summary_mismatch({"passed": 727, "failed": 4}, recorded, ambiguous=1)  # too many
    assert summary_mismatch({"passed": 726, "failed": 5}, recorded, ambiguous=0)  # none allowed
    assert summary_mismatch({"passed": 726, "failed": 6}, recorded, ambiguous=1)  # not a move
    assert summary_mismatch({"passed": 724, "failed": 7}, recorded, ambiguous=1)  # wrong way
    assert summary_mismatch({"passed": 726, "failed": 5, "error": 1}, recorded, ambiguous=1)
