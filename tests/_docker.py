"""Shared marker for tests that need a running Docker daemon."""

import shutil
import subprocess

import pytest


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    return probe.returncode == 0


requires_docker = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon"),
]
