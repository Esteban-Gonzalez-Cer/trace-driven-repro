"""The live demo's webhook receiver (development plan §8.3).

    make demo-serve                      # http://127.0.0.1:8765, B2 as measured
    uv run python scripts/demo-send.py   # sign and send werkzeug-3110's payload

`POST /webhook` takes a Sentry-style crash payload and starts one run of Pipeline B2 on it;
`GET /jobs/{id}` shows how the run went. What a request must pass, in order:

1. **From this machine.** The server listens on 127.0.0.1 only, and refuses any other client.
2. **Small.** At most 256 KB, refused while it is still being read.
3. **Signed.** `Sentry-Hook-Signature` is the HMAC-SHA256 of the body with the demo's secret
   (`DEMO_WEBHOOK_SECRET` in `.env`), as Sentry signs its webhooks.
4. **Well-formed.** The strict shape check in `payload_check.py`.
5. **Allowed.** The `tracerepro_bug` tag names a bug in the demo config
   (`benchmark/configs/live-demo.yaml`) whose image is already built.
6. **One at a time.** A second payload while a job runs is refused, and the server takes at most
   `--max-jobs` payloads in its life, so a loop cannot spend the Claude plan.

The routing tag is removed before the run, so the model sees the payload as the benchmark did.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from tracerepro.dataset.manifest import PROJECT_ROOT, Manifest, load_manifest
from tracerepro.experiment import RunConfig, load_config
from tracerepro.live.env_file import read_env_file, require
from tracerepro.live.jobs import LIVE_DIR, Job, run_job, short_message, summarize
from tracerepro.live.payload_check import MAX_BYTES, PayloadRejected, check_payload, without_tags

HOST = "127.0.0.1"
PORT = 8765
DEMO_CONFIG = PROJECT_ROOT / "benchmark" / "configs" / "live-demo.yaml"
SIGNATURE_HEADER = "sentry-hook-signature"
ROUTING_TAG = "tracerepro_bug"
SECRET_KEY = "DEMO_WEBHOOK_SECRET"
MIN_SECRET = 32
LOOPBACK = frozenset({"127.0.0.1", "::1"})
MAX_JOBS = 3
_JOB_ID = re.compile(r"[0-9a-f]{12}")

# job, manifest, payload -> the run's summary
Runner = Callable[[Job, Manifest, dict[str, Any]], dict[str, Any]]


def signature(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


class LiveService:
    """The jobs and the rules; `create_app` puts HTTP in front of it."""

    def __init__(
        self,
        config: RunConfig,
        secret: bytes,
        runner: Runner,
        *,
        max_jobs: int = MAX_JOBS,
        root: Path = LIVE_DIR,
        background: bool = True,
    ) -> None:
        if len(secret) < MIN_SECRET:
            raise ValueError(f"the webhook secret needs at least {MIN_SECRET} characters")
        self.config = config
        self.manifests = {m.id: m for m in map(load_manifest, config.manifests())}
        self._secret = secret
        self._runner = runner
        self.max_jobs = max_jobs
        self.root = root
        self._background = background
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def verify(self, body: bytes, given: str | None) -> None:
        if not given or not hmac.compare_digest(signature(self._secret, body), given.strip()):
            raise HTTPException(401, "bad or missing signature")

    def accept(self, body: bytes) -> Job:
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(400, "the body is not JSON") from None
        try:
            payload = check_payload(data)
        except PayloadRejected as exc:
            raise HTTPException(422, {"rejected": exc.reasons}) from None
        bug = payload.get("tags", {}).get(ROUTING_TAG)
        manifest = self.manifests.get(bug) if isinstance(bug, str) else None
        if manifest is None:
            raise HTTPException(422, f"the {ROUTING_TAG} tag must name a bug of the demo config")
        with self._lock:
            if any(j.state in ("queued", "running") for j in self.jobs.values()):
                raise HTTPException(409, "a job is already running")
            if len(self.jobs) >= self.max_jobs:
                raise HTTPException(429, f"this server takes at most {self.max_jobs} jobs")
            job = Job.new(manifest.id, self.root)
            self.jobs[job.id] = job
        clean = without_tags(payload, {ROUTING_TAG})
        if self._background:
            threading.Thread(target=self._run, args=(job, manifest, clean), daemon=True).start()
        else:
            self._run(job, manifest, clean)
        return job

    def _run(self, job: Job, manifest: Manifest, payload: dict[str, Any]) -> None:
        job.state = "running"
        try:
            job.summary = self._runner(job, manifest, payload)
            job.state = "done"
            job.message = "patch ready" if job.summary.get("patch") else "no patch"
        except BaseException as exc:  # the job reports every failure; the server keeps running
            job.state, job.message = "failed", short_message(exc)
        finally:
            job.finished = time.time()


def create_app(service: LiveService, *, clients: frozenset[str] = LOOPBACK) -> FastAPI:
    app = FastAPI(title="trace-driven-repro live demo", docs_url=None, redoc_url=None,
                  openapi_url=None)  # fmt: skip

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        host = request.client.host if request.client else None
        if host not in clients:
            return JSONResponse({"detail": "local requests only"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "bugs": sorted(service.manifests), "jobs": len(service.jobs)}

    @app.post("/webhook", status_code=202)
    async def webhook(request: Request) -> dict[str, Any]:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BYTES:
            raise HTTPException(413, f"at most {MAX_BYTES} bytes")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BYTES:
                raise HTTPException(413, f"at most {MAX_BYTES} bytes")
        service.verify(bytes(body), request.headers.get(SIGNATURE_HEADER))
        job = service.accept(bytes(body))
        return {"job": job.id, "status": f"/jobs/{job.id}"}

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = service.jobs.get(job_id) if _JOB_ID.fullmatch(job_id) else None
        if job is None:
            raise HTTPException(404, "no such job")
        return job.view()

    return app


def b2_runner(config: RunConfig) -> Runner:
    def run(job: Job, manifest: Manifest, payload: dict[str, Any]) -> dict[str, Any]:
        return summarize(run_job(job, manifest, config, payload))

    return run


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=DEMO_CONFIG)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--max-jobs", type=int, default=MAX_JOBS)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    secret = require(read_env_file({SECRET_KEY}), SECRET_KEY, min_length=MIN_SECRET)
    service = LiveService(config, secret.encode(), b2_runner(config), max_jobs=args.max_jobs)
    uvicorn.run(create_app(service), host=HOST, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
