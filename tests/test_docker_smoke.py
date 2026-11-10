"""Docker smoke tests: the sandbox base image runs, and --net=none really is offline."""

import subprocess

from ._docker import requires_docker

IMAGE = "python:3.11-slim"


pytestmark = requires_docker


def _run_offline(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", "--net=none", IMAGE, "python", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_offline_container_runs_python() -> None:
    assert _run_offline("1").returncode == 0


def test_offline_container_cannot_reach_network() -> None:
    result = _run_offline("import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)")
    assert result.returncode != 0
    assert "OSError" in result.stderr or "unreachable" in result.stderr.lower()
