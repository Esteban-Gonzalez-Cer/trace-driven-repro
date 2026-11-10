"""Sandbox meta-tests (plan §5.2 acceptance): the locked-down container really is locked down."""

import os
import textwrap
from pathlib import Path

import pytest

from tracerepro.sandbox.runner import Limits, RunResult, create_workspace, run_sandboxed

from .._docker import requires_docker

pytestmark = requires_docker

IMAGE = "python:3.11-slim"
QUICK = Limits(timeout_s=60)


def _python(tmp_path: Path, code: str, limits: Limits = QUICK) -> RunResult:
    ws = create_workspace(tmp_path / "ws")
    return run_sandboxed(IMAGE, ["python", "-c", textwrap.dedent(code)], ws, limits=limits)


def test_network_access_fails(tmp_path: Path) -> None:
    result = _python(
        tmp_path,
        """
        import socket
        for target in [("1.1.1.1", 53), ("10.0.0.1", 80), ("host.docker.internal", 80)]:
            try:
                socket.create_connection(target, timeout=3).close()
                print("CONNECTED", target)
            except OSError as exc:
                print("blocked", type(exc).__name__)
        """,
    )
    assert result.exit_code == 0, result.output
    assert "CONNECTED" not in result.output
    assert result.output.count("blocked") == 3


def test_writing_outside_the_work_directory_fails(tmp_path: Path) -> None:
    result = _python(
        tmp_path,
        """
        for path in ["/etc/x", "/usr/local/lib/python3.11/site-packages/x.pth", "/x",
                     "/home/runner/x", "/opt/tracerepro/plugin/x.py", "/var/tmp/x"]:
            try:
                open(path, "w").write("x")
                print("WROTE", path)
            except OSError as exc:
                print("denied", path, type(exc).__name__)
        for path in ["/repo/ok.txt", "/tmp/ok.txt", "/out/ok.txt"]:
            open(path, "w").write("ok")
            print("allowed", path)
        """,
    )
    assert result.exit_code == 0, result.output
    assert "WROTE" not in result.output
    assert result.output.count("denied") == 6
    assert result.output.count("allowed") == 3
    assert (tmp_path / "ws" / "repo" / "ok.txt").read_text() == "ok"


def test_fork_bomb_hits_the_pid_limit(tmp_path: Path) -> None:
    result = _python(
        tmp_path,
        """
        import os, time
        children = 0
        try:
            while True:
                if os.fork() == 0:
                    time.sleep(60)
                    os._exit(0)
                children += 1
        except OSError as exc:
            print("limited", children, exc.errno)
        """,
        Limits(timeout_s=60, pids=64),
    )
    assert not result.timed_out, result.output
    assert "limited" in result.output, result.output
    children = int(result.output.split("limited")[1].split()[0])
    assert children < 64


def test_infinite_loop_is_killed_at_the_timeout(tmp_path: Path) -> None:
    result = _python(tmp_path, "while True: pass", Limits(timeout_s=3))
    assert result.timed_out
    assert result.exit_code is None
    assert result.duration_s < 30


def test_no_host_environment_variables_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEREPRO_HOST_CANARY", "do-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    result = _python(tmp_path, "import os; print(sorted(os.environ.items()))")
    assert result.exit_code == 0, result.output
    assert "do-not-leak" not in result.output
    assert "sk-test" not in result.output
    assert os.environ["HOME"] not in result.output


def test_unprivileged_with_no_docker_socket_or_capabilities(tmp_path: Path) -> None:
    result = _python(
        tmp_path,
        """
        import os
        print("uid", os.getuid())
        print("socket", os.path.exists("/var/run/docker.sock"))
        status = dict(line.split(":", 1) for line in open("/proc/self/status"))
        print("caps", [status[k].strip() for k in ("CapEff", "CapBnd")])
        print("nnp", status["NoNewPrivs"].strip())
        """,
    )
    assert result.exit_code == 0, result.output
    assert "uid 0" not in result.output
    assert "socket False" in result.output
    assert "caps ['0000000000000000', '0000000000000000']" in result.output
    assert "nnp 1" in result.output


def test_bytecode_is_not_written(tmp_path: Path) -> None:
    ws = create_workspace(tmp_path / "ws")
    (ws.repo / "mod.py").write_text("X = 1\n")
    result = run_sandboxed(IMAGE, ["python", "-c", "import mod"], ws, limits=QUICK)
    assert result.exit_code == 0, result.output
    assert not (ws.repo / "__pycache__").exists()
