"""The harness pytest plugin, run on the host against a throwaway test module."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from tracerepro.sandbox.runner import PLUGIN_DIR, PLUGIN_NAME

SAMPLE = """
import pytest

def helper(rows):
    return max(len(r) for r in rows)

class AppError(Exception):
    pass

def test_ok():
    assert helper(["ab"]) == 2

def test_crash():
    helper([])

def test_custom():
    raise AppError("boom")

@pytest.fixture
def broken():
    raise KeyError("setup")

def test_setup_error(broken):
    pass

@pytest.mark.xfail(strict=True)
def test_expected():
    assert False

@pytest.mark.skip(reason="not here")
def test_skipped():
    pass

@pytest.mark.parametrize("x", ["[::1]:8080"])
def test_param(x):
    assert x
"""


def _run(tmp_path: Path, files: dict[str, str]) -> dict[str, Any]:
    for name, body in files.items():
        (tmp_path / name).write_text(textwrap.dedent(body))
    out = tmp_path / "results.json"
    env = {**os.environ, "PYTHONPATH": str(PLUGIN_DIR), "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", PLUGIN_NAME,
         f"--tracerepro-json={out}", "-q", "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, check=False, timeout=120,
    )  # fmt: skip
    data: dict[str, Any] = json.loads(out.read_text())
    return data


def _by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["nodeid"]: t for t in data["tests"]}


def test_records_outcomes_and_failures(tmp_path: Path) -> None:
    data = _run(tmp_path, {"test_sample.py": SAMPLE})
    assert data["schema"] == 1
    assert data["exitstatus"] == 1
    tests = _by_id(data)

    assert tests["test_sample.py::test_ok"]["outcome"] == "passed"
    assert tests["test_sample.py::test_skipped"]["outcome"] == "skipped"
    assert tests["test_sample.py::test_expected"]["outcome"] == "xfailed"
    assert tests["test_sample.py::test_param[[::1]:8080]"]["outcome"] == "passed"

    crash = tests["test_sample.py::test_crash"]
    assert crash["outcome"] == "failed"
    assert crash["failure"]["type"] == "ValueError"
    innermost = crash["failure"]["frames"][-1]
    assert innermost["function"] == "helper"
    assert innermost["file"].endswith("test_sample.py")

    custom = tests["test_sample.py::test_custom"]["failure"]
    assert custom["type"] == "test_sample.AppError"
    assert custom["message"] == "boom"

    setup = tests["test_sample.py::test_setup_error"]
    assert (setup["when"], setup["outcome"]) == ("setup", "error")
    assert setup["failure"]["type"] == "KeyError"


def test_collection_error_is_recorded(tmp_path: Path) -> None:
    data = _run(tmp_path, {"test_broken.py": "def test_x(:\n    pass\n"})
    (record,) = data["tests"]
    assert (record["nodeid"], record["when"], record["outcome"]) == (
        "test_broken.py",
        "collect",
        "error",
    )
    assert "SyntaxError" in record["longrepr"]


def test_plugin_is_stdlib_only() -> None:
    source = (PLUGIN_DIR / f"{PLUGIN_NAME}.py").read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imports <= {"__future__", "json", "os", "platform", "traceback", "typing", "pytest"}
