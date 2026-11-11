"""Locked-down container runner (plan §5.2, spec §7.2).

Every run gets a throwaway workspace on the host: `repo/` is a copy of the image's `/repo` where
a patch or oracle test files are placed before the run, and `out/` receives the result files
afterwards. The container itself runs with:

- no network (`--network none`) and no Docker socket;
- a read-only root filesystem; `/repo`, `/out` and `/tmp` are size-capped in-memory filesystems
  (tmpfs), so **nothing the code under test writes ever reaches the host's disk**, and the
  kernel refuses writes past the cap;
- the workspace's `repo/` mounted read-only at `/src` and copied into `/repo` at start;
- a non-root user, all capabilities dropped, `no-new-privileges`;
- PID, CPU and memory limits, and a hard wall-clock timeout enforced from the host;
- an explicit environment: nothing from the host is passed through;
- `PYTHONDONTWRITEBYTECODE=1`, and `-p no:cacheprovider -p no:unraisableexception` for
  pytest runs.

When the command ends, a small wrapper prints `TRACEREPRO-DONE <exit code>` and keeps the
container alive; the host then copies `/out` out of the running container (regular files only,
size-capped) and kills it. The log is read as a stream with a byte cap: output past the cap
(a flood) kills the run. Audit events the plugin prints (`TRACEREPRO-AUDIT {json}`,
Amendment 4) are parsed from the whole stream, not only the part kept for display.

The harness pytest plugin is mounted read-only. Everything coming out of the container is
untrusted (spec §7.3): result files are size-capped and schema-checked when read back.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
import docker.errors
import requests.exceptions
from docker.models.containers import Container
from docker.types import LogConfig

PLUGIN_DIR = Path(__file__).resolve().parent / "plugin"
PLUGIN_NAME = "tracerepro_pytest_plugin"
FALLBACK_UID = 1000  # the `runner` user in every bug image

REPO_MOUNT = "/repo"
OUT_MOUNT = "/out"
SRC_MOUNT = "/src"
SUPERVISOR = "tracerepro_supervisor.py"
PLUGIN_MOUNT = "/opt/tracerepro/plugin"
RESULTS_FILE = "results.json"
JUNIT_FILE = "junit.xml"

MAX_OUTPUT_CHARS = 200_000
MAX_RESULTS_BYTES = 20_000_000
MAX_LOG_BYTES = 64 * 1024 * 1024  # more console output than this is a flood: the run is killed
AUDIT_PREFIX = "TRACEREPRO-AUDIT "
MAX_AUDIT_EVENTS = 2000
RESULT_FILES = ("results.json", "junit.xml")

BASE_ENV = {
    "HOME": "/tmp",
    "USER": "runner",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": PLUGIN_MOUNT,
    "PIP_NO_INDEX": "1",
}


_API_ERRORS = (docker.errors.APIError, requests.exceptions.RequestException)


class SandboxError(RuntimeError):
    """The sandbox could not be set up, or its output is malformed."""


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 600.0
    pids: int = 256
    cpus: float = 2.0
    memory: str = "2g"
    tmpfs_size: str = "512m"
    repo_size: str = "1g"
    out_size: str = "64m"


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
class AuditEvent:
    kind: str  # "log", "block" or "truncated"
    event: str  # the Python audit event, e.g. "socket.connect" or "open:write"
    detail: str
    origin: str = ""  # file:line of the code that triggered it

    def __str__(self) -> str:
        where = f" (at {self.origin})" if self.origin else ""
        return f"{self.kind} {self.event} {self.detail}{where}".strip()


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None  # None when the container was killed at the timeout
    output: str  # combined stdout and stderr, truncated to the last MAX_OUTPUT_CHARS
    duration_s: float
    timed_out: bool
    oom_killed: bool
    audit: tuple[AuditEvent, ...] = ()
    flooded: bool = False  # the console output passed MAX_LOG_BYTES and the run was killed
    completed: bool = False  # the supervisor's authenticated done line arrived
    audit_overflow: bool = False  # more audit lines than the supervisor relays
    refused: tuple[str, ...] = ()  # result files the supervisor refused (link, oversized)

    @property
    def blocked(self) -> tuple[AuditEvent, ...]:
        return tuple(e for e in self.audit if e.kind == "block")


@dataclass(frozen=True)
class FailureInfo:
    type: str
    message: str
    frames: tuple[tuple[str, str, int], ...]  # (file, function, line), innermost last
    # Per frame, truncated reprs of local variables (only with --tracerepro-locals, repo frames).
    locals: tuple[dict[str, str], ...] = ()

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
    subtest: bool = False  # one subtest of the test (same node id), not the test itself


@dataclass(frozen=True)
class PytestRun:
    run: RunResult
    exitstatus: int | None
    tests: tuple[CaseResult, ...] = field(default=())
    junit_path: Path | None = None
    results_error: str | None = None  # the results file existed but was refused

    def outcomes(self) -> dict[str, str]:
        """Node id -> worst outcome across its phases (error > failed > others)."""
        rank = {"error": 2, "failed": 1}
        result: dict[str, str] = {}
        for t in self.tests:
            current = result.get(t.nodeid)
            if current is None or rank.get(t.outcome, 0) > rank.get(current, 0):
                result[t.nodeid] = t.outcome
        return result

    def failures(self) -> dict[str, FailureInfo]:
        """Node id -> the first recorded failure (call phase preferred) for tests that raised."""
        found: dict[str, FailureInfo] = {}
        for t in sorted(self.tests, key=lambda t: t.when != "call"):
            if t.failure is not None and t.nodeid not in found:
                found[t.nodeid] = t.failure
        return found

    def collection_errors(self) -> dict[str, str]:
        return {t.nodeid: t.longrepr or "" for t in self.tests if t.when == "collect"}


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


class _LogReader:
    """Streams a container's log on a thread: keeps the tail for display, takes the
    supervisor's control lines (only those carrying this run's secret), and gives up past
    MAX_LOG_BYTES."""

    def __init__(self, container: Container, nonce: str) -> None:
        self.container = container
        self.nonce = nonce
        self.tail = ""
        self.audit: list[AuditEvent] = []
        self.files: dict[str, bytearray] = {}
        self.refused: list[str] = []
        self.done_code: int | None = None
        self.overflow = False
        self.flooded = False
        self.total = 0
        self.finished = threading.Event()  # the stream ended, or a flood
        self._partial = ""
        self._control = re.compile(rf"^TRACEREPRO-([A-Z]+) {re.escape(nonce)}(?: (.*))?$")
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            for chunk in self.container.logs(stream=True, follow=True):
                self.total += len(chunk)
                if self.total > MAX_LOG_BYTES:
                    self.flooded = True
                    return
                text = chunk.decode("utf-8", errors="replace")
                lines = (self._partial + text).split("\n")
                self._partial = lines.pop()[-MAX_OUTPUT_CHARS:]
                shown = [ln for ln in lines if not self._take(ln)]
                if shown:
                    self.tail = (self.tail + "\n".join(shown) + "\n")[-MAX_OUTPUT_CHARS:]
        except _API_ERRORS:
            return
        finally:
            if self._partial and not self._take(self._partial):
                self.tail = (self.tail + self._partial)[-MAX_OUTPUT_CHARS:]
            self.finished.set()

    def _take(self, line: str) -> bool:
        """Handle an authenticated control line; False for ordinary output."""
        match = self._control.match(line.rstrip("\r"))
        if match is None:
            return False
        kind, rest = match[1], match[2] or ""
        if kind == "AUDIT" and len(self.audit) < MAX_AUDIT_EVENTS:
            self.audit.extend(_audit_event(rest))
        elif kind == "OVERFLOW":
            self.overflow = True
        elif kind == "REFUSED":
            self.refused.append(rest[:100])
        elif kind == "OUT":
            name, _, encoded = rest.partition(" ")
            if name in RESULT_FILES:
                data = self.files.setdefault(name, bytearray())
                with contextlib.suppress(ValueError):
                    data.extend(base64.b64decode(encoded, validate=True))
        elif kind == "DONE" and re.fullmatch(r"[0-9]{1,3}", rest):
            self.done_code = int(rest)
        return True


def _audit_event(text: str) -> tuple[AuditEvent, ...]:
    """One audit event from its JSON text, or nothing if it is malformed (it is untrusted)."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return ()
    if not isinstance(data, dict):
        return ()
    fields = [data.get(k) for k in ("kind", "event", "detail", "origin")]
    strings = [f[:500] for f in fields if isinstance(f, str)]
    if len(strings) != len(fields) or strings[0] not in ("log", "block", "truncated"):
        return ()
    return (AuditEvent(*strings),)


def parse_audit(text: str) -> tuple[AuditEvent, ...]:
    """Audit lines (`TRACEREPRO-AUDIT {json}`) from plain text, as the plugin writes them
    outside the sandbox. In the sandbox only the supervisor's authenticated lines count."""
    events: list[AuditEvent] = []
    for line in text.splitlines():
        start = line.find(AUDIT_PREFIX)
        if start >= 0:
            events.extend(_audit_event(line[start + len(AUDIT_PREFIX) :]))
            if len(events) >= MAX_AUDIT_EVENTS:
                break
    return tuple(events)


SANDBOX_LABEL = "org.tracerepro.sandbox"


def remove_leftover_sandboxes(client: docker.DockerClient | None = None) -> int:
    """Remove sandbox containers left behind when a run was killed mid-way (its host process
    never reached the cleanup). Only containers carrying the sandbox label are touched."""
    client = client or docker.from_env()
    found = client.containers.list(all=True, filters={"label": f"{SANDBOX_LABEL}=1"})
    for container in found:
        with contextlib.suppress(*_API_ERRORS):
            container.remove(force=True)
    return len(found)


def run_sandboxed(
    image: str,
    command: Sequence[str],
    ws: Workspace,
    *,
    limits: Limits = DEFAULT_LIMITS,
    env: dict[str, str] | None = None,
    client: docker.DockerClient | None = None,
) -> RunResult:
    """Run `command` (an argv list, never a shell string) in a locked-down container, under the
    sandbox supervisor (see `plugin/tracerepro_supervisor.py`)."""
    if isinstance(command, str):
        raise TypeError("command must be a sequence of arguments, not a string")
    client = client or docker.from_env()
    user = _container_user()
    uid, gid = user.split(":")
    owner = f"uid={uid},gid={gid}"
    nonce = secrets.token_hex(24)
    container = client.containers.create(
        image,
        entrypoint=["python", "-I", "-S", f"{PLUGIN_MOUNT}/{SUPERVISOR}"],
        command=list(command),
        working_dir=REPO_MOUNT,
        user=user,
        environment={**BASE_ENV, **(env or {}), "TRACEREPRO_NONCE": nonce},
        labels={SANDBOX_LABEL: "1"},
        network_mode="none",
        read_only=True,
        tmpfs={
            "/tmp": f"rw,nosuid,nodev,size={limits.tmpfs_size}",
            REPO_MOUNT: f"rw,nosuid,nodev,size={limits.repo_size},{owner},mode=0755",
            OUT_MOUNT: f"rw,nosuid,nodev,size={limits.out_size},{owner},mode=0755",
        },
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        pids_limit=limits.pids,
        nano_cpus=int(limits.cpus * 1e9),
        mem_limit=limits.memory,
        memswap_limit=limits.memory,  # no swap on top of the memory limit
        log_config=LogConfig(
            type=LogConfig.types.JSON, config={"max-size": "100m", "max-file": "1"}
        ),
        volumes={
            str(ws.repo.resolve()): {"bind": SRC_MOUNT, "mode": "ro"},
            str(PLUGIN_DIR): {"bind": PLUGIN_MOUNT, "mode": "ro"},
        },
    )
    timed_out = False
    exit_code: int | None = None
    start = time.monotonic()
    try:
        container.start()
        reader = _LogReader(container, nonce)
        if not reader.finished.wait(timeout=limits.timeout_s):
            timed_out = True
        elif not reader.flooded:
            # Process 1 is the supervisor, which code inside cannot kill: its exit status is
            # the command's.
            with contextlib.suppress(*_API_ERRORS):
                exit_code = int(container.wait(timeout=30)["StatusCode"])
        duration = time.monotonic() - start
        with contextlib.suppress(*_API_ERRORS):
            container.reload()
        oom_killed = bool(container.attrs["State"].get("OOMKilled"))
    finally:
        with contextlib.suppress(*_API_ERRORS):
            container.remove(force=True)
    completed = reader.done_code is not None and reader.done_code == exit_code
    if completed and not reader.flooded:
        ws.out.mkdir(parents=True, exist_ok=True)
        for name, data in reader.files.items():
            (ws.out / name).write_bytes(bytes(data))
    return RunResult(
        exit_code=None if timed_out or reader.flooded else exit_code,
        output=reader.tail,
        duration_s=duration,
        timed_out=timed_out,
        oom_killed=oom_killed,
        audit=tuple(reader.audit),
        flooded=reader.flooded,
        completed=completed,
        audit_overflow=reader.overflow,
        refused=tuple(reader.refused),
    )


# pytest's unraisable-exception plugin turns warnings raised when Python garbage-collects an
# object (an unclosed file, say) into a failure of whichever test happens to be running then.
# A crashing oracle test can leak such objects (click-3739's bug leaves temp files open), so an
# unrelated baseline test would fail at random. Every pytest run in the harness switches it off.
NO_UNRAISABLE = ("-p", "no:unraisableexception")


def pytest_command(test_ids: Sequence[str], *, extra_args: Sequence[str] = ()) -> list[str]:
    """argv for a pytest run that writes JUnit XML and plugin JSON to /out."""
    return [
        "python",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *NO_UNRAISABLE,
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
    results = ws.out / RESULTS_FILE
    junit = ws.out / JUNIT_FILE
    for stale in (results, junit):
        stale.unlink(missing_ok=True)
    run = run_sandboxed(
        image, pytest_command(test_ids, extra_args=extra_args), ws, limits=limits, client=client
    )
    exitstatus: int | None = None
    tests: tuple[CaseResult, ...] = ()
    error = None
    if results.is_file() or results.is_symlink():
        try:
            exitstatus, tests = parse_results(results)
        except (SandboxError, OSError) as exc:
            error = str(exc)
    return PytestRun(
        run=run,
        exitstatus=exitstatus,
        tests=tests,
        junit_path=junit if junit.is_file() else None,
        results_error=error,
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
    frame_vars: list[dict[str, str]] = []
    for f in obj["frames"]:
        if not isinstance(f, dict) or not isinstance(f.get("line"), int):
            raise SandboxError("results: malformed frame")
        frames.append((_str(f, "file"), _str(f, "function"), int(f["line"])))
        values = f.get("vars", {})
        if not isinstance(values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in values.items()
        ):
            raise SandboxError("results: malformed frame vars")
        frame_vars.append(values)
    return FailureInfo(_str(obj, "type"), _str(obj, "message"), tuple(frames), tuple(frame_vars))


def parse_results(path: Path) -> tuple[int | None, tuple[CaseResult, ...]]:
    """Read the plugin's JSON. It was written inside the sandbox, so treat it as untrusted."""
    if path.is_symlink() or path.stat().st_size > MAX_RESULTS_BYTES:
        raise SandboxError(f"{path}: refusing symlinked or oversized results file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as exc:  # bad UTF-8 or JSON, huge integers
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
                subtest=t.get("subtest") is True,
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
