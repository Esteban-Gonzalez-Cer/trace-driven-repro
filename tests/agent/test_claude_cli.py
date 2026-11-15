"""Claude CLI provider with a fake subprocess: the lockdown switches, the environment it gets,
and fail-closed reading of its reply. No real CLI is started."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tracerepro.agent.client import Message, Request, Settings
from tracerepro.agent.errors import InfraFailure, TransientError, UsageLimitReached
from tracerepro.agent.providers.claude_cli import (
    LOCKDOWN_ARGS,
    MAX_OUTPUT_ENV,
    ClaudeCLI,
    render_prompt,
)

MODEL = "claude-haiku-5-5"
FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKE-do-not-leak"


def request(*messages: Message) -> Request:
    return Request(
        system="SYSTEM PROMPT",
        messages=messages or (Message("user", "hello"),),
        settings=Settings(model=MODEL, effort="low", max_output_tokens=777),
    )


def reply(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "result": "pong",
        "num_turns": 1,
        "stop_reason": "end_turn",
        "total_cost_usd": 0.0001,
        "permission_denials": [],
        "usage": {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 570,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        },
        "modelUsage": {MODEL: {"inputTokens": 2, "outputTokens": 4}},
        "subagent_stats": {"spawned": 0},
    }
    data.update(overrides)
    return data


class FakeRun:
    """Stands in for subprocess.run; records every call."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "2.1.308 (Claude Code)\n", "")
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def provider(run: FakeRun) -> ClaudeCLI:
    return ClaudeCLI("/usr/bin/true", run=run)


def test_every_call_is_locked_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", FAKE_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    run = FakeRun(json.dumps(reply()))
    done = provider(run).complete(request())
    assert done.text == "pong" and done.provider_version == "2.1.308"
    argv, kwargs = run.calls[-1]
    assert argv[1] == "-p"
    joined = "\0".join(argv)
    assert "\0".join(LOCKDOWN_ARGS) in joined
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM PROMPT"
    assert "--fallback-model" not in argv
    assert kwargs["input"] == "hello"  # the prompt goes on stdin, never into argv
    env = kwargs["env"]
    assert not any(k.startswith(("ANTHROPIC_", "CLAUDE_CODE_S")) for k in env)
    assert FAKE_KEY not in json.dumps(env)
    assert env[MAX_OUTPUT_ENV] == "777"
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    cwd = Path(kwargs["cwd"])
    assert cwd.name.startswith("tracerepro-llm-") and not cwd.exists()  # removed afterwards


def test_usage_and_cost_are_read(tmp_path: Path) -> None:
    done = provider(FakeRun(json.dumps(reply()))).complete(request())
    assert done.usage.input_tokens == 2 and done.usage.cache_write_tokens == 570
    assert done.usage.total == 576 and done.cost_equiv_usd == pytest.approx(0.0001)
    assert done.model == MODEL and done.stop_reason == "end_turn"


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"num_turns": 0}, "turn count"),
        ({"permission_denials": [{"tool_name": "Bash"}]}, "tool or permission"),
        ({"modelUsage": {"claude-sonnet-5-5": {}}}, "requested claude-haiku-5-5"),
        ({"modelUsage": {MODEL: {}, "claude-haiku-4-5": {}}}, "requested"),
        ({"subagent_stats": {"spawned": 1}}, "subagent"),
        ({"usage": {"input_tokens": -1}}, "not a count"),
        ({"usage": {"input_tokens": 1, "server_tool_use": {"web_fetch_requests": 1}}}, "server"),
        ({"result": None}, "no text"),
        ({"type": "assistant"}, "not a result"),
        ({"stop_reason": None}, "stop reason"),
        ({"total_cost_usd": "free"}, "cost"),
    ],
)
def test_unexpected_replies_fail_closed(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(InfraFailure, match=match):
        provider(FakeRun(json.dumps(reply(**overrides)))).complete(request())


def test_no_json_is_retryable() -> None:
    with pytest.raises(TransientError, match="no JSON"):
        provider(FakeRun("Segmentation fault", returncode=139)).complete(request())


@pytest.mark.parametrize(
    ("status", "text", "error"),
    [
        (529, "Overloaded", TransientError),
        (503, "Service unavailable", TransientError),
        (429, "Rate limited", TransientError),
        (None, "Connection error", TransientError),
        (429, "Claude usage limit reached. Your limit will reset at 5pm", UsageLimitReached),
        # Recorded during the final run (2026-11-12): the plan's 5-hour window. It used to be
        # read as an ordinary rate limit, retried 4 times and set the trial aside.
        (
            429,
            "You've hit your session limit · resets 7:20pm (America/Mexico_City)",
            UsageLimitReached,
        ),
        (429, "You've hit your weekly limit · resets Nov 18", UsageLimitReached),
        (404, "There's an issue with the selected model", InfraFailure),
        (400, "Bad request", InfraFailure),
    ],
)
def test_errors_are_classified(status: int | None, text: str, error: type[Exception]) -> None:
    data = reply(is_error=True, api_error_status=status, result=text, modelUsage={})
    with pytest.raises(error):
        provider(FakeRun(json.dumps(data), returncode=1)).complete(request())


def test_success_with_a_failing_exit_code_is_not_trusted() -> None:
    with pytest.raises(InfraFailure, match="exit"):
        provider(FakeRun(json.dumps(reply()), returncode=1)).complete(request())


def test_refusal_and_cut_off_output_are_model_failures() -> None:
    for stop in ("refusal", "max_tokens"):
        done = provider(FakeRun(json.dumps(reply(stop_reason=stop)))).complete(request())
        assert done.model_failed


def test_output_over_the_limit_is_a_model_failure() -> None:
    # Seen for real (CLI 2.1.308): the CLI continues the answer up to 4 turns, then reports an
    # error and drops the partial text.
    text = "API Error: Claude's response exceeded the 64 output token maximum. To configure..."
    data = reply(is_error=True, result=text, num_turns=4, stop_reason="stop_sequence")
    done = provider(FakeRun(json.dumps(data), returncode=1)).complete(request())
    assert done.model_failed and done.stop_reason == "max_tokens" and done.text == ""
    assert done.usage.output_tokens == 4


def test_a_continued_answer_is_not_trusted() -> None:
    done = provider(FakeRun(json.dumps(reply(num_turns=2)))).complete(request())
    assert done.stop_reason == "max_tokens" and done.model_failed


def test_timeouts_are_retryable() -> None:
    def hang(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "2.1.308", "")
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(TransientError, match="did not answer"):
        ClaudeCLI("/usr/bin/true", run=hang, timeout_s=1).complete(request())


def test_earlier_turns_are_rendered_into_one_prompt() -> None:
    assert render_prompt(request()) == "hello"
    text = render_prompt(
        request(Message("user", "fix it"), Message("assistant", "patch 1"), Message("user", "x"))
    )
    assert text.index("turn 1: user") < text.index("patch 1") < text.index("turn 3: user")
    assert text.endswith("x")


def test_missing_cli_is_an_infra_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(InfraFailure, match="not installed"):
        ClaudeCLI(run=FakeRun()).complete(request())


def test_a_safety_refusal_is_a_model_failure_not_retried() -> None:
    # Seen for real in the prompt-injection round (CLI 2.1.308): the CLI reports the safety
    # classifier's refusal as an error. It must not be retried as a network glitch.
    text = "API Error: Sonnet 5.5's safeguards flagged this message (https://...). Try rephrasing"
    data = reply(is_error=True, result=text, stop_reason="stop_sequence", modelUsage={},
                 usage={"input_tokens": 0, "output_tokens": 0})  # fmt: skip
    done = provider(FakeRun(json.dumps(data), returncode=1)).complete(request())
    assert done.stop_reason == "refusal" and done.model_failed and done.text == ""
    other_model = reply(is_error=True, result=text, modelUsage={"claude-opus-5-5": {}})
    with pytest.raises(InfraFailure, match="requested"):
        provider(FakeRun(json.dumps(other_model), returncode=1)).complete(request())
