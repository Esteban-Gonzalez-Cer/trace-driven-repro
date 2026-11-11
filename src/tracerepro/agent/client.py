"""LLM client (development plan §6.1): one interface for every provider, plus retries, a
call log, a development cache and usage limits.

Providers (`agent/providers/`) only turn a `Request` into a `Completion` and classify their
failures. Everything else lives here, so every provider is logged, cached and limited the same
way:

- **Retries.** A provider raises `TransientError` for failures that are not the model's doing
  (network, overload, timeouts). They are retried with exponential backoff and jitter and never
  count as one of the agent's attempts. When retries run out the call fails with
  `InfraFailure`: the trial is rerun later and is never scored as a model failure.
- **Plan limits.** `UsageLimitReached` (the Claude plan's own limit) and `PacingLimitReached`
  (our usage guard) stop the run cleanly; the runner resumes later.
- **Log.** Every call, failed or not, is one JSON line: time, provider and version, model,
  prompt version and hash, request hash, the full request and response, token usage, latency,
  cost estimate, retries, and whether the cache answered.
- **Cache.** With a cache directory, an identical request returns the stored completion and
  uses nothing. Measured and final runs turn it off, since trials must be independent.

A model's own failure (a refusal, or output cut off at the token limit) is not an exception:
it comes back as a `Completion` whose `model_failed` is true, and counts as an attempt.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from tracerepro.agent.errors import (
    InfraFailure,
    LLMError,
    PacingLimitReached,
    TokenCapReached,
    TransientError,
    UsageLimitReached,
)
from tracerepro.agent.usage_guard import TokenBudget, UsageGuard

__all__ = [
    "Completion",
    "InfraFailure",
    "LLMClient",
    "LLMError",
    "Message",
    "PacingLimitReached",
    "Provider",
    "Request",
    "Settings",
    "TokenCapReached",
    "TransientError",
    "Usage",
    "UsageLimitReached",
]

EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_FAILURE_STOPS = ("max_tokens", "refusal")
CACHE_FORMAT = 1


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Settings:
    """Fixed per experiment and recorded in the run config. There is no temperature: current
    Claude models accept only their default sampling settings (see DECISIONS.md)."""

    model: str
    effort: str = "medium"
    max_output_tokens: int = 16_000

    def __post_init__(self) -> None:
        if self.effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}, not {self.effort!r}")
        if not 1 <= self.max_output_tokens <= 128_000:
            raise ValueError("max_output_tokens must be between 1 and 128000")


@dataclass(frozen=True)
class Request:
    system: str
    messages: tuple[Message, ...]
    settings: Settings
    prompt_version: str = ""
    prompt_hash: str = ""

    def __post_init__(self) -> None:
        if not self.messages or self.messages[0].role != "user":
            raise ValueError("a request needs at least one message, starting with the user")
        if self.messages[-1].role != "user":
            raise ValueError("the last message must come from the user")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def hash(self) -> str:
        """sha256 of the canonical JSON request: the cache key and the log's request id."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        """Every token the call consumed, cached or not: what the per-run cap counts."""
        cached = self.cache_read_tokens + self.cache_write_tokens
        return self.input_tokens + self.output_tokens + cached


@dataclass(frozen=True)
class Completion:
    text: str
    stop_reason: str
    usage: Usage
    model: str
    latency_s: float
    cost_equiv_usd: float  # the provider's list-price estimate; not money spent on a plan
    provider: str
    provider_version: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def model_failed(self) -> bool:
        """A refusal or output cut off at the limit: a failed attempt, not an infra error."""
        return self.stop_reason in MODEL_FAILURE_STOPS

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["usage"] = asdict(self.usage)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Completion:
        fields = dict(data)
        fields["usage"] = Usage(**fields["usage"])
        return cls(**fields)


class Provider(Protocol):
    name: str

    @property
    def version(self) -> str: ...

    def complete(self, request: Request) -> Completion: ...


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="milliseconds")


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        *,
        log_path: Path,
        cache_dir: Path | None = None,
        guard: UsageGuard | None = None,
        max_retries: int = 4,
        base_delay_s: float = 5.0,
        max_delay_s: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self.provider = provider
        self.log_path = log_path
        self.cache_dir = cache_dir
        self.guard = guard
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()

    def complete(self, request: Request, budget: TokenBudget | None = None) -> Completion:
        if budget is not None and budget.exhausted:
            raise TokenCapReached(f"run token cap of {budget.cap} reached ({budget.used} used)")
        key = request.hash()
        cached = self._cache_get(key)
        if cached is not None:
            self._log(request, key, completion=cached, retries=0, cache_hit=True)
            if budget is not None:
                budget.charge(cached.usage.total)
            return cached
        retries = 0
        while True:
            if self.guard is not None:
                try:
                    self.guard.before_call()
                except PacingLimitReached as exc:
                    self._log(request, key, error=exc, retries=retries)
                    raise
            try:
                completion = self.provider.complete(request)
            except TransientError as exc:
                if retries >= self.max_retries:
                    failure = InfraFailure(f"gave up after {retries} retries: {exc}")
                    self._log(request, key, error=failure, retries=retries)
                    raise failure from exc
                delay = min(self.max_delay_s, self.base_delay_s * 2**retries)
                delay *= 0.5 + self._rng.random()  # jitter: 50% to 150%
                retries += 1
                self._sleep(delay)
                continue
            except LLMError as exc:
                self._log(request, key, error=exc, retries=retries)
                raise
            finally:
                if self.guard is not None:
                    self.guard.after_call()
            break
        self._cache_put(key, completion)
        self._log(request, key, completion=completion, retries=retries, cache_hit=False)
        if budget is not None:
            budget.charge(completion.usage.total)
        return completion

    def _cache_get(self, key: str) -> Completion | None:
        if self.cache_dir is None:
            return None
        path = self.cache_dir / f"{key}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("format") != CACHE_FORMAT:
            return None
        try:
            return Completion.from_dict(data["completion"])
        except (KeyError, TypeError):
            return None

    def _cache_put(self, key: str, completion: Completion) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_dir / f".{key}.tmp"
        tmp.write_text(json.dumps({"format": CACHE_FORMAT, "completion": completion.to_dict()}))
        tmp.replace(self.cache_dir / f"{key}.json")

    def _log(
        self,
        request: Request,
        key: str,
        *,
        completion: Completion | None = None,
        error: LLMError | None = None,
        retries: int,
        cache_hit: bool = False,
    ) -> None:
        line: dict[str, Any] = {
            "ts": _now_iso(self._clock),
            "provider": self.provider.name,
            "provider_version": self.provider.version,
            "model": request.settings.model,
            "effort": request.settings.effort,
            "prompt_version": request.prompt_version,
            "prompt_hash": request.prompt_hash,
            "request_hash": key,
            "cache_enabled": self.cache_dir is not None,
            "cache_hit": cache_hit,
            "retries": retries,
            "request": request.to_dict(),
        }
        if completion is not None:
            line |= {
                "ok": True,
                "served_model": completion.model,
                "stop_reason": completion.stop_reason,
                "model_failed": completion.model_failed,
                "usage": asdict(completion.usage),
                "latency_s": completion.latency_s,
                "cost_equiv_usd": completion.cost_equiv_usd,
                "response": completion.to_dict(),
            }
        if error is not None:
            line |= {"ok": False, "error": type(error).__name__, "error_message": str(error)}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
