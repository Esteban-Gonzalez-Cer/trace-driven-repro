"""Read a few named settings from the git-ignored `.env` file, without touching `os.environ`.

The demo's secrets (the webhook signing secret and the GitHub token) are read into the objects
that use them and nowhere else. They never enter the process environment, so no child process
(the Claude CLI, Docker, git) can inherit them, whatever its own environment rules.
"""

from __future__ import annotations

from pathlib import Path

from tracerepro.dataset.manifest import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / ".env"


class SettingMissing(RuntimeError):
    """A required setting is not in the `.env` file."""


def read_env_file(keys: set[str], path: Path = ENV_FILE) -> dict[str, str]:
    """The values of `keys` that `path` sets (`KEY=value` lines; `#` comments; optional quotes).
    Other lines are ignored and never returned."""
    found: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return found
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key not in keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            found[key] = value
    return found


def require(values: dict[str, str], key: str, *, min_length: int = 1) -> str:
    value = values.get(key, "")
    if len(value) < min_length:
        need = f" (at least {min_length} characters)" if min_length > 1 else ""
        raise SettingMissing(f"set {key} in .env{need}; see .env.example")
    return value
