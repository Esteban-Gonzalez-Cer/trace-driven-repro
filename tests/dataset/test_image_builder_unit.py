"""Builder logic that needs no Docker daemon."""

from pathlib import Path

import pytest

from tracerepro.dataset.image_builder import (
    BuildFailed,
    check_freeze,
    prepare_context,
    render_dockerfile,
)
from tracerepro.dataset.lockfile import parse_lock
from tracerepro.dataset.manifest import load_manifest

from .test_manifest import TOY


def test_dockerfile_is_deterministic_and_pinned() -> None:
    m = load_manifest(TOY)
    text = render_dockerfile(m)
    assert text == render_dockerfile(m)
    assert text.splitlines()[1] == "FROM python:3.11-slim"
    assert "pip install --no-deps -r /opt/tracerepro/requirements.lock.txt" in text
    assert "pip install --no-deps -e /repo && pip check" in text
    assert text.rstrip().endswith("USER 1000")
    assert m.c_bug in text


def test_prepare_context_snapshots_c_bug_without_git(tmp_path: Path) -> None:
    m = load_manifest(TOY)
    context = prepare_context(m, tmp_path)
    repo = context / "repo"
    assert not (repo / ".git").exists()
    assert (repo / "tiny_lib" / "report.py").is_file()
    assert (repo / "tests" / "test_report.py").is_file()
    # The oracle test exists only at c_fix; the image must never contain it.
    assert not (repo / "tests" / "test_regression.py").exists()
    assert "default=0" not in (repo / "tiny_lib" / "report.py").read_text()
    assert (context / "requirements.lock.txt").is_file()
    assert (context / "Dockerfile").read_text() == render_dockerfile(m)


def test_check_freeze_accepts_lock_and_base_packages() -> None:
    pins = parse_lock("pytest==9.0.2\ncolorama==0.4.6 ; sys_platform == 'win32'\n")
    check_freeze(["pip==24.0", "pytest==9.0.2", "setuptools==79.0.1"], pins)


def test_check_freeze_accepts_any_marker_conditional_version() -> None:
    pins = parse_lock(
        "greenlet==2.0.2 ; python_full_version < '3.12'\n"
        "greenlet==3.5.0 ; python_full_version >= '3.12'\n"
    )
    check_freeze(["greenlet==2.0.2"], pins)
    with pytest.raises(BuildFailed, match=r"lock pins 2\.0\.2, 3\.5\.0"):
        check_freeze(["greenlet==1.0"], pins)


@pytest.mark.parametrize(
    ("freeze", "message"),
    [
        (["pytest==9.0.1"], "lock pins 9.0.2"),
        (["pytest==9.0.2", "evil==1.0"], "not in the lock"),
        (["pip==24.0"], "pinned but not installed"),
    ],
)
def test_check_freeze_rejects_drift(freeze: list[str], message: str) -> None:
    with pytest.raises(BuildFailed, match=message):
        check_freeze(freeze, parse_lock("pytest==9.0.2\n"))
