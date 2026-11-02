"""Harness-owned pytest plugin, mounted read-only into the sandbox (plan §5.2).

Loaded with `-p tracerepro_pytest_plugin --tracerepro-json=PATH`. For every test it records the
pytest node id and outcome. For every failure or error it also records the exception type,
message, and traceback frames (innermost last). The repro gate and the oracle checks read this
file, so they never have to parse console text or map JUnit names back to node ids.

This file runs inside bug images, under whatever Python the bug needs. It must stay
stdlib-only and importable on Python 3.8+.
"""

from __future__ import annotations

import json
import os
import platform
import traceback
from typing import Any

import pytest

SCHEMA_VERSION = 1
MAX_FRAMES = 40
MAX_MESSAGE = 2000
MAX_LONGREPR = 8000
REPO_ROOT = "/repo/"

_records: list[dict[str, Any]] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--tracerepro-json", default=None, help="write per-test results to PATH")


def _path(filename: str) -> str:
    return filename[len(REPO_ROOT) :] if filename.startswith(REPO_ROOT) else filename


def _type_name(exc_type: type) -> str:
    module = exc_type.__module__
    if module in ("builtins", "__main__"):
        return exc_type.__qualname__
    return f"{module}.{exc_type.__qualname__}"


def _failure(excinfo: pytest.ExceptionInfo[BaseException]) -> dict[str, Any]:
    frames = traceback.extract_tb(excinfo.tb)[-MAX_FRAMES:]
    return {
        "type": _type_name(excinfo.type),
        "message": str(excinfo.value)[:MAX_MESSAGE],
        "frames": [
            {"file": _path(f.filename), "function": f.name, "line": f.lineno} for f in frames
        ],
    }


def _outcome(report: pytest.TestReport) -> str:
    if hasattr(report, "wasxfail"):
        return "xfailed" if report.skipped else "xpassed"
    if report.when != "call" and report.failed:
        return "error"
    return report.outcome


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    # One record per phase that matters: the call, plus setup/teardown only when they fail or skip.
    if report.when != "call" and report.passed:
        return
    record: dict[str, Any] = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": _outcome(report),
        "duration": round(report.duration, 6),
    }
    if report.failed and call.excinfo is not None:
        record["failure"] = _failure(call.excinfo)
    _records.append(record)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _records.append(
            {
                "nodeid": report.nodeid,
                "when": "collect",
                "outcome": "error",
                "longrepr": str(report.longrepr)[:MAX_LONGREPR],
            }
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = session.config.getoption("--tracerepro-json")
    if not path:
        return
    data = {
        "schema": SCHEMA_VERSION,
        "exitstatus": int(exitstatus),
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "tests": _records,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
