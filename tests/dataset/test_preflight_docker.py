"""Automated preflight on the toy fixture reproduces its hand-written manifest exactly."""

from pathlib import Path

import pytest

from tracerepro.dataset.candidate import load_candidate
from tracerepro.dataset.manifest import Manifest, load_manifest
from tracerepro.dataset.preflight import Outputs, PreflightResult, preflight
from tracerepro.dataset.screening import read_log

from .._docker import requires_docker
from .test_manifest import TOY

pytestmark = requires_docker

CANDIDATE = TOY.parent / "candidate.yaml"


def _outputs(root: Path) -> Outputs:
    return Outputs(root / "manifests", root / "payloads", root / "locks", root / "log.jsonl")


@pytest.fixture(scope="module")
def first(tmp_path_factory: pytest.TempPathFactory) -> tuple[PreflightResult, Path]:
    root = tmp_path_factory.mktemp("pf1")
    result = preflight(load_candidate(CANDIDATE), work_dir=root / "work", outputs=_outputs(root))
    return result, root


def test_toy_fixture_passes_preflight(first: tuple[PreflightResult, Path]) -> None:
    result, root = first
    assert result.decision == "preflight_pass", result.reason
    (row,) = read_log(root / "log.jsonl")
    assert row["decision"] == "preflight_pass"
    assert row["src_files_changed"] == 1


def test_manifest_matches_the_hand_written_one(first: tuple[PreflightResult, Path]) -> None:
    result, root = first
    got = load_manifest(root / "manifests" / "tiny_lib-001.yaml")
    want = load_manifest(TOY)
    assert result.manifest is not None

    def key(m: Manifest) -> tuple[object, ...]:
        return (
            m.c_bug, m.c_fix, m.target_exception, m.target_frame, m.oracle_tests,
            m.excluded_fix_tests, set(m.baseline_pass_set), m.flaky_set, m.gold_files,
            m.gold_files_full, m.deps_lock, m.image_tag,
        )  # fmt: skip

    assert key(got) == key(want)


def test_payload_has_only_library_frames_and_no_test_code(
    first: tuple[PreflightResult, Path],
) -> None:
    _, root = first
    text = (root / "payloads" / "tiny_lib-001.json").read_text()
    assert "test_empty_report_does_not_crash" not in text
    assert "tests/" not in text
    assert '"function": "_widest"' in text


def test_rerun_is_byte_identical(first: tuple[PreflightResult, Path], tmp_path: Path) -> None:
    _, root = first
    again = preflight(load_candidate(CANDIDATE), work_dir=tmp_path / "work",
                      outputs=_outputs(tmp_path))  # fmt: skip
    assert again.decision == "preflight_pass"
    for rel in ("manifests/tiny_lib-001.yaml", "payloads/tiny_lib-001.json"):
        assert (tmp_path / rel).read_text() == (root / rel).read_text().replace(
            str(root), str(tmp_path)
        ), rel
