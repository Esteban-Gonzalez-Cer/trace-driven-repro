"""The live demo runs Pipeline B2 exactly as the second study measured it (DECISIONS.md,
2026-11-17: the Phase 5c switches were not adopted), only on that study's bugs, never grading."""

from tracerepro.dataset.manifest import PROJECT_ROOT
from tracerepro.experiment import load_config

CONFIGS = PROJECT_ROOT / "benchmark" / "configs"


def test_the_demo_uses_b2_as_measured() -> None:
    demo = load_config(CONFIGS / "live-demo.yaml")
    measured = load_config(CONFIGS / "final-b2-arm-b2.yaml")
    assert demo.pipeline_config == measured.pipeline_config
    assert (demo.model, demo.prompt_version, demo.pipelines) == ("claude-sonnet-5-5", "v3", ("B",))
    assert not demo.request_definitions and not demo.check_repair
    assert set(demo.bugs) <= set(measured.bugs) and demo.split == measured.split
    assert demo.guard == measured.guard
    assert not demo.evaluate and not demo.cache and demo.trials == 1
    assert [p.stem for p in demo.manifests()] == ["werkzeug-3110"]
