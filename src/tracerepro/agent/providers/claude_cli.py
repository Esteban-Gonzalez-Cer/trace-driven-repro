"""Provider that reaches Claude through the user's Claude plan: the `claude` CLI in script mode.

The CLI is an agent that normally has "hands" (it can read files and run commands). Here it is
locked down so that it acts as a plain text-in, text-out model:

- every built-in tool switched off (`--tools ""`), restricted mode, no MCP servers, no slash
  commands, nobody to answer permission prompts, no saved session;
- our own system prompt replaces Claude Code's default one;
- it runs in a fresh, empty temporary folder outside the repository, so no project file,
  `CLAUDE.md` or memory reaches the model, and there is nothing to read;
- its environment is a short allow-list. API-key variables and this session's own Claude Code
  variables are never passed, so nothing is billed to an API account and the call cannot reach
  a running Claude Code session;
- no `--fallback-model`: the model under test is never swapped. The model named in the reply
  must be exactly the one requested.

The reply is checked before it is trusted (fail closed): anything unexpected, more than one
turn, any tool use or permission request, or another model is an `InfraFailure`, never a model
result. Each call is stateless: earlier turns are rendered into the one prompt
(`render_prompt`), so a call can be repeated and cached.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tracerepro.agent.client import Completion, Request, Usage
from tracerepro.agent.errors import InfraFailure, TransientError, UsageLimitReached

DEFAULT_TIMEOUT_S = 900.0
MAX_REPLY_BYTES = 16_000_000
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# What the CLI needs to find its login (macOS keychain or ~/.claude) and a temp dir. Nothing else
# from the harness's environment is passed on.
PASSED_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")
FIXED_ENV = {
    "PATH": SAFE_PATH,
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
MAX_OUTPUT_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
LOCKDOWN_ARGS = (
    "--tools",
    "",
    "--restricted",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--permission-prompts",
    "none",
    "--no-session-persistence",
    "--output-format",
    "json",
)
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
# The plan's own limits stop the run (it resumes later); they are never retried. "You've hit
# your session limit · resets 7:20pm" is the 5-hour window (seen in the final run, 2026-11-12).
_LIMIT_RE = re.compile(
    r"usage limit|limit reached|limit will reset|out of (extra )?usage"
    r"|hit your [\w-]+ limit|(session|weekly) limit",
    re.I,
)
_OVER_LIMIT_RE = re.compile(r"response exceeded the \d+ output token maximum")
# The model's safety classifier declined the request. The CLI reports it as an error; it is the
# model's answer (a refusal), not a failure of the call, so it is never retried.
_REFUSAL_RE = re.compile(r"safeguards flagged this message")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

Runner = Callable[..., subprocess.CompletedProcess[str]]


def render_prompt(request: Request) -> str:
    """The conversation as one prompt. A single user message is sent as it is; earlier turns are
    rendered as marked sections, oldest first, ending with the current request."""
    if len(request.messages) == 1:
        return request.messages[0].content
    parts = ["The conversation so far, oldest first. Answer the last user turn."]
    for i, msg in enumerate(request.messages, 1):
        parts.append(f"=== turn {i}: {msg.role} ===\n{msg.content}")
    return "\n\n".join(parts)


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InfraFailure(f"reply usage field {key!r} is not a count: {value!r}")
    return value


class ClaudeCLI:
    name = "claude-cli"

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        run: Runner = subprocess.run,
        workdir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executable = executable
        self.timeout_s = timeout_s
        self._run = run
        self._workdir = workdir  # only for the hands-off check; normally a fresh empty folder
        self._clock = clock
        self._version: str | None = None

    @property
    def executable(self) -> str:
        found = self._executable or shutil.which("claude")
        if not found:
            raise InfraFailure("the `claude` CLI is not installed or not on PATH")
        return os.path.realpath(found)

    @property
    def version(self) -> str:
        if self._version is None:
            result = self._run(
                [self.executable, "--version"],
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            match = _VERSION_RE.search(result.stdout or "")
            if result.returncode != 0 or not match:
                raise InfraFailure("could not read the `claude` CLI version")
            self._version = match.group(0)
        return self._version

    def argv(self, request: Request) -> list[str]:
        s = request.settings
        return [
            self.executable,
            "-p",
            "--model",
            s.model,
            "--effort",
            s.effort,
            "--system-prompt",
            request.system,
            *LOCKDOWN_ARGS,
        ]

    def environment(self, max_output_tokens: int | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in PASSED_ENV if k in os.environ}
        env |= FIXED_ENV
        if max_output_tokens is not None:
            env[MAX_OUTPUT_ENV] = str(max_output_tokens)
        return env

    def complete(self, request: Request) -> Completion:
        version = self.version
        start = self._clock()
        with tempfile.TemporaryDirectory(prefix="tracerepro-llm-") as scratch:
            cwd = self._workdir or Path(scratch)
            try:
                result = self._run(
                    self.argv(request),
                    input=render_prompt(request),
                    env=self.environment(request.settings.max_output_tokens),
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                limit = f"{self.timeout_s:.0f}s"
                raise TransientError(f"the CLI did not answer within {limit}") from exc
            except OSError as exc:
                raise InfraFailure(f"could not start the CLI: {exc}") from exc
        latency = self._clock() - start
        return self.parse_reply(result.stdout, result.returncode, request, version, latency)

    def parse_reply(
        self, stdout: str, returncode: int, request: Request, version: str, latency_s: float
    ) -> Completion:
        if len(stdout) > MAX_REPLY_BYTES:
            raise InfraFailure("the CLI reply is too large")
        try:
            data = json.loads(stdout)
        except ValueError as exc:
            # No JSON at all: the CLI crashed or the network dropped. Retry, then give up.
            raise TransientError(f"the CLI gave no JSON reply (exit code {returncode})") from exc
        if not isinstance(data, dict) or data.get("type") != "result":
            raise InfraFailure("the CLI reply is not a result object")
        text = data.get("result")
        cut_off = refused = False
        if data.get("is_error") is not False:
            if isinstance(text, str) and _REFUSAL_RE.search(text):
                text, refused = "", True
            elif isinstance(text, str) and _OVER_LIMIT_RE.search(text):
                # The answer outgrew the output limit. The CLI asks the model to continue a few
                # times, then gives up and drops the partial text: the model's failure, not ours.
                text, cut_off = "", True
            else:
                self._raise_error(data, text)
        elif returncode != 0:
            raise InfraFailure(f"the CLI exited with code {returncode} but reported success")
        if not isinstance(text, str):
            raise InfraFailure("the CLI reply has no text result")
        turns = data.get("num_turns")
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
            raise InfraFailure(f"the CLI reply has no valid turn count: {turns!r}")
        # Tools are off, so a second turn can only be the CLI continuing an answer that hit the
        # output limit. Stitched text is not trusted: it counts as cut off.
        cut_off = cut_off or turns > 1
        if data.get("permission_denials") not in ([], None):
            raise InfraFailure("the model asked for a tool or permission; tools must be off")
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise InfraFailure("the CLI reply has no usage")
        server_tools = usage.get("server_tool_use") or {}
        if not isinstance(server_tools, dict) or any(server_tools.values()):
            raise InfraFailure(f"the model used a server tool: {server_tools}")
        spawned = (data.get("subagent_stats") or {}).get("spawned", 0)
        if spawned:
            raise InfraFailure(f"the CLI started {spawned} subagent(s); tools must be off")
        models = data.get("modelUsage")
        if refused and models in ({}, None):
            models = {request.settings.model: {}}  # declined before any output: nothing served
        if not isinstance(models, dict) or set(models) != {request.settings.model}:
            served = sorted(models) if isinstance(models, dict) else models
            wanted = request.settings.model
            raise InfraFailure(f"requested {wanted}, but the reply came from {served}")
        stop = "refusal" if refused else "max_tokens" if cut_off else data.get("stop_reason")
        if not isinstance(stop, str):
            raise InfraFailure("the CLI reply has no stop reason")
        cost = data.get("total_cost_usd", 0.0)
        if not isinstance(cost, int | float) or isinstance(cost, bool) or cost < 0:
            raise InfraFailure(f"the CLI reply has an invalid cost: {cost!r}")
        return Completion(
            text=text,
            stop_reason=stop,
            usage=Usage(
                input_tokens=_int(usage, "input_tokens"),
                output_tokens=_int(usage, "output_tokens"),
                cache_read_tokens=_int(usage, "cache_read_input_tokens"),
                cache_write_tokens=_int(usage, "cache_creation_input_tokens"),
            ),
            model=request.settings.model,
            latency_s=round(latency_s, 3),
            cost_equiv_usd=float(cost),
            provider=self.name,
            provider_version=version,
            raw=data,
        )

    @staticmethod
    def _raise_error(data: dict[str, Any], text: Any) -> None:
        status = data.get("api_error_status")
        message = text if isinstance(text, str) else ""
        detail = f"status {status}: {message[:300]}" if status is not None else message[:300]
        if _LIMIT_RE.search(message):
            raise UsageLimitReached(detail or "the Claude plan's usage limit was reached")
        if status is None or (isinstance(status, int) and status in RETRYABLE_STATUS):
            raise TransientError(detail or "the CLI reported an error")
        if isinstance(status, int) and status >= 500:
            raise TransientError(detail)
        raise InfraFailure(detail or "the CLI reported an error")
