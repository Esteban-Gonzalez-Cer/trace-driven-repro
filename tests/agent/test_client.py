"""LLM client (plan §6.1): retries, logging, cache, token cap and usage guard, with a fake
provider. No network, no plan usage."""

import json
import random
from pathlib import Path

import pytest

from tracerepro.agent.client import LLMClient, Message, Request, Settings
from tracerepro.agent.errors import (
    InfraFailure,
    PacingLimitReached,
    TokenCapReached,
    TransientError,
    UsageLimitReached,
)
from tracerepro.agent.providers.fake import FakeProvider, FakeReply
from tracerepro.agent.usage_guard import GuardLimits, TokenBudget, UsageGuard


def request(text: str = "fix the crash", model: str = "claude-haiku-5-5") -> Request:
    return Request(
        system="You fix bugs.",
        messages=(Message("user", text),),
        settings=Settings(model=model, effort="low", max_output_tokens=1000),
        prompt_version="patch_v1",
        prompt_hash="abc123",
    )


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(
    tmp_path: Path, provider: FakeProvider, **kwargs: object
) -> tuple[LLMClient, Clock]:
    clock = Clock()
    client = LLMClient(
        provider,
        log_path=tmp_path / "calls.jsonl",
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
        **kwargs,  # type: ignore[arg-type]
    )
    return client, clock


def log_lines(tmp_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_success_is_logged_with_every_field(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, FakeProvider.replying(["patched"]))
    done = client.complete(request())
    assert done.text == "patched" and not done.model_failed
    (line,) = log_lines(tmp_path)
    for key in (
        "ts", "provider", "provider_version", "model", "effort", "prompt_version",
        "prompt_hash", "request_hash", "request", "response", "usage", "latency_s",
        "cost_equiv_usd", "retries", "cache_hit", "cache_enabled", "stop_reason",
    ):  # fmt: skip
        assert key in line, key
    assert line["ok"] is True and line["retries"] == 0 and line["cache_hit"] is False
    assert line["request_hash"] == request().hash()
    assert line["request"]["messages"][0]["content"] == "fix the crash"  # type: ignore[index]


def test_request_hash_is_canonical() -> None:
    assert request().hash() == request().hash()
    assert request("other").hash() != request().hash()
    assert request(model="claude-sonnet-5-5").hash() != request().hash()


def test_transient_errors_are_retried_with_backoff(tmp_path: Path) -> None:
    provider = FakeProvider.replying([TransientError("overloaded"), TransientError("5xx"), "ok"])
    client, clock = make_client(tmp_path, provider, base_delay_s=4.0)
    done = client.complete(request())
    assert done.text == "ok"
    assert len(clock.slept) == 2
    assert 2.0 <= clock.slept[0] <= 6.0 and 4.0 <= clock.slept[1] <= 12.0  # 4s, 8s, +-50%
    (line,) = log_lines(tmp_path)
    assert line["retries"] == 2


def test_exhausted_retries_are_an_infra_failure(tmp_path: Path) -> None:
    provider = FakeProvider.replying([TransientError("down")] * 3)
    client, _ = make_client(tmp_path, provider, max_retries=2)
    with pytest.raises(InfraFailure, match="gave up after 2 retries"):
        client.complete(request())
    (line,) = log_lines(tmp_path)
    assert line["ok"] is False and line["error"] == "InfraFailure"


def test_plan_limit_stops_without_retrying(tmp_path: Path) -> None:
    provider = FakeProvider.replying([UsageLimitReached("usage limit reached"), "never"])
    client, clock = make_client(tmp_path, provider)
    with pytest.raises(UsageLimitReached):
        client.complete(request())
    assert clock.slept == [] and len(provider.requests) == 1
    assert log_lines(tmp_path)[0]["error"] == "UsageLimitReached"


def test_model_failures_are_completions_not_errors(tmp_path: Path) -> None:
    provider = FakeProvider.replying([FakeReply("cut", "max_tokens"), FakeReply("", "refusal")])
    client, _ = make_client(tmp_path, provider)
    assert client.complete(request("a")).model_failed
    assert client.complete(request("b")).model_failed


def test_cache_answers_identical_requests_for_free(tmp_path: Path) -> None:
    provider = FakeProvider.replying(["first"])
    client, _ = make_client(tmp_path, provider, cache_dir=tmp_path / "cache")
    one = client.complete(request())
    two = client.complete(request())
    assert one == two and len(provider.requests) == 1
    first, second = log_lines(tmp_path)
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert second["cache_enabled"] is True


def test_no_cache_means_every_call_reaches_the_provider(tmp_path: Path) -> None:
    provider = FakeProvider.replying(["a", "b"])
    client, _ = make_client(tmp_path, provider)
    assert client.complete(request()).text == "a"
    assert client.complete(request()).text == "b"
    assert log_lines(tmp_path)[0]["cache_enabled"] is False


def test_corrupt_cache_entries_are_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{request().hash()}.json").write_text("{not json")
    client, _ = make_client(tmp_path, FakeProvider.replying(["fresh"]), cache_dir=cache)
    assert client.complete(request()).text == "fresh"


def test_token_cap_stops_the_run(tmp_path: Path) -> None:
    provider = FakeProvider.replying(["x" * 400, "never"])
    client, _ = make_client(tmp_path, provider)
    budget = TokenBudget(cap=50)
    client.complete(request(), budget)
    assert budget.used >= 100 and budget.exhausted
    with pytest.raises(TokenCapReached):
        client.complete(request("again"), budget)
    assert len(provider.requests) == 1


def test_guard_paces_calls_and_refuses_past_the_hourly_limit(tmp_path: Path) -> None:
    clock = Clock()
    limits = GuardLimits(max_calls_per_hour=2, max_calls_per_day=10, min_interval_s=3.0)
    guard = UsageGuard(tmp_path / "ledger.json", limits, clock=clock, sleep=clock.sleep)
    provider = FakeProvider.replying(["a", "b", "c"])
    client = LLMClient(
        provider, log_path=tmp_path / "calls.jsonl", guard=guard, sleep=clock.sleep, clock=clock
    )
    client.complete(request("1"))
    client.complete(request("2"))
    assert clock.slept == [3.0]  # waited the minimum interval before the second call
    with pytest.raises(PacingLimitReached) as caught:
        client.complete(request("3"))
    assert caught.value.retry_at > clock.now
    assert len(provider.requests) == 2
    clock.now += 3601
    assert client.complete(request("3")).text == "c"


def test_guard_ledger_survives_a_restart(tmp_path: Path) -> None:
    clock = Clock()
    limits = GuardLimits(max_calls_per_hour=100, max_calls_per_day=1, min_interval_s=0)
    ledger = tmp_path / "ledger.json"
    UsageGuard(ledger, limits, clock=clock).after_call()
    with pytest.raises(PacingLimitReached, match="24 hours"):
        UsageGuard(ledger, limits, clock=clock).before_call()


def test_cached_answers_do_not_touch_the_guard(tmp_path: Path) -> None:
    clock = Clock()
    limits = GuardLimits(max_calls_per_hour=1, max_calls_per_day=1, min_interval_s=0)
    guard = UsageGuard(tmp_path / "ledger.json", limits, clock=clock)
    client = LLMClient(
        FakeProvider.replying(["a"]),
        log_path=tmp_path / "calls.jsonl",
        cache_dir=tmp_path / "cache",
        guard=guard,
        clock=clock,
    )
    client.complete(request())
    client.complete(request())  # cached: no PacingLimitReached


def test_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="effort"):
        Settings(model="m", effort="extreme")
    with pytest.raises(ValueError, match="max_output_tokens"):
        Settings(model="m", max_output_tokens=0)
    with pytest.raises(ValueError, match="last message"):
        Request("s", (Message("user", "a"), Message("assistant", "b")), Settings(model="m"))
