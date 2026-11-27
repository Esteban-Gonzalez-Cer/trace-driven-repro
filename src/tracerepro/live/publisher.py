"""The live demo's draft-PR publisher (spec §10, development plan §8.3).

It opens one **draft** pull request on the owner's own fork with the patch, the model's crash
check as a regression test, and the model's short explanation of the cause. It talks to GitHub's
REST API with the standard library, and is the only code that holds the token.

Rules, each checked before anything is written:

- **The owner's fork only.** The configured repository (`DEMO_REPO`) must belong to the token's
  user and be a fork, and the pull request goes from a new branch of that fork to a branch of
  the same fork. Its parent (the real project) is never a target, and no other repository is.
- **The code the patch was made for.** The fork's base branch must point at the bug's `c_bug`
  commit, and the patch is applied on the host to the files read from that commit, after the
  harness's diff rules (no workflow, dependency or test-config changes) pass again.
- **Only clean patches.** A run whose submitted patch did not run clean is not published.
- **Nothing the model wrote is read as markup or as a command.** The explanation goes into a
  fenced text block; `@` mentions and `#123` references are broken so no one is notified and no
  issue is linked, in this repository or upstream. The title and commit message are ours.
- **The token stays here.** It is read from `.env` (never `os.environ`), sent only to
  `api.github.com` (redirects are refused, so it cannot follow one elsewhere), and removed
  from every error message.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from tracerepro.agent.pipeline import AgentResult
from tracerepro.dataset.manifest import Manifest
from tracerepro.live.env_file import read_env_file, require
from tracerepro.sandbox.runner import Workspace
from tracerepro.sandbox.validator import ApplyStatus, apply_patch, validate

API = "https://api.github.com"
API_HOST = "api.github.com"
TOKEN_KEY, REPO_KEY, BASE_KEY = "GITHUB_DEMO_TOKEN", "DEMO_REPO", "DEMO_BASE"
DEFAULT_BASE = "demo-base"
CHECK_PATH = "tests/test_tracerepro_regression.py"
BRANCH_PREFIX = "tracerepro/"
EXPLANATION_LIMIT = 1_000
TIMEOUT_S = 30
_REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
_ZWSP = "​"  # zero-width space: breaks a mention or a reference without hiding the text
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

Opener = Callable[..., Any]


class PublishRefused(RuntimeError):
    """A rule above was not met; nothing was written to GitHub."""


class GitHubError(RuntimeError):
    """GitHub answered with an error (the token never appears in the message)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect becomes an HTTPError instead of a second request


def default_opener() -> Opener:
    return urllib.request.build_opener(_NoRedirect()).open


@dataclass(frozen=True)
class PublishSettings:
    repo: str
    base: str = DEFAULT_BASE
    token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not _REPO.fullmatch(self.repo) or ".." in self.repo:
            raise ValueError(f"{REPO_KEY} must be owner/name, not {self.repo[:60]!r}")
        if not _BRANCH.fullmatch(self.base) or ".." in self.base:
            raise ValueError(f"{BASE_KEY} is not a branch name")

    @classmethod
    def from_env_file(cls, path: Path | None = None) -> PublishSettings:
        keys = {TOKEN_KEY, REPO_KEY, BASE_KEY}
        values = read_env_file(keys) if path is None else read_env_file(keys, path)
        return cls(
            repo=require(values, REPO_KEY),
            base=values.get(BASE_KEY, DEFAULT_BASE),
            token=require(values, TOKEN_KEY, min_length=20),
        )


class GitHub:
    """A small REST client: JSON in and out, one host, no redirects, the token kept out of
    every message."""

    def __init__(self, token: str, *, opener: Opener | None = None, api: str = API) -> None:
        if urlsplit(api).hostname not in (API_HOST, "127.0.0.1", "localhost"):
            raise ValueError("GitHub calls go to api.github.com only")
        self._token = token
        self._open = opener or default_opener()
        self._api = api.rstrip("/")

    def __repr__(self) -> str:
        return "GitHub(<token hidden>)"

    def scrub(self, text: str) -> str:
        return text.replace(self._token, "<token>") if self._token else text

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or "//" in path or ".." in path:
            raise ValueError(f"bad API path {path[:80]!r}")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self._api + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "trace-driven-repro-demo")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._open(req, timeout=TIMEOUT_S) as reply:
                raw = reply.read(5_000_000)
        except urllib.error.HTTPError as exc:
            detail = ""
            with contextlib.suppress(ValueError, AttributeError):
                detail = str(json.loads(exc.read(20_000) or b"{}").get("message", ""))
            raise GitHubError(
                self.scrub(f"{method} {path}: HTTP {exc.code} {detail}"[:300])
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError(self.scrub(f"{method} {path}: {exc}"[:300])) from None
        return json.loads(raw) if raw else None


# --- what goes into the pull request -----------------------------------------------------------


def defuse(text: str) -> str:
    """Model or payload text, made inert for GitHub: no control characters, no mentions, no
    issue or commit references (`#12`, `owner/repo#12`, `GH-12`)."""
    text = _CONTROL.sub("", text)
    text = re.sub(r"@(?=[A-Za-z0-9_-])", "@" + _ZWSP, text)
    text = re.sub(r"#(?=\d)", "#" + _ZWSP, text)
    return re.sub(r"(?i)\bgh-(?=\d)", "GH-" + _ZWSP, text)


def fenced(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def explanation_of(result: AgentResult) -> str:
    """The model's 2-3 sentence explanation: its final reply's prose before the first edit."""
    if not result.final_from:
        return ""
    reply = result.attempts[result.final_from - 1].reply
    head = re.split(r"(?m)^FILE:", reply, maxsplit=1)[0].strip()
    if len(head) > EXPLANATION_LIMIT:
        head = head[:EXPLANATION_LIMIT].rstrip() + " …"
    return defuse(head)


def crash_of(payload: Mapping[str, Any]) -> tuple[str, str]:
    """(exception type, innermost in-app frame as file:line in function) from a checked payload."""
    value = payload["exception"]["values"][-1]
    frames = [f for f in value["stacktrace"]["frames"] if f.get("in_app")]
    top = frames[-1] if frames else value["stacktrace"]["frames"][-1]
    where = f"{top['filename']}:{top['lineno']} in {top['function']}"
    return defuse(value["type"]), defuse(where)


def publishable(result: AgentResult) -> str | None:
    """Why this run is not published, or None if it is."""
    if result.final_diff is None or not result.final_from:
        return "no patch was submitted"
    final = result.attempts[result.final_from - 1]
    if final.outcome != "clean":
        return f"the submitted patch did not run clean ({final.outcome})"
    return None


def pr_body(job: str, manifest: Manifest, result: AgentResult, payload: Mapping[str, Any]) -> str:
    kind, where = crash_of(payload)
    final = result.attempts[(result.final_from or 1) - 1]
    check = {
        ("valid", "passes"): "reproduced the crash on the buggy code and passes with the patch",
        ("valid", "still_fails"): "reproduced the crash, and still fails with the patch",
    }.get((final.check or "", final.check_after or ""), "did not reproduce the crash on the "
          "buggy code, so it does not confirm this patch")  # fmt: skip
    explanation = explanation_of(result) or "(the model gave no explanation)"
    lines = [
        "> [!WARNING]",
        "> **AI-generated draft for human review.** Written by an AI model from a crash report "
        "alone. Do not merge without reading and testing it.",
        "",
        f"**Crash:** `{kind}` at `{where}`",
        "",
        "**The model's explanation of the cause** (its own words, not checked):",
        "",
        fenced(explanation),
        "",
        "**What was checked before this PR was opened**",
        "",
        "- The patch passes the harness's diff rules (library source only; at most 3 files and "
        "40 lines; no tests, configuration, packaging or workflow files).",
        "- The library's existing tests that passed before still pass with it, in an offline "
        "sandbox.",
        f"- The crash check (`{CHECK_PATH}`) {check}.",
        "",
        "Not checked: whether this is the fix the maintainers would make. No maintainers' test "
        "was run.",
        "",
        "<sub>",
        f"trace-driven-repro live demo · job `{job}` · bug `{manifest.id}` · Pipeline B2 as "
        f"measured · {result.tokens_used:,} tokens · attempt {result.final_from}",
        "</sub>",
    ]
    return "\n".join(lines) + "\n"


# --- publishing -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    repo: str
    base: str
    base_sha: str


class Publisher:
    def __init__(self, settings: PublishSettings, github: GitHub | None = None) -> None:
        self.settings = settings
        self.gh = github or GitHub(settings.token)

    def check_target(self, c_bug: str | None = None) -> Target:
        """The rules on where the PR goes; `PublishRefused` if one fails."""
        user = self.gh.request("GET", "/user")["login"]
        repo = self.gh.request("GET", f"/repos/{self.settings.repo}")
        name = repo["full_name"]
        if name.lower() != self.settings.repo.lower():
            raise PublishRefused(f"{self.settings.repo} resolves to {name} (renamed or moved)")
        if repo["owner"]["login"].lower() != str(user).lower():
            raise PublishRefused(f"{name} does not belong to the token's user")
        if not repo.get("fork"):
            raise PublishRefused(f"{name} is not a fork: the demo publishes to a fork only")
        if repo.get("archived"):
            raise PublishRefused(f"{name} is archived")
        ref = self.gh.request("GET", f"/repos/{name}/git/ref/heads/{quote(self.settings.base)}")
        sha = ref["object"]["sha"]
        if c_bug is not None and sha != c_bug:
            raise PublishRefused(
                f"{name}:{self.settings.base} is at {sha[:12]}, not the bug's commit {c_bug[:12]}"
            )
        return Target(name, self.settings.base, sha)

    def _file(self, target: Target, path: str) -> bytes:
        found = self.gh.request(
            "GET", f"/repos/{target.repo}/contents/{quote(path)}?ref={target.base_sha}"
        )
        if not isinstance(found, dict) or found.get("type") != "file":
            raise PublishRefused(f"{path} is not a file at the base commit")
        return base64.b64decode(found["content"])

    def patched_files(self, target: Target, diff: str, check: str | None) -> dict[str, bytes]:
        """New contents of every file the patch touches, made on the host from the base
        commit's files, plus the crash check."""
        validation = validate(diff)
        if not validation.ok:
            raise PublishRefused("the patch breaks the diff rules: " + "; ".join(
                f"{v.rule} {v.path}" for v in validation.violations))  # fmt: skip
        files: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(Path(tmp))
            for f in validation.files:
                if f.old_path is None or f.new_path is None or f.old_path != f.new_path:
                    raise PublishRefused(f"{f.path}: only edits of existing files are published")
                dest = ws.repo / f.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(self._file(target, f.path))
            applied = apply_patch(ws, diff)
            if applied.status is not ApplyStatus.APPLIED:
                raise PublishRefused(f"the patch does not apply to the base: {applied.message}")
            for f in validation.files:
                files[f.path] = (ws.repo / f.path).read_bytes()
        if check:
            files[CHECK_PATH] = check.encode()
        return files

    def publish(
        self, job: str, manifest: Manifest, result: AgentResult, payload: Mapping[str, Any]
    ) -> str:
        """Open the draft PR and return its URL."""
        reason = publishable(result)
        if reason is not None:
            raise PublishRefused(reason)
        assert result.final_diff is not None
        if not re.fullmatch(r"[0-9a-f]{12}", job):
            raise PublishRefused("bad job id")
        target = self.check_target(manifest.c_bug)
        files = self.patched_files(target, result.final_diff, result.check)
        kind, where = crash_of(payload)
        r = f"/repos/{target.repo}"
        base_tree = self.gh.request("GET", f"{r}/git/commits/{target.base_sha}")["tree"]["sha"]
        tree = [
            {"path": path, "mode": "100644", "type": "blob", "sha": self.gh.request(
                "POST", f"{r}/git/blobs",
                {"content": base64.b64encode(data).decode(), "encoding": "base64"})["sha"]}
            for path, data in files.items()
        ]  # fmt: skip
        new_tree = self.gh.request("POST", f"{r}/git/trees", {"base_tree": base_tree, "tree": tree})
        message = (
            f"Fix {kind} at {where} (AI-generated draft)\n\n"
            f"trace-driven-repro live demo, job {job}. For human review; not merged.\n"
        )
        commit = self.gh.request(
            "POST", f"{r}/git/commits",
            {"message": message, "tree": new_tree["sha"], "parents": [target.base_sha]},
        )  # fmt: skip
        branch = f"{BRANCH_PREFIX}{job}"
        self.gh.request(
            "POST", f"{r}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]}
        )
        pr = self.gh.request("POST", f"{r}/pulls", {
            "title": f"[AI draft] Fix {kind} in {manifest.id}",
            "head": branch,
            "base": target.base,
            "body": pr_body(job, manifest, result, payload),
            "draft": True,
            "maintainer_can_modify": False,
        })  # fmt: skip
        if not pr.get("draft") or pr["base"]["repo"]["full_name"].lower() != target.repo.lower():
            raise GitHubError(
                f"the pull request {pr.get('html_url')} is not a draft on {target.repo}"
            )
        return str(pr["html_url"])
