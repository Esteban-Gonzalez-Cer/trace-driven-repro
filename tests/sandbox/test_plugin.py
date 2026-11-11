"""The harness pytest plugin, run on the host against a throwaway test module."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from tracerepro.sandbox.runner import PLUGIN_DIR, PLUGIN_NAME, AuditEvent, parse_audit

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
    return _run_with_audit(tmp_path, files)[0]


def _run_with_audit(
    tmp_path: Path, files: dict[str, str]
) -> tuple[dict[str, Any], tuple[AuditEvent, ...]]:
    """Run pytest with the plugin in a subprocess: its audit hook can never be removed, so it
    must not be installed in this test process."""
    for name, body in files.items():
        (tmp_path / name).write_text(textwrap.dedent(body))
    out = tmp_path / "results.json"
    env = {**os.environ, "PYTHONPATH": str(PLUGIN_DIR), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", PLUGIN_NAME,
         f"--tracerepro-json={out}", "-q", "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, check=False, timeout=120,
    )  # fmt: skip
    data: dict[str, Any] = json.loads(out.read_text())
    return data, parse_audit(proc.stdout.decode() + proc.stderr.decode())


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
    assert imports <= {
        "__future__",
        "collections",
        "contextlib",
        "io",
        "sys",
        "sysconfig",
        "tempfile",
        "threading",
        "json",
        "os",
        "platform",
        "reprlib",
        "traceback",
        "typing",
        "pytest",
    }


AUDITED = """
import os
import socket
import subprocess
import sys

def test_results_dir_is_blocked():
    for attempt in (
        lambda: open("/out/results.json", "w"),
        lambda: open("/out/results.json"),
        lambda: os.replace("/tmp/forged.json", "/out/results.json"),
        lambda: os.listdir("/out"),
    ):
        try:
            attempt()
        except PermissionError as exc:
            assert "tracerepro sandbox" in str(exc)
        else:
            raise AssertionError("not blocked")

def test_suspicious_actions_are_logged():
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=0.01)
    except OSError:
        pass
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    try:
        open("/nonexistent/.ssh/id_rsa")
    except OSError:
        pass
"""


def test_audit_hook_blocks_results_access_and_logs_actions(tmp_path: Path) -> None:
    data, audit = _run_with_audit(tmp_path, {"test_audited.py": AUDITED})
    # Our own writer still works (results.json was written and read back), and both tests pass.
    assert {t["outcome"] for t in data["tests"]} == {"passed"}
    blocked = {(e.event, e.detail) for e in audit if e.kind == "block"}
    assert ("open", "/out/results.json") in blocked
    assert ("os.rename", "/out/results.json") in blocked
    assert ("os.listdir", "/out") in blocked
    logged = {e.event: e for e in audit if e.kind == "log"}
    assert logged["socket.connect"].detail == "('192.0.2.1', 9)"
    assert logged["socket.connect"].origin.endswith("test_audited.py:23")
    assert sys.executable in logged["subprocess.Popen"].detail
    assert logged["open:sensitive"].detail == "/nonexistent/.ssh/id_rsa"


def test_plain_tests_produce_no_audit_events(tmp_path: Path) -> None:
    _, audit = _run_with_audit(tmp_path, {"test_sample.py": SAMPLE})
    assert audit == ()


SUBTESTS = """
import unittest

class TestNumbers(unittest.TestCase):
    def test_each(self):
        for n in (1, 2, 3):
            with self.subTest(n=n):
                self.assertLess(n, 3)
"""


def test_subtests_are_marked(tmp_path: Path) -> None:
    data = _run(tmp_path, {"test_sub.py": SUBTESTS})
    records = [t for t in data["tests"] if t["nodeid"].endswith("test_each")]
    assert [(t["outcome"], t.get("subtest", False)) for t in records] == [
        ("passed", True),
        ("passed", True),
        ("failed", True),
        ("passed", False),  # the test itself; pytest then reports it failed (a subtest failed)
    ]


def test_python_install_dirs_are_not_sensitive() -> None:
    # CI regression: GitHub's runner keeps Python under /home/runner, so stdlib reads were logged
    # as sensitive-file reads. Python's own folders are exempt; credentials never are.
    code = textwrap.dedent(
        """
        import json, sysconfig
        import tracerepro_pytest_plugin as p
        stdlib = sysconfig.get_paths()["stdlib"]
        p.SENSITIVE_PREFIXES = (stdlib.rsplit("/", 2)[0] + "/", "/home/")
        print(json.dumps([
            p._is_sensitive(stdlib + "/importlib/metadata/__init__.py"),
            p._is_sensitive(stdlib + "/.ssh/id_rsa"),
            p._is_sensitive("/home/someone/.netrc"),
            p._is_sensitive("/home/someone/notes.txt"),
            p._is_sensitive("/proc/1/environ"),
        ]))
        """
    )
    env = dict(os.environ, PYTHONPATH=str(PLUGIN_DIR))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.splitlines()[-1]) == [False, True, True, True, True]
