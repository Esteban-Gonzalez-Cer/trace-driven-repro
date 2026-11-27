"""Live-demo jobs: one received payload, one run of Pipeline B2 as measured, no grading.

A job is the benchmark's agent stage (`experiment.run_trial`) on a payload that came from the
webhook instead of a manifest. It uses the same workbench, sandbox, prompts and usage guard. The
evaluator is never called: a live crash has no maintainers' test to grade against.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracerepro.agent.client import Provider
from tracerepro.agent.pipeline import AgentResult, Bug
from tracerepro.agent.providers.claude_cli import ClaudeCLI
from tracerepro.agent.usage_guard import UsageGuard
from tracerepro.agent.workbench import DockerWorkbench
from tracerepro.dataset.manifest import Manifest
from tracerepro.experiment import RUNS_DIR, WORK_DIR, RunConfig, harness_commit, run_trial

LIVE_DIR = RUNS_DIR / "live"
MESSAGE_LIMIT = 300


@dataclass
class Job:
    id: str
    bug: str
    folder: Path
    received: float = field(default_factory=time.time)
    state: str = "queued"  # queued, running, done, failed
    message: str = ""
    summary: dict[str, Any] | None = None
    finished: float | None = None

    @classmethod
    def new(cls, bug: str, root: Path = LIVE_DIR) -> Job:
        job_id = uuid.uuid4().hex[:12]
        return cls(job_id, bug, root / job_id)

    def view(self) -> dict[str, Any]:
        """What `GET /jobs/{id}` shows: no paths, no payload, no model text."""
        return {
            "job": self.id,
            "bug": self.bug,
            "state": self.state,
            "message": self.message,
            "summary": self.summary,
            "seconds": round((self.finished or time.time()) - self.received, 1),
        }


def summarize(result: AgentResult) -> dict[str, Any]:
    """The run's facts for the job status (and later the draft PR)."""
    final = result.attempts[result.final_from - 1] if result.final_from else None
    return {
        "status": result.status,
        "patch": result.final_diff is not None,
        "final_from": result.final_from,
        "stopped_by": result.stopped_by,
        "tokens": result.tokens_used,
        "attempts": [
            {"n": a.n, "outcome": a.outcome, "check": a.check, "check_after": a.check_after}
            for a in result.attempts
        ],
        "check_reproduced": final is not None and final.check == "valid",
        "check_passes_after": final is not None and final.check_after == "passes",
        "files_granted": (result.file_request or {}).get("granted", []),
        "prompt_leaks": len(result.prompt_leaks),
    }


def short_message(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:MESSAGE_LIMIT]


def require_image(tag: str) -> None:
    """The demo runs only on a prebuilt image: a webhook never triggers a clone or a build."""
    import docker
    from docker.errors import ImageNotFound

    try:
        docker.from_env().images.get(tag)
    except ImageNotFound:
        raise RuntimeError(f"image {tag} is not built; run make build-images first") from None


def run_job(
    job: Job,
    manifest: Manifest,
    config: RunConfig,
    payload: dict[str, Any],
    *,
    provider: Provider | None = None,
    bench_factory: Any = DockerWorkbench,
    check_image: Any = require_image,
    ledger: Path = WORK_DIR / "llm-usage.json",
) -> AgentResult:
    """Run B2 once on `payload` and write the trial folder under `job.folder`."""
    check_image(manifest.image_tag)
    provider = provider or ClaudeCLI()
    job.folder.mkdir(parents=True, exist_ok=True)
    (job.folder / "payload.json").write_text(json.dumps(payload, indent=2) + "\n")
    guard = UsageGuard(ledger, config.guard)  # the same plan-usage ledger as the benchmark
    meta = {
        "run_id": f"live-{job.id}",
        "bug": manifest.id,
        "pipeline": "B",
        "trial": 1,
        "source": "webhook",
        "config": config.name,
        "harness": harness_commit(),
        "provider_version": provider.version,
    }
    return run_trial(
        manifest, "B", config, job.folder, provider, guard, bench_factory, meta,
        bug=Bug(manifest.id, payload),
    )  # fmt: skip
