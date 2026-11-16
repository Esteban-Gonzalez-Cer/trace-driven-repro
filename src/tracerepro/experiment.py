"""Benchmark runner (`make run-benchmark`): the agent stage, then the evaluator, for every bug,
pipeline and trial in a run config (development plan §6.4, §8.1).

    uv run python -m tracerepro.experiment benchmark/configs/dev.yaml

- **Run id:** `{name}-{harness commit}-{prompt version}-{model}-cli{version}` (`-dirty` when the
  working tree has uncommitted changes), so every result names the exact setup.
- **Resumable:** finished trials (a `record.json`, plus a `verdict.json` when evaluating) are
  skipped. A trial stopped by an infrastructure failure or a usage limit is moved aside to
  `trial_{n}.aborted-{time}` (its call log is kept) and rerun later. Trials are never rerun
  because of their result.
- **Separation (spec §7.3):** the agent stage runs on a `DockerWorkbench` that holds only the
  buggy code. The evaluator then scores the submitted diff in a fresh container with the oracle
  tests copied in. Nothing from the agent's containers is reused.
- **Plan usage:** the usage guard and the per-run token cap come from the config. A usage limit
  stops the whole run (exit code 3); run the same command again later to resume.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tracerepro.agent.client import LLMClient, Provider, Settings
from tracerepro.agent.errors import InfraFailure, PacingLimitReached, UsageLimitReached
from tracerepro.agent.pipeline import (
    AgentResult,
    Bug,
    PipelineConfig,
    run_pipeline_b,
)
from tracerepro.agent.prompts import CURRENT_VERSION
from tracerepro.agent.providers.claude_cli import ClaudeCLI
from tracerepro.agent.record import is_complete, trial_dir, write_record, write_verdict
from tracerepro.agent.usage_guard import GuardLimits, UsageGuard
from tracerepro.agent.workbench import DockerWorkbench, Workbench
from tracerepro.context.assembler import DEFAULT_BUDGET_CHARS
from tracerepro.dataset.manifest import PROJECT_ROOT, Manifest, load_manifest

RUNS_DIR = PROJECT_ROOT / "runs"
WORK_DIR = PROJECT_ROOT / "work"
PIPELINES: dict[str, Callable[..., AgentResult]] = {"B": run_pipeline_b}
# Retired after the M4 study (DECISIONS.md, 2026-11-15). Configs that used them are records of
# runs made at the `m3-freeze` tag; rerun them from that tag.
SPLITS = ("dev", "test", "practice", "test2")
# A config's split admits manifests of these splits. The M4 test set was spent by the M4 study,
# so it joins the dev bugs as the practice set for Pipeline B2.
SPLIT_MEMBERS = {"practice": {"dev", "test"}}
_SPLIT_RE = re.compile(r"\[split: ([a-z0-9]+)\]")
RETIRED = {"pipelines": {"A"}, "keys": {"max_repro_attempts"}}
EXIT_PAUSED = 3


@dataclass(frozen=True)
class RunConfig:
    name: str
    model: str
    bugs: tuple[str, ...]
    pipelines: tuple[str, ...] = ("B",)
    trials: int = 1
    effort: str = "medium"
    max_output_tokens: int = 16_000
    prompt_version: str = CURRENT_VERSION
    max_patch_attempts: int = 2
    token_cap: int = 400_000
    context_budget: int = DEFAULT_BUDGET_CHARS
    cache: bool = True
    evaluate: bool = True
    guard: GuardLimits = field(default_factory=GuardLimits)
    # dev: the 5 dev bugs; test: the M4 test set (spent); practice: dev + M4 test, for tuning
    # Pipeline B2 (Phase 5b); test2: the fresh held-out set, only after its freeze.
    split: str = "dev"
    # Pipeline B2 switches (Phase 5b); all unset reproduces Pipeline B as run in the M4 study.
    crash_check: bool = False
    file_requests: bool = False  # the model may ask once for up to 3 more library files
    retry_model: str | None = None  # escalation: the model for attempts after the first
    retry_effort: str | None = None  # escalation: the effort for attempts after the first

    @property
    def settings(self) -> Settings:
        return Settings(self.model, self.effort, self.max_output_tokens)

    @property
    def pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(
            settings=self.settings,
            prompt_version=self.prompt_version,
            max_patch_attempts=self.max_patch_attempts,
            token_cap=self.token_cap,
            context_budget=self.context_budget,
            crash_check=self.crash_check,
            file_requests=self.file_requests,
            retry_settings=self.retry_settings,
        )

    @property
    def retry_settings(self) -> Settings | None:
        if self.retry_model is None and self.retry_effort is None:
            return None
        return Settings(
            self.retry_model or self.model, self.retry_effort or self.effort, self.max_output_tokens
        )

    def manifests(self, root: Path = PROJECT_ROOT) -> list[Path]:
        """The config's manifests. Every one must belong to the config's `split`: no model may
        see a test-set bug through a dev config (spec §5.7: selection is blind to the models)."""
        paths: list[Path] = []
        for pattern in self.bugs:
            found = sorted(Path(p) for p in glob.glob(str(root / pattern)))
            if not found:
                raise ValueError(f"no manifest matches {pattern!r}")
            paths += [p for p in found if p not in paths]
        allowed = SPLIT_MEMBERS.get(self.split, {self.split})
        wrong = [p.stem for p in paths if manifest_split(load_manifest(p)) not in allowed]
        if wrong:
            raise ValueError(f"not in the {self.split} split: {wrong}")
        return paths


def manifest_split(manifest: Manifest) -> str:
    """The split preflight recorded in the manifest's notes (`[split: test]`); dev otherwise
    (the toy fixture and manifests written before splits existed)."""
    # Any held-out mark wins over dev, so a bug marked test anywhere is never treated as dev.
    held_out = [s for s in _SPLIT_RE.findall(manifest.notes) if s != "dev"]
    return held_out[-1] if held_out else "dev"


def load_config(path: Path) -> RunConfig:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a run config is a mapping")
    retired = set(data) & RETIRED["keys"] | set(data.get("pipelines") or ()) & RETIRED["pipelines"]
    if retired:
        raise ValueError(
            f"{path}: uses {sorted(retired)}, retired with Pipeline A after the M4 study; "
            "rerun this config from the m3-freeze tag"
        )
    known = {f.name for f in dataclasses.fields(RunConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    guard = GuardLimits(**data.pop("guard", {}) or {})
    for key in ("bugs", "pipelines"):
        if key in data:
            data[key] = tuple(data[key])
    config = RunConfig(guard=guard, **data)
    bad = set(config.pipelines) - set(PIPELINES)
    if bad:
        raise ValueError(f"{path}: unknown pipelines {sorted(bad)}")
    config.settings  # validates effort and output limit  # noqa: B018
    config.retry_settings  # validates the escalation's effort  # noqa: B018
    if (config.crash_check or config.file_requests) and config.prompt_version in ("v1", "v2"):
        raise ValueError(f"{path}: crash_check and file_requests need prompts v3 or later")
    if config.split not in SPLITS:
        raise ValueError(f"{path}: split must be one of {list(SPLITS)}")
    return config


def harness_commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    commit = git("rev-parse", "--short=8", "HEAD")
    return commit + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def run_id(config: RunConfig, commit: str, provider_version: str) -> str:
    return f"{config.name}-{commit}-{config.prompt_version}-{config.model}-cli{provider_version}"


def bug_of(manifest: Manifest) -> Bug:
    if manifest.payload_path is None:
        raise ValueError(f"{manifest.id}: manifest has no payload")
    return Bug(manifest.id, json.loads((PROJECT_ROOT / manifest.payload_path).read_text()))


def evaluate(manifest: Manifest, result: AgentResult) -> tuple[dict[str, Any], str | None]:
    """Score the submitted diff in a fresh container (imported here: the agent stage never
    imports the evaluator)."""
    from tracerepro.dataset.repo import diff
    from tracerepro.evaluate.coverage import context_coverage
    from tracerepro.evaluate.patch_oracle import bug_repo, gold_diff, review_report, score_patch
    from tracerepro.evaluate.repro_oracle import judge_repro
    from tracerepro.evaluate.taxonomy import label_run

    git_dir = bug_repo(manifest)
    # Whether the agent was shown the lines the maintainers changed for the crash (Amendment 5;
    # `gold_files` is the crash-only part of the fix, Amendment 1).
    crash_fix = diff(git_dir, manifest.c_bug, manifest.c_fix, list(manifest.gold_files))
    data: dict[str, Any] = {
        "context_coverage": context_coverage(crash_fix, result.context.get("shown_lines", {}))
    }
    if result.repro is not None:  # Pipeline A: the repro oracle (spec §7.4)
        verdict_r = judge_repro(manifest, result.repro, gold_diff(manifest, git_dir),
                                work_dir=WORK_DIR)  # fmt: skip
        data["repro_oracle"] = verdict_r.to_dict()
    review = None
    if result.final_diff is None:
        data |= {"kind": result.status, "message": "no patch was submitted"}
    else:
        verdict = score_patch(manifest, result.final_diff)
        data |= {
            "kind": str(verdict.kind),
            "message": verdict.message,
            "failing_oracle": list(verdict.failing_oracle),
            "regressions": list(verdict.regressions),
            "flags": [str(f) for f in verdict.flags],
        }
        review = review_report(manifest, verdict, result.final_diff)
    data["label"], data["solved"] = label_run(result.pipeline, result.to_dict(), data)
    return data, review


def run_trial(
    manifest: Manifest,
    pipeline: str,
    config: RunConfig,
    folder: Path,
    provider: Provider,
    guard: UsageGuard | None,
    bench_factory: Callable[[Manifest, Path], Any],
    meta: dict[str, Any],
) -> AgentResult:
    client = LLMClient(
        provider,
        log_path=folder / "calls.jsonl",
        cache_dir=WORK_DIR / "llm-cache" if config.cache else None,
        guard=guard,
    )
    started = time.time()
    with bench_factory(manifest, WORK_DIR / "agent" / uuid.uuid4().hex[:12]) as bench:
        bench_: Workbench = bench
        result = PIPELINES[pipeline](bug_of(manifest), client, bench_, config.pipeline_config)
    meta = {**meta, "agent_seconds": round(time.time() - started, 1)}
    write_record(folder, result, meta)
    return result


def _set_aside(folder: Path) -> None:
    if folder.exists():
        folder.rename(folder.with_name(f"{folder.name}.aborted-{time.strftime('%Y%m%dT%H%M%S')}"))


def run(
    config: RunConfig,
    *,
    provider: Provider | None = None,
    runs_dir: Path = RUNS_DIR,
    bench_factory: Callable[[Manifest, Path], Any] = DockerWorkbench,
    evaluator: Callable[[Manifest, AgentResult], tuple[dict[str, Any], str | None]] = evaluate,
    commit: str | None = None,
) -> int:
    provider = provider or ClaudeCLI()
    rid = run_id(config, commit or harness_commit(), provider.version)
    if bench_factory is DockerWorkbench:  # a real run: clear what a killed run left behind
        from tracerepro.sandbox.runner import remove_leftover_sandboxes

        stale = remove_leftover_sandboxes()
        if stale:
            print(f"removed {stale} sandbox container(s) left by an interrupted run")
        shutil.rmtree(WORK_DIR / "agent", ignore_errors=True)
    root = runs_dir / rid
    root.mkdir(parents=True, exist_ok=True)
    (root / "run.json").write_text(
        json.dumps({"run_id": rid, "config": dataclasses.asdict(config)}, indent=2) + "\n"
    )
    guard = UsageGuard(WORK_DIR / "llm-usage.json", config.guard)
    summary: dict[str, int] = {}
    for manifest_path in config.manifests():
        manifest = load_manifest(manifest_path)
        for pipeline in config.pipelines:
            for n in range(1, config.trials + 1):
                folder = trial_dir(runs_dir, rid, manifest.id, pipeline, config.model, n)
                if is_complete(folder, evaluated=config.evaluate):
                    summary["skipped"] = summary.get("skipped", 0) + 1
                    continue
                label = f"{manifest.id} {pipeline} trial {n}"
                if not (folder / "record.json").is_file():
                    _set_aside(folder)
                    meta = {"run_id": rid, "bug": manifest.id, "pipeline": pipeline, "trial": n}
                    try:
                        result = run_trial(
                            manifest, pipeline, config, folder, provider, guard, bench_factory,
                            meta,
                        )  # fmt: skip
                    except (UsageLimitReached, PacingLimitReached) as exc:
                        _set_aside(folder)
                        print(f"{label}: paused ({exc}); run the same command later to resume")
                        return EXIT_PAUSED
                    except InfraFailure as exc:
                        _set_aside(folder)
                        print(f"{label}: infrastructure failure, will be rerun: {exc}")
                        summary["infra"] = summary.get("infra", 0) + 1
                        continue
                else:
                    record = json.loads((folder / "record.json").read_text())
                    result = _result_from(record["result"])
                if config.evaluate:
                    verdict, review = evaluator(manifest, result)
                    write_verdict(folder, verdict, review)
                    outcome = verdict["kind"]
                else:
                    outcome = result.status
                summary[outcome] = summary.get(outcome, 0) + 1
                print(f"{label}: {result.status}, {result.tokens_used} tokens -> {outcome}")
    print(f"run {rid}: {json.dumps(summary, sort_keys=True)}")
    return 1 if summary.get("infra") else 0


def _result_from(data: dict[str, Any]) -> AgentResult:
    from tracerepro.agent.pipeline import Attempt

    attempts = [Attempt(**{**a, "regressions": tuple(a["regressions"])}) for a in data["attempts"]]
    return AgentResult(**{**data, "attempts": attempts})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent pipelines and score them.")
    parser.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    return run(load_config(args.config))


if __name__ == "__main__":
    sys.exit(main())
