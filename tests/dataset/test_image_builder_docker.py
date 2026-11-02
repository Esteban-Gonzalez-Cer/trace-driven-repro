"""Acceptance tests for the image builder (plan §5.1) on the toy fixture."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tracerepro.dataset.image_builder import BuildRecord, build_image
from tracerepro.dataset.manifest import load_manifest

from .test_manifest import TOY


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    return probe.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="no Docker daemon"),
]


@pytest.fixture(scope="module")
def first_build(tmp_path_factory: pytest.TempPathFactory) -> BuildRecord:
    return build_image(load_manifest(TOY), work_dir=tmp_path_factory.mktemp("work"))


OFFLINE_RUN = ["docker", "run", "--rm", "--net=none", "--read-only", "--tmpfs", "/tmp"]


def _run(image: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*OFFLINE_RUN, image, "sh", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_build_records_pinned_freeze(first_build: BuildRecord) -> None:
    assert first_build.image_tag == "bug_env:tiny_lib-001"
    assert "pytest==9.0.2" in first_build.freeze
    assert first_build.lock_sha256 is not None


def test_image_is_c_bug_snapshot_for_non_root_user(first_build: BuildRecord) -> None:
    result = _run(
        first_build.image_tag,
        "id -u && test ! -e /repo/.git && test ! -e /repo/tests/test_regression.py && echo ok",
    )
    assert result.stdout.split() == ["1000", "ok"], result.stderr


def test_baseline_passes_offline_in_image(first_build: BuildRecord) -> None:
    result = _run(first_build.image_tag, "python -m pytest -q -p no:cacheprovider tests")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout


def test_no_cache_rebuild_installs_same_versions(first_build: BuildRecord, tmp_path: Path) -> None:
    rebuilt = build_image(load_manifest(TOY), work_dir=tmp_path, no_cache=True)
    assert rebuilt.freeze == first_build.freeze
    assert rebuilt.dockerfile_sha256 == first_build.dockerfile_sha256
