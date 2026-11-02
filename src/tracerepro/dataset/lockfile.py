"""Pinned dependency locks for bug images (manifest field `deps_lock`).

A lock is a requirements file in which every requirement is pinned with `==`, as produced by
`uv export --no-hashes` or `uv pip compile --universal`. Anything that could pull in unpinned or
off-index code (options such as `--index-url`, `-e`, `-r`, or direct URL references) is rejected:
the lock is part of the reproducible environment and of the build's attack surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9._,-]+\])?"
    r"==(?P<version>[A-Za-z0-9.+!_-]+)"
    r"(?:\s*;\s*(?P<marker>.+))?$"
)


class LockError(ValueError):
    """A lock file contains an unpinned or disallowed line."""


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    marker: str | None = None

    @property
    def key(self) -> str:
        """Normalized project name (PEP 503)."""
        return normalize_name(self.name)


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> list[Pin]:
    pins: list[Pin] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            raise LockError(f"line {lineno}: options are not allowed in a lock: {line!r}")
        match = _PIN_RE.match(line)
        if match is None:
            raise LockError(f"line {lineno}: not an exact '==' pin: {line!r}")
        pins.append(Pin(match["name"], match["version"], match["marker"]))
    keys = [p.key for p in pins if p.marker is None]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise LockError(f"duplicate unconditional pins: {duplicates}")
    return pins


def load_lock(path: Path) -> list[Pin]:
    try:
        return parse_lock(path.read_text())
    except LockError as exc:
        raise LockError(f"{path}: {exc}") from exc
