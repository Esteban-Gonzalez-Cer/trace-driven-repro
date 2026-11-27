"""The live demo's draft-PR publisher against a fake GitHub: no network, no token. Covers the
rules in `publisher.py`: the owner's fork only, the bug's commit as the base, clean patches
only, the diff rules re-checked, an always-draft PR, model text made inert, and a token that
never leaves the request header (no redirects, scrubbed from errors, hidden in reprs)."""

import base64
import http.server
import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tracerepro.live.env_file import SettingMissing
from tracerepro.live.publisher import (
    CHECK_PATH,
    GitHub,
    GitHubError,
    Publisher,
    PublishRefused,
    PublishSettings,
    defuse,
    fenced,
)

from ..agent.test_pipeline import CLEAN, FIXTURE, GOLD, REGRESSED, TOY, payload, run

TOKEN = "github_pat_" + "x" * 40
REPO = "owner/tiny_lib"
JOB = "0123456789ab"
CHECK = "def test_crash():\n    assert True\n"


class Reply(io.BytesIO):
    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeGitHub:
    """Answers the calls the publisher makes; records every request."""

    def __init__(self, **repo: Any) -> None:
        self.repo = {"full_name": REPO, "owner": {"login": "owner"}, "fork": True, **repo}
        self.base_sha = TOY.c_bug
        self.pr: dict[str, Any] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, req: urllib.request.Request, timeout: float = 0) -> Reply:
        method, url = req.get_method(), req.full_url
        path = url.removeprefix("https://api.github.com")
        body = json.loads(req.data) if req.data else None  # type: ignore[arg-type]
        self.calls.append((method, path, body))
        self.headers.append(dict(req.header_items()))
        return Reply(json.dumps(self.answer(method, path, body)).encode())

    def answer(self, method: str, path: str, body: Any) -> Any:
        r = f"/repos/{REPO}"
        if path == "/user":
            return {"login": "owner"}
        if path == r:
            return self.repo
        if path == f"{r}/git/ref/heads/demo-base":
            return {"object": {"sha": self.base_sha}}
        if path.startswith(f"{r}/contents/"):
            file = path.removeprefix(f"{r}/contents/").split("?")[0]
            data = (FIXTURE / "buggy" / file).read_bytes()
            return {"type": "file", "content": base64.b64encode(data).decode()}
        if path == f"{r}/git/commits/{self.base_sha}":
            return {"tree": {"sha": "t0"}}
        if method == "POST":
            kind = path.rsplit("/", 1)[-1]
            if kind == "pulls":
                self.pr = body
                return {"draft": body["draft"], "html_url": "https://github.com/owner/tiny_lib/pull/1",
                        "base": {"repo": {"full_name": REPO}}}  # fmt: skip
            return {"sha": f"{kind}-sha"}
        raise AssertionError(f"unexpected call {method} {path}")

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


def publisher(fake: FakeGitHub) -> Publisher:
    return Publisher(PublishSettings(REPO, token=TOKEN), GitHub(TOKEN, opener=fake))


def result(tmp_path: Path, reply: str = GOLD, runs: Any = (CLEAN,)) -> Any:
    res, _, _ = run(tmp_path, [reply] * 2, list(runs) * 2)
    res.check = CHECK
    return res


def test_a_clean_patch_becomes_a_draft_pr_on_the_fork(tmp_path: Path) -> None:
    fake = FakeGitHub()
    url = publisher(fake).publish(JOB, TOY, result(tmp_path), payload())
    assert url == "https://github.com/owner/tiny_lib/pull/1"
    assert {path.split("/")[2] for _, path, _ in fake.calls if path.startswith("/repos/")} == {
        "owner"
    } and all(f"/repos/{REPO}" in p for _, p, _ in fake.calls if p.startswith("/repos/"))
    (tree,) = [b for m, p, b in fake.writes if p.endswith("/git/trees")]
    assert tree["base_tree"] == "t0" and {t["path"] for t in tree["tree"]} == {
        "tiny_lib/report.py",
        CHECK_PATH,
    }
    blobs = [base64.b64decode(b["content"]).decode() for m, p, b in fake.writes if "blobs" in p]
    assert any("default=0" in text for text in blobs) and CHECK in blobs
    (commit,) = [b for m, p, b in fake.writes if p.endswith("/git/commits")]
    assert commit["parents"] == [TOY.c_bug] and "AI-generated draft" in commit["message"]
    (ref,) = [b for m, p, b in fake.writes if p.endswith("/git/refs")]
    assert ref["ref"] == f"refs/heads/tracerepro/{JOB}"
    pr = fake.pr
    assert pr["draft"] is True and pr["maintainer_can_modify"] is False
    assert (pr["head"], pr["base"]) == (f"tracerepro/{JOB}", "demo-base")
    assert "AI-generated draft for human review" in pr["body"]
    assert "The cause is max() on an empty sequence." in pr["body"]
    assert "FILE:" not in pr["body"]  # the edits are in the commit, not the text
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for h in fake.headers)


@pytest.mark.parametrize(
    ("repo", "why"),
    [
        ({"fork": False}, "not a fork"),
        ({"owner": {"login": "pallets"}}, "does not belong"),
        ({"full_name": "pallets/tiny_lib"}, "resolves to"),
        ({"archived": True}, "archived"),
    ],
)
def test_anything_but_the_owners_fork_is_refused_before_any_write(
    tmp_path: Path, repo: dict[str, Any], why: str
) -> None:
    fake = FakeGitHub(**repo)
    with pytest.raises(PublishRefused, match=why):
        publisher(fake).publish(JOB, TOY, result(tmp_path), payload())
    assert fake.writes == []


def test_the_base_branch_must_be_at_the_bugs_commit(tmp_path: Path) -> None:
    fake = FakeGitHub()
    fake.base_sha = "f" * 40
    with pytest.raises(PublishRefused, match="not the bug's commit"):
        publisher(fake).publish(JOB, TOY, result(tmp_path), payload())
    assert fake.writes == []


def test_patches_that_did_not_run_clean_are_not_published(tmp_path: Path) -> None:
    fake = FakeGitHub()
    res = result(tmp_path, runs=(REGRESSED,))
    with pytest.raises(PublishRefused, match="did not run clean"):
        publisher(fake).publish(JOB, TOY, res, payload())
    res.final_diff = None
    with pytest.raises(PublishRefused, match="no patch"):
        publisher(fake).publish(JOB, TOY, res, payload())
    assert fake.calls == []


def test_the_diff_rules_are_checked_again(tmp_path: Path) -> None:
    fake = FakeGitHub()
    res = result(tmp_path)
    res.final_diff = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
        "@@ -1 +1 @@\n-on: push\n+on: pull_request_target\n"
    )
    with pytest.raises(PublishRefused, match="diff rules"):
        publisher(fake).publish(JOB, TOY, res, payload())
    assert fake.writes == []


def test_model_text_cannot_mention_link_or_break_out_of_its_block(tmp_path: Path) -> None:
    hostile = (
        "Cause: see @pallets/maintainers and @someone, fixes #123, pallets/werkzeug#9, GH-4.\n"
        "```\n</details>![x](https://evil.example/p.png) [click](https://evil.example)\x07\n"
    )
    fake = FakeGitHub()
    publisher(fake).publish(JOB, TOY, result(tmp_path, hostile + GOLD), payload())
    body = fake.pr["body"]
    for raw in ("@pallets", "@someone", "#123", "werkzeug#9", "GH-4", "\x07"):
        assert raw not in body, raw
    start = body.index("````text\n")  # a longer fence than any backtick run in the text
    assert "evil.example" in body[start : body.index("\n````\n", start)]


def test_defuse_and_fence() -> None:
    assert defuse("@a #1 GH-2 x@y.z") == "@​a #​1 GH-​2 x@​y.z"
    assert fenced("a ``` b") == "````text\na ``` b\n````"


def test_the_token_never_appears_in_errors_or_reprs() -> None:
    def failing(req: urllib.request.Request, timeout: float = 0) -> Reply:
        body = io.BytesIO(json.dumps({"message": f"Bad credentials {TOKEN}"}).encode())
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, body)  # type: ignore[arg-type]

    gh = GitHub(TOKEN, opener=failing)
    with pytest.raises(GitHubError) as info:
        gh.request("GET", "/user")
    assert TOKEN not in str(info.value) and "<token>" in str(info.value)
    assert TOKEN not in repr(gh) and TOKEN not in repr(PublishSettings(REPO, token=TOKEN))


def test_redirects_are_never_followed() -> None:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            if self.path == "/user":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere"
                )
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        gh = GitHub(TOKEN, api=f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(GitHubError, match="HTTP 302"):
            gh.request("GET", "/user")
    finally:
        server.shutdown()
    assert hits == ["/user"]  # the redirect target never saw the token


@pytest.mark.parametrize("api", ["https://evil.example", "http://api.github.com.evil.example"])
def test_only_github_is_called(api: str) -> None:
    with pytest.raises(ValueError, match=r"api\.github\.com only"):
        GitHub(TOKEN, api=api)


@pytest.mark.parametrize("path", ["user", "//evil.example/x", "/repos/../x"])
def test_odd_api_paths_are_refused(path: str) -> None:
    with pytest.raises(ValueError, match="bad API path"):
        GitHub(TOKEN, opener=FakeGitHub()).request("GET", path)


@pytest.mark.parametrize("repo", ["../x", "owner/../x", "a/b/c", "owner", "own er/x"])
def test_the_repo_setting_must_be_owner_slash_name(repo: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        PublishSettings(repo, token=TOKEN)


def test_settings_come_from_the_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"GITHUB_DEMO_TOKEN={TOKEN}\nDEMO_REPO={REPO}\n")
    settings = PublishSettings.from_env_file(env)
    assert (settings.repo, settings.base, settings.token) == (REPO, "demo-base", TOKEN)
    env.write_text(f"DEMO_REPO={REPO}\n")
    with pytest.raises(SettingMissing, match="GITHUB_DEMO_TOKEN"):
        PublishSettings.from_env_file(env)


# --- the server's runner ------------------------------------------------------------------


def test_the_runner_publishes_or_reports_why_not(tmp_path: Path, monkeypatch: Any) -> None:
    from tracerepro.live import server
    from tracerepro.live.jobs import Job

    res = result(tmp_path)
    monkeypatch.setattr(server, "run_job", lambda *a, **k: res)

    class Fake:
        def __init__(self, outcome: Any) -> None:
            self.outcome = outcome

        def publish(self, *args: Any) -> str:
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return str(self.outcome)

    job = Job.new(TOY.id, tmp_path)
    ok = server.b2_runner(None, Fake("u"))  # type: ignore[arg-type]
    assert ok(job, TOY, payload())["pr"] == "u"
    refused = server.b2_runner(None, Fake(PublishRefused("not a fork")))  # type: ignore[arg-type]
    assert refused(job, TOY, payload())["publish"] == "not published: not a fork"
