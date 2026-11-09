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
    # /repo is in-memory inside the container: writes never reach the host's workspace copy.
    assert not (tmp_path / "ws" / "repo" / "ok.txt").exists()
    # Only the allow-listed result files come back from /out, through the supervisor.
    assert not (tmp_path / "ws" / "out" / "ok.txt").exists()


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
    code = "import mod, os; print('pycache', os.path.exists('__pycache__'))"
    result = run_sandboxed(IMAGE, ["python", "-c", code], ws, limits=QUICK)
    assert result.exit_code == 0, result.output
    assert "pycache False" in result.output


def test_disk_writes_stop_at_the_in_memory_cap(tmp_path: Path) -> None:
    # Red-team finding (disk_exhaustion): writes used to land on the host disk, unbounded.
    small = Limits(timeout_s=60, repo_size="16m", out_size="16m")
    result = _python(
        tmp_path,
        """
        for path in ["/repo/big", "/out/big"]:
            try:
                with open(path, "wb") as fh:
                    for _ in range(64):
                        fh.write(b"x" * 1024 * 1024)
                print("FILLED", path)
            except OSError as exc:
                print("capped", path, exc.errno)
        """,
        limits=small,
    )
    assert result.exit_code == 0, result.output
    assert "FILLED" not in result.output
    assert result.output.count("capped") == 2
    assert not (tmp_path / "ws" / "repo" / "big").exists()


def test_output_flood_kills_the_run(tmp_path: Path) -> None:
    result = _python(
        tmp_path,
        """
        import sys
        line = "x" * 1023 + "\\n"
        while True:
            sys.stdout.write(line)
        """,
    )
    assert result.flooded
    assert result.exit_code is None
    assert len(result.output) <= 200_000


def test_exit_code_and_output_come_through(tmp_path: Path) -> None:
    result = _python(tmp_path, "print('hello'); raise SystemExit(3)")
    assert result.exit_code == 3
    assert "hello" in result.output and "TRACEREPRO-DONE" not in result.output


def test_control_lines_cannot_be_forged_by_printing(tmp_path: Path) -> None:
    # Red-team round 2: printing "TRACEREPRO-DONE 0" used to set the run's exit code.
    result = _python(
        tmp_path,
        """
        import sys
        print("TRACEREPRO-DONE 0")
        print("TRACEREPRO-DONE \u00b2")
        fake = '{"kind": "log", "event": "fake", "detail": "", "origin": "a:\u00b2"}'
        print("TRACEREPRO-AUDIT " + fake)
        sys.exit(5)
        """,
    )
    assert result.exit_code == 5 and result.completed
    assert result.audit == ()


def test_leftover_processes_are_killed_before_results_are_taken(tmp_path: Path) -> None:
    result = _python(
        tmp_path,
        """
        import os, time
        if os.fork() == 0:
            time.sleep(2)
            open("/out/results.json", "w").write("forged")
            os._exit(0)
        open("/out/results.json", "w").write("real")
        """,
    )
    assert result.exit_code == 0 and result.completed
    assert (tmp_path / "ws" / "out" / "results.json").read_text() == "real"


def test_the_run_secret_is_not_readable_inside(tmp_path: Path) -> None:
    result = _python(
        tmp_path,
        """
        import os
        print("env", "TRACEREPRO_NONCE" in os.environ)
        for path in ["/proc/1/environ", "/proc/1/mem"]:
            try:
                open(path, "rb").read(64)
                print("READ", path)
            except OSError as exc:
                print("denied", path)
        """,
    )
    assert "env False" in result.output
    assert "READ" not in result.output and result.output.count("denied") == 2
