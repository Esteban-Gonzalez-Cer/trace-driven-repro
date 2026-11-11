"""Pipeline B end to end on the toy fixture, with the real sandbox and evaluator but a scripted
model: a patch that breaks a visible test, the feedback, then the human fix, scored `correct`."""

from pathlib import Path

import pytest

from tracerepro.agent.client import LLMClient, Settings
from tracerepro.agent.edits import EditResult, apply_edits
from tracerepro.agent.parse import Edit
from tracerepro.agent.pipeline import Bug, PipelineConfig, run_pipeline_b
from tracerepro.agent.providers.fake import FakeProvider
from tracerepro.agent.workbench import DockerWorkbench, regressions_of
from tracerepro.dataset.image_builder import build_image
from tracerepro.dataset.manifest import Manifest, load_manifest
from tracerepro.evaluate.patch_oracle import VerdictKind, score_patch
from tracerepro.sandbox.runner import Limits

from .._docker import requires_docker
from .test_pipeline import BREAKS_LABEL, FIXTURE, GOLD, payload

pytestmark = requires_docker


@pytest.fixture(scope="module")
def toy(tmp_path_factory: pytest.TempPathFactory) -> tuple[Manifest, Path]:
    manifest = load_manifest(FIXTURE / "manifest.yaml")
    work = tmp_path_factory.mktemp("work")
    build_image(manifest, work_dir=work)
    return manifest, work


def test_feedback_loop_then_correct_verdict(toy: tuple[Manifest, Path], tmp_path: Path) -> None:
    manifest, work = toy
    provider = FakeProvider.replying([BREAKS_LABEL, GOLD])
    client = LLMClient(provider, log_path=tmp_path / "calls.jsonl")
    config = PipelineConfig(settings=Settings("claude-haiku-5-5", "low", 4000))
    with DockerWorkbench(manifest, tmp_path / "bench", limits=Limits(timeout_s=120),
                         work_dir=work) as bench:  # fmt: skip
        files, _ = bench.visible_baseline()
        assert files == ["tests/test_report.py"]  # the oracle file is not at c_bug
        assert not (bench.source_tree() / "tests" / "test_regression.py").exists()
        result = run_pipeline_b(Bug(manifest.id, payload()), client, bench, config)
    assert not (tmp_path / "bench").exists()  # cleaned up
    first, second = result.attempts
    assert first.outcome == "regressions"
    assert first.regressions == ("tests/test_report.py::test_missing_name_is_question_mark",)
    assert "test_missing_name_is_question_mark" in (first.feedback or "")
    assert second.outcome == "clean" and result.final_from == 2
    verdict = score_patch(manifest, result.final_diff or "", work_dir=work)
    assert verdict.kind is VerdictKind.CORRECT, verdict.message


def test_a_patch_that_breaks_collection_counts_every_test_in_the_file(
    toy: tuple[Manifest, Path], tmp_path: Path
) -> None:
    manifest, work = toy
    with DockerWorkbench(manifest, tmp_path / "bench", limits=Limits(timeout_s=120),
                         work_dir=work) as bench:  # fmt: skip
        edit = Edit(
            "tiny_lib/__init__.py",
            "from tiny_lib.report import format_report\n",
            "raise ImportError('broken on purpose')\nfrom tiny_lib.report import format_report\n",
        )
        made = apply_edits(bench.source_tree(), [edit])
        assert isinstance(made, EditResult)
        trial = bench.try_patch(made.diff)
        _, baseline = bench.visible_baseline()
    assert trial.applied and trial.finished
    assert set(trial.regressions) == set(baseline) and trial.run is not None
    assert regressions_of(trial.run, baseline) == trial.regressions
