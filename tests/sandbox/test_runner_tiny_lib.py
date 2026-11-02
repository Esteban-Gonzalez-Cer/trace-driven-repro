"""End to end on the toy fixture: workspace from the image, oracle copied in, gold patch applied."""

import subprocess
from pathlib import Path

import pytest

from tracerepro.dataset.image_builder import build_image
from tracerepro.dataset.manifest import Manifest, load_manifest
from tracerepro.sandbox.runner import Limits, Workspace, copy_into, create_workspace, run_pytest

from .._docker import requires_docker
from ..dataset.test_manifest import TOY

pytestmark = requires_docker

FIXTURE = TOY.parent
ORACLE = "tests/test_regression.py::test_empty_report_does_not_crash"
LIMITS = Limits(timeout_s=120)


@pytest.fixture(scope="module")
def manifest(tmp_path_factory: pytest.TempPathFactory) -> Manifest:
    m = load_manifest(TOY)
    build_image(m, work_dir=tmp_path_factory.mktemp("work"))
    return m


def _workspace(manifest: Manifest, tmp_path: Path, *, with_oracle: bool) -> Workspace:
    ws = create_workspace(tmp_path / "ws", manifest.image_tag)
    if with_oracle:
        oracle = (FIXTURE / "tests" / "test_regression.py").read_bytes()
        copy_into(ws, {"tests/test_regression.py": oracle})
    return ws


def test_workspace_is_a_copy_of_c_bug(manifest: Manifest, tmp_path: Path) -> None:
    ws = _workspace(manifest, tmp_path, with_oracle=False)
    buggy = (FIXTURE / "buggy" / "tiny_lib" / "report.py").read_text()
    assert (ws.repo / "tiny_lib" / "report.py").read_text() == buggy
    assert not (ws.repo / ".git").exists()


def test_baseline_passes(manifest: Manifest, tmp_path: Path) -> None:
    ws = _workspace(manifest, tmp_path, with_oracle=False)
    result = run_pytest(manifest.image_tag, ["tests"], ws, limits=LIMITS)
    assert result.exitstatus == 0, result.run.output
    assert set(result.outcomes().values()) == {"passed"}
    assert set(result.outcomes()) == set(manifest.baseline_pass_set)
    assert result.junit_path is not None


def test_oracle_fails_with_target_exception_from_target_frame(
    manifest: Manifest, tmp_path: Path
) -> None:
    ws = _workspace(manifest, tmp_path, with_oracle=True)
    result = run_pytest(manifest.image_tag, [ORACLE], ws, limits=LIMITS)
    assert result.outcomes() == {ORACLE: "failed"}, result.run.output
    (record,) = result.tests
    assert record.failure is not None
    assert record.failure.type == manifest.target_exception
    assert manifest.target_frame is not None
    file, function, _ = record.failure.innermost or ("", "", 0)
    assert (file, function) == (manifest.target_frame.file, manifest.target_frame.function)


def test_oracle_passes_after_gold_patch(manifest: Manifest, tmp_path: Path) -> None:
    ws = _workspace(manifest, tmp_path, with_oracle=True)
    subprocess.run(
        ["git", "apply", str(FIXTURE / "gold.patch")], cwd=ws.repo, check=True, timeout=30
    )
    result = run_pytest(manifest.image_tag, [ORACLE, "tests/test_report.py"], ws, limits=LIMITS)
    assert result.exitstatus == 0, result.run.output
    assert set(result.outcomes().values()) == {"passed"}
    assert len(result.outcomes()) == 5
