"""Synthetic Sentry-style crash payloads (spec §5.6).

Built from the gold oracle test's failure on `c_bug`, recorded by the harness pytest plugin with
`--tracerepro-locals`. The payload is what the agent sees instead of a production crash report.

What is kept, and why:

- Frames from the outermost library frame inward. Test frames and pytest internals are dropped,
  so the held-out oracle test's code never reaches the agent; a production report would show the
  caller's code instead, which here does not exist.
- Source context (5 lines either side) and truncated local reprs for library frames only.
- Memory addresses are masked, so the same bug always yields the same bytes.

Issue text, commit messages and PR descriptions never enter the payload.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tracerepro.sandbox.paths import is_library_frame
from tracerepro.sandbox.runner import FailureInfo

SCHEMA_VERSION = 1
CONTEXT_LINES = 5
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{6,}")


class PayloadError(ValueError):
    """The failure has no library frame, so no payload can describe it."""


def _mask(text: str) -> str:
    return _ADDRESS_RE.sub("0x…", text)


def _context(repo: Path, file: str, line: int) -> dict[str, Any]:
    path = repo / file
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not 1 <= line <= len(lines):
        return {}
    idx = line - 1
    return {
        "pre_context": lines[max(0, idx - CONTEXT_LINES) : idx],
        "context_line": lines[idx],
        "post_context": lines[idx + 1 : idx + 1 + CONTEXT_LINES],
    }


def build_payload(
    bug_id: str, c_bug: str, python_version: str, failure: FailureInfo, repo: Path
) -> dict[str, Any]:
    """Sentry-like event for one failure. `repo` is the c_bug source tree (for context lines)."""
    library = [i for i, (file, _, _) in enumerate(failure.frames) if is_library_frame(file)]
    if not library:
        raise PayloadError(f"{bug_id}: failure has no library frame (Amendment 2)")
    frames = []
    for i in range(library[0], len(failure.frames)):
        file, function, line = failure.frames[i]
        in_app = is_library_frame(file)
        frame: dict[str, Any] = {
            "filename": file,
            "function": function,
            "lineno": line,
            "in_app": in_app,
        }
        if in_app:
            frame.update(_context(repo, file, line))
            values = failure.locals[i] if i < len(failure.locals) else {}
            if values:
                frame["vars"] = {k: _mask(v) for k, v in sorted(values.items())}
        frames.append(frame)

    module, _, name = failure.type.rpartition(".")
    return {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "event_id": hashlib.sha256(f"{bug_id}:{c_bug}".encode()).hexdigest()[:32],
        "platform": "python",
        "level": "error",
        "exception": {
            "values": [
                {
                    "type": name,
                    "module": module or "builtins",
                    "value": _mask(failure.message),
                    "mechanism": {"type": "generic", "handled": False},
                    "stacktrace": {"frames": frames},
                }
            ]
        },
        "contexts": {"runtime": {"name": "CPython", "version": python_version}},
        "tags": {"synthetic": "true"},
    }


def dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
