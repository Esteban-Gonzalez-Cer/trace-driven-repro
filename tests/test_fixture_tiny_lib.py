"""Known-answer checks on the toy fixture that every fast harness test builds on."""

import filecmp
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_lib"
ORACLE = "test_regression.py::test_empty_report_does_not_crash"


@dataclass(frozen=True)
class Outcome:
    passed: bool
    message: str = ""
    details: str = ""


def run_fixture_tests(version: str, tmp_path: Path) -> dict[str, Outcome]:
    """Run the fixture's tests against buggy/ or fixed/ and read results from JUnit XML."""
    junit = tmp_path / f"{version}.junit.xml"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(FIXTURE / version),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", f"--junitxml={junit}"],
        cwd=FIXTURE,
        env=env,
        capture_output=True,
        check=False,
        timeout=60,
    )
    results: dict[str, Outcome] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        test_id = f"{case.get('classname', '').rsplit('.', 1)[-1]}.py::{case.get('name')}"
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            results[test_id] = Outcome(passed=True)
        else:
            results[test_id] = Outcome(False, failure.get("message", ""), failure.text or "")
    return results


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Outcome]]:
    tmp = tmp_path_factory.mktemp("tiny_lib")
    return {v: run_fixture_tests(v, tmp) for v in ("buggy", "fixed")}


def test_oracle_fails_on_buggy_with_target_exception_and_frame(
    outcomes: dict[str, dict[str, Outcome]],
) -> None:
    result = outcomes["buggy"][ORACLE]
    assert not result.passed
    assert result.message.startswith("ValueError")
    assert "in _widest" in result.details or "def _widest" in result.details


def test_oracle_passes_on_fixed(outcomes: dict[str, dict[str, Outcome]]) -> None:
    assert outcomes["fixed"][ORACLE].passed


def test_baseline_passes_on_both_versions(outcomes: dict[str, dict[str, Outcome]]) -> None:
    for version, results in outcomes.items():
        baseline = {k: v for k, v in results.items() if k.startswith("test_report.py::")}
        assert len(baseline) == 4, version
        assert all(o.passed for o in baseline.values()), version


def _same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__"])
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(_same_tree(a / d, b / d) for d in cmp.common_dirs)


def test_gold_patch_turns_buggy_into_fixed(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE / "buggy", work, ignore=shutil.ignore_patterns("__pycache__"))
    patch = str(FIXTURE / "gold.patch")
    subprocess.run(["git", "apply", "--check", patch], cwd=work, check=True)
    subprocess.run(["git", "apply", patch], cwd=work, check=True)
    assert _same_tree(work, FIXTURE / "fixed")
    assert not _same_tree(FIXTURE / "buggy", FIXTURE / "fixed")
