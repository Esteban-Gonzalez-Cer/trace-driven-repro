"""Benchmark runner (plan §6.4, §8.1): config loading, run ids, the trial folder layout, resuming,
and how infrastructure failures and plan limits are handled. Fake model, workbench and
evaluator: no Docker, no plan usage."""

import json
from pathlib import Path
from typing import Any

import pytest

from tracerepro import experiment
from tracerepro.agent.errors import InfraFailure, UsageLimitReached
from tracerepro.agent.pipeline import AgentResult
from tracerepro.agent.providers.fake import FakeProvider
from tracerepro.dataset.manifest import PROJECT_ROOT, Manifest

from .agent.test_pipeline import CLEAN, GOLD, FakeBench

DEV = PROJECT_ROOT / "benchmark" / "configs" / "dev.yaml"


def config(**overrides: Any) -> experiment.RunConfig:
    base: dict[str, Any] = {
        "name": "t",
        "model": "claude-haiku-5-5",
        "bugs": ("benchmark/manifests/marshmallow-3016.yaml",),
        "cache": False,
    }
    return experiment.RunConfig(**{**base, **overrides})


class Bench:
    """Context-manager wrapper around the fake workbench, as the runner expects."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.count = 0

    def __call__(self, manifest: Manifest, root: Path) -> "Bench":
        self.count += 1
        self.bench = FakeBench(self.tmp / f"b{self.count}", [CLEAN, CLEAN])
        return self

    def __enter__(self) -> FakeBench:
        return self.bench

    def __exit__(self, *exc: object) -> None:
        return None


def fake_evaluator(manifest: Manifest, result: AgentResult) -> tuple[dict[str, Any], str]:
    return {"kind": "correct" if result.final_diff else result.status}, "# review\n"


@pytest.fixture
def toy_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev bug's real payload points at marshmallow; the fake bench holds tiny_lib, so the
    runner is given a tiny_lib payload instead."""
    from .agent.test_pipeline import payload

    monkeypatch.setattr(experiment, "bug_of", lambda m: experiment.Bug(m.id, payload()))


def test_the_dev_config_loads() -> None:
    dev = experiment.load_config(DEV)
    assert dev.model == "claude-haiku-5-5" and dev.pipelines == ("A", "B")
    assert len(dev.manifests()) == 5 and dev.guard.max_calls_per_hour == 60


def test_bad_configs_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("name: x\nmodel: m\nbugs: [a]\ntemperature: 0\n")
    with pytest.raises(ValueError, match="unknown keys"):
        experiment.load_config(path)
    path.write_text("name: x\nmodel: m\nbugs: [a]\npipelines: [Z]\n")
    with pytest.raises(ValueError, match="unknown pipelines"):
        experiment.load_config(path)
    path.write_text("name: x\nmodel: m\nbugs: [a]\neffort: extreme\n")
    with pytest.raises(ValueError, match="effort"):
        experiment.load_config(path)
    with pytest.raises(ValueError, match="no manifest"):
        config(bugs=("benchmark/manifests/nothing-*.yaml",)).manifests()


def test_run_id_names_the_whole_setup() -> None:
    rid = experiment.run_id(config(), "abc12345", "2.1.308")
    assert rid == "t-abc12345-v2-claude-haiku-5-5-cli2.1.308"


def test_a_run_writes_the_trial_folder_and_resumes(
    tmp_path: Path, toy_payload: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(experiment, "WORK_DIR", tmp_path / "work")
    runs = tmp_path / "runs"
    bench = Bench(tmp_path)
    code = experiment.run(
        config(), provider=FakeProvider.replying([GOLD]), runs_dir=runs,
        bench_factory=bench, evaluator=fake_evaluator, commit="abc12345",
    )  # fmt: skip
    assert code == 0
    folder = runs / "t-abc12345-v2-claude-haiku-5-5-cli0" / "marshmallow-3016" / "B"
    folder = folder / "claude-haiku-5-5" / "trial_1"
    names = sorted(p.name for p in folder.iterdir())
    assert names == [
        "attempt_1.diff", "attempt_1.reply.md", "calls.jsonl", "final.diff", "record.json",
        "review.md", "verdict.json",
    ]  # fmt: skip
    record = json.loads((folder / "record.json").read_text())
    assert record["result"]["status"] == "submitted" and record["meta"]["trial"] == 1
    assert json.loads((folder / "verdict.json").read_text()) == {"kind": "correct"}
    assert (runs / "t-abc12345-v2-claude-haiku-5-5-cli0" / "run.json").is_file()
    # Run again: the finished trial is skipped, no model call is made.
    again = FakeProvider.replying([])
    assert experiment.run(
        config(), provider=again, runs_dir=runs, bench_factory=bench,
        evaluator=fake_evaluator, commit="abc12345",
    ) == 0  # fmt: skip
    assert again.requests == [] and bench.count == 1


def test_a_record_without_a_verdict_is_only_evaluated(
    tmp_path: Path, toy_payload: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(experiment, "WORK_DIR", tmp_path / "work")
    runs = tmp_path / "runs"
    kwargs: dict[str, Any] = {"runs_dir": runs, "bench_factory": Bench(tmp_path), "commit": "c"}
    experiment.run(config(evaluate=False), provider=FakeProvider.replying([GOLD]), **kwargs)
    scored: list[str] = []

    def evaluator(m: Manifest, r: AgentResult) -> tuple[dict[str, Any], None]:
        scored.append(r.final_diff or "")
        return {"kind": "incorrect"}, None

    none = FakeProvider.replying([])
    assert experiment.run(config(), provider=none, evaluator=evaluator, **kwargs) == 0
    assert none.requests == [] and "default=0" in scored[0]


def test_a_plan_limit_pauses_the_run_and_sets_the_trial_aside(
    tmp_path: Path, toy_payload: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(experiment, "WORK_DIR", tmp_path / "work")
    runs = tmp_path / "runs"
    code = experiment.run(
        config(), provider=FakeProvider.replying([UsageLimitReached("usage limit reached")]),
        runs_dir=runs, bench_factory=Bench(tmp_path), evaluator=fake_evaluator, commit="c",
    )  # fmt: skip
    assert code == experiment.EXIT_PAUSED
    trials = runs / "t-c-v2-claude-haiku-5-5-cli0" / "marshmallow-3016" / "B" / "claude-haiku-5-5"
    (aside,) = list(trials.iterdir())
    assert aside.name.startswith("trial_1.aborted-") and (aside / "calls.jsonl").is_file()
    assert not (aside / "record.json").exists()


def test_an_infra_failure_is_set_aside_and_never_scored(
    tmp_path: Path, toy_payload: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(experiment, "WORK_DIR", tmp_path / "work")
    scored: list[str] = []

    def evaluator(m: Manifest, r: AgentResult) -> tuple[dict[str, Any], None]:
        scored.append("x")
        return {"kind": "incorrect"}, None

    code = experiment.run(
        config(), provider=FakeProvider.replying([InfraFailure("gave up")]),
        runs_dir=tmp_path / "runs", bench_factory=Bench(tmp_path), evaluator=evaluator,
        commit="c",
    )  # fmt: skip
    assert code == 1 and scored == []
