"""Harness-owned pytest plugin, mounted read-only into the sandbox (plan §5.2).

Loaded with `-p tracerepro_pytest_plugin --tracerepro-json=PATH`. For every test it records the
pytest node id and outcome. For every failure or error it also records the exception type,
message, and traceback frames (innermost last). The repro gate and the oracle checks read this
file, so they never have to parse console text or map JUnit names back to node ids.

It also installs a Python audit hook (Amendment 4, layer 3) as soon as pytest imports it, which
is before conftest files and the library under test are imported. Python offers no way to remove
an audit hook. The hook:

- **blocks** any access to `/out` (where results are written) except by this plugin's own writer
  and pytest's JUnit writer, and blocks `gc.get_objects`/`get_referrers`/`get_referents` and
  `__code__` replacement unless only test files, the stdlib and installed packages are acting
  (the usual ways to reach and rewrite the recorder's state);
- **logs** network, process, ctypes and sensitive-file events (including `/proc/*/environ`),
  writes outside `/repo`, `/tmp` and `/out`, and library code listing the test directories.

Each event is written at once as one JSON line to the pipe the sandbox supervisor passes in
(TRACEREPRO_AUDIT_FD); the supervisor relays it to the container log with a per-run secret, so
printing cannot forge an event, and the Docker daemon keeps the log, so code that runs later
cannot take a line back. Outside the sandbox the lines go to a copy of stderr taken at import.

This file runs inside bug images, under whatever Python the bug needs. It must stay
stdlib-only and importable on Python 3.8+.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import reprlib
import sys
import sysconfig
import tempfile
import threading
import traceback
from collections.abc import Callable
from typing import Any

import pytest

SCHEMA_VERSION = 1
MAX_FRAMES = 40
MAX_MESSAGE = 2000
MAX_LONGREPR = 8000
REPO_ROOT = "/repo/"
MAX_LOCALS = 25

_repr = reprlib.Repr()
_repr.maxlevel = 3
_repr.maxstring = 160
_repr.maxother = 160
_repr.maxlist = _repr.maxtuple = _repr.maxset = _repr.maxdict = 8

_records: list[dict[str, Any]] = []

# Taken before any library code runs, so later monkeypatching of these names cannot reach the
# results writer or the audit log.
_open = io.open
_replace = os.replace
_dumps = json.dumps
_fd_write = os.write
_realpath = os.path.realpath
_getframe = sys._getframe

# --- audit hook (Amendment 4, layer 3) -------------------------------------------------------

AUDIT_PREFIX = "TRACEREPRO-AUDIT "
MAX_AUDIT_LINES = 200
OUT_DIR = "/out"
# Writes here are expected; the platform temp dir is /tmp in bug images (it differs on a host).
WRITE_ROOTS = ("/repo/", "/tmp/", "/dev/", os.path.realpath(tempfile.gettempdir()) + "/")
SENSITIVE_PARTS = (
    "/.ssh/", "/.aws/", "/.netrc", "/.pypirc", "/.git-credentials", "/.docker/", "docker.sock",
    "/run/secrets", "/var/run/secrets",
)  # fmt: skip
SENSITIVE_PREFIXES = ("/etc/passwd", "/etc/shadow", "/etc/sudoers", "/root/", "/home/")
# Process information that can hold secrets (environment, command line, memory).
PROC_SENSITIVE = ("environ", "cmdline", "mem", "maps", "auxv", "stack", "syscall")
TEST_DIR_NAMES = ("tests", "test", "testing")
# Event -> positions of path arguments. All but listing and `open` (checked by mode) are writes.
PATH_EVENTS = {
    "open": (0,), "os.rename": (0, 1), "os.remove": (0,), "os.rmdir": (0,), "os.truncate": (0,),
    "os.chmod": (0,), "os.chown": (0,), "os.symlink": (0, 1), "os.link": (0, 1),
    "os.utime": (0,), "os.listdir": (0,), "os.scandir": (0,), "shutil.rmtree": (0,),
    "shutil.move": (0, 1), "shutil.copyfile": (0, 1), "os.chdir": (0,),
}  # fmt: skip
READ_EVENTS = ("os.listdir", "os.scandir", "os.chdir")
# Frames from these directories (stdlib, installed packages) are skipped when naming the code
# that triggered an event, so the origin is the library or test line rather than socket.py.
_LIB_DIRS = tuple(
    {os.path.realpath(sysconfig.get_paths()[k]) + "/" for k in ("stdlib", "platstdlib",
                                                                 "purelib", "platlib")}
)  # fmt: skip
GC_EVENTS = ("gc.get_objects", "gc.get_referrers", "gc.get_referents")
TEST_FILE_PARTS = ("/tests/", "/test/", "/testing/", "/conftest.py")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _detail_first(args: tuple[Any, ...]) -> str:
    return str(args[0])[:200] if args else ""


def _detail_address(args: tuple[Any, ...]) -> str:
    return str(args[1])[:200] if len(args) > 1 else ""


def _detail_argv(args: tuple[Any, ...]) -> str:
    return str(args[1])[:200] if len(args) > 1 else _detail_first(args)


# Event -> how to describe it. Logged wherever they come from (tests included); the host compares
# them with the gold patch's run.
LOGGED: dict[str, Callable[[tuple[Any, ...]], str]] = {
    "socket.connect": _detail_address,
    "socket.bind": _detail_address,
    "socket.sendto": _detail_address,
    "socket.getaddrinfo": _detail_first,
    "socket.gethostbyname": _detail_first,
    "http.client.connect": _detail_address,
    "urllib.Request": _detail_first,
    "ftplib.connect": _detail_address,
    "smtplib.connect": _detail_address,
    "webbrowser.open": _detail_first,
    "subprocess.Popen": _detail_argv,
    "os.system": _detail_first,
    "os.exec": _detail_first,
    "os.posix_spawn": _detail_first,
    "os.spawn": _detail_argv,
    "os.fork": lambda args: "",
    "os.forkpty": lambda args: "",
    "os.kill": _detail_address,
    "sys.addaudithook": lambda args: "",
    "ctypes.dlopen": _detail_first,
    "ctypes.dlsym": lambda args: "",
    "ctypes.addressof": lambda args: "",
    "ctypes.string_at": lambda args: "",
    "ctypes.call_function": lambda args: "",
}


def _as_path(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str):
        return None  # file descriptors and path-like objects whose __fspath__ we must not call
    try:
        # Against the *current* directory (red-team round 2: `os.chdir("/")` then "out/x" got
        # past a check that assumed /repo), resolving symlinks and /proc/self/fd/N.
        return _realpath(value)
    except (OSError, ValueError):
        return None


def _is_write(event: str, args: tuple[Any, ...]) -> bool:
    if event in READ_EVENTS:
        return False
    if event != "open":
        return True
    mode = args[1] if len(args) > 1 else None
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    flags = args[2] if len(args) > 2 else 0
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _caller() -> Any:
    """The Python frame that triggered the event: 0 is the hook, 1 is its caller."""
    try:
        return _getframe(2)
    except ValueError:
        return None


def _origin(frame: Any) -> str:
    """file:line of the innermost frame outside the stdlib and installed packages."""
    first = frame
    while frame is not None:
        filename = frame.f_code.co_filename
        if not filename.startswith(_LIB_DIRS) and not filename.startswith("<"):
            return f"{_path(filename)}:{frame.f_lineno}"
        frame = frame.f_back
    return f"{_path(first.f_code.co_filename)}:{first.f_lineno}" if first is not None else ""


def _is_test_file(filename: str) -> bool:
    name = os.path.basename(filename)
    return (
        any(p in filename for p in TEST_FILE_PARTS)
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _actor(frame: Any) -> str:
    """Who is acting: "harness" (only stdlib and installed packages on the stack, e.g. pytest),
    "test" (the innermost other frame is a test file) or "code" (anything else: the library,
    or code compiled at runtime, such as `exec(compile(src, "<x>", "exec"))`).

    Judged by the innermost frame outside the stdlib and installed packages, so calling through
    a stdlib helper does not hide the caller, and a made-up filename does not look trusted."""
    while frame is not None:
        filename = frame.f_code.co_filename
        if not (filename.startswith(_LIB_DIRS) or filename.startswith("<frozen")):
            return "test" if _is_test_file(filename) else "code"
        frame = frame.f_back
    # Only stdlib and installed packages on the stack. pytest runs in the main thread; a worker
    # thread with no frame of its own was started by code under test (red-team round 2:
    # `Thread(target=gc.get_objects)`), so it acts for that code.
    if threading.current_thread() is not threading.main_thread():
        return "code"
    return "harness"


def _install_audit_hook() -> Callable[[Any], None]:
    """Install the hook. Returns `trust(code)`, used once to register the results writer."""
    # In the sandbox the supervisor passes a pipe and relays its lines with a per-run secret,
    # so code under test cannot forge them by printing. Elsewhere (host tests), a copy of stderr.
    passed_fd = os.environ.pop("TRACEREPRO_AUDIT_FD", "")
    audit_fd = int(passed_fd) if passed_fd.isdigit() else os.dup(2)
    prefix = "" if passed_fd.isdigit() else AUDIT_PREFIX
    trusted_codes: set[Any] = set()
    seen: set[tuple[str, str, str]] = set()
    count = [0]

    def emit(kind: str, event: str, detail: str, origin: str) -> None:
        key = (kind, event, detail)
        if key in seen:
            return
        seen.add(key)
        count[0] += 1
        if count[0] > MAX_AUDIT_LINES + 1:
            return
        if count[0] == MAX_AUDIT_LINES + 1:
            kind, event, detail, origin = "truncated", "", "", ""
        record = {"kind": kind, "event": event, "detail": detail, "origin": origin}
        line = prefix + _dumps(record, ensure_ascii=True) + "\n"
        with contextlib.suppress(OSError):
            _fd_write(audit_fd, line.encode("ascii"))

    def trusted(frame: Any) -> bool:
        # By code object identity, never by module name or file name: both can be forged
        # (red-team round 2 set a module's __name__ to "_pytest.junitxml").
        return frame is not None and frame.f_code in trusted_codes

    def hook(event: str, args: tuple[Any, ...]) -> None:
        denied = ""
        try:
            if event in PATH_EVENTS:
                paths = [_as_path(args[i]) for i in PATH_EVENTS[event] if i < len(args)]
                for path in paths:
                    if path is None:
                        continue
                    if path == OUT_DIR or path.startswith(OUT_DIR + "/"):
                        frame = _caller()
                        if not trusted(frame):
                            denied = path
                            emit("block", event, path, _origin(frame))
                            break
                    elif _is_write(event, args) and not (path + "/").startswith(WRITE_ROOTS):
                        emit("log", f"{event}:write", path, _origin(_caller()))
                    elif (
                        path.startswith(SENSITIVE_PREFIXES)
                        or any(p in path for p in SENSITIVE_PARTS)
                        or (path.startswith("/proc/")
                            and os.path.basename(path) in PROC_SENSITIVE)
                    ):  # fmt: skip
                        emit("log", f"{event}:sensitive", path, _origin(_caller()))
                    elif (
                        event in READ_EVENTS
                        and (os.path.basename(path) in TEST_DIR_NAMES
                             or any(f"/{d}/" in path for d in TEST_DIR_NAMES))
                        and _actor(_caller()) == "code"
                    ):  # fmt: skip
                        # Library code looking at the test files: how the red team found the
                        # canary. pytest's own collection is "harness" and is not logged.
                        emit("log", f"{event}:tests", path, _origin(_caller()))
            elif event in LOGGED:
                emit("log", event, LOGGED[event](args), _origin(_caller()))
            elif event in GC_EVENTS or (
                event == "object.__setattr__" and len(args) > 1 and args[1] == "__code__"
            ):
                frame = _caller()
                if _actor(frame) == "code":
                    denied = event
                    emit("block", event, "", _origin(frame))
        except Exception:
            return  # never break the code under test because of a bug in this hook
        if denied:
            raise PermissionError(f"tracerepro sandbox: {event} on {denied} is not allowed")

    trusted_codes.update(_junitxml_codes())
    sys.addaudithook(hook)
    return trusted_codes.add


def _junitxml_codes() -> set[Any]:
    """The code objects of pytest's JUnit writer, taken before any code under test runs."""
    try:
        from _pytest import junitxml
    except ImportError:
        return set()
    codes: set[Any] = set()
    for value in vars(junitxml).values():
        members = [value, *vars(value).values()] if isinstance(value, type) else [value]
        for member in members:
            code = getattr(member, "__code__", None)
            if code is not None:
                codes.add(code)
    return codes


_trust = _install_audit_hook()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--tracerepro-json", default=None, help="write per-test results to PATH")
    parser.addoption(
        "--tracerepro-locals",
        action="store_true",
        default=False,
        help="also record truncated reprs of local variables for repo frames of each failure",
    )


def _path(filename: str) -> str:
    return filename[len(REPO_ROOT) :] if filename.startswith(REPO_ROOT) else filename


def _type_name(exc_type: type) -> str:
    module = exc_type.__module__
    if module in ("builtins", "__main__"):
        return exc_type.__qualname__
    return f"{module}.{exc_type.__qualname__}"


def _safe_repr(value: object) -> str:
    try:
        return _repr.repr(value)
    except Exception:
        return "<unrepresentable>"


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return "<unprintable>"


def _locals(frame: Any) -> dict[str, str]:
    names = [n for n in frame.f_locals if not n.startswith("__")][:MAX_LOCALS]
    return {n: _safe_repr(frame.f_locals[n]) for n in names}


def _failure(excinfo: pytest.ExceptionInfo[BaseException], with_locals: bool) -> dict[str, Any]:
    walked = list(traceback.walk_tb(excinfo.tb))[-MAX_FRAMES:]
    summary = traceback.StackSummary.extract(iter(walked), lookup_lines=False)
    frames = []
    for (frame, _), f in zip(walked, summary):  # noqa: B905 (strict= needs 3.10; plugin is 3.8+)
        record: dict[str, Any] = {"file": _path(f.filename), "function": f.name, "line": f.lineno}
        if with_locals and f.filename.startswith(REPO_ROOT):
            record["vars"] = _locals(frame)
        frames.append(record)
    return {
        "type": _type_name(excinfo.type),
        "message": _safe_str(excinfo.value)[:MAX_MESSAGE],
        "frames": frames,
    }


def _outcome(report: pytest.TestReport) -> str:
    if hasattr(report, "wasxfail"):
        return "xfailed" if report.skipped else "xpassed"
    if report.when != "call" and report.failed:
        return "error"
    return report.outcome


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    # One record per phase that matters: the call, plus setup/teardown only when they fail or skip.
    if report.when != "call" and report.passed:
        return
    record: dict[str, Any] = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": _outcome(report),
        "duration": round(report.duration, 6),
    }
    if report.failed and call.excinfo is not None:
        record["failure"] = _failure(call.excinfo, item.config.getoption("--tracerepro-locals"))
    _records.append(record)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # Subtests (pytest 9's built-in `subtests`, or the pytest-subtests plugin) reuse the test's
    # node id and go through makereport like the test itself; only the logged report says which
    # it is. Mark the record makereport just added, so the host can count like pytest does.
    if type(report).__name__ != "SubtestReport":
        return
    for record in reversed(_records):
        if record["nodeid"] == report.nodeid and record["when"] == "call":
            if not record.get("subtest"):
                record["subtest"] = True
            return


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _records.append(
            {
                "nodeid": report.nodeid,
                "when": "collect",
                "outcome": "error",
                "longrepr": str(report.longrepr)[:MAX_LONGREPR],
            }
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = session.config.getoption("--tracerepro-json")
    if not path:
        return
    data = {
        "schema": SCHEMA_VERSION,
        "exitstatus": int(exitstatus),
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "tests": _records,
    }
    _write_results(path, _dumps(data))


def _write_results(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with _open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    _replace(tmp, path)


_trust(_write_results.__code__)
