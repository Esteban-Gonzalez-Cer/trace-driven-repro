"""Runner pieces that need no Docker: argv, result parsing, workspace file placement."""

import json
from pathlib import Path
from typing import Any

import pytest

from tracerepro.sandbox.runner import (
    CaseResult,
    PytestRun,
    RunResult,
    SandboxError,
    Workspace,
    copy_into,
    create_workspace,
    parse_results,
    pytest_command,
    run_sandboxed,
)

GOOD: dict[str, Any] = {
    "schema": 1,
    "exitstatus": 1,
    "tests": [
        {"nodeid": "tests/t.py::test_ok", "when": "call", "outcome": "passed"},
        {
            "nodeid": "tests/t.py::test_crash",
            "when": "call",
            "outcome": "failed",
            "failure": {
                "type": "ValueError",
                "message": "max() arg is an empty sequence",
                "frames": [
                    {"file": "tests/t.py", "function": "test_crash", "line": 3},
                    {"file": "pkg/mod.py", "function": "_widest", "line": 11},
                ],
            },
        },
    ],
}


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(data))
    return path


def test_pytest_command_keeps_ids_as_separate_arguments() -> None:
    nodeid = "tests/test_basic.py::test_run_from_config[None-None-[::1]:8080-::1-8080]"
    argv = pytest_command([nodeid, "tests/test_x.py"])
    assert argv[:3] == ["python", "-m", "pytest"]
    assert argv[3:5] == ["-p", "no:cacheprovider"]
    assert argv[-2:] == [nodeid, "tests/test_x.py"]
    assert "--tracerepro-json=/out/results.json" in argv
    assert "--junitxml=/out/junit.xml" in argv


def test_run_sandboxed_rejects_shell_strings(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        run_sandboxed("img", "pytest -q", Workspace(tmp_path))


def test_parse_results(tmp_path: Path) -> None:
    exitstatus, tests = parse_results(_write(tmp_path, GOOD))
    assert exitstatus == 1
    crash = tests[1]
    assert crash.failure is not None
    assert crash.failure.type == "ValueError"
    assert crash.failure.innermost == ("pkg/mod.py", "_widest", 11)
    assert tests[0].failure is None


@pytest.mark.parametrize(
    "data",
    [
        [],
        {**GOOD, "schema": 2},
        {**GOOD, "exitstatus": "1"},
        {**GOOD, "tests": [{"nodeid": 3, "when": "call", "outcome": "passed"}]},
        {**GOOD, "tests": [{**GOOD["tests"][1], "failure": {"type": "E", "message": ""}}]},
        {
            **GOOD,
            "tests": [
                {
                    **GOOD["tests"][1],
                    "failure": {"type": "E", "message": "", "frames": [{"file": "a"}]},
                }
            ],
        },
    ],
)
def test_parse_results_rejects_malformed(tmp_path: Path, data: object) -> None:
    with pytest.raises(SandboxError):
        parse_results(_write(tmp_path, data))


def test_parse_results_rejects_invalid_json_and_symlinks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff{")
    with pytest.raises(SandboxError):
        parse_results(bad)
    link = tmp_path / "link.json"
    link.symlink_to(_write(tmp_path, GOOD))
    with pytest.raises(SandboxError):
        parse_results(link)


def test_outcomes_keep_the_worst_phase() -> None:
    run = RunResult(exit_code=1, output="", duration_s=0.1, timed_out=False, oom_killed=False)
    result = PytestRun(
        run=run,
        exitstatus=1,
        tests=(
            CaseResult("t.py::a", "call", "passed"),
            CaseResult("t.py::a", "teardown", "error"),
            CaseResult("t.py::b", "call", "failed"),
        ),
    )
    assert result.outcomes() == {"t.py::a": "error", "t.py::b": "failed"}


def test_copy_into_places_files_and_refuses_escapes(tmp_path: Path) -> None:
    ws = create_workspace(tmp_path / "ws")
    copy_into(ws, {"tests/test_regression.py": b"def test_x(): pass\n"})
    assert (ws.repo / "tests" / "test_regression.py").read_bytes() == b"def test_x(): pass\n"
    for bad in ("../escape.py", "/etc/passwd", "tests/../../escape.py"):
        with pytest.raises(SandboxError):
            copy_into(ws, {bad: b""})
    assert not (tmp_path / "ws" / "escape.py").exists()


def test_create_workspace_refuses_non_empty_root(tmp_path: Path) -> None:
    (tmp_path / "leftover").write_text("x")
    with pytest.raises(SandboxError):
        create_workspace(tmp_path)


def test_parse_results_refuses_huge_integers(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    path.write_text('{"schema": 1, "exitstatus": ' + "9" * 5000 + ', "tests": []}')
    with pytest.raises(SandboxError):
        parse_results(path)
