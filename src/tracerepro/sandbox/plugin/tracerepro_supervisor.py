"""Sandbox supervisor: process 1 inside every sandbox container (Amendment 4, red-team round 2).

The host starts it with `python -I -S` (no site, no PYTHONPATH, no current directory on the
path, so nothing in the workspace can be imported into it) and a per-run secret in the
environment variable TRACEREPRO_NONCE. It:

1. marks itself non-dumpable, so code running as the same user cannot read its memory or its
   `/proc/1/environ`, and drops the secret from the environment its children get;
2. copies the read-only `/src` into the size-capped in-memory `/repo`;
3. runs the command with an extra pipe whose number is in TRACEREPRO_AUDIT_FD, and relays every
   line the harness plugin writes there as `TRACEREPRO-AUDIT <nonce> <line>`;
4. when the command ends, kills every process left behind (code under test may leave threads
   or children running), then sends the allow-listed result files as
   `TRACEREPRO-OUT <nonce> <name> <base64>` lines and `TRACEREPRO-DONE <nonce> <exit code>`;
5. exits with the command's exit code. As process 1 of the container it cannot be killed from
   inside, so `docker wait` reports that code faithfully.

Lines without the nonce are ordinary output: code under test can print anything it likes, but
cannot forge a control line. Stdlib only; runs on Python 3.8+.
"""

import base64
import contextlib
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import threading

PR_SET_DUMPABLE = 4
SRC, REPO, OUT = "/src", "/repo", "/out"
RESULT_FILES = ("results.json", "junit.xml")
MAX_RESULT_BYTES = 20_000_000
MAX_AUDIT_LINES = 2000
MAX_AUDIT_LINE = 3000
CHUNK = 48_000  # bytes of file per OUT line (64 KB of base64)


def _write(line: str) -> None:
    # A leading newline: a partial line left by the code under test cannot swallow ours.
    data = ("\n" + line + "\n").encode("ascii", "replace")
    while data:
        data = data[os.write(1, data) :]


def _relay(fd: int, nonce: str, done: threading.Event) -> None:
    count = 0
    with os.fdopen(fd, "rb", buffering=0) as pipe:
        buffered = b""
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            buffered += chunk
            *lines, buffered = buffered.split(b"\n")
            buffered = buffered[-MAX_AUDIT_LINE:]
            for raw in lines:
                count += 1
                if count == MAX_AUDIT_LINES + 1:
                    _write(f"TRACEREPRO-OVERFLOW {nonce}")
                if count > MAX_AUDIT_LINES:
                    continue
                text = raw[:MAX_AUDIT_LINE].decode("ascii", "replace")
                _write(f"TRACEREPRO-AUDIT {nonce} {text}")
    done.set()


def _kill_leftovers() -> None:
    with contextlib.suppress(OSError):
        os.kill(-1, signal.SIGKILL)  # every process we may signal, never process 1 itself
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return


def _send_results(nonce: str) -> None:
    for name in RESULT_FILES:
        path = os.path.join(OUT, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not os.path.isfile(path) or os.path.islink(path) or st.st_size > MAX_RESULT_BYTES:
            _write(f"TRACEREPRO-REFUSED {nonce} {name}")
            continue
        with open(path, "rb") as fh:
            data = fh.read(MAX_RESULT_BYTES + 1)
        if len(data) > MAX_RESULT_BYTES:
            _write(f"TRACEREPRO-REFUSED {nonce} {name}")
            continue
        for start in range(0, max(len(data), 1), CHUNK):
            encoded = base64.b64encode(data[start : start + CHUNK]).decode("ascii")
            _write(f"TRACEREPRO-OUT {nonce} {name} {encoded}")


def main() -> int:
    nonce = os.environ.pop("TRACEREPRO_NONCE", "")
    if len(nonce) < 32:
        return 95
    if ctypes.CDLL(None).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        return 96
    try:
        shutil.copytree(SRC, REPO, symlinks=True, dirs_exist_ok=True)
    except (OSError, shutil.Error):
        return 97
    read_fd, write_fd = os.pipe()
    env = dict(os.environ, TRACEREPRO_AUDIT_FD=str(write_fd))
    relayed = threading.Event()
    threading.Thread(target=_relay, args=(read_fd, nonce, relayed), daemon=True).start()
    try:
        child = subprocess.Popen(
            sys.argv[1:], stdin=subprocess.DEVNULL, pass_fds=(write_fd,), env=env, cwd=REPO
        )
    except OSError:
        return 98
    os.close(write_fd)
    code = child.wait()
    _kill_leftovers()
    relayed.wait(timeout=10)
    _send_results(nonce)
    code = code if 0 <= code <= 255 else 128 + (-code if code < 0 else 0) % 128
    _write(f"TRACEREPRO-DONE {nonce} {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
