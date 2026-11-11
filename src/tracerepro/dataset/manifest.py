"""Bug manifests: one YAML file per bug (spec §5.5, plus Amendment 1 fields).

Fields written by preflight automation (oracle tests, baseline sets, gold files) are optional here
so the image builder can run before preflight. Completeness is checked where it matters.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PYTHON_RE = re.compile(r"^3\.\d{1,2}(\.\d{1,2})?$")
_GITHUB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
LOCAL_PREFIX = "local:"

REQUIRED = ("id", "repo", "c_bug", "c_fix", "python_version", "image_tag")
OPTIONAL = (
    "issue_url",
    "fix_date",
    "target_exception",
    "target_frame",
    "crash_kind",
    "payload_path",
    "oracle_tests",
    "excluded_fix_tests",
    "baseline_pass_set",
    "flaky_set",
    "gold_files",
    "gold_files_full",
    "deps_lock",
    "notes",
)


class ManifestError(ValueError):
    """A manifest file is malformed or inconsistent."""


@dataclass(frozen=True)
class TargetFrame:
    file: str
    function: str


@dataclass(frozen=True)
class Manifest:
    id: str
    repo: str
    c_bug: str
    c_fix: str
    python_version: str
    image_tag: str
    issue_url: str | None = None
    fix_date: str | None = None
    target_exception: str | None = None
    target_frame: TargetFrame | None = None
    crash_kind: str | None = None  # python_failure or library_raise (DECISIONS.md)
    payload_path: str | None = None
    oracle_tests: tuple[str, ...] = ()
    excluded_fix_tests: tuple[dict[str, str], ...] = ()
    baseline_pass_set: tuple[str, ...] = ()
    flaky_set: tuple[str, ...] = ()
    gold_files: tuple[str, ...] = ()
    gold_files_full: tuple[str, ...] = ()
    deps_lock: str | None = None
    notes: str = ""
    source_path: Path | None = field(default=None, compare=False)

    @property
    def base_image(self) -> str:
        return f"python:{self.python_version}-slim"

    def repo_location(self, root: Path = PROJECT_ROOT) -> str:
        """Clone source: a GitHub URL, or an absolute local path for `local:` repos."""
        if self.repo.startswith(LOCAL_PREFIX):
            return str((root / self.repo.removeprefix(LOCAL_PREFIX)).resolve())
        return f"https://github.com/{self.repo}.git"

    def deps_lock_path(self, root: Path = PROJECT_ROOT) -> Path | None:
        return None if self.deps_lock is None else (root / self.deps_lock).resolve()


def _str_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{key} must be a list of strings")
    return tuple(value)


def parse_manifest(data: Any, source_path: Path | None = None) -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a mapping")
    unknown = set(data) - set(REQUIRED) - set(OPTIONAL)
    if unknown:
        raise ManifestError(f"unknown fields: {sorted(unknown)}")
    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        raise ManifestError(f"missing required fields: {missing}")
    for key in REQUIRED:
        if not isinstance(data[key], str):
            raise ManifestError(f"{key} must be a string")

    bug_id = data["id"]
    if not _ID_RE.match(bug_id):
        raise ManifestError(f"invalid id {bug_id!r}")
    repo = data["repo"]
    if not (repo.startswith(LOCAL_PREFIX) or _GITHUB_RE.match(repo)):
        raise ManifestError(f"repo must be 'owner/name' or 'local:<path>', got {repo!r}")
    for key in ("c_bug", "c_fix"):
        if not _SHA_RE.match(data[key]):
            raise ManifestError(f"{key} must be a full 40-character commit hash")
    if data["c_bug"] == data["c_fix"]:
        raise ManifestError("c_bug and c_fix must differ")
    if not _PYTHON_RE.match(data["python_version"]):
        raise ManifestError(f"invalid python_version {data['python_version']!r}")
    if data["image_tag"] != f"bug_env:{bug_id}":
        raise ManifestError(f"image_tag must be 'bug_env:{bug_id}'")

    frame = data.get("target_frame")
    target_frame = None
    if frame is not None:
        if not isinstance(frame, dict) or set(frame) != {"file", "function"}:
            raise ManifestError("target_frame must have exactly 'file' and 'function'")
        target_frame = TargetFrame(str(frame["file"]), str(frame["function"]))

    excluded = data.get("excluded_fix_tests") or []
    if not isinstance(excluded, list) or not all(
        isinstance(e, dict) and set(e) == {"id", "reason"} for e in excluded
    ):
        raise ManifestError("excluded_fix_tests entries must have exactly 'id' and 'reason'")

    kind = data.get("crash_kind")
    if kind is not None and kind not in ("python_failure", "library_raise"):
        raise ManifestError("crash_kind must be 'python_failure' or 'library_raise'")

    fix_date = data.get("fix_date")
    if isinstance(fix_date, dt.date):
        fix_date = fix_date.isoformat()

    deps_lock = data.get("deps_lock")
    if deps_lock is not None and (not isinstance(deps_lock, str) or Path(deps_lock).is_absolute()):
        raise ManifestError("deps_lock must be a path relative to the project root")

    return Manifest(
        id=bug_id,
        repo=repo,
        c_bug=data["c_bug"],
        c_fix=data["c_fix"],
        python_version=data["python_version"],
        image_tag=data["image_tag"],
        issue_url=data.get("issue_url"),
        fix_date=fix_date,
        target_exception=data.get("target_exception"),
        target_frame=target_frame,
        crash_kind=kind,
        payload_path=data.get("payload_path"),
        oracle_tests=_str_list(data, "oracle_tests"),
        excluded_fix_tests=tuple(
            {"id": str(e["id"]), "reason": str(e["reason"])} for e in excluded
        ),
        baseline_pass_set=_str_list(data, "baseline_pass_set"),
        flaky_set=_str_list(data, "flaky_set"),
        gold_files=_str_list(data, "gold_files"),
        gold_files_full=_str_list(data, "gold_files_full"),
        deps_lock=deps_lock,
        notes=str(data.get("notes") or "").strip(),
        source_path=source_path,
    )


def load_manifest(path: Path) -> Manifest:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return parse_manifest(data, source_path=path)
    except ManifestError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def manifest_to_dict(m: Manifest) -> dict[str, Any]:
    """Plain data in field order, ready for YAML. Inverse of `parse_manifest`."""
    data: dict[str, Any] = {}
    for key in (*REQUIRED[:4], *OPTIONAL, *REQUIRED[4:]):
        value = getattr(m, key)
        if isinstance(value, TargetFrame):
            value = {"file": value.file, "function": value.function}
        elif isinstance(value, tuple):
            value = [dict(v) if isinstance(v, dict) else v for v in value]
        data[key] = value
    return data


def dump_manifest(m: Manifest, header: str = "") -> str:
    """Deterministic YAML for a manifest. The same manifest always gives the same bytes."""
    body = yaml.safe_dump(
        manifest_to_dict(m),
        sort_keys=False,
        default_flow_style=False,
        width=100,
        allow_unicode=True,
    )
    comments = "".join(f"# {line}\n" if line else "#\n" for line in header.splitlines())
    return comments + body
