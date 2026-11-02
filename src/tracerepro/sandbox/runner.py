"""Locked-down container runner (plan §5.2, spec §7.2).

Every run gets a throwaway workspace on the host: `repo/` is a writable copy of the image's
`/repo` (where a patch or oracle test files can be placed before the run), and `out/` receives
result files. The container itself runs with:

- no network (`--network none`) and no Docker socket;
- a read-only root filesystem, with tmpfs at `/tmp` (also `HOME`, so caches land there);
- a non-root user, all capabilities dropped, `no-new-privileges`;
- PID, CPU and memory limits, and a hard wall-clock timeout enforced from the host;
- an explicit environment: nothing from the host is passed through;
- `PYTHONDONTWRITEBYTECODE=1`, and `-p no:cacheprovider` for pytest runs.

The only writable paths are `/repo`, `/out` and `/tmp`. The harness pytest plugin is mounted
read-only. Everything written inside the container is untrusted (spec §7.3): result files are
size-capped and schema-checked when read back.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
import docker.errors
import requests.exceptions
from docker.models.containers import Container

PLUGIN_DIR = Path(__file__).resolve().parent / "plugin"
PLUGIN_NAME = "tracerepro_pytest_plugin"
FALLBACK_UID = 1000  # the `runner` user in every bug image

REPO_MOUNT = "/repo"
OUT_MOUNT = "/out"
PLUGIN_MOUNT = "/opt/tracerepro/plugin"
RESULTS_FILE = "results.json"
JUNIT_FILE = "junit.xml"

MAX_OUTPUT_CHARS = 200_000
MAX_RESULTS_BYTES = 20_000_000

BASE_ENV = {
    "HOME": "/tmp",
    "USER": "runner",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": PLUGIN_MOUNT,
    "PIP_NO_INDEX": "1",
}


class SandboxError(RuntimeError):
    """The sandbox could not be set up, or its output is malformed."""


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 600.0
    pids: int = 256
    cpus: float = 2.0
    memory: str = "2g"
    tmpfs_size: str = "512m"


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Workspace:
    """Host directories mounted into one sandbox run. Discarded afterwards."""

    root: Path

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def out(self) -> Path:
        return self.root / "out"


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None  # None when the container was killed at the timeout
    output: str  # combined stdout and stderr, truncated to the last MAX_OUTPUT_CHARS
    duration_s: float
    timed_out: bool
    oom_killed: bool


@dataclass(frozen=True)
class FailureInfo:
    type: str
    message: str
    frames: tuple[tuple[str, str, int], ...]  # (file, function, line), innermost last

    @property
    def innermost(self) -> tuple[str, str, int] | None:
        return self.frames[-1] if self.frames else None


@dataclass(frozen=True)
class CaseResult:
    nodeid: str
    when: str  # "setup", "call", "teardown" or "collect"
    outcome: str  # passed, failed, error, skipped, xfailed, xpassed
    failure: FailureInfo | None = None
    longrepr: str | None = None


@dataclass(frozen=True)
class PytestRun:
    run: RunResult
    exitstatus: int | None
    tests: tuple[CaseResult, ...] = field(default=())
    junit_path: Path | None = None

    def outcomes(self) -> dict[str, str]:
        """Node id -> worst outcome across its phases (error > failed > others)."""
        rank = {"error": 2, "failed": 1}
        result: dict[str, str] = {}
        for t in self.tests:
            current = result.get(t.nodeid)
            if current is None or rank.get(t.outcome, 0) > rank.get(current, 0):
                result[t.nodeid] = t.outcome
        return result


def _container_user() -> str:
    """Run as the invoking host user when it is non-root, so the bind-mounted workspace is
    writable on Linux hosts without loosening its permissions. Docker Desktop ignores ownership."""
    uid, gid = os.getuid(), os.getgid()
    return f"{uid}:{gid}" if uid != 0 else f"{FALLBACK_UID}:{FALLBACK_UID}"


def _open_up(path: Path) -> None:
    """Make a workspace writable by FALLBACK_UID (only needed when the harness runs as root)."""
    for dirpath, dirnames, filenames in os.walk(path):
        os.chmod(dirpath, 0o777)
        for name in [*dirnames, *filenames]:
            p = Path(dirpath, name)
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o666)


def create_workspace(
    root: Path, image: str | None = None, *, client: docker.DockerClient | None = None
) -> Workspace:
    """Create `root/repo` and `root/out`. With `image`, `repo/` is a copy of the image's `/repo`."""
    ws = Workspace(root)
    if root.exists() and any(root.iterdir()):
        raise SandboxError(f"workspace root is not empty: {root}")
    ws.out.mkdir(parents=True)
    if image is None:
        ws.repo.mkdir()
    else:
        _extract_repo(client or docker.from_env(), image, root)
    if os.getuid() == 0:
        _open_up(root)
    return ws


def _extract_repo(client: docker.DockerClient, image: str, dest: Path) -> None:
    container = client.containers.create(image, command=["true"], network_disabled=True)
    try:
        with tempfile.TemporaryFile() as buf:
            stream, _ = container.get_archive(REPO_MOUNT)
            for chunk in stream:
                buf.write(chunk)
            buf.seek(0)
            with tarfile.open(fileobj=buf) as tar:
                # The "data" filter refuses absolute paths, `..`, and links that leave dest.
                tar.extractall(dest, filter="data")
    except docker.errors.NotFound as exc:
        raise SandboxError(f"{image} has no {REPO_MOUNT}") from exc
    finally:
        container.remove(force=True)
    if not (dest / "repo").is_dir():
        raise SandboxError(f"{image}: {REPO_MOUNT} did not extract as a directory")


def remove_workspace(ws: Workspace) -> None:
    shutil.rmtree(ws.root, ignore_errors=True)


def _logs(container: Container) -> str:
    raw = container.logs(stdout=True, stderr=True)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    return text[-MAX_OUTPUT_CHARS:]


def run_sandboxed(
    image: str,
    command: Sequence[str],
    ws: Workspace,
    *,
    limits: Limits = DEFAULT_LIMITS,
    env: dict[str, str] | None = None,
    client: docker.DockerClient | None = None,
) -> RunResult:
    """Run `command` (an argv list, never a shell string) in a locked-down container."""
    if isinstance(command, str):
        raise TypeError("command must be a sequence of arguments, not a string")
    client = client or docker.from_env()
    container = client.containers.create(
        image,
        command=list(command),
        working_dir=REPO_MOUNT,
        user=_container_user(),
        environment={**BASE_ENV, **(env or {})},
        network_mode="none",
        read_only=True,
        tmpfs={"/tmp": f"rw,nosuid,nodev,size={limits.tmpfs_size}"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        pids_limit=limits.pids,
        nano_cpus=int(limits.cpus * 1e9),
        mem_limit=limits.memory,
        memswap_limit=limits.memory,  # no swap on top of the memory limit
        volumes={
            str(ws.repo.resolve()): {"bind": REPO_MOUNT, "mode": "rw"},
            str(ws.out.resolve()): {"bind": OUT_MOUNT, "mode": "rw"},
            str(PLUGIN_DIR): {"bind": PLUGIN_MOUNT, "mode": "ro"},
        },
    )
    timed_out = False
    exit_code: int | None = None
    start = time.monotonic()
    try:
        container.start()
        try:
            status = container.wait(timeout=limits.timeout_s)
            exit_code = int(status["StatusCode"])
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            timed_out = True
            container.kill()
            container.wait(timeout=30)
        duration = time.monotonic() - start
        container.reload()
        oom_killed = bool(container.attrs["State"].get("OOMKilled"))
        output = _logs(container)
    finally:
        container.remove(force=True)
    return RunResult(
        exit_code=None if timed_out else exit_code,
        output=output,
        duration_s=duration,
        timed_out=timed_out,
        oom_killed=oom_killed,
    )


def pytest_command(test_ids: Sequence[str], *, extra_args: Sequence[str] = ()) -> list[str]:
    """argv for a pytest run that writes JUnit XML and plugin JSON to /out."""
    return [
        "python",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        PLUGIN_NAME,
        f"--tracerepro-json={OUT_MOUNT}/{RESULTS_FILE}",
        f"--junitxml={OUT_MOUNT}/{JUNIT_FILE}",
        "-q",
        *extra_args,
        *test_ids,
    ]


def run_pytest(
    image: str,
    test_ids: Sequence[str],
    ws: Workspace,
    *,
    limits: Limits = DEFAULT_LIMITS,
    extra_args: Sequence[str] = (),
    client: docker.DockerClient | None = None,
) -> PytestRun:
    run = run_sandboxed(
        image, pytest_command(test_ids, extra_args=extra_args), ws, limits=limits, client=client
    )
    results = ws.out / RESULTS_FILE
    junit = ws.out / JUNIT_FILE
    exitstatus, tests = (None, ()) if not results.is_file() else parse_results(results)
    return PytestRun(
        run=run,
        exitstatus=exitstatus,
        tests=tests,
        junit_path=junit if junit.is_file() else None,
    )


def _str(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise SandboxError(f"results: {key!r} must be a string")
    return value


def _parse_failure(obj: Any) -> FailureInfo:
    if not isinstance(obj, dict) or not isinstance(obj.get("frames"), list):
        raise SandboxError("results: malformed failure")
    frames = []
    for f in obj["frames"]:
        if not isinstance(f, dict) or not isinstance(f.get("line"), int):
            raise SandboxError("results: malformed frame")
        frames.append((_str(f, "file"), _str(f, "function"), int(f["line"])))
    return FailureInfo(_str(obj, "type"), _str(obj, "message"), tuple(frames))


def parse_results(path: Path) -> tuple[int | None, tuple[CaseResult, ...]]:
    """Read the plugin's JSON. It was written inside the sandbox, so treat it as untrusted."""
    if path.is_symlink() or path.stat().st_size > MAX_RESULTS_BYTES:
        raise SandboxError(f"{path}: refusing symlinked or oversized results file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise SandboxError(f"{path}: unknown results schema")
    exitstatus = data.get("exitstatus")
    if not isinstance(exitstatus, int) or not isinstance(data.get("tests"), list):
        raise SandboxError(f"{path}: malformed results")
    records = []
    for t in data["tests"]:
        if not isinstance(t, dict):
            raise SandboxError(f"{path}: malformed test record")
        longrepr = t.get("longrepr")
        records.append(
            CaseResult(
                nodeid=_str(t, "nodeid"),
                when=_str(t, "when"),
                outcome=_str(t, "outcome"),
                failure=_parse_failure(t["failure"]) if "failure" in t else None,
                longrepr=longrepr if isinstance(longrepr, str) else None,
            )
        )
    return exitstatus, tuple(records)


def copy_into(ws: Workspace, files: dict[str, bytes]) -> None:
    """Place files (e.g. oracle tests from c_fix) into the workspace repo at relative paths."""
    for rel, content in files.items():
        target = (ws.repo / rel).resolve()
        if Path(rel).is_absolute() or not target.is_relative_to(ws.repo.resolve()):
            raise SandboxError(f"refusing path outside the workspace: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
